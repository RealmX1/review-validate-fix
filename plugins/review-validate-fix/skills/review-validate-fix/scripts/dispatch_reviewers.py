#!/usr/bin/env python3
"""RVF santa-method 双 review 的路由 + 派发单一入口。

主 RVF 会话不再自行选择 reviewer harness、拼装 CLI、或把 in-harness subagent 当作
double-review 的一腿。它只：

    1. ``source review-env.sh``
    2. ``python3 dispatch_reviewers.py --execute ...``
    3. 校验每路 ``review-result.json``
    4. 按 ``references/review-merge-policy.md`` 合并

本脚本负责：解析主 harness → probe 可用 harness → 按路由规则 R0–R4 选出
**恰好两路 external_cli reviewer** → 写 ``artifacts/reviewers/reviewer-plan.json`` →
（``--execute`` 时）并行复用 ``run_alternative_reviewer.py`` 执行内核派发两路 external。

路由规则（``route()`` 为纯函数、可单测；选择纯核已抽至
``core/reviewer_harness_selection/``，S7·R15/R20）：

- ``M`` = 主 dispatch harness（本轮 RVF 主会话所在 harness）
- ``A`` = 本机 probe 通过的 harness 集合（``reviewer-registry.json`` 中 enabled 且 probe OK）

| |A| | rule | 选择 |
|-----|------|------|
| ≥3  | R0   | 两腿选择池 = A − M（非主不足两路时 M 也以 external 跑，绝不退回 in-harness）；|
|     |      | **池内无偏序 = 伪随机选二**（seed 落 plan 的 ``reviewer_selection`` 块，可复现）；|
|     |      | 池恰两路时为受迫配对（无随机、保 priority 序） |
| ==2 | R1   | 这两路 external（受迫配对；M∈A 时 M 也以 external 跑） |
| ==1 | R2   | 同 harness 双 external 实例（reviewer_id 后缀 ``-a``/``-b``） |
| ==0 | R3   | 默认 needs_last_resort_fallback（交主会话按 last-resort reference 兜底）；
|     |      | 仅 ``--require-external`` 时 fail |

旧 R4（cursor 必选一腿 + ``cursor_unavailable`` 降级修饰）随「池内无偏序」语义退役
（R15/R20 裁决：cursor 不再是默认必选腿，缺席只是池组成变化而非降级）。
"""
from __future__ import annotations

from contextvars import copy_context
from independent_backup_route import durable_json, guard_normal_mutation, tracked_subprocess_run

import argparse
import hashlib
from datetime import datetime, timezone
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401  — 把 pyroot 加入 sys.path，供 core.* import
from review_start_boundary_gate import require_prepared_run_review_start_boundary
from core.review_start_boundary.review_start_boundary_verification import ReviewStartBoundaryNotVerified
import harness_limit_cooldown
from core.run_ledger.run_ledger import safe_token, start_run
from core.host_adapter.current_agent_session_identity_from_environment import (
    CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES,
)
from rvf_detached_thread import (
    LAUNCH_FAILED,
    LAUNCH_LAUNCHED,
    _iso_now,
    launch_detached,
)
from run_alternative_reviewer import (
    EXTERNAL_REVIEWER_USAGE_LIMIT_EXIT_CODE,
    EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG,
    reviewer_id_from_label,
    safe_artifact_token,
    user_local_alternative_reviewer_config_override_path,
)
from write_review_result import SUPERSEDED_RESULT_ARTIFACTS_DIRNAME
from core.host_adapter.host_transcript_format_detection import (
    HOST_CLAUDE,
    HOST_CODEX,
    detect_transcript_format,
)
from core.reviewer_harness_selection import (
    SELECTION_MODE_SAME_HARNESS_DOUBLE,
    compute_reviewer_harness_selection,
)
from core.reviewer_harness_probe_result_cache import (
    PROBE_OUTCOME_AVAILABLE,
    PROBE_OUTCOME_UNAVAILABLE,
    compute_probe_identity_digest,
    invalidate_cached_probe_outcome,
    read_cached_probe_outcome,
    write_probe_outcome,
)
from core.temporary_disable_window import TemporaryDisableWindow
from core.rvf_user_local_data_paths import resolved_rvf_reviewer_registry_path


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = resolved_rvf_reviewer_registry_path(
    SKILL_DIR / "config" / "reviewer-registry.json"
)
RUN_ALTERNATIVE_REVIEWER = Path(__file__).resolve().parent / "run_alternative_reviewer.py"
PLAN_SCHEMA_VERSION = 1

# 本轮 double-review 的**非完成态终态**：有腿返回合法 ``kind: request``（review 协议规定的
# 待处理终态，既不是故障也不是完成态 review）。给名字而不是散落字面量，便于消费方对齐。
PLAN_STATUS_BLOCKED_ON_REVIEWER_REQUEST = "blocked_on_reviewer_request"
# 该终态的**专用退出码**。既不能是 failed 的 1（那会把「无人出故障、只是等主会话处置」
# 谎报成故障），更不能是 0——0 表示「被包命令干净完成」，会让 detached 幂等锁被
# ``rvf_detached_thread._detached_run_finished_clean`` 判为「已完成的 run」而永久保留，
# 后续按协议重派只能拿到 ``already_running``，本 run 再也产不出完成态 review（见 main()）。
DISPATCH_BLOCKED_ON_REVIEWER_REQUEST_EXIT_CODE = 3
# 真失败腿与待处理 request **并存**时，追加到失败类 reason 末尾的后缀。plan 的 ``reason``
# 只有一个槽位，混合情形必须自报混合（与 usage_limit × no_review_result 的混合 reason 同源）。
REVIEW_REQUEST_PENDING_REASON_SUFFIX = "_with_review_request_pending"

# Detached 派发线程（--detached / --wait-status）：把「agent 前台 Bash 调用 ↔ 整轮
# 派发 wall-clock」解耦，突破 Bash 工具 600s 硬上限。status/log/lock 落在
# <artifacts>/reviewers/ 下，与 reviewer-plan.json / 各 reviewer artifact 同处。
#
# ponytail: awaiting orchestration overhaul —— 这套「主 agent 派后台 reviewer 并等它」
# 的 detached-thread + status.json + in-progress 锁（Fix A）是「RVF 内 reviewer 等待」的
# 手搓实例。与 RVF 外实现期派发的 wait-on 登记（rvf_awaiting_dispatched_agent_marker）同源，
# 都该收敛进待建 orchestration 系统的同一套 dispatch + wait-on 登记：让「RVF 内 reviewer 等待」
# 与「RVF 外实现期派发等待」走同一条 arm/clear/query 路径，Stop 侧只需读一处。
DISPATCH_STATUS_SCHEMA_VERSION = 1
DISPATCH_STATUS_FILENAME = ".dispatch-thread.status.json"
DISPATCH_LOG_FILENAME = ".dispatch-thread.log"
DISPATCH_LOCK_FILENAME = ".dispatch-thread.lock"
# 单次 waiter 阻塞上限：480 < 600s Bash 上限，留余量；agent 循环调用直到 done。
DEFAULT_DISPATCH_MAX_WAIT_SECONDS = 480.0
# 总超时 backstop：比任何单 reviewer 都宽（单 reviewer idle-timeout 300s、无总上限），
# 保证 detached 派发 status.json 终会落终态，又不误杀正当长 review。
DEFAULT_DISPATCH_TOTAL_TIMEOUT_SECONDS = 2700.0

DEFAULT_MAIN_HARNESS = HOST_CODEX  # 与现有 Kanban 兜底一致

# 临时 external reviewer policy（R21 时间盒泛化后的声明式实例）：按用户 2026-07-02
# 指示，Claude Code 在 2026-07-09 结束前不可作为 RVF external reviewer。过期点覆盖
# 美国时区的 2026-07-09 全日，避免当前运行机器为 Asia/Shanghai 时提前失效。
# 已于 2026-07-10T12:00Z 自动过期——按 core/temporary_disable_window.py 的约定，
# 过期声明保留在原地即是「已过期时间盒」的考古记录，同时充当泛化组件的活用例。
TEMPORARY_DISABLE_CLAUDE_CODE_EXTERNAL_REVIEWER_WINDOW = TemporaryDisableWindow(
    disabled_subject=HOST_CLAUDE,
    reason="temporary_disable_claude_code_external_reviewer_until_2026_07_09_us_timezones",
    expires_at_utc=datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc),
    metadata={
        "expires_after_requested_date": "2026-07-09",
        "expiry_timezone_policy": "covers_end_of_2026_07_09_across_us_timezones",
    },
)

# 已知存在并行 headless session/auth 全局锁、需要顺序执行的 harness。
# 由 S3 同 CLI 并行 spike 填充；空集合表示默认全部可并行。
SEQUENTIAL_HARNESSES: frozenset[str] = frozenset()


def temporary_disable_claude_code_external_reviewer_override(
    now_utc: datetime | None = None,
) -> dict[str, str] | None:
    """返回当前是否命中临时 Claude Code external reviewer 禁用策略。

    R21 泛化后委托 :data:`TEMPORARY_DISABLE_CLAUDE_CODE_EXTERNAL_REVIEWER_WINDOW`；
    对外载荷契约保持不变（``disabled_harness`` 键名沿用，供既有 plan/警告消费者）。
    """
    override = TEMPORARY_DISABLE_CLAUDE_CODE_EXTERNAL_REVIEWER_WINDOW.active_override(now_utc)
    if override is None:
        return None
    override["disabled_harness"] = override.pop("disabled_subject")
    return override


def _temporarily_disabled_external_reviewer_harnesses(
    now_utc: datetime | None = None,
) -> dict[str, dict[str, str]]:
    override = temporary_disable_claude_code_external_reviewer_override(now_utc)
    if override is None:
        return {}
    return {HOST_CLAUDE: override}


def load_registry(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("harnesses"), dict):
        raise ValueError(f"invalid reviewer registry: {path}")
    return payload


def _enabled_harnesses(registry: dict[str, Any]) -> list[str]:
    harnesses = registry.get("harnesses", {})
    return [hid for hid, spec in harnesses.items() if spec.get("enabled") is True]


