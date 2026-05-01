#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SKILL = ROOT / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"
CONTRACT_SCRIPT = ROOT / "scripts" / "check_skill_contracts.sh"
TIMING_JSONL_ENV = "RVF_CONTRACT_TIMING_JSONL"
TIMING_REPORT_ENV = "RVF_CONTRACT_TIMING_REPORT"
TIMING_SCRIPT_ENV = "RVF_CONTRACT_TIMING_SCRIPT"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def timing_step(
    *,
    label: str,
    source: str,
    started_ms: int,
    returncode: int,
) -> dict[str, Any]:
    duration_ms = max(0, monotonic_ms() - started_ms)
    return {
        "label": label,
        "source": source,
        "status": "completed" if returncode == 0 else "failed",
        "returncode": returncode,
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3),
    }


def read_shell_timing_steps(path: Path) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if not path.is_file():
        return steps
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        label = payload.get("label")
        duration_ms = payload.get("duration_ms")
        returncode = payload.get("returncode")
        if not isinstance(label, str):
            continue
        try:
            normalized_duration_ms = max(0, int(duration_ms))
            normalized_returncode = int(returncode)
        except (TypeError, ValueError):
            continue
        steps.append(
            {
                "label": label,
                "source": "check_skill_contracts.sh",
                "status": "completed" if normalized_returncode == 0 else "failed",
                "returncode": normalized_returncode,
                "duration_ms": normalized_duration_ms,
                "duration_seconds": round(normalized_duration_ms / 1000, 3),
                "execution_mode": payload.get("execution_mode") or "serial",
            }
        )
    return steps


def timing_group(label: str) -> str:
    if label.startswith("tests: "):
        return "tests"
    if label.startswith("shell syntax: "):
        return "shell syntax"
    if label == "python compile":
        return "python compile"
    if label == "shell script overhead":
        return "shell overhead"
    return "contract preflight"


def add_percentages(
    steps: list[dict[str, Any]],
    wall_duration_ms: int,
    measured_work_ms: int,
) -> list[dict[str, Any]]:
    wall_denominator = max(1, wall_duration_ms)
    work_denominator = max(1, measured_work_ms)
    enriched: list[dict[str, Any]] = []
    for step in steps:
        item = dict(step)
        duration_ms = max(0, int(item.get("duration_ms") or 0))
        item["percentage_of_total"] = round(duration_ms * 100 / wall_denominator, 2)
        item["percentage_of_wall_time"] = round(duration_ms * 100 / wall_denominator, 2)
        item["percentage_of_measured_work"] = round(duration_ms * 100 / work_denominator, 2)
        enriched.append(item)
    return enriched


def group_totals(
    steps: list[dict[str, Any]],
    wall_duration_ms: int,
    measured_work_ms: int,
) -> list[dict[str, Any]]:
    totals: dict[str, int] = {}
    for step in steps:
        label = str(step.get("label") or "")
        totals[timing_group(label)] = totals.get(timing_group(label), 0) + int(
            step.get("duration_ms") or 0
        )
    groups = [
        {
            "name": name,
            "duration_ms": duration_ms,
            "duration_seconds": round(duration_ms / 1000, 3),
            "percentage_of_total": round(duration_ms * 100 / max(1, wall_duration_ms), 2),
            "percentage_of_wall_time": round(duration_ms * 100 / max(1, wall_duration_ms), 2),
            "percentage_of_measured_work": round(
                duration_ms * 100 / max(1, measured_work_ms),
                2,
            ),
        }
        for name, duration_ms in totals.items()
    ]
    return sorted(groups, key=lambda item: int(item["duration_ms"]), reverse=True)


def build_timing_report(
    *,
    started_at: str,
    ended_at: str,
    duration_ms: int,
    returncode: int,
    command: list[str],
    top_level_steps: list[dict[str, Any]],
    shell_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    accounted_steps: list[dict[str, Any]] = []
    preflight_steps = [step for step in top_level_steps if step.get("label") == "preflight"]
    accounted_steps.extend(preflight_steps)
    accounted_steps.extend(shell_steps)

    shell_total_ms = sum(
        int(step.get("duration_ms") or 0)
        for step in top_level_steps
        if step.get("label") == "contract shell script"
    )
    shell_accounted_ms = sum(int(step.get("duration_ms") or 0) for step in shell_steps)
    shell_overhead_ms = max(0, shell_total_ms - shell_accounted_ms)
    if shell_overhead_ms:
        accounted_steps.append(
            {
                "label": "shell script overhead",
                "source": "check_skill_contracts.sh",
                "status": "completed" if returncode == 0 else "failed",
                "returncode": returncode,
                "duration_ms": shell_overhead_ms,
                "duration_seconds": round(shell_overhead_ms / 1000, 3),
            }
        )

    measured_work_ms = sum(int(step.get("duration_ms") or 0) for step in accounted_steps)
    accounted_steps = add_percentages(accounted_steps, duration_ms, measured_work_ms)
    top_level_steps = add_percentages(top_level_steps, duration_ms, measured_work_ms)
    slowest_step = max(
        accounted_steps,
        key=lambda step: int(step.get("duration_ms") or 0),
        default=None,
    )
    return {
        "version": 1,
        "kind": "plugin-contract-timing",
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3),
        "measured_work_duration_ms": measured_work_ms,
        "measured_work_duration_seconds": round(measured_work_ms / 1000, 3),
        "returncode": returncode,
        "command": command,
        "cwd": str(ROOT),
        "steps": accounted_steps,
        "groups": group_totals(accounted_steps, duration_ms, measured_work_ms),
        "slowest_step": slowest_step,
        "top_level_steps": top_level_steps,
    }


