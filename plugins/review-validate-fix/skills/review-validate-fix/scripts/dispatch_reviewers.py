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
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rvf_logging import start_run
from run_alternative_reviewer import reviewer_id_from_label, safe_artifact_token
from trajectory_distill import HOST_CODEX, detect_transcript_format


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = SKILL_DIR / "config" / "reviewer-registry.json"
RUN_ALTERNATIVE_REVIEWER = Path(__file__).resolve().parent / "run_alternative_reviewer.py"
PLAN_SCHEMA_VERSION = 1

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
) -> list[str]:
    """对每个 enabled harness 跑 ``run_alternative_reviewer.py --config <path> --<probe_mode>``。

    ``assume_available`` 给定时跳过真实 probe（用于 --plan-only 测试 / 确定性路由），
    只取其与 enabled 的交集。
    """
    enabled = _enabled_harnesses(registry)
    if assume_available is not None:
        return [h for h in assume_available if h in enabled]
    available: list[str] = []
    for harness_id in enabled:
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
) -> dict[str, Any]:
    """并行（或顺序）派发 plan 中的两路 external reviewer，回填 returncode / result path / status。"""
    reviewers = plan.get("reviewers", [])
    results: dict[int, int] = {}

    def _run(index: int, reviewer: dict[str, Any]) -> None:
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
            results[index] = completed.returncode
        except OSError as exc:  # pragma: no cover - 启动失败
            results[index] = 1
            reviewer["error"] = f"{type(exc).__name__}: {exc}"

    if plan.get("sequential_execution"):
        for index, reviewer in enumerate(reviewers):
            _run(index, reviewer)
    else:
        threads = [
            threading.Thread(target=_run, args=(index, reviewer))
            for index, reviewer in enumerate(reviewers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    ok = 0
    for index, reviewer in enumerate(reviewers):
        returncode = results.get(index, 1)
        reviewer["returncode"] = returncode
        reviewer["review_result_path"] = str(
            artifacts_dir / "reviewers" / reviewer["reviewer_id"] / "review-result.json"
        )
        if returncode == 0:
            ok += 1

    if ok == len(reviewers) and reviewers:
        plan["status"] = "completed"
    elif ok == 0:
        plan["status"] = "failed"
    else:
        plan["status"] = "partial"
    return plan


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
    args = parser.parse_args()

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
    available = probe_available(
        registry,
        probe_mode=args.probe_mode,
        timeout=args.probe_timeout,
        assume_available=assume_available,
    )

    plan = route(
        main_harness,
        available,
        registry,
        require_external_only=args.require_external,
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
        plan = execute_plan(
            plan,
            repo=repo,
            review_packet=review_packet,
            session_context=session_context,
            scope_contract=scope_contract,
            run_id=ledger.run_id,
            run_dir=str(ledger.run_dir),
            artifacts_dir=artifacts_dir,
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