def _order_by_priority(registry: dict[str, Any], harness_ids: list[str]) -> list[str]:
    harnesses = registry.get("harnesses", {})

    def sort_key(hid: str) -> tuple[int, str]:
        priority = harnesses.get(hid, {}).get("priority_default", 0)
        return (-int(priority), hid)

    return sorted(harness_ids, key=sort_key)


def resolve_main_harness(
    *,
    cli_main_harness: str | None,
    env_main_harness: str | None,
    main_harness_file: Path | None,
    transcript: Path | None,
) -> str:
    """主 harness 解析优先级：CLI > env > main-harness.json > transcript 探测 > 默认 codex。

    cursor 永远不会被 transcript 探测命中（``detect_transcript_format`` 只返回
    codex/claude_code/None），只能经显式 CLI/env 覆盖到达。
    """
    if cli_main_harness and cli_main_harness != "auto":
        return cli_main_harness
    if env_main_harness:
        return env_main_harness
    if main_harness_file is not None and main_harness_file.exists():
        try:
            payload = json.loads(main_harness_file.read_text(encoding="utf-8"))
            value = payload.get("main_harness")
            if isinstance(value, str) and value:
                return value
        except (json.JSONDecodeError, ValueError, OSError):
            pass
    if transcript is not None:
        detected = detect_transcript_format(transcript)
        if detected:
            return detected
    return DEFAULT_MAIN_HARNESS


def _reviewer_spec(
    registry: dict[str, Any],
    harness_id: str,
    *,
    slot: int,
    label_suffix: str = "",
    reviewer_id_suffix: str = "",
) -> dict[str, Any]:
    spec = registry["harnesses"][harness_id]
    label_prefix = spec["label_prefix"]
    base_id = reviewer_id_from_label(label_prefix)
    label = f"{label_prefix}#{label_suffix}" if label_suffix else label_prefix
    reviewer_id = f"{base_id}-{reviewer_id_suffix}" if reviewer_id_suffix else base_id
    config_path = SKILL_DIR / spec["config_path"]
    return {
        "slot": slot,
        "harness_id": harness_id,
        "dispatch_mode": spec.get("dispatch_mode", "external_cli"),
        "label": label,
        "config_path": str(config_path),
        "reviewer_id": safe_artifact_token(reviewer_id),
    }


def _warn(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _quota_pool_identity_by_harness(registry: dict[str, Any]) -> dict[str, str]:
    """registry → {harness_id: 额度池标识}，未声明者不入 mapping（视为各自独立）。

    出厂 registry 每条都必须声明，漏填由 check_skill_contracts 的契约测试抓；
    这里对缺失保持宽松，是为了不让一条刚加进来的腿把选腿逼进降级。
    """
    harnesses = registry.get("harnesses", {})
    identities: dict[str, str] = {}
    for harness_id, spec in harnesses.items():
        if not isinstance(spec, dict):
            continue
        declared = str(
            spec.get("underlying_provider_account_quota_pool_identity", "")
        ).strip()
        if declared:
            identities[harness_id] = declared
    return identities


def route(
    main_harness: str,
    available: list[str],
    registry: dict[str, Any],
    *,
    require_external_only: bool = False,
    sequential_harnesses: frozenset[str] = SEQUENTIAL_HARNESSES,
    now_utc: datetime | None = None,
    selection_seed: int | None = None,
) -> dict[str, Any]:
    """纯路由函数：给定主 harness + 可用集 + registry，返回 plan dict（无 IO）。

    返回 dict 含 routing_rule / reviewers / reviewer_selection / warnings /
    needs_last_resort_fallback / sequential_execution / status。``available`` 会先
    过滤到 registry 中 enabled 的 harness 并按 priority 排序。R0–R3 分类与两腿选材
    （含池内无偏序伪随机选二）单源于
    :func:`core.reviewer_harness_selection.compute_reviewer_harness_selection`；
    ``selection_seed`` 缺省时由纯核现场生成并记录进 ``reviewer_selection`` 块
    （同 seed 严格可复现）。
    """
    enabled = set(_enabled_harnesses(registry))
    temporarily_disabled = _temporarily_disabled_external_reviewer_harnesses(now_utc)
    excluded_by_temporary_policy = [
        h for h in available if h in temporarily_disabled
    ]
    A = _order_by_priority(
        registry,
        [
            h
            for h in available
            if h in enabled and h not in temporarily_disabled
        ],
    )
    warnings: list[dict[str, str]] = []

    plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "main_harness": main_harness,
        "available_harnesses": A,
        "routing_rule": None,
        "reviewers": [],
        "warnings": warnings,
        "needs_last_resort_fallback": False,
        "sequential_execution": False,
        # 额度耗尽轮内 reroute 记录：每项 {slot, from, from_reviewer_id, to, to_reviewer_id, round}。
        # additive 字段，schema_version 仍为 1（既有消费者忽略未知 key）。
        "fallbacks": [],
        "status": "planned",
    }
    if temporarily_disabled:
        plan["external_reviewer_selection_override"] = {
            "active": True,
            "disabled_harnesses": sorted(temporarily_disabled),
            "excluded_available_harnesses": sorted(set(excluded_by_temporary_policy)),
            "policy": next(iter(temporarily_disabled.values())),
        }
    if temporarily_disabled:
        warnings.append(
            _warn(
                "claude_code_external_reviewer_temporarily_disabled",
                "warning",
                "Claude Code external reviewer is temporarily disabled; "
                "routing will exclude claude_code and use the remaining external reviewers.",
            )
        )

    # R0–R3 分类 + 两腿选材单源于 core 纯核（S7·R15/R20：Tier-1 池 − 主 harness、
    # 池内无偏序伪随机选二、R1–R3 降级维持既有语义）。旧 R4 cursor 必选腿修饰随之退役。
    selection = compute_reviewer_harness_selection(
        main_harness=main_harness,
        available_harnesses=A,
        quota_pool_identity_by_harness=_quota_pool_identity_by_harness(registry),
        selection_seed=selection_seed,
    )
    plan["routing_rule"] = selection.routing_rule
    # 选材遥测块（seed 落此处，可复现；schema additive，既有消费者忽略未知 key）。
    plan["reviewer_selection"] = selection.telemetry_payload()
    if selection.shared_quota_pool_collision_accepted:
        warnings.append(
            _warn(
                "reviewer_legs_share_quota_pool",
                "warning",
                "本轮两腿共用同一额度池（同一底层模型 + 同一订阅账号）："
                f"{', '.join(selection.selected_harnesses)}。"
                "双评审的模型分歧因此消失，且该订阅额度耗尽会同时打掉两腿。"
                "这是可用集里已无异池组合时的降级——减少腿数会更糟，故仍照常派发。",
            )
        )

    # R3 — 零可用
    if not selection.selected_harnesses:
        if require_external_only:
            plan["status"] = "failed"
            plan["reason"] = "no_reviewer_harness_available"
            warnings.append(
                _warn(
                    "no_external_reviewer_available",
                    "error",
                    "本机无可用 external reviewer harness，且本轮要求必须 external（--require-external）。",
                )
            )
        else:
            plan["needs_last_resort_fallback"] = True
            warnings.append(
                _warn(
                    "no_external_reviewer_available",
                    "warning",
                    "本机无可用 external reviewer harness；交主会话按 last-resort "
                    "in-harness fallback reference 执行兜底双 review。",
                )
            )
        return plan

    # R2 — 仅一个可用：同 harness 双 external 实例
    if selection.selection_mode == SELECTION_MODE_SAME_HARNESS_DOUBLE:
        only = selection.selected_harnesses[0]
        if not selection.only_available_is_main:
            warnings.append(
                _warn(
                    "available_reviewer_harness_mismatch",
                    "warning",
                    f"唯一可用 reviewer harness 是 {only}，与主 harness {main_harness} 不同；"
                    "已派发同 harness 双 external 实例，建议把主 dispatch harness 也配置为可用 external reviewer。",
                )
            )
        else:
            warnings.append(
                _warn(
                    "only_main_harness_available",
                    "info",
                    f"仅主 harness {only} 可用；已派发同 harness 双 external 实例。",
                )
            )
        plan["reviewers"] = [
            _reviewer_spec(registry, only, slot=1, label_suffix="a", reviewer_id_suffix="a"),
            _reviewer_spec(registry, only, slot=2, label_suffix="b", reviewer_id_suffix="b"),
        ]
        if only in sequential_harnesses:
            plan["sequential_execution"] = True
            warnings.append(
                _warn(
                    "collision_risk",
                    "warning",
                    f"{only} 同 harness 并行 headless 存在 session/auth 冲突风险；已改为顺序执行（仍产出两份独立 artifact）。",
                )
            )
        return plan

    # R0/R1 — 受迫配对或伪随机选二（纯核已定 slot 顺序）
    plan["reviewers"] = [
        _reviewer_spec(registry, hid, slot=index + 1)
        for index, hid in enumerate(selection.selected_harnesses)
    ]
    return plan


