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

路由规则（``route()`` 为纯函数、可单测）：

- ``M`` = 主 dispatch harness（本轮 RVF 主会话所在 harness）
- ``A`` = 本机 probe 通过的 harness 集合（``reviewer-registry.json`` 中 enabled 且 probe OK）

| |A| | rule | 选择 |
|-----|------|------|
| ≥3  | R0   | 两路非主 external，cursor 必选一腿（cursor∈A 恒成立） |
| ==2 | R1   | 这两路 external（M∈A 时 M 也以 external 跑，不退回 in-harness） |
| ==1 | R2   | 同 harness 双 external 实例（reviewer_id 后缀 ``-a``/``-b``） |
| ==0 | R3   | 默认 needs_last_resort_fallback（交主会话按 last-resort reference 兜底）；
|     |      | 仅 ``--require-external`` 时 fail |

R4 = cursor probe 失败的降级修饰：cursor∉A 时记 warning ``cursor_unavailable``，
在剩余非主 harness 中另选；只剩一个非主则落入 R2。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401  — 把 pyroot 加入 sys.path，供 core.* import
import harness_limit_cooldown
from core.run_ledger.run_ledger import safe_token, start_run
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
)
from core.host_adapter.host_transcript_format_detection import HOST_CODEX, detect_transcript_format


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = SKILL_DIR / "config" / "reviewer-registry.json"
RUN_ALTERNATIVE_REVIEWER = Path(__file__).resolve().parent / "run_alternative_reviewer.py"
PLAN_SCHEMA_VERSION = 1

# Detached 派发线程（--detached / --wait-status）：把「agent 前台 Bash 调用 ↔ 整轮
# 派发 wall-clock」解耦，突破 Bash 工具 600s 硬上限。status/log/lock 落在
# <artifacts>/reviewers/ 下，与 reviewer-plan.json / 各 reviewer artifact 同处。
DISPATCH_STATUS_SCHEMA_VERSION = 1
DISPATCH_STATUS_FILENAME = ".dispatch-thread.status.json"
DISPATCH_LOG_FILENAME = ".dispatch-thread.log"
DISPATCH_LOCK_FILENAME = ".dispatch-thread.lock"
# 单次 waiter 阻塞上限：480 < 600s Bash 上限，留余量；agent 循环调用直到 done。
DEFAULT_DISPATCH_MAX_WAIT_SECONDS = 480.0
# 总超时 backstop：比任何单 reviewer 都宽（单 reviewer idle-timeout 300s、无总上限），
# 保证 detached 派发 status.json 终会落终态，又不误杀正当长 review。
DEFAULT_DISPATCH_TOTAL_TIMEOUT_SECONDS = 2700.0

CURSOR_HARNESS = "cursor"
DEFAULT_MAIN_HARNESS = HOST_CODEX  # 与现有 Kanban 兜底一致

# 已知存在并行 headless session/auth 全局锁、需要顺序执行的 harness。
# 由 S3 同 CLI 并行 spike 填充；空集合表示默认全部可并行。
SEQUENTIAL_HARNESSES: frozenset[str] = frozenset()


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


