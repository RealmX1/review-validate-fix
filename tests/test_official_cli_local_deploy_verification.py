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
    # 生产部署使用干净源码；worktree 的共享 state 可能含真实 run，不能跟随链接复制进 fixture。
    fixture_source_directory = tmp_path / "isolated-plugin-source-without-runtime-state"
    def exclude_fixture_source_runtime_artifacts(directory, names):
        ignored = installer._bundle_copy_ignore(directory, names)
        if Path(directory) == ROOT / "plugins/review-validate-fix/skills/review-validate-fix":
            ignored.add("state")
        return ignored
    for relative in ("plugins/review-validate-fix", "core", "adapters"):
        shutil.copytree(ROOT / relative, fixture_source_directory / relative,
            ignore=exclude_fixture_source_runtime_artifacts)
    for relative in (".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json", ".rvf-pyroot"):
        destination = fixture_source_directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    home_directory = tmp_path / "home"
    source_commit = "b" * 40
    release = installer.build_developer_marketplace_release(
        source_repository=fixture_source_directory,
        home_directory=home_directory,
        source_commit=source_commit,
        source_commit_count=400,
    )
    active_marketplace_root = installer.activate_developer_marketplace_release(
        release,
        home_directory=home_directory,
    )
    user_paths = installer.initialize_rvf_user_local_data(
        source_repository=fixture_source_directory,
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
    router = plugin_root / "skills/review-validate-fix/scripts/task_snapshot_review_hook.py"
    prompt_hook = plugin_root / "skills/review-validate-fix/scripts/task_snapshot_review_hook.py"
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
            "deployment_source_worktree": str(fixture_source_directory),
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
    assert result["synthetic_hook_smoke"]["Stop"]["status"] == "ok"
    assert result["synthetic_hook_smoke"]["UserPromptSubmit"]["status"] == "ok"
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


def test_verifier_rejects_second_codex_rvf_plugin_registration(tmp_path: Path) -> None:
    """canonical 在场也不够：第二份 RVF 注册必须让 verifier 判红。

    这正是本机长期同时装着两套 RVF 的形状——旧的 `review-validate-fix@local-codex-plugins`
    从 2026-07-21 起一直活着，而只做存在性断言的 verifier 一路绿灯。
    """
    verifier = _load_module("verifier_for_duplicate_codex_test", VERIFIER_PATH)
    fixture = _verification_fixture(tmp_path)
    fixture["official_cli_state"]["codex_plugins"]["installed"].append(
        {
            "pluginId": "review-validate-fix@local-codex-plugins",
            "version": "0.1.0-dev.290+gdeadbeef1234",
            "installed": True,
            "enabled": True,
        }
    )

    with pytest.raises(
        verifier.RegistrationUniquenessError,
        match="Codex 存在多份 RVF 插件注册",
    ) as raised:
        verifier.verify_local_deploy(
            installation_receipt_path=fixture["receipt_path"],
            hooks_path=fixture["hooks_path"],
            home_directory=fixture["home_directory"],
            official_cli_state=fixture["official_cli_state"],
        )
    # 失败信息必须自带可执行清理命令，否则又退化成「写在文档里的人工待办」。
    assert (
        "codex plugin remove review-validate-fix@local-codex-plugins --json"
        in str(raised.value)
    )


def test_verifier_rejects_second_claude_rvf_plugin_registration(tmp_path: Path) -> None:
    verifier = _load_module("verifier_for_duplicate_claude_test", VERIFIER_PATH)
    fixture = _verification_fixture(tmp_path)
    fixture["official_cli_state"]["claude_plugins"].append(
        {
            "id": "review-validate-fix@review-validate-fix-local",
            "version": "0.1.0-dev.290+gdeadbeef1234",
            "scope": "user",
            "enabled": True,
            "installPath": str(tmp_path / "stale-claude-cache"),
        }
    )

    with pytest.raises(
        verifier.RegistrationUniquenessError,
        match="Claude 存在多份 RVF 插件注册",
    ) as raised:
        verifier.verify_local_deploy(
            installation_receipt_path=fixture["receipt_path"],
            hooks_path=fixture["hooks_path"],
            home_directory=fixture["home_directory"],
            official_cli_state=fixture["official_cli_state"],
        )
    assert (
        "claude plugin uninstall review-validate-fix@review-validate-fix-local"
        " --scope user"
    ) in str(raised.value)


def test_verifier_rejects_second_codex_plugin_cache_payload_tree(
    tmp_path: Path,
) -> None:
    """注册清干净了、cache 树还在磁盘上，同样必须判红。

    旧会话 rollout 会把 skill 根与 hook 命令的**绝对路径**固化下来；只要那棵树还在，它就还会
    被加载。而 `codex plugin remove` 是否连带删 cache 并不确定，所以必须独立断言。
    """
    verifier = _load_module("verifier_for_duplicate_cache_test", VERIFIER_PATH)
    fixture = _verification_fixture(tmp_path)
    stale_cache_root = (
        fixture["home_directory"]
        / ".codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0"
    )
    stale_cache_root.mkdir(parents=True)

    with pytest.raises(
        verifier.RegistrationUniquenessError,
        match="Codex plugin cache 里存在多份 RVF payload 树",
    ):
        verifier.verify_local_deploy(
            installation_receipt_path=fixture["receipt_path"],
            hooks_path=fixture["hooks_path"],
            home_directory=fixture["home_directory"],
            official_cli_state=fixture["official_cli_state"],
        )


def test_registration_uniqueness_failure_exits_with_dedicated_code_not_generic_verification_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册唯一性失败必须用**专属退出码**，与其余验证失败分开。

    调用方（`scripts/rvf_local_deploy.py`）是以子进程方式跑 verifier 的，只看得见退出码；两类
    失败的处置正好相反——其余失败要回滚，这一类**绝不能**回滚（回滚会移除刚装好的 canonical，
    只留下残留那份）。退出码是这个区别唯一过得了进程边界的载体。
    """
    verifier = _load_module("verifier_for_uniqueness_exit_code_test", VERIFIER_PATH)
    receipt_argv = ["--installation-receipt", str(tmp_path / "receipt.json")]

    def _raise_registration_uniqueness_failure(**_kwargs: object):
        raise verifier.RegistrationUniquenessError("Codex 存在多份 RVF 插件注册: [...]")

    def _raise_generic_verification_failure(**_kwargs: object):
        raise verifier.VerificationError("Codex plugin version 不匹配")

    monkeypatch.setattr(
        verifier, "verify_local_deploy", _raise_registration_uniqueness_failure
    )
    assert verifier.main(receipt_argv) == verifier.REGISTRATION_UNIQUENESS_EXIT_CODE

    monkeypatch.setattr(
        verifier, "verify_local_deploy", _raise_generic_verification_failure
    )
    assert verifier.main(receipt_argv) == 2
    assert verifier.REGISTRATION_UNIQUENESS_EXIT_CODE != 2


def test_verifier_registration_uniqueness_exit_code_matches_deploy_runner_routing_constant() -> None:
    """两个文件各写一份退出码常量，漂移了就没人会发现——这条断言就是那个防漂移锚。"""
    verifier = _load_module("verifier_for_exit_code_parity_test", VERIFIER_PATH)
    deploy_runner = _load_module(
        "deploy_runner_for_exit_code_parity_test",
        ROOT / "scripts/rvf_local_deploy.py",
    )

    assert (
        deploy_runner.OFFICIAL_CLI_VERIFIER_REGISTRATION_UNIQUENESS_EXIT_CODE
        == verifier.REGISTRATION_UNIQUENESS_EXIT_CODE
    )