def probe_available(
    registry: dict[str, Any],
    *,
    probe_mode: str = "preflight",
    timeout: float = 60.0,
    assume_available: list[str] | None = None,
    cooldown_active: set[str] | None = None,
    now_utc: datetime | None = None,
) -> list[str]:
    """对每个 enabled harness 跑 ``run_alternative_reviewer.py --config <path> --<probe_mode>``。

    ``assume_available`` 给定时跳过真实 probe（用于 --plan-only 测试 / 确定性路由），
    只取其与 enabled 的交集；此路径**不**应用 cooldown（D-O4：assume 仅测试用，须走真实 probe
    才会被额度冷却排除）。

    ``cooldown_active`` 给定时，处于额度耗尽冷却期的 harness **连 probe 都跳过**（额度耗尽时
    ``codex login status`` 之类 auth probe 仍返回 0，单看 auth 发现不了，故需独立额度信号）。
    """
    enabled = _enabled_harnesses(registry)
    temporarily_disabled = _temporarily_disabled_external_reviewer_harnesses(now_utc)
    if assume_available is not None:
        return [
            h
            for h in assume_available
            if h in enabled and h not in temporarily_disabled
        ]
    cooled = cooldown_active or set()
    available: list[str] = []
    for harness_id in enabled:
        if harness_id in cooled or harness_id in temporarily_disabled:
            continue
        spec = registry["harnesses"][harness_id]
        config_path = SKILL_DIR / spec["config_path"]

        # 第一道：零成本否定。可执行文件都不在 PATH 上，真跑 probe 必然失败，没必要为
        # 一个注定的结论付一次模型调用。**这个方向是安全的**——「不在 PATH」只能证否、
        # 不能用来证真，所以它不构成 health parity gate 要防的那种廉价替代判据。
        executable_path, override_path, dynamic_model_requires_fresh_probe = _resolved_probe_execution_inputs(config_path)
        if executable_path is None:
            continue

        # 第二道：复用一次**真实** probe 的结论。摘要绑 config 内容 + 可执行文件身份 +
        # probe 模式，任一变化即视为未命中。
        probe_identity_digest = compute_probe_identity_digest(
            config_path=config_path,
            user_local_config_override_path=override_path,
            executable_path=executable_path,
            probe_mode=probe_mode,
        )
        now_epoch_seconds = time.time()
        # shortcut: 动态默认选模每次真探测，缓存身份能绑定实际被探测的默认模型后再恢复复用。
        cached_outcome = (
            None
            if dynamic_model_requires_fresh_probe
            else read_cached_probe_outcome(
                harness_id,
                probe_identity_digest=probe_identity_digest,
                now_epoch_seconds=now_epoch_seconds,
            )
        )
        if cached_outcome == PROBE_OUTCOME_AVAILABLE:
            available.append(harness_id)
            continue
        if cached_outcome == PROBE_OUTCOME_UNAVAILABLE:
            continue

        cmd = [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config_path),
            f"--{probe_mode}",
        ]
        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            # 这里兜的**只是外层 probe 进程**级的失败，不要把它读成「health 超时不写缓存」。
            # 两种超时归属不同，处理也必须不同：
            #
            # - 腿**自己**的 health 超时（config 的 health_timeout_seconds）根本走不到这个
            #   except。各腿的预算一律小于 --probe-timeout（30/45/45/50 < 60，config 注释里
            #   写死了这条约束），所以慢腿——omp 这种 thinking xhigh 冷启动——的主超时路径是
            #   内层先触发：run_health 捕获 TimeoutExpired 后经 fail() 返回非 0，从这里看就是
            #   一个普通的 returncode != 0，随后被写成 unavailable。**这是刻意的**：一条腿在
            #   它自己声明的预算内没响应，本身就是关于这条腿的信息，够格记下来；至于它多半是
            #   瞬时的，由 unavailable 那条短 TTL（默认 300s，只有 available 的六分之一）兜住，
            #   下一轮很快就会重新真跑。
            # - 走到这个 except 的只剩两种：连内层的超时预算都没兑现（进程卡在更外层），以及
            #   进程压根没起来。那是 probe 机制本身失效，**不构成关于这条腿的任何信息**——
            #   记 available 会把死腿判活（health probe parity 事故的形状），记 unavailable
            #   会让好腿凭空缺席，所以两侧都不记，下一轮老实重跑。
            continue
        probe_outcome = (
            PROBE_OUTCOME_AVAILABLE
            if completed.returncode == 0
            else PROBE_OUTCOME_UNAVAILABLE
        )
        if not dynamic_model_requires_fresh_probe:
            write_probe_outcome(
                harness_id,
                probe_outcome,
                probe_identity_digest=probe_identity_digest,
                now_epoch_seconds=now_epoch_seconds,
            )
        if probe_outcome == PROBE_OUTCOME_AVAILABLE:
            available.append(harness_id)
    return available


def _resolved_probe_execution_inputs(
    config_path: Path,
) -> tuple[Path | None, Path | None, bool]:
    """解析这一轮 probe 真正依赖的两个外部输入：可执行文件，以及 user-local override 文件。

    返回 ``(executable_path, user_local_config_override_path, dynamic_model_requires_fresh_probe)``。

    ``executable_path`` 解析不出（config 读不动 / 没有 command / 命令不在 PATH）即代表这条腿
    不可用——这是**证否**用途，绝不用它来判「可用」。

    ``user_local_config_override_path`` 是**摘要输入**，不是可用性判据：``--preflight`` 跑的是
    bundled config 叠加 override 之后的有效配置，而 override 能改掉 ``health_command`` 等
    真正被执行的字段。它指向的文件多数机器上并不存在，此处仍如实返回路径，由摘要把「当前
    有没有 override、内容是什么」一并纳入——否则新建或改动 override 不会让缓存失效。
    解析不出 label（因而推不出 override 路径）时返回 None，摘要会用固定占位表示这一状态。
    """
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None, False
    command = payload.get("command")
    if not isinstance(command, list) or not command:
        return None, None, False
    executable_name = str(command[0]).strip()
    if not executable_name:
        return None, None, False
    resolved = shutil.which(executable_name)
    if not resolved:
        return None, None, False

    override_path: Path | None = None
    reviewer_label = payload.get("label")
    if isinstance(reviewer_label, str) and reviewer_label.startswith("alternative-reviewer:"):
        try:
            override_path = user_local_alternative_reviewer_config_override_path(reviewer_label)
        except (OSError, ValueError):
            override_path = None
    return Path(resolved), override_path, payload.get("reviewer_model_selection_source") is not None


def invalidate_probe_result_cache_for_failed_dispatch(harness_id: str) -> bool:
    """派发阶段这条腿真挂了 ⇒ 立刻废掉它的缓存结论。

    不做这一步的话，一条「可用」缓存会在整个 TTL 内把死腿一次次喂回选腿层，而每一轮都
    因为命中缓存而不再真实 probe——错误会自我延续，正好是缓存最危险的失效形状。
    """
    return invalidate_cached_probe_outcome(harness_id)


def _build_reviewer_command(
    reviewer: dict[str, Any],
    *,
    repo: str | None,
    review_packet: str | None,
    session_context: str | None,
    scope_contract: str | None,
    run_id: str,
    run_dir: str,
) -> list[str]:
    cmd = [
        sys.executable,
        str(RUN_ALTERNATIVE_REVIEWER),
        "--config",
        reviewer["config_path"],
        "--rvf-run-id",
        run_id,
        "--rvf-run-dir",
        run_dir,
        "--reviewer-id",
        reviewer["reviewer_id"],
    ]
    if repo:
        cmd += ["--repo", repo]
    if review_packet:
        cmd += ["--review-packet", review_packet]
    if session_context:
        cmd += ["--session-context", session_context]
    if scope_contract:
        cmd += ["--scope-contract", scope_contract]
    return cmd


