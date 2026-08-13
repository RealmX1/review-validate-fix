"""RVF 用户本机可变数据的唯一默认路径解析器。

**只有一个状态根**：``$RVF_USER_LOCAL_DATA_ROOT``，默认 ``~/.rvf/user-local-data``。
``CODEX_RVF_LOG_ROOT`` 与 ``CODEX_RVF_STATE_DIR`` 不是两个并列的根，而是这一个根
下两棵子树的直接覆盖（分别对应 ``run-logs/`` 与 ``runtime-state/``）；
``scripts/install_to_codex.py`` 把它们装成不同目录正是因为如此，两者必须同源派生。
从属关系写在下面两个 resolver 里，任何调用方都不得自行解析。

历史教训：Claude host 的 hook shim 曾用一行 ``env.setdefault`` 把
``CODEX_RVF_LOG_ROOT`` 钉在 ``~/.claude/rvf``，于是「走 hook 的一切」与「shell 里
跑的 prepare/seal/reviewer/rvf-agent」各写一棵树，且没有任何东西比较过它们：
``/rvf-land`` 封窗写进的水位与铸 run 读的水位不同根 ⇒ 已封窗的轮次被反复重新
派发；reviewer 又在另一个根里查不到 lease。结构性守卫见
``tests/test_rvf_user_local_data_paths.py``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


RVF_USER_LOCAL_DATA_ROOT_ENVIRONMENT_VARIABLE = "RVF_USER_LOCAL_DATA_ROOT"
RVF_RUNTIME_LOG_ROOT_ENVIRONMENT_VARIABLE = "CODEX_RVF_LOG_ROOT"
RVF_RUNTIME_STATE_DIR_ENVIRONMENT_VARIABLE = "CODEX_RVF_STATE_DIR"
RVF_SESSION_HOOK_STATE_DIR_ENVIRONMENT_VARIABLE = "CODEX_RVF_SESSION_HOOK_STATE_DIR"
SESSION_HOOK_STATE_SUBDIRECTORY_NAME = "session-hook"


@dataclass(frozen=True)
class RvfUserLocalDataPaths:
    user_local_data_root: Path
    reviewer_configuration_root: Path
    reviewer_registry_path: Path
    runtime_state_root: Path
    run_logs_root: Path
    deployment_receipts_root: Path


def resolved_rvf_user_local_data_paths(
    *,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
) -> RvfUserLocalDataPaths:
    effective_environment = os.environ if environment is None else environment
    effective_home_directory = Path.home() if home_directory is None else home_directory
    configured_root = effective_environment.get(
        RVF_USER_LOCAL_DATA_ROOT_ENVIRONMENT_VARIABLE,
        "",
    ).strip()
    user_local_data_root = (
        Path(configured_root).expanduser()
        if configured_root
        else effective_home_directory.expanduser() / ".rvf/user-local-data"
    )
    reviewer_configuration_root = user_local_data_root / "reviewer-configuration"
    return RvfUserLocalDataPaths(
        user_local_data_root=user_local_data_root,
        reviewer_configuration_root=reviewer_configuration_root,
        reviewer_registry_path=reviewer_configuration_root / "reviewer-registry.json",
        runtime_state_root=user_local_data_root / "runtime-state",
        run_logs_root=user_local_data_root / "run-logs",
        deployment_receipts_root=user_local_data_root / "deployment-receipts",
    )


def resolved_rvf_runtime_log_root(
    *,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
) -> Path:
    effective_environment = os.environ if environment is None else environment
    for environment_variable_name in (
        RVF_RUNTIME_LOG_ROOT_ENVIRONMENT_VARIABLE,
        RVF_RUNTIME_STATE_DIR_ENVIRONMENT_VARIABLE,
    ):
        configured_root = effective_environment.get(environment_variable_name, "").strip()
        if configured_root:
            return Path(configured_root).expanduser()
    return resolved_rvf_user_local_data_paths(
        environment=effective_environment,
        home_directory=home_directory,
    ).run_logs_root


def resolved_rvf_session_hook_state_dir(
    *,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
) -> Path:
    """session-hook 状态目录的**唯一**裁决点。

    历史上 ``stop_hook_router.state_dir()`` 与 ``stop_review_validate_fix.
    session_hook_state_dir()`` 是同一概念的两份实现，且判据不同：前者在
    ``CODEX_RVF_STATE_DIR`` 缺失时会回落到 ``CODEX_RVF_LOG_ROOT``，后者不会。
    「只设 LOG_ROOT、不设 STATE_DIR」恰好是 Claude host shim 制造过的组合，
    于是两者会指向不同目录（当时未引爆，因为 router 在该路径上未被走到）。

    这里统一采用**不回落到 LOG_ROOT** 的语义：session-hook 是运行时状态，
    不是 run 日志。若回落到 LOG_ROOT，reviewer 子进程 source 过 review-env.sh
    之后（其中 ``CODEX_RVF_LOG_ROOT`` 指向该 run 的日志根）会把会话状态写进
    某一个 run 的日志目录里去。
    """

    effective_environment = os.environ if environment is None else environment
    explicit_state_dir = effective_environment.get(
        RVF_SESSION_HOOK_STATE_DIR_ENVIRONMENT_VARIABLE,
        "",
    ).strip()
    if explicit_state_dir:
        return Path(explicit_state_dir).expanduser()

    configured_runtime_state_root = effective_environment.get(
        RVF_RUNTIME_STATE_DIR_ENVIRONMENT_VARIABLE,
        "",
    ).strip()
    if configured_runtime_state_root:
        return Path(configured_runtime_state_root).expanduser() / SESSION_HOOK_STATE_SUBDIRECTORY_NAME

    return resolved_rvf_user_local_data_paths(
        environment=effective_environment,
        home_directory=home_directory,
    ).runtime_state_root / SESSION_HOOK_STATE_SUBDIRECTORY_NAME


def resolved_rvf_reviewer_registry_path(
    bundled_reviewer_registry_path: Path,
    *,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
) -> Path:
    user_registry_path = resolved_rvf_user_local_data_paths(
        environment=environment,
        home_directory=home_directory,
    ).reviewer_registry_path
    return user_registry_path if user_registry_path.is_file() else bundled_reviewer_registry_path
