"""RVF 用户本机可变材料及 reviewer 配置的目录解析。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


RVF_USER_LOCAL_DATA_ROOT_ENVIRONMENT_VARIABLE = "RVF_USER_LOCAL_DATA_ROOT"
RVF_RUNTIME_LOG_ROOT_ENVIRONMENT_VARIABLE = "CODEX_RVF_LOG_ROOT"
RVF_RUNTIME_STATE_DIR_ENVIRONMENT_VARIABLE = "CODEX_RVF_STATE_DIR"


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