def _newest_artifact(reviewer_dir: Path, pattern: str) -> Path | None:
    try:
        candidates = [p for p in reviewer_dir.glob(pattern) if p.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _read_reviewer_summary(reviewer_dir: Path) -> dict[str, Any] | None:
    """读子 reviewer 的 ``reviewer.summary*.json``（unique 命名，取 mtime 最新一份）。"""
    path = _newest_artifact(reviewer_dir, "reviewer.summary*.json")
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_reviewer_stderr(reviewer_dir: Path) -> str | None:
    path = _newest_artifact(reviewer_dir, "reviewer.stderr*.txt")
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _leg_usage_limit(reviewer: dict[str, Any], artifacts_dir: Path) -> tuple[bool, str | None]:
    """D3 双条件：returncode==125 且（summary.output_error_reason==usage_limit_exhausted
    或 stderr 含 flag）。返回 (是否额度耗尽, 错误文案 snippet 供解析 reset hint)。

    只看裸 125 会误判（exit code 可能来自非本脚本的命令）；故要求 artifact 佐证，
    对齐 timeout 的「124 AND flag」先例。
    """
    if reviewer.get("returncode") != EXTERNAL_REVIEWER_USAGE_LIMIT_EXIT_CODE:
        return False, None
    reviewer_dir = artifacts_dir / "reviewers" / reviewer["reviewer_id"]
    summary = _read_reviewer_summary(reviewer_dir)
    if isinstance(summary, dict) and summary.get("output_error_reason") == "usage_limit_exhausted":
        return True, summary.get("output_error_message")
    stderr_text = _read_reviewer_stderr(reviewer_dir)
    if stderr_text and EXTERNAL_REVIEWER_USAGE_LIMIT_FLAG in stderr_text:
        return True, stderr_text
    return False, None


def _leg_produced_review_result(reviewer: dict[str, Any], artifacts_dir: Path) -> bool:
    """这条腿是否真的产出了合法评审结论——double-review 契约的唯一真判据。

    比 ``returncode == 0`` 严格：退出码只说进程怎么退出，不说有没有可用结论。2026-08-11 的
    run rvf-20260811T170157Z 上，cursor 腿 rc=1、目录里根本没有 review-result.json，plan 却
    只按 rc 计成 ``partial`` 就收工——主会话拿一份结论当双评审用（详见 ``execute_plan``）。

    判据优先取 ``reviewer.summary.json`` 的 ``review_result_complete``（跨 harness 通用，由
    run_alternative_reviewer.py 按 review-result 的 ``kind`` ∈ {no_issues, issues} 写入）；
    summary 缺失/不可解析时回退到直接读 review-result.json 自身的 kind，避免因 artifact
    形态变化而误判成「有结论」。
    """
    reviewer_dir = artifacts_dir / "reviewers" / reviewer["reviewer_id"]
    summary = _read_reviewer_summary(reviewer_dir)
    if isinstance(summary, dict) and "review_result_complete" in summary:
        return summary["review_result_complete"] is True
    result_path = reviewer.get("review_result_path")
    path = Path(result_path) if result_path else reviewer_dir / "review-result.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("kind") in {"no_issues", "issues"}


def _leg_is_effective(reviewer: dict[str, Any], artifacts_dir: Path) -> bool:
    """腿有效 = 干净退出 **且** 产出了合法评审结论（两者缺一不可）。"""
    return reviewer.get("returncode") == 0 and _leg_produced_review_result(reviewer, artifacts_dir)


def _leg_review_request_pending(reviewer: dict[str, Any], artifacts_dir: Path) -> bool:
    """这条腿是否写出了合法的 ``kind: request``（非完成态、但也**不是**失败）。

    request 是 review 协议的合法待处理终态：reviewer 要标准子集/命令锁/测量结果/上下文，
    主会话应按 ``references/review-merge-policy.md`` 的 Contract Gate + Request Handling
    满足、驳回或 spawn 子任务后**让原 reviewer 重试**。runner 对它以 rc=0 收尾，并在
    ``reviewer.summary.json`` 写 ``review_request_pending: true``。

    必须与「没产出结论的失败腿」分开判：否则 request 会被当成 ``no_review_result`` 换腿，
    替换 leg 一成功 plan 就变 ``completed``，那条 request 再也不会被主会话按 Contract Gate
    处理——reviewer 明确说「我还没审完、缺东西」却被静默丢弃。
    """
    if reviewer.get("returncode") != 0:
        return False
    reviewer_dir = artifacts_dir / "reviewers" / reviewer["reviewer_id"]
    summary = _read_reviewer_summary(reviewer_dir)
    if isinstance(summary, dict) and "review_request_pending" in summary:
        return summary["review_request_pending"] is True
    result_path = reviewer.get("review_result_path")
    path = Path(result_path) if result_path else reviewer_dir / "review-result.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("kind") == "request"


def _dedupe_reviewer_id(reviewer_id: str, used_ids: set[str]) -> str:
    """bounded 去重：替换 leg id 与已占用 id 碰撞时追加序号（多轮 reroute 也不撞）。"""
    if reviewer_id not in used_ids:
        return reviewer_id
    for index in range(2, 100):
        candidate = f"{reviewer_id}-{index}"
        if candidate not in used_ids:
            return candidate
    return f"{reviewer_id}-{secrets.token_hex(2)}"


def _reroute_candidates(
    main_harness: str | None,
    available: list[str],
    cooled: set[str],
    registry: dict[str, Any],
    *,
    now_utc: datetime | None = None,
    already_failed: set[str] | None = None,
) -> list[str]:
    """对 ``A' = available − cooled − already_failed`` 重调 ``route()`` 取替换候选顺序（R-c 单源）。

    候选 = route 在 A' 上的两腿选择（已含 R0–R3 的池内无偏序选二 / main-as-external /
    R2 双实例语义；选择池 >2 时该两腿为伪随机），再按 priority 补齐 A' 其余，保证
    「需补两腿但 route 只给两腿」时仍有后备。当前 reroute 只消费 sub-plan 的 harness
    顺序，未持久化其 reviewer_selection/seed；池扩到 >2 后若要求逐次复现，须先补该遥测。

    ``already_failed`` 是本轮内已经失败过的 harness。它与 ``cooled`` 分开传是有意的：
    cooldown 是**跨 run 持久化**的额度信号，只该由额度耗尽写入；而 auth 失效之类的失败
    只在本轮成立，不配把 harness 禁到后续 run 去。但两者都必须排除出本轮的替换候选——
    否则刚失败的 harness 会被立刻重新选中，reroute 空转。
    """
    a_prime = [
        h
        for h in available
        if h not in cooled and h not in (already_failed or set())
    ]
    candidates: list[str] = []
    if main_harness is not None and a_prime:
        sub_plan = route(main_harness, a_prime, registry, now_utc=now_utc)
        for reviewer in sub_plan.get("reviewers", []):
            hid = reviewer.get("harness_id")
            if hid and hid not in candidates:
                candidates.append(hid)
    for hid in _order_by_priority(registry, a_prime):
        if hid not in candidates:
            candidates.append(hid)
    return candidates


def _resume_plan_for_review_request_legs(
    reviewers_dir: Path, artifacts_dir: Path
) -> tuple[dict[str, Any], list[int]]:
    """载入上一轮 plan，挑出停在 ``kind: request`` 的腿。**只读**：不动磁盘上任何工件。

    为什么不重新 ``route()``：重派必须用**同一对** reviewer（同 selection_seed、同 reviewer_id），
    否则「处置完 request 让原 reviewer 重试」就变成了换人重审，merge policy 的双腿身份对不上。

    为什么这里**不**做工件轮转（2026-08-15 RVF-008）：本函数在 ``--dry-run`` 短路**之前**被调用，
    ``--dry-run`` 自己的契约是 “Print plan JSON to stdout; do not write or execute”。曾经在这里
    ``os.replace`` 掉 canonical ``review-result.json``，于是一次纯预演就把当前结果搬走了——
    ``--plan-only`` 同理。轮转推迟到真正 execute 的那一步（``_rotate_review_request_leg_artifacts``）。

    fail-closed：plan 缺失 / 不可读 / 无 reviewers 一律抛，绝不退回「整盘重派」——那正是要修的
    行为，静默降级回去等于没修。
    """
    plan_path = reviewers_dir / "reviewer-plan.json"
    if not plan_path.is_file():
        raise ValueError(
            f"--redispatch-review-request-legs-only 需要上一轮的 {plan_path}，但它不存在。"
        )
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"上一轮 reviewer-plan.json 不可读：{exc}") from exc
    if not isinstance(plan, dict) or not isinstance(plan.get("reviewers"), list) or not plan["reviewers"]:
        raise ValueError(f"上一轮 reviewer-plan.json 里没有 reviewers：{plan_path}")

    indices = [
        index
        for index, reviewer in enumerate(plan["reviewers"])
        if _leg_review_request_pending(reviewer, artifacts_dir)
    ]

    plan.setdefault("warnings", [])
    plan["warnings"].append(
        _warn(
            "redispatching_review_request_legs_only",
            "warning",
            "只重派停在 kind: request 的腿（"
            + ", ".join(plan["reviewers"][i]["reviewer_id"] for i in indices)
            + "）；健康且已完成的腿保留上一轮结果，不重跑、不覆写。",
        )
    )
    # 上一轮的终态标记必须清掉，否则本轮无论结果如何都还挂着 blocked/failed。
    plan["status"] = "planned"
    plan.pop("reason", None)
    return plan, indices


def _rotate_review_request_leg_artifacts(
    plan: dict[str, Any], indices: list[int], artifacts_dir: Path
) -> list[str]:
    """把待重派的 request 腿的 canonical ``review-result.json`` 轮转进归档目录。

    **只在真正 execute 前调用**（RVF-008）：这是本流程唯一有破坏性的一步，``--dry-run`` /
    ``--plan-only`` 都不得触发它。

    为什么重派前必须轮转：不轮转的话，重跑那条 request 腿会把新的 request 追加在旧的后面
    （writer 对同 kind 是原地追加），跨轮累积成一份越滚越大的 request 列表；而换成 issues 时
    ``write_review_result.ensure_supersession_allowed`` 只放行 ``request → issues``、
    ``issues`` 一旦落盘就是终态。轮转 = 每轮从**没有 canonical 文件**的干净起点开始，
    上一轮内容仍可在归档里查。
    """
    rotated: list[str] = []
    for index in indices:
        reviewer_id = plan["reviewers"][index]["reviewer_id"]
        result_path = artifacts_dir / "reviewers" / reviewer_id / "review-result.json"
        if not result_path.is_file():
            continue
        archive_dir = result_path.parent / SUPERSEDED_RESULT_ARTIFACTS_DIRNAME
        archive_dir.mkdir(parents=True, exist_ok=True)
        sequence = 1 + sum(1 for _ in archive_dir.glob("review-result.*.json"))
        archive_path = archive_dir / f"review-result.{sequence:02d}.request.json"
        os.replace(result_path, archive_path)
        rotated.append(str(archive_path))
    return rotated


def _backup_route_dispatch_repository(arguments):
    # 原有开审资格核验先于接管登记；非仓库或未开审输入仍按原契约拒绝。
    supplemental_qualification = arguments.get("supplemental_review_qualification_path")
    if supplemental_qualification is not None:
        from supplemental_land_completion import require_supplemental_review_dispatch_qualification
        require_supplemental_review_dispatch_qualification(
            supplemental_qualification, review_packet_path=arguments.get("review_packet"),
            **{name: arguments.get(name) for name in ("repo", "scope_contract", "run_id", "run_dir")},
        )
    else:
        require_prepared_run_review_start_boundary(
            **{name: arguments.get(name) for name in ("repo", "scope_contract", "run_id", "run_dir")}
        )
    return arguments.get("repo")


