"""评审腿可用性探针结果的带 TTL 缓存契约。

**为什么需要**：`probe_available` 对每个 enabled harness、每一轮 RVF 都真跑一次
`run_alternative_reviewer.py --preflight`，而那是一次**真实模型调用**。腿从 3 条扩到
5 条之后，每轮 RVF 稳态多付 2 次（实测 omp 单次 probe input 39,499 tokens），
而两轮之间 harness 的可用性几乎不会变。

**为什么不能改用「更便宜的检查」来判可用**：这正是 health probe parity gate 要防的形状。
run rvf-20260811T170157Z：cursor 登录过期时 `cursor-agent status` 退 0 而 `-p` 退 1，
RVF 因此把死腿选进两腿，整轮自认为做了双评审、实际只有单路结论。所以本模块的定位是
**复用一次真实 probe 的结论**，不是**用廉价信号替代它**：

- 零成本手段只用于**快速否定**（可执行文件不在 PATH ⇒ 必然不可用），这个方向不会把
  死腿判活；
- 判「可用」永远只能来自真实 probe，或它未过期、且身份未变的缓存。

**为什么成功与失败必须用不同 TTL**：缓存「可用」过久 ⇒ 选到已经死掉的腿（parity 事故的
重演，只是延迟了）；缓存「不可用」过久 ⇒ 刚修好登录的腿在很长一段时间里无故缺席。
两个方向的代价不对称，所以 TTL 也必须不对称。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.reviewer_harness_probe_result_cache import (  # noqa: E402
    PROBE_OUTCOME_AVAILABLE,
    PROBE_OUTCOME_UNAVAILABLE,
    compute_probe_identity_digest,
    invalidate_cached_probe_outcome,
    read_cached_probe_outcome,
    resolved_probe_result_cache_entry_path,
    write_probe_outcome,
)


def _environment(root: Path) -> dict[str, str]:
    return {"RVF_USER_LOCAL_DATA_ROOT": str(root)}


def _identity(value: str = "identity-a") -> str:
    return value


# --- PC-1 · TTL 语义 -----------------------------------------------------------


def test_outcome_written_now_is_readable_within_its_ttl(tmp_path: Path):
    env = _environment(tmp_path)
    write_probe_outcome(
        "cursor",
        PROBE_OUTCOME_AVAILABLE,
        probe_identity_digest=_identity(),
        now_epoch_seconds=1_000.0,
        available_ttl_seconds=600.0,
        unavailable_ttl_seconds=60.0,
        environment=env,
    )
    assert (
        read_cached_probe_outcome(
            "cursor",
            probe_identity_digest=_identity(),
            now_epoch_seconds=1_599.0,
            environment=env,
        )
        == PROBE_OUTCOME_AVAILABLE
    )


def test_outcome_is_a_miss_once_its_ttl_elapsed(tmp_path: Path):
    env = _environment(tmp_path)
    write_probe_outcome(
        "cursor",
        PROBE_OUTCOME_AVAILABLE,
        probe_identity_digest=_identity(),
        now_epoch_seconds=1_000.0,
        available_ttl_seconds=600.0,
        unavailable_ttl_seconds=60.0,
        environment=env,
    )
    assert (
        read_cached_probe_outcome(
            "cursor",
            probe_identity_digest=_identity(),
            now_epoch_seconds=1_601.0,
            environment=env,
        )
        is None
    )


# --- PC-2 · 成功与失败的 TTL 不对称 ---------------------------------------------


def test_unavailable_outcome_expires_on_its_own_shorter_ttl(tmp_path: Path):
    """腿修好登录后不该被一条长寿的「不可用」缓存继续挡在门外。"""
    env = _environment(tmp_path)
    write_probe_outcome(
        "kimi_code",
        PROBE_OUTCOME_UNAVAILABLE,
        probe_identity_digest=_identity(),
        now_epoch_seconds=1_000.0,
        available_ttl_seconds=600.0,
        unavailable_ttl_seconds=60.0,
        environment=env,
    )
    assert (
        read_cached_probe_outcome(
            "kimi_code",
            probe_identity_digest=_identity(),
            now_epoch_seconds=1_059.0,
            environment=env,
        )
        == PROBE_OUTCOME_UNAVAILABLE
    )
    assert (
        read_cached_probe_outcome(
            "kimi_code",
            probe_identity_digest=_identity(),
            now_epoch_seconds=1_061.0,
            environment=env,
        )
        is None
    )


# --- PC-3 · 身份变化即失效 -------------------------------------------------------


def test_identity_change_is_a_miss_even_within_ttl(tmp_path: Path):
    """改了 config 或换了 CLI 版本之后，上一轮的结论不再代表这一轮要跑的东西。"""
    env = _environment(tmp_path)
    write_probe_outcome(
        "codex",
        PROBE_OUTCOME_AVAILABLE,
        probe_identity_digest=_identity("identity-a"),
        now_epoch_seconds=1_000.0,
        available_ttl_seconds=600.0,
        unavailable_ttl_seconds=60.0,
        environment=env,
    )
    assert (
        read_cached_probe_outcome(
            "codex",
            probe_identity_digest=_identity("identity-b"),
            now_epoch_seconds=1_010.0,
            environment=env,
        )
        is None
    )


def test_probe_identity_digest_tracks_config_content_and_executable_identity(
    tmp_path: Path,
):
    config = tmp_path / "reviewer.json"
    config.write_text('{"command": ["x"]}', encoding="utf-8")
    executable = tmp_path / "x"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    first = compute_probe_identity_digest(
        config_path=config,
        user_local_config_override_path=None, executable_path=executable, probe_mode="preflight"
    )
    assert first == compute_probe_identity_digest(
        config_path=config,
        user_local_config_override_path=None, executable_path=executable, probe_mode="preflight"
    )

    config.write_text('{"command": ["y"]}', encoding="utf-8")
    assert (
        compute_probe_identity_digest(
            config_path=config,
            user_local_config_override_path=None, executable_path=executable, probe_mode="preflight"
        )
        != first
    )


def test_probe_mode_is_part_of_the_identity(tmp_path: Path):
    config = tmp_path / "reviewer.json"
    config.write_text("{}", encoding="utf-8")
    executable = tmp_path / "x"
    executable.write_text("", encoding="utf-8")
    assert compute_probe_identity_digest(
        config_path=config,
        user_local_config_override_path=None, executable_path=executable, probe_mode="preflight"
    ) != compute_probe_identity_digest(
        config_path=config,
        user_local_config_override_path=None, executable_path=executable, probe_mode="check"
    )


def test_missing_executable_still_yields_a_stable_digest(tmp_path: Path):
    config = tmp_path / "reviewer.json"
    config.write_text("{}", encoding="utf-8")
    absent = tmp_path / "not-installed"
    digest = compute_probe_identity_digest(
        config_path=config,
        user_local_config_override_path=None, executable_path=absent, probe_mode="preflight"
    )
    assert digest == compute_probe_identity_digest(
        config_path=config,
        user_local_config_override_path=None, executable_path=absent, probe_mode="preflight"
    )


# --- PC-4 · 主动作废 ------------------------------------------------------------


def test_invalidation_removes_a_live_entry(tmp_path: Path):
    """派发时这条腿真挂了 ⇒ 必须立刻废掉它的「可用」缓存，否则整轮都会把死腿喂回选腿层。"""
    env = _environment(tmp_path)
    write_probe_outcome(
        "oh_my_pi",
        PROBE_OUTCOME_AVAILABLE,
        probe_identity_digest=_identity(),
        now_epoch_seconds=1_000.0,
        available_ttl_seconds=600.0,
        unavailable_ttl_seconds=60.0,
        environment=env,
    )
    assert invalidate_cached_probe_outcome("oh_my_pi", environment=env) is True
    assert (
        read_cached_probe_outcome(
            "oh_my_pi",
            probe_identity_digest=_identity(),
            now_epoch_seconds=1_010.0,
            environment=env,
        )
        is None
    )


def test_invalidating_an_absent_entry_is_not_an_error(tmp_path: Path):
    assert (
        invalidate_cached_probe_outcome("never_written", environment=_environment(tmp_path))
        is False
    )


# --- PC-5 · 读侧一律 fail-open 到「重新 probe」，绝不 fail-open 到「可用」-----------


def test_unreadable_entry_is_a_miss_rather_than_an_assumed_available(tmp_path: Path):
    env = _environment(tmp_path)
    path = resolved_probe_result_cache_entry_path("cursor", environment=env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all", encoding="utf-8")
    assert (
        read_cached_probe_outcome(
            "cursor",
            probe_identity_digest=_identity(),
            now_epoch_seconds=1_000.0,
            environment=env,
        )
        is None
    )


def test_entry_with_unknown_outcome_value_is_a_miss(tmp_path: Path):
    env = _environment(tmp_path)
    path = resolved_probe_result_cache_entry_path("cursor", environment=env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "outcome": "probably_fine",
                "probe_identity_digest": _identity(),
                "expires_at_epoch_seconds": 9_999_999.0,
            }
        ),
        encoding="utf-8",
    )
    assert (
        read_cached_probe_outcome(
            "cursor",
            probe_identity_digest=_identity(),
            now_epoch_seconds=1_000.0,
            environment=env,
        )
        is None
    )


def test_absent_entry_is_a_miss(tmp_path: Path):
    assert (
        read_cached_probe_outcome(
            "cursor",
            probe_identity_digest=_identity(),
            now_epoch_seconds=1_000.0,
            environment=_environment(tmp_path),
        )
        is None
    )


def test_writing_an_unknown_outcome_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        write_probe_outcome(
            "cursor",
            "probably_fine",
            probe_identity_digest=_identity(),
            now_epoch_seconds=1_000.0,
            available_ttl_seconds=600.0,
            unavailable_ttl_seconds=60.0,
            environment=_environment(tmp_path),
        )


# --- PC-6 · 逃生口 --------------------------------------------------------------


def test_cache_can_be_disabled_by_environment_for_a_forced_fresh_probe(tmp_path: Path):
    env = _environment(tmp_path)
    write_probe_outcome(
        "cursor",
        PROBE_OUTCOME_AVAILABLE,
        probe_identity_digest=_identity(),
        now_epoch_seconds=1_000.0,
        available_ttl_seconds=600.0,
        unavailable_ttl_seconds=60.0,
        environment=env,
    )
    disabled = dict(env)
    disabled["RVF_REVIEWER_HARNESS_PROBE_RESULT_CACHE_DISABLED"] = "1"
    assert (
        read_cached_probe_outcome(
            "cursor",
            probe_identity_digest=_identity(),
            now_epoch_seconds=1_010.0,
            environment=disabled,
        )
        is None
    )


# --- PC-7 · 隔离与落点 ----------------------------------------------------------


def test_entries_are_isolated_per_harness(tmp_path: Path):
    env = _environment(tmp_path)
    write_probe_outcome(
        "cursor",
        PROBE_OUTCOME_AVAILABLE,
        probe_identity_digest=_identity(),
        now_epoch_seconds=1_000.0,
        available_ttl_seconds=600.0,
        unavailable_ttl_seconds=60.0,
        environment=env,
    )
    assert (
        read_cached_probe_outcome(
            "codex",
            probe_identity_digest=_identity(),
            now_epoch_seconds=1_010.0,
            environment=env,
        )
        is None
    )


def test_entry_lives_under_the_single_user_local_state_root(tmp_path: Path):
    """必须认 $RVF_USER_LOCAL_DATA_ROOT——本仓栽过「状态根分裂成两棵树」的亏。"""
    env = _environment(tmp_path)
    path = resolved_probe_result_cache_entry_path("cursor", environment=env)
    assert tmp_path in path.parents


# --------------------------------------------------------------------------------------
# PC-8：probe_available 的接线层
#
# 单测模块本身证明不了「每轮 RVF 真的少付了钱」——省钱发生在 probe_available 决定
# 不 spawn 子进程那一刻。这一段直接观测 spawn 次数。
# --------------------------------------------------------------------------------------

import importlib.util  # noqa: E402
import types  # noqa: E402

SKILL_ROOT = ROOT / "plugins/review-validate-fix/skills/review-validate-fix"


def _load_dispatch_reviewers():
    scripts_dir = SKILL_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location(
        "dispatch_reviewers_for_probe_cache_tests", scripts_dir / "dispatch_reviewers.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _registry_with(harness_id: str, config_relative_path: str) -> dict:
    return {
        "schema_version": 1,
        "harnesses": {
            harness_id: {
                "harness_id": harness_id,
                "label_prefix": f"alternative-reviewer:{harness_id}",
                "config_path": config_relative_path,
                "dispatch_mode": "external_cli",
                "enabled": True,
                "priority_default": 50,
            }
        },
    }


class _SpawnCounter:
    def __init__(self, returncode: int = 0):
        self.calls = 0
        self.returncode = returncode

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return types.SimpleNamespace(returncode=self.returncode)


def _probe_once(dispatch, registry, counter, monkeypatch, state_root: Path):
    monkeypatch.setattr(dispatch.subprocess, "run", counter)
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(state_root))
    return dispatch.probe_available(registry)


HERMETIC_PROBE_HARNESS_ID = "hermetic-probe-cache"


def _write_fake_probe_executable(executable_path: Path, shell_body: str) -> Path:
    """造一个只为「能被 shutil.which 从 PATH 上解析出来」而存在的假 CLI。

    它永远不会被真的执行（``subprocess.run`` 在这些测试里被 ``_SpawnCounter`` 顶掉），
    只提供两样东西：``_resolved_probe_execution_inputs`` 要的 PATH 命中，以及
    ``compute_probe_identity_digest`` 要的 (size, mtime_ns) 身份。
    """
    executable_path.parent.mkdir(parents=True, exist_ok=True)
    executable_path.write_text(f"#!/bin/sh\n{shell_body}\n", encoding="utf-8")
    executable_path.chmod(0o755)
    return executable_path


def _install_hermetic_probe_harness(
    dispatch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict, Path]:
    """把 probe 依赖的三样外部东西全部关进 tmp_path：PATH、reviewer config、SKILL_DIR。

    这一层存在的唯一理由，是让「TTL 内第二次不再 spawn」那条断言**不取决于本机装了
    哪个 CLI**。它此前拿出厂 cursor 模板当输入，于是在没装 cursor-agent 的机器上
    落进零成本否定分支、静默 ``pytest.skip``，而 check_skill_contracts.sh 照报契约
    通过——这个功能唯一证明「省到钱了」的断言在那些机器上零覆盖。
    """
    executable_name = "rvf-fake-probe-cli"
    fake_executable_path = _write_fake_probe_executable(
        tmp_path / "bin" / executable_name, "exit 0"
    )
    # 整个替换 PATH 而不是前置：保证唯一能被解析出来的可执行文件就是我们自己造的那个，
    # 本机装没装任何真实 reviewer CLI 都不再影响结论。
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    config_relative_path = f"config/alternative-reviewer.{HERMETIC_PROBE_HARNESS_ID}.json"
    config_path = tmp_path / config_relative_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"command": [executable_name]}), encoding="utf-8")
    monkeypatch.setattr(dispatch, "SKILL_DIR", tmp_path)

    return (
        _registry_with(HERMETIC_PROBE_HARNESS_ID, config_relative_path),
        fake_executable_path,
    )


def test_second_probe_within_ttl_does_not_spawn_a_second_real_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """这一条就是这个功能的全部意义：同一轮结论在 TTL 内复用，不再付第二次钱。"""
    dispatch = _load_dispatch_reviewers()
    registry, _ = _install_hermetic_probe_harness(dispatch, tmp_path, monkeypatch)

    counter = _SpawnCounter(returncode=0)
    state_root = tmp_path / "state"

    first = _probe_once(dispatch, registry, counter, monkeypatch, state_root)
    assert first == [HERMETIC_PROBE_HARNESS_ID]
    # 先钉住首轮真的 spawn 过，否则「第二轮没再 spawn」在一个从头到尾没 spawn 过的
    # 路径上恒真——那正是这条断言此前 skip 掉时的形状。
    assert counter.calls == 1, "首轮必须真的 spawn 一次真实 probe"

    second = _probe_once(dispatch, registry, counter, monkeypatch, state_root)
    assert second == [HERMETIC_PROBE_HARNESS_ID]
    assert counter.calls == 1, "第二轮不该再 spawn 真实 probe"


def test_probe_respawns_after_the_probed_executable_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """缓存的另一侧：CLI 换了版本 ⇒ 上一轮结论不再代表这一轮要探的东西，必须重新 spawn。

    只断言「第二次不再 spawn」是不够的：那与「永远不再 spawn」无法区分，而后者正是
    把已经死掉的腿一路判活的形状。
    """
    dispatch = _load_dispatch_reviewers()
    registry, fake_executable_path = _install_hermetic_probe_harness(
        dispatch, tmp_path, monkeypatch
    )

    counter = _SpawnCounter(returncode=0)
    state_root = tmp_path / "state"

    assert _probe_once(dispatch, registry, counter, monkeypatch, state_root) == [
        HERMETIC_PROBE_HARNESS_ID
    ]
    assert _probe_once(dispatch, registry, counter, monkeypatch, state_root) == [
        HERMETIC_PROBE_HARNESS_ID
    ]
    assert counter.calls == 1, "身份未变的第二轮必须命中缓存"

    # 模拟 CLI 升级：(size, mtime_ns) 一变，probe 身份摘要就不再是上一轮那个。
    _write_fake_probe_executable(
        fake_executable_path, "exit 0  # 升级后的新版本，字节数与上一版不同"
    )

    third = _probe_once(dispatch, registry, counter, monkeypatch, state_root)
    assert third == [HERMETIC_PROBE_HARNESS_ID]
    assert counter.calls == 2, "可执行文件身份变了之后必须重新 spawn 真实 probe"


def test_absent_executable_is_rejected_without_spawning_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """零成本否定：命令不在 PATH ⇒ 直接判不可用，不为一个注定的结论付一次模型调用。"""
    dispatch = _load_dispatch_reviewers()
    config_directory = tmp_path / "config"
    config_directory.mkdir(parents=True, exist_ok=True)
    config_path = config_directory / "alternative-reviewer.ghost.json"
    config_path.write_text(
        json.dumps({"command": ["definitely-not-installed-anywhere-xyz"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(dispatch, "SKILL_DIR", tmp_path)
    registry = _registry_with("ghost", "config/alternative-reviewer.ghost.json")

    counter = _SpawnCounter(returncode=0)
    assert _probe_once(dispatch, registry, counter, monkeypatch, tmp_path / "state") == []
    assert counter.calls == 0


def test_failed_dispatch_invalidates_the_cached_available_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """派发实证腿挂了 ⇒ 缓存必须当场作废，否则错误会在整个 TTL 内自我延续。"""
    dispatch = _load_dispatch_reviewers()
    state_root = tmp_path / "state"
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(state_root))
    env = {"RVF_USER_LOCAL_DATA_ROOT": str(state_root)}

    write_probe_outcome(
        "codex",
        PROBE_OUTCOME_AVAILABLE,
        probe_identity_digest="identity-a",
        now_epoch_seconds=1_000.0,
        available_ttl_seconds=3_600.0,
        unavailable_ttl_seconds=60.0,
        environment=env,
    )
    assert dispatch.invalidate_probe_result_cache_for_failed_dispatch("codex") is True
    assert (
        read_cached_probe_outcome(
            "codex",
            probe_identity_digest="identity-a",
            now_epoch_seconds=1_010.0,
            environment=env,
        )
        is None
    )


# --------------------------------------------------------------------------------------
# PC-9：TTL env 取值语义
#
# 这个模块只有一条方向性纪律：**拿不准一律落在「重新 probe」那一侧，绝不落在「继续采信
# 一条陈旧的 available」那一侧**。env 解析是这条纪律最容易被悄悄破掉的地方，因为它的输入
# 来自人手，而人会写 0、写负数、写 inf。
# --------------------------------------------------------------------------------------

from core.reviewer_harness_probe_result_cache import (  # noqa: E402
    DEFAULT_AVAILABLE_OUTCOME_TTL_SECONDS,
    DEFAULT_UNAVAILABLE_OUTCOME_TTL_SECONDS,
    resolved_outcome_ttl_seconds,
)

AVAILABLE_TTL_VARIABLE = "RVF_REVIEWER_HARNESS_PROBE_RESULT_CACHE_AVAILABLE_TTL_SECONDS"
UNAVAILABLE_TTL_VARIABLE = "RVF_REVIEWER_HARNESS_PROBE_RESULT_CACHE_UNAVAILABLE_TTL_SECONDS"


def _available_ttl(raw: str | None) -> float:
    env = {} if raw is None else {AVAILABLE_TTL_VARIABLE: raw}
    return resolved_outcome_ttl_seconds(PROBE_OUTCOME_AVAILABLE, environment=env)


def test_explicit_zero_means_do_not_reuse_rather_than_fall_back_to_the_default():
    """写 0 的意图是「这一轮别复用」，回退成 1800 会把意图翻转到不安全的一侧。"""
    assert _available_ttl("0") == 0.0


def test_negative_ttl_is_clamped_to_zero_not_to_the_default():
    """负数不是合法 TTL，但写负数的人绝不是想要「缓存更久」——夹到 0 才落在安全侧。"""
    assert _available_ttl("-1") == 0.0
    assert _available_ttl("-3600") == 0.0


def test_infinite_ttl_is_rejected_because_it_would_never_re_probe_a_dead_leg():
    """`inf` 等价于永不过期的 available 缓存：一条腿死了也再没有任何时刻会重新 probe。"""
    assert _available_ttl("inf") == DEFAULT_AVAILABLE_OUTCOME_TTL_SECONDS
    assert _available_ttl("-inf") == DEFAULT_AVAILABLE_OUTCOME_TTL_SECONDS
    assert _available_ttl("Infinity") == DEFAULT_AVAILABLE_OUTCOME_TTL_SECONDS


def test_nan_is_rejected_explicitly_rather_than_by_coincidence():
    """旧实现靠 `nan > 0` 恒假才碰巧安全；靠巧合成立的安全性不能留着等人重构掉。"""
    assert _available_ttl("nan") == DEFAULT_AVAILABLE_OUTCOME_TTL_SECONDS


def test_unparseable_value_falls_back_to_the_default():
    assert _available_ttl("abc") == DEFAULT_AVAILABLE_OUTCOME_TTL_SECONDS
    assert _available_ttl("") == DEFAULT_AVAILABLE_OUTCOME_TTL_SECONDS


def test_absent_variable_uses_the_default():
    assert _available_ttl(None) == DEFAULT_AVAILABLE_OUTCOME_TTL_SECONDS


def test_positive_value_is_taken_verbatim():
    assert _available_ttl("600") == 600.0
    assert _available_ttl("0.5") == 0.5


def test_the_same_rules_apply_to_the_unavailable_ttl_variable():
    """两条 TTL 走同一套解析——只修 available 那条会留下一个不对称的坑。"""
    def unavailable(raw: str) -> float:
        return resolved_outcome_ttl_seconds(
            PROBE_OUTCOME_UNAVAILABLE, environment={UNAVAILABLE_TTL_VARIABLE: raw}
        )

    assert unavailable("0") == 0.0
    assert unavailable("-1") == 0.0
    assert unavailable("inf") == DEFAULT_UNAVAILABLE_OUTCOME_TTL_SECONDS
    assert unavailable("nan") == DEFAULT_UNAVAILABLE_OUTCOME_TTL_SECONDS
    assert unavailable("90") == 90.0


def test_zero_ttl_actually_prevents_reuse_end_to_end(tmp_path: Path):
    """解析对了还不够：0 必须在读侧真的表现为「不复用」。"""
    env = {
        "RVF_USER_LOCAL_DATA_ROOT": str(tmp_path),
        AVAILABLE_TTL_VARIABLE: "0",
    }
    write_probe_outcome(
        "cursor",
        PROBE_OUTCOME_AVAILABLE,
        probe_identity_digest="identity-a",
        now_epoch_seconds=1_000.0,
        environment=env,
    )
    assert (
        read_cached_probe_outcome(
            "cursor",
            probe_identity_digest="identity-a",
            now_epoch_seconds=1_000.001,
            environment=env,
        )
        is None
    )


# --------------------------------------------------------------------------------------
# PC-10：身份摘要必须覆盖「实际会被执行的那份配置」
#
# `--preflight` 跑的是 bundled config **叠加** user-local override 之后的有效配置，而
# override 可以改掉 health_command / health_timeout_seconds / pre_run_health / env_set。
# 摘要只哈希 bundled 那份时，在 TTL 内改 override 仍会命中旧结论——失效方向落在不安全一侧
# （一条已经失效的腿被继续喂给选腿层）。
# --------------------------------------------------------------------------------------


def _digest(config: Path, override: Path | None, executable: Path) -> str:
    return compute_probe_identity_digest(
        config_path=config,
        user_local_config_override_path=override,
        executable_path=executable,
        probe_mode="preflight",
    )


def _config_and_executable(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "reviewer.json"
    config.write_text('{"command": ["x"], "label": "alternative-reviewer:x-cli"}', encoding="utf-8")
    executable = tmp_path / "x"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    return config, executable


def test_override_content_change_changes_the_identity_digest(tmp_path: Path):
    """改掉 override 里的 health_command 必须让上一轮结论失效。"""
    config, executable = _config_and_executable(tmp_path)
    override = tmp_path / "override.json"
    override.write_text('{"health_command": ["x", "--check"]}', encoding="utf-8")
    first = _digest(config, override, executable)

    override.write_text('{"health_command": ["x", "--totally-different"]}', encoding="utf-8")
    assert _digest(config, override, executable) != first


def test_creating_an_override_where_there_was_none_changes_the_digest(tmp_path: Path):
    """从无到有新建 override，正是最需要缓存失效的时刻——不能因为「之前没有」就跳过它。"""
    config, executable = _config_and_executable(tmp_path)
    override = tmp_path / "override.json"
    without = _digest(config, override, executable)  # 文件尚不存在
    override.write_text('{"pre_run_health": false}', encoding="utf-8")
    assert _digest(config, override, executable) != without


def test_absent_override_yields_a_stable_digest(tmp_path: Path):
    """多数机器上没有 override；这条路径必须稳定，否则每轮都不命中、缓存等于没有。"""
    config, executable = _config_and_executable(tmp_path)
    override = tmp_path / "override.json"
    assert _digest(config, override, executable) == _digest(config, override, executable)


def test_unresolved_override_path_is_distinguishable_from_an_absent_file(tmp_path: Path):
    """「推不出 override 路径」与「路径推得出但文件不存在」是两种状态，不该压成同一个摘要。"""
    config, executable = _config_and_executable(tmp_path)
    absent = tmp_path / "override.json"
    assert _digest(config, None, executable) != _digest(config, absent, executable)


def test_override_is_not_confused_with_the_bundled_config(tmp_path: Path):
    """同样的字节放在 bundled 和 override 两个位置，摘要必须不同（否则分隔符形同虚设）。"""
    config, executable = _config_and_executable(tmp_path)
    same_bytes = '{"pre_run_health": false}'
    override = tmp_path / "override.json"
    override.write_text(same_bytes, encoding="utf-8")
    with_override = _digest(config, override, executable)

    config2 = tmp_path / "reviewer2.json"
    config2.write_text(same_bytes, encoding="utf-8")
    absent = tmp_path / "nope.json"
    assert with_override != _digest(config2, absent, executable)



# --------------------------------------------------------------------------------------
# PC-9：内层 health 失败（含超时）走的是「写成 unavailable + 短 TTL」这一路
#
# 这一段存在的理由是一句曾经会误导人的注释：`probe_available` 的 except 只兜得住**外层
# probe 进程**的超时，而各腿 config 里的 health_timeout_seconds 一律小于 --probe-timeout
# （30/45/45/50 < 60），所以慢腿真实的 health 超时**几乎总是**由内层先触发、经 run_health
# 的 fail() 变成一个非 0 退出码，再被写成 unavailable。这是刻意的取舍而非漏网：腿在自己
# 声明的预算内没响应，本身就是关于这条腿的信息；它的瞬时性由短 TTL 兜住。
#
# 下面两条把这条链的两端各钉死一次：上游钉「内层超时不会冒泡成外层 TimeoutExpired」，
# 下游钉「非 0 退出码确实落盘成 unavailable，且用的是 unavailable 那条短 TTL」。少了任何
# 一端，后人都可能把它悄悄改成「不写缓存」或「按 available 的长 TTL 缓存」而不被发现。
# --------------------------------------------------------------------------------------

import shutil  # noqa: E402
import subprocess  # noqa: E402

from core.reviewer_harness_probe_result_cache.reviewer_harness_probe_result_cache import (  # noqa: E402
    PROBE_RESULT_CACHE_AVAILABLE_TTL_SECONDS_ENVIRONMENT_VARIABLE,
    PROBE_RESULT_CACHE_DISABLED_ENVIRONMENT_VARIABLE,
    PROBE_RESULT_CACHE_UNAVAILABLE_TTL_SECONDS_ENVIRONMENT_VARIABLE,
)


def test_inner_health_timeout_reaches_the_probe_as_a_nonzero_exit_code(
    monkeypatch: pytest.MonkeyPatch,
):
    """链条上游：腿自己的 health 超时**不会**冒泡成外层的 TimeoutExpired。

    它在 `run_health` 里就被 `fail()` 收成一个普通的非 0 退出码，因此 `probe_available`
    看到的是 returncode != 0——这正是它会被缓存成 unavailable 的原因。
    """
    scripts_directory = SKILL_ROOT / "scripts"
    if str(scripts_directory) not in sys.path:
        sys.path.insert(0, str(scripts_directory))
    import run_alternative_reviewer as runner  # noqa: E402

    def _always_times_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["health"], timeout=1)

    monkeypatch.setattr(runner.subprocess, "run", _always_times_out)
    assert runner.run_health(["health"], {}, 1) != 0


def test_failed_inner_health_is_cached_unavailable_on_the_short_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """链条下游：非 0 退出码确实落盘成 unavailable，且计的是 unavailable 那条短 TTL。

    短 TTL 是这条路径成立的全部前提——它让「慢腿这一轮没答上来」很快作废、下一轮重新真跑。
    要是哪天被改成按 available 的长 TTL 记，一次瞬时超时就会把腿封禁半小时。
    """
    dispatch = _load_dispatch_reviewers()
    if shutil.which("sh") is None:
        pytest.skip("本机 PATH 上没有 sh，过不了零成本否定那一关")

    config_directory = tmp_path / "config"
    config_directory.mkdir(parents=True, exist_ok=True)
    (config_directory / "alternative-reviewer.slow-health.json").write_text(
        json.dumps({"command": ["sh"]}), encoding="utf-8"
    )
    monkeypatch.setattr(dispatch, "SKILL_DIR", tmp_path)
    registry = _registry_with(
        "slow-health", "config/alternative-reviewer.slow-health.json"
    )

    state_root = tmp_path / "state"
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(state_root))
    monkeypatch.delenv(PROBE_RESULT_CACHE_DISABLED_ENVIRONMENT_VARIABLE, raising=False)
    # 两条 TTL 拉开量级，好让「用错了哪一条」变成一个可观测的差异。
    monkeypatch.setenv(
        PROBE_RESULT_CACHE_AVAILABLE_TTL_SECONDS_ENVIRONMENT_VARIABLE, "9000"
    )
    monkeypatch.setenv(
        PROBE_RESULT_CACHE_UNAVAILABLE_TTL_SECONDS_ENVIRONMENT_VARIABLE, "60"
    )

    clock = {"now": 1_000.0}
    monkeypatch.setattr(dispatch.time, "time", lambda: clock["now"])

    # 内层 health 超时抵达这一层时就长这样：一个非 0 退出码，和其他 health 失败无从区分。
    counter = _SpawnCounter(returncode=1)
    monkeypatch.setattr(dispatch.subprocess, "run", counter)

    assert dispatch.probe_available(registry) == []
    assert counter.calls == 1

    entry = json.loads(
        resolved_probe_result_cache_entry_path(
            "slow-health", environment=_environment(state_root)
        ).read_text(encoding="utf-8")
    )
    assert entry["outcome"] == PROBE_OUTCOME_UNAVAILABLE, (
        "内层 health 失败/超时必须记成 unavailable；改成不写缓存就等于每轮都重付这次探针钱"
    )
    assert entry["ttl_seconds"] == 60.0, (
        "必须计 unavailable 那条短 TTL，不是 available 的长 TTL"
    )

    # 短 TTL 之内：结论被复用、不再 spawn —— 证明它**确实**写进了缓存。
    clock["now"] = 1_030.0
    assert dispatch.probe_available(registry) == []
    assert counter.calls == 1, "短 TTL 内应命中缓存，不该再 spawn 一次真实 probe"

    # 短 TTL 之外、长 TTL 之内：必须重新真跑 —— 证明用的不是 available 的长 TTL。
    clock["now"] = 1_100.0
    assert dispatch.probe_available(registry) == []
    assert counter.calls == 2, (
        "短 TTL 一过就该重新真跑；仍然命中说明这条 unavailable 被按长 TTL 缓存了"
    )