def write_timing_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 review-validate-fix plugin 契约检查。")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示底层验证与测试命令输出。")
    parser.add_argument(
        "--timing-report",
        help=(
            "写入 JSON timing report；也可通过 "
            f"{TIMING_REPORT_ENV}=<path> 配置。"
        ),
    )
    args = parser.parse_args()
    report_value = args.timing_report or os.environ.get(TIMING_REPORT_ENV)
    timing_report_path = Path(report_value).expanduser() if report_value else None

    started_at = utc_now()
    overall_started_ms = monotonic_ms()
    top_level_steps: list[dict[str, Any]] = []
    shell_steps: list[dict[str, Any]] = []

    def finish(returncode: int, command: list[str]) -> int:
        if timing_report_path is not None:
            ended_at = utc_now()
            duration_ms = max(0, monotonic_ms() - overall_started_ms)
            report = build_timing_report(
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
                returncode=returncode,
                command=command,
                top_level_steps=top_level_steps,
                shell_steps=shell_steps,
            )
            write_timing_report(timing_report_path, report)
        return returncode

    preflight_started_ms = monotonic_ms()
    if not PLUGIN_SKILL.exists():
        print(f"缺少 plugin skill: {PLUGIN_SKILL}", file=sys.stderr)
        top_level_steps.append(
            timing_step(
                label="preflight",
                source="check_plugin_contracts.py",
                started_ms=preflight_started_ms,
                returncode=2,
            )
        )
        return finish(2, [])
    if not CONTRACT_SCRIPT.exists():
        print(f"缺少契约检查脚本: {CONTRACT_SCRIPT}", file=sys.stderr)
        top_level_steps.append(
            timing_step(
                label="preflight",
                source="check_plugin_contracts.py",
                started_ms=preflight_started_ms,
                returncode=2,
            )
        )
        return finish(2, [])
    top_level_steps.append(
        timing_step(
            label="preflight",
            source="check_plugin_contracts.py",
            started_ms=preflight_started_ms,
            returncode=0,
        )
    )

    command = ["bash", str(CONTRACT_SCRIPT)]
    if args.verbose:
        command.append("--verbose")
    child_env = os.environ.copy()
    child_env.pop(TIMING_REPORT_ENV, None)
    child_env.pop(TIMING_JSONL_ENV, None)
    child_env.pop(TIMING_SCRIPT_ENV, None)
    with tempfile.TemporaryDirectory(prefix="rvf-contract-timing-") as tmp_dir:
        shell_timing_path = Path(tmp_dir) / "check_skill_contracts.steps.jsonl"
        if timing_report_path is not None:
            child_env[TIMING_JSONL_ENV] = str(shell_timing_path)
            child_env[TIMING_SCRIPT_ENV] = str(CONTRACT_SCRIPT)
        shell_started_ms = monotonic_ms()
        if args.verbose:
            completed = subprocess.run(command, cwd=ROOT, text=True, env=child_env)
        else:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                env=child_env,
            )
        top_level_steps.append(
            timing_step(
                label="contract shell script",
                source="check_plugin_contracts.py",
                started_ms=shell_started_ms,
                returncode=completed.returncode,
            )
        )
        shell_steps = read_shell_timing_steps(shell_timing_path)

        if completed.returncode != 0 and not args.verbose:
            print("plugin 契约检查失败", file=sys.stderr)
            if completed.stdout:
                print(completed.stdout, end="", file=sys.stderr)
            if completed.stderr:
                print(completed.stderr, end="", file=sys.stderr)
        elif completed.returncode == 0 and not args.verbose:
            print("plugin 契约检查通过")
        return finish(completed.returncode, command)


if __name__ == "__main__":
    raise SystemExit(main())