@guard_normal_mutation(_backup_route_dispatch_repository, "dispatch-reviewers", "run_dir")
def execute_plan(
    plan: dict[str, Any],
    *,
    repo: str | None,
    review_packet: str | None,
    session_context: str | None,
    scope_contract: str | None,
    run_id: str,
    run_dir: str,
    artifacts_dir: Path,
    registry: dict[str, Any] | None = None,
    main_harness: str | None = None,
    available: list[str] | None = None,
    max_fallback_rounds: int = 2,
    initial_dispatch_indices: list[int] | None = None,
    reviewer_command_output_directory: Path | None = None,
    supplemental_review_qualification_path: Path | None = None,
) -> dict[str, Any]:
    """派发 plan 的两路 external reviewer，回填 returncode / result path / status。

    **任何**没产出合法评审结论的腿都触发**轮内 reroute**：对
    ``A' = available − cooled − 本轮已失败`` 重调 ``route()`` 取替换 → in-place 替换失败 slot
    （保持恰好两腿、保留成功 leg 的 artifact）→ 派发替换 leg；替换也失败则 bounded 再来一轮。
    补不上则 fail-close（F1/F2/R-e）。``registry``/``main_harness``/``available`` 缺省（None）
    时退化为「无 reroute」，仅按有效腿数计 status（兼容旧调用方）。

    失败分两类，处置不同：

    - **额度耗尽**（returncode 125 + 佐证）：额外记 cooldown（跨 run 持久化的额度信号），
      fail-close 时沿用既有 ``*_usage_limit_*`` reason 码。
    - **其它任何未产出结论**（auth 失效、启动失败、崩溃、写不出 review-result …）：只在本轮
      排除该 harness，**不**写 cooldown——本轮的环境故障不该把 harness 禁到后续 run。

    第二类原先完全没有处置：2026-08-11 的 run rvf-20260811T170157Z 上 cursor 登录过期，那腿
    rc=1、无 review-result，plan 只按 rc 计成 ``partial`` 就收工，既不补位也不报错，主会话最终
    拿单路结论当双评审用。缺一条合法腿即非法 double-review，与额度耗尽同等对待。

    两类失败可以**同时**发生，因此 fail-close 分支不是互斥的：各自 warning 都会冒出来，
    ``reason`` 在混合时用 ``reviewer_legs_failed_mixed_usage_limit_and_no_review_result``；
    ``all_reviewers_usage_limit_exhausted`` 只在所有失败腿都是额度类时才发（否则主会话会
    照「等配额恢复」处置，把同时需要修的 auth/崩溃故障藏住）。

    **合法 ``kind: request`` 不是失败腿**：runner 以 rc=0 收尾并记 ``review_request_pending``。
    它既不换腿也不排除 harness，而是作为待处理终态冒泡——无真失败腿时 plan 记
    ``status: blocked_on_reviewer_request`` + ``reason: reviewer_review_request_pending``，
    并在 ``review_request_pending_reviewer_ids`` 列出腿。否则替换 leg 一成功 plan 就变
    ``completed``，主会话再也不会按 Contract Gate 满足/驳回该 request。

    request 腿同样**不算**「额度失败腿」：``all_reviewers_usage_limit_exhausted`` 的「全部」
    要求所有腿都撞额度，夹着一条 request 时该断言为假。真失败腿与 request 并存时 fail-close
    仍优先（``status: failed``），但 reason 追加 ``_with_review_request_pending`` 后缀自报混合，
    否则主会话只会去等配额/修 auth，那条 request 从头到尾没进过 Contract Gate。
    """
    reviewers = plan.get("reviewers", [])
    cooled: set[str] = set()
    # 本轮内失败过的 harness（含额度与非额度）——只用于排除替换候选，不落 cooldown。
    failed_harnesses: set[str] = set()
    cooldown_recorded: list[str] = []
    used_ids: set[str] = {r["reviewer_id"] for r in reviewers}
    available = available or []

    def _dispatch_indices(indices: list[int]) -> None:
        """派发给定下标的 reviewer（尊重 sequential_execution），回填 returncode/result path。"""
        local_results: dict[int, int] = {}

        def _run(index: int) -> None:
            reviewer = reviewers[index]
            cmd = _build_reviewer_command(
                reviewer,
                repo=repo,
                review_packet=review_packet,
                session_context=session_context,
                scope_contract=scope_contract,
                run_id=run_id,
                run_dir=run_dir,
            )
            try:
                if reviewer_command_output_directory is None:
                    completed = tracked_subprocess_run(cmd)
                else:
                    reviewer_command_output_directory.mkdir(parents=True, exist_ok=True)
                    output_stem = reviewer_command_output_directory / reviewer["reviewer_id"]
                    # 嵌入机器 JSON 接口时保留各腿原始输出，避免子进程 stdout 混入调用方协议。
                    with Path(str(output_stem) + ".stdout.log").open("ab") as stdout_log, Path(str(output_stem) + ".stderr.log").open("ab") as stderr_log:
                        completed = tracked_subprocess_run(cmd, stdout=stdout_log, stderr=stderr_log)
                    receipt = {"schema": "rvf-reviewer-command-execution-result", "schema_version": 1,
                               "run_id": run_id, "repo": repo, "reviewer_id": reviewer["reviewer_id"],
                               "packet_sha256": hashlib.sha256(Path(review_packet).read_bytes()).hexdigest(),
                               "returncode": completed.returncode, "completed_at": datetime.now(timezone.utc).isoformat()}
                    receipt_bytes = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                    durable_json(reviewer_command_output_directory / "execution-results" / (hashlib.sha256(receipt_bytes).hexdigest() + ".json"), receipt)
                    durable_json(Path(str(output_stem) + ".execution-result.json"), receipt)
                local_results[index] = completed.returncode
            except OSError as exc:  # pragma: no cover - 启动失败
                local_results[index] = 1
                reviewer["error"] = f"{type(exc).__name__}: {exc}"

        if plan.get("sequential_execution"):
            for index in indices:
                _run(index)
        else:
            threads = [threading.Thread(target=copy_context().run, args=(_run, index)) for index in indices]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        for index in indices:
            reviewer = reviewers[index]
            reviewer["returncode"] = local_results.get(index, 1)
            reviewer["review_result_path"] = str(
                artifacts_dir / "reviewers" / reviewer["reviewer_id"] / "review-result.json"
            )

    # 初派：默认全部两腿；``initial_dispatch_indices`` 在场时只派指定的那几腿。
    # 后者是「只重派 kind: request 的腿」用的——整盘重派会把健康且已完成的那腿也重跑一遍，
    # 它的结果被新一轮覆写丢弃（实测一条 cursor 腿跑了三轮 2/4/3 条 issue，只留最后一轮）。
    _dispatch_indices(
        list(range(len(reviewers)))
        if initial_dispatch_indices is None
        else list(initial_dispatch_indices)
    )

    # 反应式 reroute：bounded 轮次，每轮替换本轮新失败（额度 or 未产出结论）的 slot。
    can_reroute = registry is not None and main_harness is not None
    for _round in range(max_fallback_rounds):
        failed_slots: list[tuple[int, str | None]] = []
        usage_limited_slots: list[tuple[int, str | None]] = []
        for index, reviewer in enumerate(reviewers):
            hit, err = _leg_usage_limit(reviewer, artifacts_dir)
            if hit:
                failed_slots.append((index, err))
                usage_limited_slots.append((index, err))
            elif _leg_review_request_pending(reviewer, artifacts_dir):
                # 合法 request：非完成态但也非失败 → 既不换腿也不排除该 harness，
                # 原样保留待主会话按 Contract Gate 处理后让**同一** reviewer 重试。
                continue
            elif not _leg_is_effective(reviewer, artifacts_dir):
                failed_slots.append((index, None))
        if not failed_slots:
            break

        # 本轮失败的 harness 一律排除出后续候选；但只有额度类才写跨 run 的 cooldown
        # （无论能否 reroute 都记——跨轮 cooldown 是第二层防线）。
        for index, _err in failed_slots:
            failed_harness_id = reviewers[index]["harness_id"]
            failed_harnesses.add(failed_harness_id)
            # 派发实证这条腿本轮没能产出评审结论 ⇒ 它此前那条「可用」probe 缓存已被现实
            # 推翻，立刻废掉。不废的话，整个 TTL 内每一轮都会命中缓存、不再真实 probe，
            # 于是同一条死腿被反复选进两腿——缓存把一次性的失败变成自我延续的错误。
            invalidate_probe_result_cache_for_failed_dispatch(failed_harness_id)
        for index, err in usage_limited_slots:
            harness_id = reviewers[index]["harness_id"]
            if harness_id not in cooled:
                reset_hint = harness_limit_cooldown.parse_reset_hint(err)
                harness_limit_cooldown.record(
                    harness_id,
                    reset_hint=reset_hint,
                    reason="usage_limit_exhausted",
                    error_message=err,
                )
                cooled.add(harness_id)
                cooldown_recorded.append(harness_id)

        if not can_reroute:
            break

        candidates = _reroute_candidates(
            main_harness,
            available,
            cooled,
            registry,
            already_failed=failed_harnesses,
        )
        surviving = {
            reviewers[i]["harness_id"]
            for i in range(len(reviewers))
            if i not in {idx for idx, _ in failed_slots}
        }
        usage_limited_indices = {idx for idx, _ in usage_limited_slots}
        used_replacement: set[str] = set()
        new_indices: list[int] = []
        any_assigned = False
        for index, _err in failed_slots:
            pick = None
            # 第一遍：偏好与 surviving + 本轮已用替换都不同的 harness（两腿尽量异构）。
            for hid in candidates:
                if hid in surviving or hid in used_replacement:
                    continue
                pick = hid
                break
            # 第二遍：放宽到「不在 surviving」（仅剩一个 eligible 时允许同 harness 双实例，R-e/R-b）。
            if pick is None:
                for hid in candidates:
                    if hid in surviving:
                        continue
                    pick = hid
                    break
            if pick is None:
                continue  # 该 slot 补不上（A' 耗尽）→ 留待 fail-close 判定。
            slot_no = reviewers[index]["slot"]
            spec = _reviewer_spec(
                registry,
                pick,
                slot=slot_no,
                reviewer_id_suffix=f"fb{slot_no}",
            )
            spec["reviewer_id"] = _dedupe_reviewer_id(spec["reviewer_id"], used_ids)
            plan["fallbacks"].append(
                {
                    "slot": slot_no,
                    "from": reviewers[index]["harness_id"],
                    "from_reviewer_id": reviewers[index]["reviewer_id"],
                    "to": pick,
                    "to_reviewer_id": spec["reviewer_id"],
                    "round": _round + 1,
                    # additive 字段：额度耗尽 vs 其它「没产出评审结论」，事后分诊用。
                    "reason": (
                        "usage_limit_exhausted"
                        if index in usage_limited_indices
                        else "no_review_result"
                    ),
                }
            )
            reviewers[index] = spec
            used_ids.add(spec["reviewer_id"])
            used_replacement.add(pick)
            new_indices.append(index)
            any_assigned = True

        if not any_assigned:
            break  # 没有任何可补的替换 → 停止 reroute，进入 fail-close 判定。
        _dispatch_indices(new_indices)

    # 统计 + status + fail-close（F1/F2/R-e）。
    # ok 用「有效腿」而非裸 returncode：rc==0 但没写出 review-result 的腿不算数。
    ok = sum(1 for reviewer in reviewers if _leg_is_effective(reviewer, artifacts_dir))
    # 合法 request 腿：非完成态、但也不是失败，既不计入 unfilled_* 也不该被换腿。
    pending_request_slots = [
        index
        for index, reviewer in enumerate(reviewers)
        if _leg_review_request_pending(reviewer, artifacts_dir)
    ]
    unfilled_usage = [
        index
        for index, reviewer in enumerate(reviewers)
        if _leg_usage_limit(reviewer, artifacts_dir)[0]
    ]
    unfilled_without_review_result = [
        index
        for index, reviewer in enumerate(reviewers)
        if index not in unfilled_usage
        and index not in pending_request_slots
        and not _leg_is_effective(reviewer, artifacts_dir)
    ]
    plan["cooldown_recorded"] = cooldown_recorded
    # 让消费者不必自己重算「到底有几条腿真的有结论」（additive，schema_version 仍为 1）。
    plan["effective_reviewer_count"] = ok
    # additive：哪几路停在 kind: request，待主会话按 Contract Gate / Request Handling 处置后重试。
    plan["review_request_pending_reviewer_ids"] = [
        reviewers[index]["reviewer_id"] for index in pending_request_slots
    ]

    if ok == len(reviewers) and reviewers:
        plan["status"] = "completed"
    elif ok == 0:
        plan["status"] = "failed"
    else:
        plan["status"] = "partial"

    if unfilled_usage:
        # external 补不上：缺一条合法 leg 即非法 double-review（merge policy 要求恰好两腿），
        # 整体 fail-close 判 failed + 响亮信号，绝不静默置伪 R3 last-resort。
        plan["status"] = "failed"
        main_exhausted = bool(main_harness) and main_harness in cooled
        if (
            ok == 0
            and not unfilled_without_review_result
            and not pending_request_slots
        ):
            # 「全部」只在**所有**腿都是额度失败时才成立。混着 auth/崩溃见下方混合分支；
            # 混着合法 kind: request 腿时同样不成立——那条腿没失败、更不是额度问题，把它算进
            # 「全部额度耗尽」会让主会话照「等配额恢复」处置，漏掉必须走 Contract Gate 的 request
            #（此处仅排除该谎报，混合事实由下方 pending 分支的 reason 后缀自报）。
            reason = "all_reviewers_usage_limit_exhausted"
            message = "全部 reviewer 腿均因额度/配额耗尽失败，且无可用 harness 可回退；本轮 external double-review 无法完成。"
        elif main_exhausted:
            reason = "main_harness_usage_limit_exhausted"
            message = f"主/兜底 harness {main_harness} 额度耗尽且 external 补位不足；不退回 in-harness mimic，请改用其它 harness 或稍后重跑。"
        else:
            reason = "reviewer_usage_limit_unfilled"
            message = "部分 reviewer 腿因额度耗尽失败且无可用 harness 回退补位。"
        plan["reason"] = reason
        plan["warnings"].append(_warn(reason, "error", message))

    if unfilled_without_review_result:
        # 非额度类的腿失败（auth 失效、启动失败、崩溃、写不出 review-result …）且补位不足。
        # 与额度路径同等 fail-close：缺一条合法 leg 就不是 double-review，绝不让主会话
        # 拿单路结论当两路用（rvf-20260811T170157Z 正是这样静默滑过去的）。
        # 这里刻意**不是** elif：两类失败可以同时发生，各自的 warning 都必须冒出来——
        # 否则「一腿额度耗尽 + 一腿认证失败」只报额度，主会话会去等配额恢复，认证故障被藏住。
        plan["status"] = "failed"
        failed_labels = ", ".join(
            f"{reviewers[index]['harness_id']}(rc={reviewers[index].get('returncode')})"
            for index in unfilled_without_review_result
        )
        plan["warnings"].append(
            _warn(
                "reviewer_leg_produced_no_review_result",
                "error",
                f"以下 reviewer 腿没有产出合法 review-result 且无可用 harness 回退补位：{failed_labels}。"
                f"本轮只有 {ok} 条有效腿，不构成 double-review；请查看对应 reviewer 目录下的 "
                "reviewer.stderr*.txt 排障（常见原因：该 harness 登录过期/额度以外的认证失败）。",
            )
        )
        # reason 只有一个槽位：混合失败必须自报混合，不能被上面的额度 reason 独占。
        plan["reason"] = (
            "reviewer_legs_failed_mixed_usage_limit_and_no_review_result"
            if unfilled_usage
            else "reviewer_leg_produced_no_review_result"
        )

    if pending_request_slots:
        pending_labels = ", ".join(
            f"{reviewers[index]['harness_id']}({reviewers[index]['reviewer_id']})"
            for index in pending_request_slots
        )
        plan["warnings"].append(
            _warn(
                "reviewer_review_request_pending",
                "error",
                f"以下 reviewer 腿返回 kind: request，属于待处理终态而非完成态 review：{pending_labels}。"
                "按 references/review-merge-policy.md 的 Contract Gate / Request Handling 满足、"
                "驳回或 spawn 子任务后，让**同一** reviewer 用同一 review packet 重试；"
                "request 不得进入 merge table，也不会被自动换腿。",
            )
        )
        if not unfilled_usage and not unfilled_without_review_result:
            # 没有任何真失败腿时，本轮的 canonical 终态就是「被 request 阻塞」——
            # 既不是 completed（reviewer 没审完），也不是失败（无人出故障）。
            plan["status"] = PLAN_STATUS_BLOCKED_ON_REVIEWER_REQUEST
            plan["reason"] = "reviewer_review_request_pending"
        elif plan.get("reason"):
            # 与真失败腿并存：fail-close 优先，status 保持 failed；但 reason 只有一个槽位，
            # 必须自报「还夹着一条待处理 request」——否则主会话照失败类 reason 处置完
            #（等配额 / 修 auth）就重跑，那条 request 从头到尾没被 Contract Gate 处理过。
            plan["reason"] = f"{plan['reason']}{REVIEW_REQUEST_PENDING_REASON_SUFFIX}"

    return plan


