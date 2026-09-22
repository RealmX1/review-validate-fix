"""评审腿可用性探针结果的带 TTL 缓存（host-中性，唯一状态根下）。

**它解决什么**：``dispatch_reviewers.probe_available`` 对每个 enabled harness、每一轮 RVF
都真跑一次 ``run_alternative_reviewer.py --preflight``——那是一次**真实模型调用**。腿从
3 条扩到 5 条后，每轮稳态多付 2 次（实测 omp 单次 probe input 39,499 tokens），而两轮之间
harness 的可用性几乎不变。本模块让一次真实 probe 的结论在一段时间内可复用。

**它刻意不做什么**：不用「更便宜的检查」替代真实 probe。那正是 health probe parity gate
要防的形状——run rvf-20260811T170157Z 里 ``cursor-agent status`` 退 0 而 ``-p`` 退 1，
RVF 因此把死腿选进两腿，整轮自认为做了双评审、实际只有单路结论。本模块只搬运真实 probe
的结论，判据的来源一个字都没变。

**三条不对称，都是刻意的**：

1. **成功与失败用不同 TTL**。缓存「可用」过久 ⇒ 选到已经死掉的腿（parity 事故的延迟重演）；
   缓存「不可用」过久 ⇒ 刚修好登录的腿长时间无故缺席。代价不对称，TTL 也就不该对称。
2. **读侧任何异常都退化成 miss，绝不退化成「可用」**。文件损坏、outcome 取值不认识、
   身份对不上——一律当没有缓存、重新跑真实 probe。多花一次 probe 是可接受代价，
   把死腿判活不是。
3. **身份变了即失效**。摘要绑 config 内容 + 可执行文件身份 + probe 模式：改了配置或换了
   CLI 版本之后，上一轮的结论不再代表这一轮要跑的东西。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Mapping

from core.rvf_user_local_data_paths import resolved_rvf_user_local_data_paths

PROBE_OUTCOME_AVAILABLE = "available"
PROBE_OUTCOME_UNAVAILABLE = "unavailable"
SUPPORTED_PROBE_OUTCOMES = frozenset({PROBE_OUTCOME_AVAILABLE, PROBE_OUTCOME_UNAVAILABLE})

PROBE_RESULT_CACHE_DISABLED_ENVIRONMENT_VARIABLE = (
    "RVF_REVIEWER_HARNESS_PROBE_RESULT_CACHE_DISABLED"
)
PROBE_RESULT_CACHE_AVAILABLE_TTL_SECONDS_ENVIRONMENT_VARIABLE = (
    "RVF_REVIEWER_HARNESS_PROBE_RESULT_CACHE_AVAILABLE_TTL_SECONDS"
)
PROBE_RESULT_CACHE_UNAVAILABLE_TTL_SECONDS_ENVIRONMENT_VARIABLE = (
    "RVF_REVIEWER_HARNESS_PROBE_RESULT_CACHE_UNAVAILABLE_TTL_SECONDS"
)

# 「可用」默认 30 分钟：够覆盖同一会话里连着跑的几轮 RVF，又短于一次登录过期通常被察觉的时间。
DEFAULT_AVAILABLE_OUTCOME_TTL_SECONDS = 1800.0
# 「不可用」默认 5 分钟：登录/额度这类故障往往几分钟内就被人修好，压长了会让腿凭空缺席。
DEFAULT_UNAVAILABLE_OUTCOME_TTL_SECONDS = 300.0

PROBE_RESULT_CACHE_RELATIVE_DIRECTORY = "reviewer-harness-probe-result-cache"

_HARNESS_ID_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9_.-]")


def _cache_root(
    *,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
) -> Path:
    paths = resolved_rvf_user_local_data_paths(
        environment=environment, home_directory=home_directory
    )
    return paths.runtime_state_root / PROBE_RESULT_CACHE_RELATIVE_DIRECTORY


def resolved_probe_result_cache_entry_path(
    harness_id: str,
    *,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
) -> Path:
    """某条 harness 的缓存条目落点（唯一状态根下，harness id 已做文件名净化）。"""
    safe_harness_id = _HARNESS_ID_SAFE_PATTERN.sub("_", harness_id.strip()) or "_"
    return _cache_root(
        environment=environment, home_directory=home_directory
    ) / f"{safe_harness_id}.json"


def compute_probe_identity_digest(
    *,
    config_path: Path,
    user_local_config_override_path: Path | None,
    executable_path: Path | None,
    probe_mode: str,
) -> str:
    """把「这一轮要探的到底是什么」压成一个摘要：**有效**配置 + 可执行文件身份 + probe 模式。

    「有效」配置是 bundled config **叠加** user-local override 的结果,两份都要进摘要。
    只哈希 bundled 那份不够:override 可以改掉 ``health_command`` / ``health_timeout_seconds``
    / ``pre_run_health`` / ``env_set``——也就是 ``--preflight`` 实际执行的东西。漏掉它时,
    在 TTL 内改 override 仍会命中旧结论,把一条已经失效的腿继续喂给选腿层(或把一条刚修好的
    腿继续挡在门外),而失效方向恰好落在不安全的一侧。

    ``user_local_config_override_path`` 指向的文件**通常不存在**(多数机器上就没有 override)。
    这种情况也必须进摘要——用固定占位而不是跳过——否则「从无到有新建一个 override」这件事
    本身不会让缓存失效,而那正是最需要它失效的时刻。

    可执行文件取 (size, mtime_ns) 而不是内容摘要：CLI 动辄上百 MB（kimi 是 158 MB），
    每轮 probe 前读一遍纯属浪费，而升级必然改动这两个值之一。
    可执行文件不存在时用一个固定占位，保证摘要仍然稳定可比。
    """
    hasher = hashlib.sha256()
    hasher.update(b"probe_mode\0")
    hasher.update(probe_mode.encode("utf-8"))
    hasher.update(b"\0config\0")
    try:
        hasher.update(Path(config_path).read_bytes())
    except OSError:
        hasher.update(b"<config-unreadable>")
    hasher.update(b"\0user_local_config_override\0")
    if user_local_config_override_path is None:
        hasher.update(b"<override-path-unresolved>")
    else:
        try:
            hasher.update(Path(user_local_config_override_path).read_bytes())
        except FileNotFoundError:
            hasher.update(b"<override-absent>")
        except OSError:
            hasher.update(b"<override-unreadable>")
    hasher.update(b"\0executable\0")
    if executable_path is None:
        hasher.update(b"<executable-unresolved>")
    else:
        try:
            stat_result = Path(executable_path).stat()
        except OSError:
            hasher.update(b"<executable-absent>")
        else:
            hasher.update(
                f"{stat_result.st_size}:{stat_result.st_mtime_ns}".encode("utf-8")
            )
    return hasher.hexdigest()


def _cache_is_disabled(environment: Mapping[str, str] | None) -> bool:
    effective = os.environ if environment is None else environment
    return bool(
        effective.get(PROBE_RESULT_CACHE_DISABLED_ENVIRONMENT_VARIABLE, "").strip()
    )


def _configured_ttl_seconds(
    environment: Mapping[str, str] | None,
    variable_name: str,
    fallback_seconds: float,
) -> float:
    effective = os.environ if environment is None else environment
    raw = effective.get(variable_name, "").strip()
    if not raw:
        return fallback_seconds
    try:
        configured = float(raw)
    except ValueError:
        return fallback_seconds
    # inf / -inf / nan 一律当作「读不懂的输入」回退默认。**尤其是 inf**：它等价于
    # 「永不过期的 available 缓存」——一条腿死了也再没有任何时刻会让它重新被 probe，
    # 正是这个模块最不该出现的状态。nan 在旧实现里只是**碰巧**安全（靠 nan > 0
    # 恒假才落进回退分支），靠巧合成立的安全性不能留着等人重构掉。
    if not math.isfinite(configured):
        return fallback_seconds
    # 0 是**有意义的显式取值**，不是非法值：操作者写 0 的意思是「这一轮别复用」。
    # 旧实现把它和 "abc" 一起回退成 1800，方向与意图恰好相反，且落在不安全一侧
    # （继续采信一条陈旧的 available）。
    # 负数同样不是合法 TTL，但写负数的人的意图**绝不是**「缓存更久」，所以夹到 0
    # 而不是回退默认——0 和负数都落在「重新 probe」那一侧，符合本模块的方向性纪律。
    return configured if configured > 0 else 0.0


def resolved_outcome_ttl_seconds(
    outcome: str,
    *,
    environment: Mapping[str, str] | None = None,
    available_ttl_seconds: float | None = None,
    unavailable_ttl_seconds: float | None = None,
) -> float:
    if outcome == PROBE_OUTCOME_AVAILABLE:
        if available_ttl_seconds is not None:
            return available_ttl_seconds
        return _configured_ttl_seconds(
            environment,
            PROBE_RESULT_CACHE_AVAILABLE_TTL_SECONDS_ENVIRONMENT_VARIABLE,
            DEFAULT_AVAILABLE_OUTCOME_TTL_SECONDS,
        )
    if unavailable_ttl_seconds is not None:
        return unavailable_ttl_seconds
    return _configured_ttl_seconds(
        environment,
        PROBE_RESULT_CACHE_UNAVAILABLE_TTL_SECONDS_ENVIRONMENT_VARIABLE,
        DEFAULT_UNAVAILABLE_OUTCOME_TTL_SECONDS,
    )


def write_probe_outcome(
    harness_id: str,
    outcome: str,
    *,
    probe_identity_digest: str,
    now_epoch_seconds: float,
    available_ttl_seconds: float | None = None,
    unavailable_ttl_seconds: float | None = None,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
) -> Path:
    """记录一次**真实** probe 的结论。写失败一律吞掉——缓存不可用不该拖垮派发。"""
    if outcome not in SUPPORTED_PROBE_OUTCOMES:
        raise ValueError(
            f"unsupported probe outcome: {outcome!r}; "
            f"expected one of {sorted(SUPPORTED_PROBE_OUTCOMES)}"
        )
    ttl_seconds = resolved_outcome_ttl_seconds(
        outcome,
        environment=environment,
        available_ttl_seconds=available_ttl_seconds,
        unavailable_ttl_seconds=unavailable_ttl_seconds,
    )
    entry_path = resolved_probe_result_cache_entry_path(
        harness_id, environment=environment, home_directory=home_directory
    )
    payload = {
        "harness_id": harness_id,
        "outcome": outcome,
        "probe_identity_digest": probe_identity_digest,
        "recorded_at_epoch_seconds": now_epoch_seconds,
        "expires_at_epoch_seconds": now_epoch_seconds + ttl_seconds,
        "ttl_seconds": ttl_seconds,
    }
    try:
        entry_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = entry_path.with_name(f".{entry_path.name}.{os.getpid()}.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary_path, entry_path)
    except OSError:
        pass
    return entry_path


def read_cached_probe_outcome(
    harness_id: str,
    *,
    probe_identity_digest: str,
    now_epoch_seconds: float,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
) -> str | None:
    """命中则返回缓存的 outcome，否则 None（= 请跑一次真实 probe）。

    **任何拿不准的情况都返回 None**，绝不返回 ``available``：多跑一次 probe 只是多花钱，
    把死腿判活会让整轮双评审静默退化成单路。
    """
    if _cache_is_disabled(environment):
        return None
    entry_path = resolved_probe_result_cache_entry_path(
        harness_id, environment=environment, home_directory=home_directory
    )
    try:
        payload = json.loads(entry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    outcome = payload.get("outcome")
    if outcome not in SUPPORTED_PROBE_OUTCOMES:
        return None
    if payload.get("probe_identity_digest") != probe_identity_digest:
        return None
    expires_at = payload.get("expires_at_epoch_seconds")
    if not isinstance(expires_at, (int, float)):
        return None
    if now_epoch_seconds > float(expires_at):
        return None
    return outcome


def invalidate_cached_probe_outcome(
    harness_id: str,
    *,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
) -> bool:
    """废掉某条 harness 的缓存，返回是否真的删掉了一条。

    派发阶段这条腿真挂了就必须调它：否则同一条「可用」缓存会在整个 TTL 内把死腿一次次
    喂回选腿层，而每一轮都因为命中缓存而不再真实 probe——错误会自我延续。
    """
    entry_path = resolved_probe_result_cache_entry_path(
        harness_id, environment=environment, home_directory=home_directory
    )
    try:
        entry_path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True
