"""部署时把 bundled reviewer registry 增量并入 user-local registry 的结构性契约。

**为什么需要 merge 而不是 seed**：`initialize_rvf_user_local_data` 原本只在
user-local registry 不存在时整份 copy 一次。于是任何一台已经部署过 RVF 的机器，
其 user-local registry 就永久冻结在首次部署那一刻的 harness 集合上——
后来往 bundled registry 里新增的评审腿（kimi_code / oh_my_pi 就是第一批）
**永远不会**出现在那台机器上，而且没有任何东西会报错：部署照常成功、
`dispatch_reviewers` 照常从 3 条腿里选 2 条，看起来一切正常。
这正是本仓反复栽过的假绿形状，所以并入必须是**幂等 additive merge**，
不能靠「首次 seed」把新腿带进去。

**为什么是 additive-only**：user-local registry 是用户可改的（关掉某条腿、
调 priority、加自己的 harness）。bundled 那份是 seed 不是真相源，
`install_to_codex.py` 构建时会把它从 payload 里删掉、`verify_local_deploy.py`
断言 payload 里不得存在它——真相源自始至终是 user-local 那份。
所以 merge 只补 user-local 缺的条目，**绝不覆盖**已存在的条目。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "scripts/install_to_codex.py"
BUNDLED_REGISTRY_PATH = (
    ROOT
    / "plugins/review-validate-fix/skills/review-validate-fix"
    / "config/reviewer-registry.json"
)


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "installer_for_reviewer_registry_merge_tests", INSTALLER_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_registry(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read_registry(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _bundled_registry() -> dict:
    return json.loads(BUNDLED_REGISTRY_PATH.read_text(encoding="utf-8"))


def _harness_entry(harness_id: str, *, priority_default: int = 50) -> dict:
    return {
        "harness_id": harness_id,
        "label_prefix": f"alternative-reviewer:{harness_id}",
        "config_path": f"config/alternative-reviewer.{harness_id}.json",
        "dispatch_mode": "external_cli",
        "enabled": True,
        "priority_default": priority_default,
    }


# --- RM-1 · 缺失时的行为必须与合并前逐字一致 ---------------------------------


def test_missing_user_local_registry_is_seeded_verbatim_from_bundled(tmp_path: Path):
    installer = _load_installer()
    user_local = tmp_path / "reviewer-configuration/reviewer-registry.json"

    added = installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
        bundled_registry_path=BUNDLED_REGISTRY_PATH,
        user_local_registry_path=user_local,
    )

    assert _read_registry(user_local) == _bundled_registry()
    assert added == sorted(_bundled_registry()["harnesses"])


# --- RM-2 · 只补缺的，不动已有的 ---------------------------------------------


def test_existing_user_local_registry_gains_only_the_harnesses_it_lacks(tmp_path: Path):
    installer = _load_installer()
    user_local = tmp_path / "reviewer-registry.json"
    bundled = _bundled_registry()
    _write_registry(
        user_local,
        {
            "schema_version": bundled["schema_version"],
            "harnesses": {"cursor": bundled["harnesses"]["cursor"]},
        },
    )

    added = installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
        bundled_registry_path=BUNDLED_REGISTRY_PATH,
        user_local_registry_path=user_local,
    )

    merged = _read_registry(user_local)
    assert set(merged["harnesses"]) == set(bundled["harnesses"])
    assert added == sorted(set(bundled["harnesses"]) - {"cursor"})


def test_existing_harness_entries_are_never_overwritten_by_the_bundled_copy(
    tmp_path: Path,
):
    """用户关掉的腿不得被一次部署重新打开——这是 merge 与 copy 的分水岭。"""
    installer = _load_installer()
    user_local = tmp_path / "reviewer-registry.json"
    bundled = _bundled_registry()
    disabled_cursor = dict(bundled["harnesses"]["cursor"])
    disabled_cursor["enabled"] = False
    disabled_cursor["priority_default"] = 7
    _write_registry(
        user_local,
        {
            "schema_version": bundled["schema_version"],
            "harnesses": {"cursor": disabled_cursor},
        },
    )

    installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
        bundled_registry_path=BUNDLED_REGISTRY_PATH,
        user_local_registry_path=user_local,
    )

    survived = _read_registry(user_local)["harnesses"]["cursor"]
    assert survived["enabled"] is False
    assert survived["priority_default"] == 7


def test_user_added_harnesses_absent_from_bundled_survive_the_merge(tmp_path: Path):
    installer = _load_installer()
    user_local = tmp_path / "reviewer-registry.json"
    bundled = _bundled_registry()
    _write_registry(
        user_local,
        {
            "schema_version": bundled["schema_version"],
            "harnesses": {"home_grown_reviewer": _harness_entry("home_grown_reviewer")},
        },
    )

    installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
        bundled_registry_path=BUNDLED_REGISTRY_PATH,
        user_local_registry_path=user_local,
    )

    merged = _read_registry(user_local)["harnesses"]
    assert "home_grown_reviewer" in merged
    assert set(bundled["harnesses"]).issubset(merged)


# --- RM-3 · 幂等 -------------------------------------------------------------


def test_merge_is_idempotent_across_repeated_deployments(tmp_path: Path):
    installer = _load_installer()
    user_local = tmp_path / "reviewer-registry.json"

    first_added = installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
        bundled_registry_path=BUNDLED_REGISTRY_PATH,
        user_local_registry_path=user_local,
    )
    after_first = user_local.read_text(encoding="utf-8")

    second_added = installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
        bundled_registry_path=BUNDLED_REGISTRY_PATH,
        user_local_registry_path=user_local,
    )

    assert first_added != []
    assert second_added == []
    assert user_local.read_text(encoding="utf-8") == after_first


# --- RM-4 · fail-closed，不静默跳过 -------------------------------------------


def test_schema_version_mismatch_fails_closed_instead_of_silently_skipping(
    tmp_path: Path,
):
    """静默跳过会让新腿永远进不来，且部署照常报成功——必须响亮地失败。"""
    installer = _load_installer()
    user_local = tmp_path / "reviewer-registry.json"
    bundled = _bundled_registry()
    _write_registry(
        user_local,
        {
            "schema_version": bundled["schema_version"] + 1,
            "harnesses": {"cursor": bundled["harnesses"]["cursor"]},
        },
    )

    with pytest.raises(installer.OfficialCliInstallationError) as excinfo:
        installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
            bundled_registry_path=BUNDLED_REGISTRY_PATH,
            user_local_registry_path=user_local,
        )

    assert "schema_version" in str(excinfo.value)


def test_malformed_user_local_registry_fails_closed(tmp_path: Path):
    installer = _load_installer()
    user_local = tmp_path / "reviewer-registry.json"
    user_local.parent.mkdir(parents=True, exist_ok=True)
    user_local.write_text('{"schema_version": 1, "harnesses": []}\n', encoding="utf-8")

    with pytest.raises(installer.OfficialCliInstallationError):
        installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
            bundled_registry_path=BUNDLED_REGISTRY_PATH,
            user_local_registry_path=user_local,
        )


def test_missing_bundled_registry_fails_closed(tmp_path: Path):
    installer = _load_installer()

    with pytest.raises(installer.OfficialCliInstallationError):
        installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
            bundled_registry_path=tmp_path / "does-not-exist.json",
            user_local_registry_path=tmp_path / "reviewer-registry.json",
        )


# --- RM-5 · 走真实部署入口的集成断言 -------------------------------------------


def test_initialize_rvf_user_local_data_merges_into_an_existing_registry(
    tmp_path: Path,
):
    """已部署过的机器再次部署时，新腿必须真的落进它的 user-local registry。"""
    installer = _load_installer()
    home_directory = tmp_path / "home"
    bundled = _bundled_registry()
    stale_registry = (
        home_directory
        / ".rvf/user-local-data/reviewer-configuration/reviewer-registry.json"
    )
    _write_registry(
        stale_registry,
        {
            "schema_version": bundled["schema_version"],
            "harnesses": {"cursor": bundled["harnesses"]["cursor"]},
        },
    )

    installer.initialize_rvf_user_local_data(
        source_repository=ROOT,
        # 夹具播种在默认 home，隔离外层显式数据根覆盖。
        environment={},
        home_directory=home_directory,
    )

    merged = _read_registry(stale_registry)["harnesses"]
    assert set(merged) == set(bundled["harnesses"])


def test_every_shipped_reviewer_leg_reaches_the_user_local_registry(tmp_path: Path):
    """防的是「加了 config 却忘了登记进 registry」——腿在磁盘上却永远不会被选中。"""
    installer = _load_installer()
    home_directory = tmp_path / "home"

    paths = installer.initialize_rvf_user_local_data(
        source_repository=ROOT,
        home_directory=home_directory,
    )

    merged = _read_registry(paths.reviewer_registry_path)["harnesses"]
    skill_root = ROOT / "plugins/review-validate-fix/skills/review-validate-fix"
    shipped_configs = sorted(
        path.name for path in (skill_root / "config").glob("alternative-reviewer.*.json")
    )
    registered_configs = sorted(
        Path(spec["config_path"]).name for spec in merged.values()
    )
    assert registered_configs == shipped_configs


# --- RM-6 · additive 必须补到字段一级，不只是条目一级 ---------------------------
#
# 条目级 additive 只挡住「新腿进不来」。已部署过的机器上，其已有条目冻结在首次
# 部署那天的字段集合上：之后往出厂条目里加的字段永远不会到达它们。第一例是
# underlying_provider_account_quota_pool_identity——缺了它，选腿层的同额度池互斥
# 在生产路径上静默失效（运行时对未声明是刻意宽松的），而 QP-* 只读出厂 registry、
# RM-* 只断言 harness id 集合相等，全部照绿。下面这组测试钉住的正是那条路径。


QUOTA_POOL_FIELD = "underlying_provider_account_quota_pool_identity"


def _registry_frozen_before_a_field_was_introduced(
    *, harness_ids: tuple[str, ...], omitted_field: str
) -> dict:
    """复刻「已部署过的机器」：只有当年那几条腿，且条目里没有后来新增的字段。"""
    bundled = _bundled_registry()
    harnesses = {}
    for harness_id in harness_ids:
        entry = dict(bundled["harnesses"][harness_id])
        entry.pop(omitted_field, None)
        harnesses[harness_id] = entry
    return {"schema_version": bundled["schema_version"], "harnesses": harnesses}


def test_existing_entries_gain_fields_the_bundled_registry_added_later(tmp_path: Path):
    installer = _load_installer()
    user_local = tmp_path / "reviewer-registry.json"
    _write_registry(
        user_local,
        _registry_frozen_before_a_field_was_introduced(
            harness_ids=("cursor", "claude_code", "codex"),
            omitted_field=QUOTA_POOL_FIELD,
        ),
    )

    installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
        bundled_registry_path=BUNDLED_REGISTRY_PATH,
        user_local_registry_path=user_local,
    )

    merged = _read_registry(user_local)["harnesses"]
    undeclared = sorted(
        harness_id
        for harness_id, spec in merged.items()
        if not str(spec.get(QUOTA_POOL_FIELD, "")).strip()
    )
    assert undeclared == [], undeclared
    assert merged == _bundled_registry()["harnesses"]


def test_field_backfill_never_overwrites_a_value_the_user_already_set(tmp_path: Path):
    """键存在 = 用户资产。只有「键根本不存在」才算他从未表达过意见。"""
    installer = _load_installer()
    user_local = tmp_path / "reviewer-registry.json"
    stale = _registry_frozen_before_a_field_was_introduced(
        harness_ids=("cursor", "codex"), omitted_field=QUOTA_POOL_FIELD
    )
    stale["harnesses"]["codex"][QUOTA_POOL_FIELD] = "my-own-corporate-codex-account"
    stale["harnesses"]["codex"]["priority_default"] = 3
    _write_registry(user_local, stale)

    installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
        bundled_registry_path=BUNDLED_REGISTRY_PATH,
        user_local_registry_path=user_local,
    )

    survived = _read_registry(user_local)["harnesses"]["codex"]
    assert survived[QUOTA_POOL_FIELD] == "my-own-corporate-codex-account"
    assert survived["priority_default"] == 3


def test_a_field_the_user_explicitly_emptied_is_left_empty(tmp_path: Path):
    """置空是「我要这条腿算独立池」的表达方式，不是缺失，不得被出厂值顶回去。"""
    installer = _load_installer()
    user_local = tmp_path / "reviewer-registry.json"
    stale = _registry_frozen_before_a_field_was_introduced(
        harness_ids=("cursor", "codex"), omitted_field=QUOTA_POOL_FIELD
    )
    stale["harnesses"]["codex"][QUOTA_POOL_FIELD] = ""
    _write_registry(user_local, stale)

    installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
        bundled_registry_path=BUNDLED_REGISTRY_PATH,
        user_local_registry_path=user_local,
    )

    assert _read_registry(user_local)["harnesses"]["codex"][QUOTA_POOL_FIELD] == ""


def test_field_backfill_writes_the_file_even_when_no_new_harness_was_added(
    tmp_path: Path,
):
    """全部 harness 都已存在、只缺字段时，返回值是空表——但磁盘必须被改到。

    这正是这条缺陷曾经藏身的地方：早退在「没有新 harness」上，字段永远补不进去。
    """
    installer = _load_installer()
    user_local = tmp_path / "reviewer-registry.json"
    bundled = _bundled_registry()
    _write_registry(
        user_local,
        _registry_frozen_before_a_field_was_introduced(
            harness_ids=tuple(bundled["harnesses"]), omitted_field=QUOTA_POOL_FIELD
        ),
    )

    added = installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
        bundled_registry_path=BUNDLED_REGISTRY_PATH,
        user_local_registry_path=user_local,
    )
    after_backfill = user_local.read_text(encoding="utf-8")

    assert added == []
    assert _read_registry(user_local)["harnesses"] == bundled["harnesses"]

    second_added = installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
        bundled_registry_path=BUNDLED_REGISTRY_PATH,
        user_local_registry_path=user_local,
    )

    assert second_added == []
    assert user_local.read_text(encoding="utf-8") == after_backfill


def test_field_backfill_leaves_user_added_harnesses_alone(tmp_path: Path):
    """出厂 registry 里没有的 harness 没有「出厂字段集合」可言，不得被塞字段。"""
    installer = _load_installer()
    user_local = tmp_path / "reviewer-registry.json"
    bundled = _bundled_registry()
    home_grown = _harness_entry("home_grown_reviewer")
    _write_registry(
        user_local,
        {
            "schema_version": bundled["schema_version"],
            "harnesses": {"home_grown_reviewer": dict(home_grown)},
        },
    )

    installer.merge_bundled_reviewer_harnesses_into_user_local_registry(
        bundled_registry_path=BUNDLED_REGISTRY_PATH,
        user_local_registry_path=user_local,
    )

    assert _read_registry(user_local)["harnesses"]["home_grown_reviewer"] == home_grown


def test_initialize_rvf_user_local_data_backfills_fields_on_a_deployed_machine(
    tmp_path: Path,
):
    """走真实部署入口：老机器的三条腿必须在这次部署后拿到新字段。"""
    installer = _load_installer()
    home_directory = tmp_path / "home"
    stale_registry = (
        home_directory
        / ".rvf/user-local-data/reviewer-configuration/reviewer-registry.json"
    )
    _write_registry(
        stale_registry,
        _registry_frozen_before_a_field_was_introduced(
            harness_ids=("cursor", "claude_code", "codex"),
            omitted_field=QUOTA_POOL_FIELD,
        ),
    )

    installer.initialize_rvf_user_local_data(
        source_repository=ROOT,
        # 夹具播种在默认 home，隔离外层显式数据根覆盖。
        environment={},
        home_directory=home_directory,
    )

    merged = _read_registry(stale_registry)["harnesses"]
    assert merged == _bundled_registry()["harnesses"]