def _read_dispatch_status(status_path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


DISPATCH_PLAN_STATUS_UNKNOWN = "unknown"


def _dispatch_plan_outcome(plan_path: Path | None) -> dict[str, Any]:
    """从 ``reviewer-plan.json`` 读本轮 double-review 的**实际结果**，供轮询输出使用。

    为什么不能只报 ``returncode``：那是 detached 派发进程本身的退出码，与「两腿是否都产出
    了评审结论」是两回事。2026-08-11 的 run rvf-20260811T170157Z 上，cursor 腿 rc=1、没有
    review-result，plan 记的是 ``status: partial``，而派发进程照样 exit 0 —— 轮询只打印
    ``returncode=0``，严格照 SKILL.md 协议轮询的主会话据此认为「派发成功」，然后拿一份结论
    当双评审用。plan 才是本轮结果的 canonical 事实源，必须一起冒到轮询输出。

    plan 缺失/不可解析时报 ``unknown``（而非乐观值）——派发尚未写出 plan 也属于「不知道」。
    """
    if plan_path is None:
        return {"plan_status": DISPATCH_PLAN_STATUS_UNKNOWN}
    try:
        payload = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"plan_status": DISPATCH_PLAN_STATUS_UNKNOWN}
    if not isinstance(payload, dict):
        return {"plan_status": DISPATCH_PLAN_STATUS_UNKNOWN}
    reviewers = payload.get("reviewers")
    reviewer_count = len(reviewers) if isinstance(reviewers, list) else 0
    effective = payload.get("effective_reviewer_count")
    return {
        "plan_status": payload.get("status") or DISPATCH_PLAN_STATUS_UNKNOWN,
        "plan_reason": payload.get("reason"),
        "reviewer_count": reviewer_count,
        "effective_reviewer_count": effective if isinstance(effective, int) else None,
    }


def _dispatch_status_summary(
    payload: dict[str, Any],
    *,
    plan_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "launch_status": payload.get("launch_status"),
        "returncode": payload.get("returncode"),
        "finished_at": payload.get("finished_at"),
        "error": payload.get("error"),
        **_dispatch_plan_outcome(plan_path),
    }


def _dispatch_status_is_terminal(payload: dict[str, Any]) -> bool:
    """终态：被包命令已退出（``finished_at`` 落值）或 launch 从未成功（``launch_failed``）。"""
    if payload.get("finished_at"):
        return True
    return payload.get("launch_status") == LAUNCH_FAILED


def wait_for_dispatch_status(
    status_path: Path,
    *,
    max_wait: float,
    poll_interval: float = 2.0,
) -> dict[str, Any]:
    """有界轮询 detached 派发的 ``status.json``，直到终态或 ``max_wait`` 耗尽。

    返回 ``{"state": "done"|"running", "launch_status", "returncode", "finished_at",
    "error", "plan_status", "plan_reason", "reviewer_count", "effective_reviewer_count"}``。
    单次调用 ≤ ``max_wait`` 秒（默认 480 < Bash 工具 600s 上限），agent 循环调用直到
    ``state == "done"``——每次都在上限内、永不被砍断。

    ``plan_*`` 取自 ``status_path`` 同目录的 ``reviewer-plan.json``：``returncode`` 只说
    派发进程怎么退出，``plan_status`` 才说两腿到底有没有产出评审结论（见
    :func:`_dispatch_plan_outcome`）。判「本轮 double-review 是否成立」一律看后者。
    """
    import time

    plan_path = Path(status_path).parent / "reviewer-plan.json"
    deadline = time.monotonic() + max(0.0, max_wait)
    latest: dict[str, Any] = {}
    while True:
        payload = _read_dispatch_status(status_path)
        if payload is not None:
            latest = payload
            if _dispatch_status_is_terminal(payload):
                return {"state": "done", **_dispatch_status_summary(payload, plan_path=plan_path)}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"state": "running", **_dispatch_status_summary(latest, plan_path=plan_path)}
        time.sleep(min(poll_interval, remaining))


