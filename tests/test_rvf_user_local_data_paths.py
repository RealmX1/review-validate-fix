from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import rvf_user_local_data_paths
from core.run_ledger import run_ledger


def test_user_local_data_paths_default_to_home_scoped_rvf_root(tmp_path: Path) -> None:
    paths = rvf_user_local_data_paths.resolved_rvf_user_local_data_paths(
        environment={},
        home_directory=tmp_path,
    )

    assert paths.user_local_data_root == tmp_path / ".rvf/user-local-data"
    assert paths.reviewer_registry_path == (
        tmp_path
        / ".rvf/user-local-data/reviewer-configuration/reviewer-registry.json"
    )
    assert paths.runtime_state_root == tmp_path / ".rvf/user-local-data/runtime-state"
    assert paths.run_logs_root == tmp_path / ".rvf/user-local-data/run-logs"
    assert paths.deployment_receipts_root == (
        tmp_path / ".rvf/user-local-data/deployment-receipts"
    )


def test_user_local_data_root_environment_override_is_expanded(
    tmp_path: Path,
) -> None:
    override_root = tmp_path / "explicit-user-local-data"

    paths = rvf_user_local_data_paths.resolved_rvf_user_local_data_paths(
        environment={"RVF_USER_LOCAL_DATA_ROOT": str(override_root)},
        home_directory=tmp_path / "unused-home",
    )

    assert paths.user_local_data_root == override_root


def test_runtime_log_root_preserves_existing_explicit_environment_precedence(
    tmp_path: Path,
) -> None:
    explicit_log_root = tmp_path / "explicit-log-root"
    explicit_state_root = tmp_path / "explicit-state-root"

    resolved = rvf_user_local_data_paths.resolved_rvf_runtime_log_root(
        environment={
            "CODEX_RVF_LOG_ROOT": str(explicit_log_root),
            "CODEX_RVF_STATE_DIR": str(explicit_state_root),
        },
        home_directory=tmp_path,
    )

    assert resolved == explicit_log_root


def test_reviewer_registry_uses_bundled_default_until_user_copy_exists(
    tmp_path: Path,
) -> None:
    bundled_registry = tmp_path / "plugin/config/reviewer-registry.json"
    bundled_registry.parent.mkdir(parents=True)
    bundled_registry.write_text("{}\n", encoding="utf-8")

    without_user_copy = rvf_user_local_data_paths.resolved_rvf_reviewer_registry_path(
        bundled_registry,
        environment={},
        home_directory=tmp_path,
    )
    assert without_user_copy == bundled_registry

    user_registry = (
        tmp_path
        / ".rvf/user-local-data/reviewer-configuration/reviewer-registry.json"
    )
    user_registry.parent.mkdir(parents=True)
    user_registry.write_text("{\"user\": true}\n", encoding="utf-8")

    with_user_copy = rvf_user_local_data_paths.resolved_rvf_reviewer_registry_path(
        bundled_registry,
        environment={},
        home_directory=tmp_path,
    )
    assert with_user_copy == user_registry


def test_run_ledger_default_moves_out_of_replaceable_plugin_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("CODEX_RVF_LOG_ROOT", raising=False)
    monkeypatch.delenv("CODEX_RVF_STATE_DIR", raising=False)
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "rvf-user-data"))

    assert run_ledger.log_root() == tmp_path / "rvf-user-data/run-logs"


def test_run_ledger_explicit_log_root_still_wins(
    tmp_path: Path,
    monkeypatch,
) -> None:
    explicit_log_root = tmp_path / "explicit-log-root"
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "rvf-user-data"))
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(explicit_log_root))
    monkeypatch.setenv("CODEX_RVF_STATE_DIR", str(tmp_path / "explicit-state-root"))

    assert run_ledger.log_root() == explicit_log_root
