from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "scripts/install_to_codex.py"
VERIFIER_PATH = (
    ROOT
    / "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _verification_fixture(tmp_path: Path):
    installer = _load_module("installer_for_verifier_tests", INSTALLER_PATH)
    home_directory = tmp_path / "home"
    source_commit = "b" * 40
    release = installer.build_developer_marketplace_release(
        source_repository=ROOT,
        home_directory=home_directory,
        source_commit=source_commit,
        source_commit_count=400,
    )
    active_marketplace_root = installer.activate_developer_marketplace_release(
        release,
        home_directory=home_directory,
    )
    user_paths = installer.initialize_rvf_user_local_data(
        source_repository=ROOT,
        home_directory=home_directory,
    )
    plugin_root = active_marketplace_root / "plugins/review-validate-fix"
    codex_cache_root = (
        home_directory
        / ".codex/plugins/cache/review-validate-fix-local-main/review-validate-fix"
        / release.generated_plugin_version
    )
    claude_cache_root = (
        home_directory
        / ".claude/plugins/cache/review-validate-fix-local-main/review-validate-fix"
        / release.generated_plugin_version
    )
    codex_cache_root.parent.mkdir(parents=True, exist_ok=True)
    claude_cache_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(plugin_root, codex_cache_root)
    shutil.copytree(plugin_root, claude_cache_root)
    hooks_path = home_directory / ".codex/hooks.json"
    router = plugin_root / "skills/review-validate-fix/scripts/stop_hook_router.py"
    prompt_hook = plugin_root / "skills/review-validate-fix/scripts/rvf_user_prompt_submit.py"
    environment_prefix = (
        f"RVF_USER_LOCAL_DATA_ROOT={user_paths.user_local_data_root} "
        f"CODEX_RVF_LOG_ROOT={user_paths.run_logs_root} "
        f"CODEX_RVF_STATE_DIR={user_paths.runtime_state_root} "
        f"CODEX_RVF_INSTALLED_SKILL_DIR={plugin_root / 'skills/review-validate-fix'} "
    )
    _write_json(
        hooks_path,
        {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": environment_prefix + f"python3 {router}",
                            }
                        ]
                    }
                ],
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": environment_prefix + f"python3 {prompt_hook}",
                            }
                        ]
                    }
                ],
            }
        },
    )
    receipt_path = user_paths.deployment_receipts_root / "latest-official-cli-installation.json"
    _write_json(
        receipt_path,
        {
            "schema_version": 2,
            "status": "installed",
            "local_main_worktree": str(ROOT),
            "local_main_commit": source_commit,
            "generated_plugin_version": release.generated_plugin_version,
            "payload_digest": release.payload_digest,
            "release_root": str(release.release_root),
            "active_marketplace_root": str(active_marketplace_root),
            "marketplace_identifier": "review-validate-fix-local-main",
            "plugin_selector": "review-validate-fix@review-validate-fix-local-main",
            "user_local_data_root": str(user_paths.user_local_data_root),
            "rollback_status": "not_required",
            "official_cli_versions": {
                "codex": "codex-cli test",
                "claude": "claude-code test",
            },
        },
    )
    official_cli_state = {
        "codex_marketplaces": {
            "marketplaces": [
                {
                    "name": "review-validate-fix-local-main",
                    "root": str(active_marketplace_root),
                }
            ]
        },
        "codex_plugins": {
            "installed": [
                {
                    "pluginId": "review-validate-fix@review-validate-fix-local-main",
                    "version": release.generated_plugin_version,
                    "installed": True,
                    "enabled": True,
                }
            ]
        },
        "claude_marketplaces": [
            {
                "name": "review-validate-fix-local-main",
                "path": str(active_marketplace_root),
            }
        ],
        "claude_plugins": [
            {
                "id": "review-validate-fix@review-validate-fix-local-main",
                "version": release.generated_plugin_version,
                "scope": "user",
                "enabled": True,
                "installPath": str(claude_cache_root),
            }
        ],
    }
    return {
        "installer": installer,
        "home_directory": home_directory,
        "receipt_path": receipt_path,
        "hooks_path": hooks_path,
        "official_cli_state": official_cli_state,
        "generated_plugin_version": release.generated_plugin_version,
    }


def test_installer_and_verifier_digests_ignore_python_bytecode_but_detect_payload_changes(
    tmp_path: Path,
) -> None:
    installer = _load_module("installer_for_digest_tests", INSTALLER_PATH)
    verifier = _load_module("verifier_for_digest_tests", VERIFIER_PATH)
    payload_root = tmp_path / "payload"
    payload_file = payload_root / "package/runtime.py"
    payload_file.parent.mkdir(parents=True)
    payload_file.write_text("VALUE = 1\n", encoding="utf-8")

    initial_installer_digest = installer.deployment_file_digest(payload_root)
    initial_verifier_digest = verifier._directory_digest(payload_root)
    assert initial_installer_digest == initial_verifier_digest

    bytecode_cache_root = payload_root / "package/__pycache__"
    bytecode_cache_root.mkdir()
    (bytecode_cache_root / "runtime.cpython-313.pyc").write_bytes(b"cached bytecode")
    (bytecode_cache_root / "runtime-cache-marker.txt").write_text(
        "runtime cache\n",
        encoding="utf-8",
    )
    (payload_root / "standalone.pyc").write_bytes(b"standalone bytecode")

    assert installer.deployment_file_digest(payload_root) == initial_installer_digest
    assert verifier._directory_digest(payload_root) == initial_verifier_digest

    payload_file.write_text("VALUE = 2\n", encoding="utf-8")
    changed_installer_digest = installer.deployment_file_digest(payload_root)
    changed_verifier_digest = verifier._directory_digest(payload_root)
    assert changed_installer_digest == changed_verifier_digest
    assert changed_installer_digest != initial_installer_digest


def test_verifier_accepts_official_cli_installation_and_shared_user_state(
    tmp_path: Path,
) -> None:
    verifier = _load_module("verifier_for_official_cli_tests", VERIFIER_PATH)
    fixture = _verification_fixture(tmp_path)

    result = verifier.verify_local_deploy(
        installation_receipt_path=fixture["receipt_path"],
        hooks_path=fixture["hooks_path"],
        home_directory=fixture["home_directory"],
        official_cli_state=fixture["official_cli_state"],
    )

    assert result["status"] == "ok"
    assert result["generated_plugin_version"] == fixture["generated_plugin_version"]
    assert result["official_cli"]["codex"]["installed"] is True
    assert result["official_cli"]["claude"]["installed"] is True
    assert result["cache_payloads_match_active_release"] is True
    assert result["synthetic_hook_smoke"]["codex_stop_router"]["status"] == "ok"
    assert result["synthetic_hook_smoke"]["codex_user_prompt_submit"]["status"] == "ok"
    assert result["official_cli_versions"]["codex"] == "codex-cli test"


def test_verifier_rejects_missing_official_codex_plugin(tmp_path: Path) -> None:
    verifier = _load_module("verifier_for_missing_codex_test", VERIFIER_PATH)
    fixture = _verification_fixture(tmp_path)
    fixture["official_cli_state"]["codex_plugins"] = {"installed": []}

    with pytest.raises(verifier.VerificationError, match="Codex.*plugin selector"):
        verifier.verify_local_deploy(
            installation_receipt_path=fixture["receipt_path"],
            hooks_path=fixture["hooks_path"],
            home_directory=fixture["home_directory"],
            official_cli_state=fixture["official_cli_state"],
        )