def _build_detached_child_argv(
    args: argparse.Namespace,
    *,
    run_id: str,
    run_dir: str,
    repo: str | None,
    review_packet: str | None,
    session_context: str | None,
    scope_contract: str | None,
) -> list[str]:
    """拼 detached 子进程命令：``dispatch_reviewers.py --execute`` 复用同一 run。

    刻意**不带** ``--detached``——子进程跑真正的 probe/route/execute/reroute 本体；
    run_id/run_dir 显式透传以复用父进程已解析的 run；repo / packet 等输入按解析后的值
    透传，缺省时让 child 经 ``launch_env`` 里的 ``RVF_*`` env 回填。
    """
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--execute",
        "--rvf-run-id",
        run_id,
        "--rvf-run-dir",
        run_dir,
        "--registry",
        args.registry,
        "--probe-mode",
        args.probe_mode,
        "--probe-timeout",
        str(args.probe_timeout),
        "--main-harness",
        args.main_harness,
    ]
    for flag, value in (
        ("--repo", repo),
        ("--review-packet", review_packet),
        ("--session-context", session_context),
        ("--scope-contract", scope_contract),
        ("--transcript", args.transcript),
        ("--main-harness-file", args.main_harness_file),
        ("--assume-available", args.assume_available),
    ):
        if value:
            argv += [flag, value]
    if args.require_external:
        argv.append("--require-external")
    # getattr 兜底：程序化调用方（含测试）可能手拼 Namespace 而缺该属性。
    selection_seed = getattr(args, "selection_seed", None)
    if selection_seed is not None:
        argv += ["--selection-seed", str(selection_seed)]
    # 必须透传：不传的话 detached child 会退回整盘重派，把「只重派 request 腿」在真正跑活的那条
    # 路径（--execute --detached 是主会话的标准用法）上悄悄作废。
    if getattr(args, "redispatch_review_request_legs_only", False):
        argv.append("--redispatch-review-request-legs-only")
    return argv