def route(
    main_harness: str,
    available: list[str],
    registry: dict[str, Any],
    *,
    require_external_only: bool = False,
    sequential_harnesses: frozenset[str] = SEQUENTIAL_HARNESSES,
) -> dict[str, Any]:
    """纯路由函数：给定主 harness + 可用集 + registry，返回 plan dict（无 IO）。

    返回 dict 含 routing_rule / reviewers / warnings / needs_last_resort_fallback /
    sequential_execution / status。``available`` 会先过滤到 registry 中 enabled 的
    harness 并按 priority 排序。
    """
    enabled = set(_enabled_harnesses(registry))
    A = _order_by_priority(registry, [h for h in available if h in enabled])
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

    # R4 降级修饰：cursor 是 R0/R1 默认必选一腿，仅在 |A|>=2 时缺席才记 warning。
    # R2（|A|==1）有自己的 mismatch / only_main warning，不再叠加 cursor_unavailable。
    cursor_preferred = main_harness != CURSOR_HARNESS
    if cursor_preferred and CURSOR_HARNESS not in A and len(A) >= 2:
        warnings.append(
            _warn(
                "cursor_unavailable",
                "warning",
                "cursor probe 失败或未启用；已在剩余 harness 中选择 reviewer（R4 降级）。",
            )
        )

    # R3 — 零可用
    if len(A) == 0:
        plan["routing_rule"] = "R3"
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
    if len(A) == 1:
        only = A[0]
        plan["routing_rule"] = "R2"
        if only != main_harness:
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

    # |A| >= 2 — R1（==2）或 R0（>=3）：选两路非主 external，cursor 优先
    plan["routing_rule"] = "R0" if len(A) >= 3 else "R1"
    non_main = [h for h in A if h != main_harness]
    prefs: list[str] = []
    if CURSOR_HARNESS in non_main:
        prefs.append(CURSOR_HARNESS)
    for h in non_main:
        if h not in prefs:
            prefs.append(h)
    if main_harness in A and main_harness not in prefs:
        # 仅当非主 harness 不足两路时，主 harness 也以 external 跑（绝不退回 in-harness）。
        prefs.append(main_harness)
    selected = prefs[:2]
    plan["reviewers"] = [
        _reviewer_spec(registry, hid, slot=index + 1)
        for index, hid in enumerate(selected)
    ]
    return plan


def probe_available(
    registry: dict[str, Any],
    *,
    probe_mode: str = "preflight",
    timeout: float = 60.0,
    assume_available: list[str] | None = None,
    cooldown_active: set[str] | None = None,
) -> list[str]:
    """对每个 enabled harness 跑 ``run_alternative_reviewer.py --config <path> --<probe_mode>``。

    ``assume_available`` 给定时跳过真实 probe（用于 --plan-only 测试 / 确定性路由），
    只取其与 enabled 的交集；此路径**不**应用 cooldown（D-O4：assume 仅测试用，须走真实 probe
    才会被额度冷却排除）。

    ``cooldown_active`` 给定时，处于额度耗尽冷却期的 harness **连 probe 都跳过**（额度耗尽时
    ``codex login status`` 之类 auth probe 仍返回 0，单看 auth 发现不了，故需独立额度信号）。
    """
    enabled = _enabled_harnesses(registry)
    if assume_available is not None:
        return [h for h in assume_available if h in enabled]
    cooled = cooldown_active or set()
    available: list[str] = []
    for harness_id in enabled:
        if harness_id in cooled:
            continue
        spec = registry["harnesses"][harness_id]
        config_path = SKILL_DIR / spec["config_path"]
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
            if completed.returncode == 0:
                available.append(harness_id)
        except (subprocess.TimeoutExpired, OSError):
            continue
    return available


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
) -> list[str]:
    """对 ``A' = available − cooled`` 重调 ``route()`` 取替换候选 harness 顺序（R-c 单源）。

    候选 = route 在 A' 上的两腿选择（已含 R0–R4 的 cursor 优先 / main-as-external / R2 双实例
    偏好），再按 priority 补齐 A' 其余，保证「需补两腿但 route 只给两腿」时仍有后备。
    """
    a_prime = [h for h in available if h not in cooled]
    candidates: list[str] = []
    if main_harness is not None and a_prime:
        sub_plan = route(main_harness, a_prime, registry)
        for reviewer in sub_plan.get("reviewers", []):
            hid = reviewer.get("harness_id")
            if hid and hid not in candidates:
                candidates.append(hid)
    for hid in _order_by_priority(registry, a_prime):
        if hid not in candidates:
            candidates.append(hid)
    return candidates


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
) -> dict[str, Any]:
    """派发 plan 的两路 external reviewer，回填 returncode / result path / status。

    撞额度耗尽（returncode 125 + 佐证）时，做**轮内 reroute**：记 cooldown → 对
    ``A' = available − cooled`` 重调 ``route()`` 取替换 → in-place 替换失败 slot（保持恰好两腿、
    保留成功 leg 的 artifact）→ 派发替换 leg；替换也 125 则 bounded 再来一轮。补不上则
    fail-close（F1/F2/R-e）。``registry``/``main_harness``/``available`` 缺省（None）时退化为
    「无 reroute」，仅按 returncode 计 status（兼容旧调用方）。
    """
    reviewers = plan.get("reviewers", [])
    cooled: set[str] = set()
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
                completed = subprocess.run(cmd)
                local_results[index] = completed.returncode
            except OSError as exc:  # pragma: no cover - 启动失败
                local_results[index] = 1
                reviewer["error"] = f"{type(exc).__name__}: {exc}"

        if plan.get("sequential_execution"):
            for index in indices:
                _run(index)
        else:
            threads = [threading.Thread(target=_run, args=(index,)) for index in indices]
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

    # 初派全部两腿。
    _dispatch_indices(list(range(len(reviewers))))

    # 反应式 reroute：bounded 轮次，每轮替换本轮新撞额度的 slot。
    can_reroute = registry is not None and main_harness is not None
    for _round in range(max_fallback_rounds):
        failed_slots: list[tuple[int, str | None]] = []
        for index, reviewer in enumerate(reviewers):
            hit, err = _leg_usage_limit(reviewer, artifacts_dir)
            if hit:
                failed_slots.append((index, err))
        if not failed_slots:
            break

        # 记 cooldown（无论能否 reroute 都记——跨轮 cooldown 是第二层防线）。
        for index, err in failed_slots:
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

        candidates = _reroute_candidates(main_harness, available, cooled, registry)
        surviving = {
            reviewers[i]["harness_id"]
            for i in range(len(reviewers))
            if i not in {idx for idx, _ in failed_slots}
        }
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
    ok = sum(1 for reviewer in reviewers if reviewer.get("returncode") == 0)
    unfilled_usage = [
        index
        for index, reviewer in enumerate(reviewers)
        if _leg_usage_limit(reviewer, artifacts_dir)[0]
    ]
    plan["cooldown_recorded"] = cooldown_recorded

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
        if ok == 0:
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

    return plan


