#!/usr/bin/env python3
"""Provider 健康探测的 host-中性内核（S10f：从共享审查引擎抽出）。

承载「跑一条 provider 健康/认证探测命令、判定 ok/failed、组织结果记录」的可复用中性
机制：``ProviderHealthRequirement`` 值对象 + 通用探测 runner + 两个 subprocess 输出整形
工具。**完全 host-agnostic**：runner 只执行 requirement 携带的 ``command`` 元组，并用
requirement 携带的 ``output_failure_predicate`` 判定输出健康——它从不命名任何 host
（codex 的 ``codex login status`` 命令与 auth-marker predicate 由 codex adapter 注入构造，
见 ``adapters/codex/codex_provider_login.py``）。

stdlib-only 叶子：不 import 任何兄弟 core 模块 / adapter；唯一子进程是「执行注入进来的
探测命令」——非 host-agent 执行（``codex exec`` / ``claude … -p`` / host SDK），符合 core
子进程守卫。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ProviderHealthRequirement:
    provider: str
    reason: str
    command: tuple[str, ...]
    remediation: str
    # 输出健康判定谓词（host-specific，由 adapter 注入）：returncode==0 仍可能 auth 不健康
    # （如 codex「session expired」但退出码为 0 的边界），命中即判 failed。None=只看 returncode。
    # core 借此保持 host-agnostic——不在本模块命名任何 host。
    output_failure_predicate: Callable[[str], bool] | None = None


def command_output_text(stdout: str | None, stderr: str | None) -> str:
    return "\n".join(part for part in (stdout or "", stderr or "") if part).strip()


def subprocess_output_text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_provider_health_requirement(
    requirement: ProviderHealthRequirement,
    timeout_seconds: float,
) -> dict[str, Any]:
    command = list(requirement.command)
    record: dict[str, Any] = {
        "provider": requirement.provider,
        "reason": requirement.reason,
        "command": command,
        "remediation": requirement.remediation,
        "status": "failed",
    }
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        record.update(
            {
                "returncode": None,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
                "failure_reason": "command_missing",
            }
        )
        return record
    except subprocess.TimeoutExpired as exc:
        record.update(
            {
                "returncode": None,
                "stdout": subprocess_output_text(exc.stdout),
                "stderr": subprocess_output_text(exc.stderr),
                "failure_reason": "timeout",
                "timeout_seconds": timeout_seconds,
            }
        )
        return record
    except Exception as exc:
        record.update(
            {
                "returncode": None,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
                "failure_reason": "error",
            }
        )
        return record

    output = command_output_text(completed.stdout, completed.stderr)
    failed = completed.returncode != 0
    if requirement.output_failure_predicate is not None and requirement.output_failure_predicate(output):
        failed = True
    record.update(
        {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "status": "failed" if failed else "ok",
            "failure_reason": "nonzero_or_auth_unhealthy" if failed else None,
        }
    )
    return record