def launch_detached_dispatch(
    args: argparse.Namespace,
    ledger: Any,
    reviewers_dir: Path,
    *,
    repo: str | None,
    review_packet: str | None,
    session_context: str | None,
    scope_contract: str | None,
) -> int:
    """把 ``dispatch_reviewers.py --execute`` 派进 detached tmux session，立即返回。

    解除「agent 前台 Bash 调用 ↔ 整轮派发 wall-clock」的耦合：派发本体（probe/route/
    execute/reroute）跑在 detached 子进程里、不受 Bash 工具 600s 上限约束；agent 改用
    ``--wait-status`` 有界轮询 ``status.json`` 直到终态。施加一个比任何单 reviewer 都宽
    的总 backstop（``--total-timeout``，默认 2700s）保证 ``status.json`` 终会落终态。
    幂等：同一 run 重复 ``--execute --detached`` 命中 O_EXCL 锁 → ``already_running``。
    该幂等只对**干净完成**（被包命令 rc=0）的 run 成立：非完成态终态以非 0 收尾
    （failed→1、``blocked_on_reviewer_request``→``DISPATCH_BLOCKED_ON_REVIEWER_REQUEST_EXIT_CODE``），
    线程已死时锁会被判陈旧并重夺，主会话处置完 reviewer request 后能经同一入口重派。
    """
    prepared_boundary = require_prepared_run_review_start_boundary(repo=repo, scope_contract=scope_contract, run_id=ledger.run_id if ledger else None, run_dir=ledger.run_dir if ledger else None)
    reviewers_dir.mkdir(parents=True, exist_ok=True)
    status_path = reviewers_dir / DISPATCH_STATUS_FILENAME
    log_path = reviewers_dir / DISPATCH_LOG_FILENAME
    lock_path = reviewers_dir / DISPATCH_LOCK_FILENAME
    run_name = ledger.run_dir.name
    session_name = f"rvf-dispatch-{safe_token(run_name)}"

    child_argv = _build_detached_child_argv(
        args,
        run_id=ledger.run_id,
        run_dir=str(ledger.run_dir),
        repo=repo,
        review_packet=review_packet,
        session_context=session_context,
        scope_contract=scope_contract,
    )
    status_payload = {
        "schema_version": DISPATCH_STATUS_SCHEMA_VERSION,
        "kind": "dispatch",
        "repo": repo,
        "session_id": (prepared_boundary or {}).get("session_id"),
        "run_id": ledger.run_id,
        "run_dir": str(ledger.run_dir),
        "run_name": run_name,
        "tmux_session": session_name,
        "command": child_argv,
        "total_timeout_seconds": args.total_timeout,
        "pid": None,
        "started_at": _iso_now(),
        "returncode": None,
        "finished_at": None,
        "launch_status": LAUNCH_LAUNCHED,
        "error": None,
    }

    # 已有 tmux server 不会继承 client 的当前环境。所有受支持身份字段都须在内层覆盖；
    # 父进程缺失的字段写空，防止 server 残留的任务／会话／harness 信号改变 marker 分键。
    detached_dispatch_environment_exports = {
        name: os.environ.get(name, "")
        for name in (
            "KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID",
            *CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES,
            "CLAUDECODE", "CURSOR_AGENT", "CODEX_RVF_CLINE_KANBAN_AGENT_ID", "AI_AGENT",
        )
    }
    detached_dispatch_environment_exports.update(ledger.env())

    result = launch_detached(
        session_name=session_name,
        argv=child_argv,
        log_path=log_path,
        status_path=status_path,
        lock_path=lock_path,
        status_payload=status_payload,
        # run 日志根与当前身份一并写进内层 shell，保持 prepare 的 tracker 库与任务基线。
        exports=detached_dispatch_environment_exports,
        launch_env={**os.environ, **detached_dispatch_environment_exports},
        idempotency_key=f"rvf-dispatch:{run_name}",
        total_timeout_seconds=args.total_timeout,
        require_actual_dispatcher_writer_execution_evidence=True,
    )

    ledger.event(
        phase="review",
        event="dispatch_detached_launched",
        status=(
            "completed"
            if result["launch_status"] in (LAUNCH_LAUNCHED, "already_running")
            else "failed"
        ),
        reason_code=f"dispatch_detached_{result['launch_status']}",
        tmux_session=session_name,
        status_path=str(status_path),
        launch_status=result["launch_status"],
    )

    # agent 解析这两行：status.json 落点 + 本次 launch 结果，随后用 --wait-status 轮询。
    print(f"RVF_DISPATCH_STATUS={status_path}")
    print(f"RVF_DISPATCH_LAUNCH={result['launch_status']}")
    if result["launch_status"] == LAUNCH_FAILED:
        print(result.get("error") or "dispatch detached launch failed", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Route + dispatch RVF santa-method double-review reviewers."
    )
    parser.add_argument("--repo", help="Target git repository.")
    parser.add_argument("--review-packet", help="Self-contained review packet path.")
    parser.add_argument("--session-context", help="Scope-of-work / session context path.")
    parser.add_argument("--scope-contract", help="scope.contract.json path.")
    parser.add_argument(
        "--registry",
        default=str(DEFAULT_REGISTRY),
        help="reviewer-registry.json path.",
    )
    parser.add_argument(
        "--main-harness",
        default="auto",
        choices=["auto", "cursor", "claude_code", "codex"],
        help="Override main dispatch harness. cursor only reachable via explicit override.",
    )
    parser.add_argument("--transcript", help="Transcript path for main-harness auto-detection.")
    parser.add_argument(
        "--main-harness-file",
        help="main-harness.json path (defaults to <artifacts>/inputs/main-harness.json).",
    )
    parser.add_argument("--rvf-run-id", help="Existing RVF run id.")
    parser.add_argument("--rvf-run-dir", help="Existing RVF run directory.")
    parser.add_argument(
        "--probe-mode",
        default="preflight",
        choices=["check", "preflight"],
        help="run_alternative_reviewer probe mode (default preflight runs health command).",
    )
    parser.add_argument("--probe-timeout", type=float, default=60.0, help="Per-harness probe timeout seconds.")
    parser.add_argument(
        "--assume-available",
        help="Comma-separated harness ids; skip real probe (testing / deterministic plan).",
    )
    parser.add_argument(
        "--require-external",
        action="store_true",
        help="Fail (no last-resort fallback) when 0 external harness available.",
    )
    parser.add_argument(
        "--selection-seed",
        type=int,
        default=None,
        help="Override pseudo-random reviewer selection seed (reproduce a recorded "
        "reviewer_selection.selection_seed; default: freshly generated and recorded).",
    )
    parser.add_argument(
        "--redispatch-review-request-legs-only",
        action="store_true",
        help="With --execute: reuse the previous reviewer-plan.json and re-run ONLY the legs "
        "that ended in `kind: request`. Healthy completed legs keep their results untouched "
        "(a whole-board re-dispatch re-runs them and their findings get overwritten by the "
        "new round). Each re-dispatched leg's old artifact is rotated into "
        "superseded-review-result-artifacts/ first.",
    )
    parser.add_argument("--plan-only", action="store_true", help="Plan + write reviewer-plan.json; do not execute.")
    parser.add_argument("--execute", action="store_true", help="Plan then execute reviewers in parallel.")
    parser.add_argument("--dry-run", action="store_true", help="Print plan JSON to stdout; do not write or execute.")
    parser.add_argument(
        "--detached",
        action="store_true",
        help="With --execute: self-fork the whole dispatch into a detached tmux "
        "thread and return the status.json path immediately (breaks the Bash-tool "
        "600s cap). Host-agnostic; does not use Claude-only run_in_background.",
    )
    parser.add_argument(
        "--wait-status",
        action="store_true",
        help="Bounded-poll a detached dispatch status.json until terminal; use with "
        "--status-path / --max-wait. Prints RVF_DISPATCH_STATE=done|running, exit 0.",
    )
    parser.add_argument("--status-path", help="Detached dispatch status.json path (required with --wait-status).")
    parser.add_argument(
        "--max-wait",
        type=float,
        default=DEFAULT_DISPATCH_MAX_WAIT_SECONDS,
        help=f"--wait-status max blocking seconds per call (default {DEFAULT_DISPATCH_MAX_WAIT_SECONDS:g} < 600s cap).",
    )
    parser.add_argument(
        "--total-timeout",
        type=float,
        default=DEFAULT_DISPATCH_TOTAL_TIMEOUT_SECONDS,
        help=f"--detached total backstop seconds, wider than any single reviewer "
        f"(default {DEFAULT_DISPATCH_TOTAL_TIMEOUT_SECONDS:g}); guarantees status.json reaches a terminal state.",
    )
    args = parser.parse_args()

    # --wait-status：纯读 status.json 的有界轮询，不需要 registry / run；最先短路。
    if args.wait_status:
        if not args.status_path:
            print("--wait-status requires --status-path", file=sys.stderr)
            return 2
        result = wait_for_dispatch_status(
            Path(args.status_path).expanduser(),
            max_wait=args.max_wait,
        )
        if result["state"] == "done":
            # plan_status / effective_reviewer_count 与 returncode 并列打印：后者只说派发
            # 进程怎么退出，前两者才说本轮 double-review 是否真的成立（见 SKILL.md 轮询协议）。
            print(
                f"RVF_DISPATCH_STATE=done "
                f"launch_status={result.get('launch_status')} "
                f"returncode={result.get('returncode')} "
                f"plan_status={result.get('plan_status')} "
                f"effective_reviewers={result.get('effective_reviewer_count')}"
                f"/{result.get('reviewer_count')}"
            )
            if result.get("plan_reason"):
                print(f"RVF_DISPATCH_PLAN_REASON={result.get('plan_reason')}", file=sys.stderr)
        else:
            print(
                f"RVF_DISPATCH_STATE=running "
                f"launch_status={result.get('launch_status')}"
            )
        return 0

    if args.detached and not args.execute:
        print("--detached requires --execute", file=sys.stderr)
        return 2

    # SKILL.md 契约：`source review-env.sh` 后只需运行 `dispatch_reviewers.py --execute`。
    # review-env.sh 导出 RVF_REPO / RVF_REVIEW_PACKET / RVF_SCOPE_OF_WORK(/RVF_SESSION_CONTEXT) /
    # RVF_SCOPE_CONTRACT，因此显式 CLR 参数缺省时从这些 env 回填，否则子进程 reviewer 会因缺
    # --repo/--review-packet 而失败，与文档的最短派发路径矛盾。
    repo = args.repo or os.environ.get("RVF_REPO")
    review_packet = args.review_packet or os.environ.get("RVF_REVIEW_PACKET")
    session_context = (
        args.session_context
        or os.environ.get("RVF_SCOPE_OF_WORK")
        or os.environ.get("RVF_SESSION_CONTEXT")
    )
    scope_contract = args.scope_contract or os.environ.get("RVF_SCOPE_CONTRACT")

    if args.execute:
        try:
            boundary_contract = json.loads(Path(scope_contract).read_text()) if scope_contract else {}
            args.rvf_run_id = args.rvf_run_id or os.environ.get("RVF_RUN_ID") or boundary_contract.get("run_id")
            require_prepared_run_review_start_boundary(repo=repo, scope_contract=scope_contract, run_id=args.rvf_run_id, run_dir=args.rvf_run_dir)
        except (ReviewStartBoundaryNotVerified, OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 4

    try:
        registry = load_registry(Path(args.registry))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"reviewer registry 加载失败: {exc}", file=sys.stderr)
        return 2

    ledger = start_run(
        "reviewer",
        repo=repo,
        cwd=repo,
        run_id=args.rvf_run_id,
        run_dir=Path(args.rvf_run_dir).expanduser().resolve() if args.rvf_run_dir else None,
    )
    artifacts_dir = ledger.artifacts_dir
    reviewers_dir = artifacts_dir / "reviewers"

    # --detached：把整轮派发本体（下方 probe/route/execute）self-fork 进 detached tmux
    # 线程并立即返回。run 已解析（上方 start_run），child 复用同一 run_id/run_dir。
    if args.detached:
        return launch_detached_dispatch(
            args,
            ledger,
            reviewers_dir,
            repo=repo,
            review_packet=review_packet,
            session_context=session_context,
            scope_contract=scope_contract,
        )

    env_main_harness = os.environ.get("RVF_MAIN_HARNESS")
    main_harness_file = (
        Path(args.main_harness_file).expanduser()
        if args.main_harness_file
        else reviewers_dir.parent / "inputs" / "main-harness.json"
    )
    main_harness = resolve_main_harness(
        cli_main_harness=args.main_harness,
        env_main_harness=env_main_harness,
        main_harness_file=main_harness_file,
        transcript=Path(args.transcript).expanduser() if args.transcript else None,
    )

    assume_available = (
        [h.strip() for h in args.assume_available.split(",") if h.strip()]
        if args.assume_available is not None
        else None
    )
    # 跨轮 cooldown：真实 probe 才应用（assume-available 仅测试用、不受额度冷却影响，D-O4）。
    # 额度耗尽时 auth probe 仍返回 0，故须以独立 cooldown 标记把冷却中的 harness 排除在 probe 外。
    cooled_markers = (
        {} if assume_available is not None else harness_limit_cooldown.active_harnesses()
    )
    available = probe_available(
        registry,
        probe_mode=args.probe_mode,
        timeout=args.probe_timeout,
        assume_available=assume_available,
        cooldown_active=set(cooled_markers),
    )

    redispatch_indices: list[int] | None = None
    if args.redispatch_review_request_legs_only:
        try:
            plan, redispatch_indices = _resume_plan_for_review_request_legs(
                reviewers_dir, artifacts_dir
            )
        except ValueError as exc:
            print(f"[dispatch_reviewers] error: {exc}", file=sys.stderr)
            return 1
        if not redispatch_indices:
            print(
                "[dispatch_reviewers] 上一轮 plan 里没有 kind: request 的腿，无需重派。",
                file=sys.stderr,
            )
            print(str(reviewers_dir / "reviewer-plan.json"))
            return 0
    else:
        plan = route(
            main_harness,
            available,
            registry,
            require_external_only=args.require_external,
            selection_seed=args.selection_seed,
        )

    # 被 cooldown 排除的 enabled harness：逐一发可观测 warning（O2 契约 code）。
    enabled_ids = set(_enabled_harnesses(registry))
    cooled_enabled = [hid for hid in cooled_markers if hid in enabled_ids]
    for hid in cooled_enabled:
        marker = cooled_markers.get(hid) or {}
        plan["warnings"].append(
            _warn(
                "harness_limit_cooldown_active",
                "warning",
                f"{hid} 仍处额度耗尽冷却期（到 {marker.get('expires_at')}），本轮 probe 跳过；已在其余 harness 中路由。",
            )
        )
    # F2：A 被 cooldown 清空（而非 probe 全失败）→ 响亮 error fail-close，绝不静默置伪 R3 last-resort。
    if not available and cooled_enabled:
        plan["status"] = "failed"
        plan["reason"] = "all_harnesses_usage_limited"
        plan["needs_last_resort_fallback"] = False
        plan["warnings"].append(
            _warn(
                "all_harnesses_usage_limited",
                "error",
                "所有 enabled reviewer harness 均处额度耗尽冷却期；本轮无可用 external reviewer，"
                "不退回 in-harness mimic。请稍后重跑或更换可用 harness。",
            )
        )

    ledger.event(
        phase="review",
        event="dispatch_planned",
        status="completed" if plan["status"] != "failed" else "failed",
        reason_code=f"dispatch_routing_{plan['routing_rule']}",
        routing_rule=plan["routing_rule"],
        main_harness=main_harness,
        available_harnesses=available,
        warnings=plan["warnings"],
    )
    for warning in plan["warnings"]:
        print(
            f"[dispatch_reviewers] {warning['severity']}: {warning['code']}: {warning['message']}",
            file=sys.stderr,
        )

    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0 if plan["status"] != "failed" else 1

    if args.execute and plan["status"] != "failed" and plan["reviewers"]:
        pre_execute_warning_count = len(plan["warnings"])
        if redispatch_indices:
            # 破坏性的一步，只能在这里发生：--dry-run / --plan-only 已在上面短路/落空，
            # 它们看到的仍是上一轮完整的 canonical review-result.json（RVF-008）。
            plan["rotated_review_request_artifacts"] = _rotate_review_request_leg_artifacts(
                plan, redispatch_indices, artifacts_dir
            )
        plan = execute_plan(
            plan,
            repo=repo,
            review_packet=review_packet,
            session_context=session_context,
            scope_contract=scope_contract,
            run_id=ledger.run_id,
            run_dir=str(ledger.run_dir),
            artifacts_dir=artifacts_dir,
            registry=registry,
            main_harness=main_harness,
            available=available,
            initial_dispatch_indices=redispatch_indices,
        )
        # reroute / fail-close 在 execute_plan 内可能追加 warning，只打印增量部分（避免重复已打印的）。
        for warning in plan["warnings"][pre_execute_warning_count:]:
            print(
                f"[dispatch_reviewers] {warning['severity']}: {warning['code']}: {warning['message']}",
                file=sys.stderr,
            )
        ledger.event(
            phase="review",
            event="dispatch_executed",
            status=plan["status"],
            reason_code=f"dispatch_{plan['status']}",
            reviewers=[
                {"reviewer_id": r["reviewer_id"], "returncode": r.get("returncode")}
                for r in plan["reviewers"]
            ],
            fallbacks=plan.get("fallbacks", []),
            cooldown_recorded=plan.get("cooldown_recorded", []),
        )

    reviewers_dir.mkdir(parents=True, exist_ok=True)
    plan_path = reviewers_dir / "reviewer-plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(plan_path))

    if plan["status"] == "failed":
        return 1
    if plan["status"] == PLAN_STATUS_BLOCKED_ON_REVIEWER_REQUEST:
        # 非完成态终态**不得**以 0 收尾。0 的含义是「被包命令干净完成」：detached 幂等锁的
        # staleness 判定（``rvf_detached_thread._detached_run_finished_clean``）据此认定该 run
        # 已完成、拒绝重夺锁，于是主会话按 SKILL.md 处置完 request 后再跑
        # ``--execute --detached`` 只会拿到 ``already_running``——本 run 永远产不出
        # ``status: completed`` 的 reviewed-candidate 证据，只能绕过单一入口或手改协议 artifact。
        # 专用码 3 让锁可被重夺（可正常重派），又与真故障的 1 区分开。
        return DISPATCH_BLOCKED_ON_REVIEWER_REQUEST_EXIT_CODE
    return 0


if __name__ == "__main__":
    from dispatcher_writer_execution_evidence import run_actual_dispatcher_with_writer_execution_evidence

    raise SystemExit(run_actual_dispatcher_with_writer_execution_evidence(main, dispatcher_script_path=Path(__file__).resolve()))
