#!/usr/bin/env python3
"""Codex provider 登录 / 二进制发现缝（adapters/codex 的 provider-login 维）。

S10f：从共享审查引擎（``codex_stop_review_validate_fix.py``）抽出的 **Codex 专属**
provider 健康接入——codex 二进制发现 (``codex_bin``)、codex auth 失败输出 marker 判定
(``codex_login_output_indicates_failure``)、codex agent_id 识别 (``is_codex_agent_id``)、
以及 ``CODEX_RVF_AUTO_CODEX_LOGIN=1`` 时后台拉起 ``codex login``
(``maybe_start_codex_login``)。

host-specific 实现：只依赖 stdlib 与本文件自带的 trivial ``is_truthy`` 副本（与引擎同源，
本仓既有惯例 = 每个 adapter 脚本自含这些 1-3 行 helper，见
``codex_gui_fork_app_server_bridge``），**刻意不反向 import 共享审查引擎**，以保持
``adapters → engine`` 零依赖边、无循环。

中性 provider 健康机制（``ProviderHealthRequirement`` / 探测 runner）住
``core/provider_health/provider_health_guard.py``；引擎侧 ``provider_health_requirements``
用本模块 ``codex_bin`` 构造 codex ``login status`` 探测命令，并把
``codex_login_output_indicates_failure`` 作为 requirement 的 ``output_failure_predicate``
注入，使 core runner 保持 host-agnostic。
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any


# ── 自带的 trivial 工具副本（与引擎同源；本仓既有惯例 = 每个 adapter 自含，避免
#    adapters → engine import 造成循环）。 ──
def is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def codex_bin() -> str:
    return os.environ.get("CODEX_RVF_CODEX_BIN", "codex")


def is_codex_agent_id(agent_id: str | None) -> bool:
    if agent_id is None:
        return False
    normalized = agent_id.strip().lower()
    return (
        normalized in {"codex", "codex-cli", "openai-codex"}
        or normalized.startswith("codex:")
        or "codex" in re.split(r"[^a-z0-9]+", normalized)
    )


def codex_login_output_indicates_failure(output: str) -> bool:
    normalized = output.strip().lower()
    if not normalized:
        return False
    failure_markers = (
        "not logged in",
        "not authenticated",
        "logged out",
        "login expired",
        "session expired",
        "expired session",
        "authentication expired",
        "auth expired",
        "invalid credentials",
        "credential expired",
        "token expired",
    )
    return any(marker in normalized for marker in failure_markers)


def maybe_start_codex_login(ledger: Any) -> dict[str, Any] | None:
    """``CODEX_RVF_AUTO_CODEX_LOGIN`` 为真时，后台拉起 ``codex login`` 并把日志落进 run
    artifacts；返回启动结果 dict（或开关关闭时 None）。``ledger`` 鸭子类型，只用
    ``artifacts_dir``（避免反向 import core.run_ledger 造成 adapters→core 运行时耦合）。"""
    if not is_truthy(os.environ.get("CODEX_RVF_AUTO_CODEX_LOGIN")):
        return None

    log_path = ledger.artifacts_dir / "codex-login.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("ab") as log_file:
            process = subprocess.Popen(
                [codex_bin(), "login"],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
    except Exception as exc:
        return {
            "started": False,
            "error": f"{type(exc).__name__}: {exc}",
            "command": [codex_bin(), "login"],
            "log_path": str(log_path),
        }

    return {
        "started": True,
        "pid": process.pid,
        "command": [codex_bin(), "login"],
        "log_path": str(log_path),
    }