def _read_dispatch_status(status_path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _dispatch_status_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "launch_status": payload.get("launch_status"),
        "returncode": payload.get("returncode"),
        "finished_at": payload.get("finished_at"),
        "error": payload.get("error"),
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
    "error"}``。单次调用 ≤ ``max_wait`` 秒（默认 480 < Bash 工具 600s 上限），agent
    循环调用直到 ``state == "done"``——每次都在上限内、永不被砍断。
    """
    import time

    deadline = time.monotonic() + max(0.0, max_wait)
    latest: dict[str, Any] = {}
    while True:
        payload = _read_dispatch_status(status_path)
        if payload is not None:
            latest = payload
            if _dispatch_status_is_terminal(payload):
                return {"state": "done", **_dispatch_status_summary(payload)}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"state": "running", **_dispatch_status_summary(latest)}
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
    """
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

    result = launch_detached(
        session_name=session_name,
        argv=child_argv,
        log_path=log_path,
        status_path=status_path,
        lock_path=lock_path,
        status_payload=status_payload,
        # 把 RVF env 显式写进 tmux 内层 shell 的 `export X=Y;` 行，让 detached
        # reviewer 子进程稳定读到 CODEX_RVF_LOG_ROOT 等——不再依赖 tmux server 是否
        # 预存在的 env 继承（既有 tmux server 会让 new-session 继承 server 的 env，
        # 丢掉这里 launch_env 传入的值）。否则 reviewer 的 diff-tracker DB 落到默认
        # state 目录、与 prepare 写 lease 的库分叉 → lease_not_found。
        exports=ledger.env(),
        launch_env={**os.environ, **ledger.env()},
        idempotency_key=f"rvf-dispatch:{run_name}",
        total_timeout_seconds=args.total_timeout,
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
            print(
                f"RVF_DISPATCH_STATE=done "
                f"launch_status={result.get('launch_status')} "
                f"returncode={result.get('returncode')}"
            )
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

    plan = route(
        main_harness,
        available,
        registry,
        require_external_only=args.require_external,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
