from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install_to_codex.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "install_to_codex_for_official_cli_tests",
        INSTALLER,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _minimal_source_repository(tmp_path: Path) -> Path:
    source_repository = tmp_path / "review-validate-fix-local-main"
    plugin_root = source_repository / "plugins/review-validate-fix"
    _write_json(
        source_repository / ".agents/plugins/marketplace.json",
        {
            "name": "review-validate-fix-local-main",
            "plugins": [
                {
                    "name": "review-validate-fix",
                    "source": {
                        "source": "local",
                        "path": "plugins/review-validate-fix",
                    },
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                }
            ],
        },
    )
    _write_json(
        source_repository / ".claude-plugin/marketplace.json",
        {
            "name": "review-validate-fix-local-main",
            "plugins": [
                {
                    "name": "review-validate-fix",
                    "source": "./plugins/review-validate-fix",
                }
            ],
        },
    )
    for manifest_relative_path in (
        ".codex-plugin/plugin.json",
        ".claude-plugin/plugin.json",
    ):
        _write_json(
            plugin_root / manifest_relative_path,
            {"name": "review-validate-fix", "version": "0.1.0"},
        )
    skill_root = plugin_root / "skills/review-validate-fix"
    (skill_root / "scripts").mkdir(parents=True)
    (skill_root / "scripts/runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
    (skill_root / "scripts/install_to_codex.py").write_text(
        "dev only\n",
        encoding="utf-8",
    )
    (skill_root / "SKILL.md").write_text("# RVF\n", encoding="utf-8")
    _write_json(skill_root / "config/reviewer-registry.json", {"local": False})
    (skill_root / "state").mkdir()
    (skill_root / "state/runtime.json").write_text("{}\n", encoding="utf-8")
    for package_name in ("core", "adapters"):
        package_root = source_repository / package_name
        package_root.mkdir(parents=True)
        (package_root / "__init__.py").write_text("\n", encoding="utf-8")
    (source_repository / ".rvf-pyroot").write_text("\n", encoding="utf-8")
    return source_repository


class RecordingOfficialCliRunner:
    def __init__(
        self,
        *,
        codex_marketplaces: list[dict[str, object]],
        codex_installed_plugins: list[dict[str, object]],
        claude_marketplaces: list[dict[str, object]],
        claude_installed_plugins: list[dict[str, object]],
    ) -> None:
        self.commands: list[list[str]] = []
        self.codex_marketplaces = codex_marketplaces
        self.codex_installed_plugins = codex_installed_plugins
        self.claude_marketplaces = claude_marketplaces
        self.claude_installed_plugins = claude_installed_plugins

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        executable_name = Path(command[0]).name
        if executable_name == "codex" and command[1:] == [
            "plugin",
            "marketplace",
            "list",
            "--json",
        ]:
            stdout = json.dumps({"marketplaces": self.codex_marketplaces})
        elif executable_name == "codex" and command[1:] == [
            "plugin",
            "list",
            "--json",
        ]:
            stdout = json.dumps({"installed": self.codex_installed_plugins})
        elif executable_name == "claude" and command[1:] == [
            "plugin",
            "marketplace",
            "list",
            "--json",
        ]:
            stdout = json.dumps(self.claude_marketplaces)
        elif executable_name == "claude" and command[1:] == [
            "plugin",
            "list",
            "--json",
        ]:
            stdout = json.dumps(self.claude_installed_plugins)
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


class FailOnceDuringClaudeUpdateRunner(RecordingOfficialCliRunner):
    def __init__(self, *, active_marketplace_root: Path) -> None:
        plugin_selector = "review-validate-fix@review-validate-fix-local-main"
        super().__init__(
            codex_marketplaces=[
                {
                    "name": "review-validate-fix-local-main",
                    "root": str(active_marketplace_root),
                }
            ],
            codex_installed_plugins=[
                {"pluginId": plugin_selector, "installed": True}
            ],
            claude_marketplaces=[
                {
                    "name": "review-validate-fix-local-main",
                    "path": str(active_marketplace_root),
                }
            ],
            claude_installed_plugins=[{"id": plugin_selector, "enabled": True}],
        )
        self.failed = False

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        if (
            not self.failed
            and Path(command[0]).name == "claude"
            and command[1:3] == ["plugin", "update"]
        ):
            self.failed = True
            self.commands.append(command)
            return subprocess.CompletedProcess(
                command,
                9,
                stdout="",
                stderr="simulated Claude update failure",
            )
        return super().__call__(command)


def test_generated_developer_plugin_version_is_commit_addressed() -> None:
    installer = _load_installer()

    assert installer.generated_developer_plugin_version(
        base_version="0.1.0",
        commit_count=317,
        full_commit="abc123def4567890abc123def4567890abc123de",
    ) == "0.1.0-dev.317+gabc123def456"


def test_build_release_is_self_contained_and_excludes_mutable_state(
    tmp_path: Path,
) -> None:
    installer = _load_installer()
    source_repository = _minimal_source_repository(tmp_path)
    full_commit = "abc123def4567890abc123def4567890abc123de"

    release = installer.build_developer_marketplace_release(
        source_repository=source_repository,
        home_directory=tmp_path / "home",
        source_commit=full_commit,
        source_commit_count=317,
    )

    release_root = release.release_root
    plugin_root = release_root / "plugins/review-validate-fix"
    assert release.generated_plugin_version == "0.1.0-dev.317+gabc123def456"
    assert (plugin_root / "core/__init__.py").is_file()
    assert (plugin_root / "adapters/__init__.py").is_file()
    assert (plugin_root / ".rvf-pyroot").is_file()
    assert not (plugin_root / "skills/review-validate-fix/state").exists()
    assert not (
        plugin_root / "skills/review-validate-fix/config/reviewer-registry.json"
    ).exists()
    assert not (
        plugin_root / "skills/review-validate-fix/scripts/install_to_codex.py"
    ).exists()
    for manifest_relative_path in (
        ".codex-plugin/plugin.json",
        ".claude-plugin/plugin.json",
    ):
        manifest = json.loads(
            (plugin_root / manifest_relative_path).read_text(encoding="utf-8")
        )
        assert manifest["version"] == release.generated_plugin_version
    codex_marketplace = json.loads(
        (release_root / ".agents/plugins/marketplace.json").read_text(
            encoding="utf-8"
        )
    )
    claude_marketplace = json.loads(
        (release_root / ".claude-plugin/marketplace.json").read_text(
            encoding="utf-8"
        )
    )
    assert codex_marketplace["name"] == "review-validate-fix-local-main"
    assert codex_marketplace["plugins"][0]["source"]["path"] == (
        "./plugins/review-validate-fix"
    )
    assert claude_marketplace["name"] == "review-validate-fix-local-main"
    assert claude_marketplace["plugins"][0]["version"] == (
        release.generated_plugin_version
    )


def test_official_cli_first_install_uses_marketplace_add_and_plugin_add(
    tmp_path: Path,
) -> None:
    installer = _load_installer()
    active_marketplace_root = tmp_path / "active-marketplace"
    runner = RecordingOfficialCliRunner(
        codex_marketplaces=[],
        codex_installed_plugins=[],
        claude_marketplaces=[],
        claude_installed_plugins=[],
    )

    installer.install_developer_marketplace_with_official_clis(
        active_marketplace_root=active_marketplace_root,
        codex_executable=Path("/opt/test/bin/codex"),
        claude_executable=Path("/opt/test/bin/claude"),
        command_runner=runner,
    )

    command_tails = [command[1:] for command in runner.commands]
    assert [
        "plugin",
        "marketplace",
        "add",
        str(active_marketplace_root),
        "--json",
    ] in command_tails
    assert [
        "plugin",
        "add",
        "review-validate-fix@review-validate-fix-local-main",
        "--json",
    ] in command_tails
    assert [
        "plugin",
        "marketplace",
        "add",
        str(active_marketplace_root),
        "--scope",
        "user",
    ] in command_tails
    assert [
        "plugin",
        "install",
        "review-validate-fix@review-validate-fix-local-main",
        "--scope",
        "user",
    ] in command_tails


def test_official_cli_update_uses_codex_remove_add_and_claude_update(
    tmp_path: Path,
) -> None:
    installer = _load_installer()
    active_marketplace_root = tmp_path / "active-marketplace"
    plugin_selector = "review-validate-fix@review-validate-fix-local-main"
    runner = RecordingOfficialCliRunner(
        codex_marketplaces=[
            {
                "name": "review-validate-fix-local-main",
                "root": str(active_marketplace_root),
            }
        ],
        codex_installed_plugins=[{"pluginId": plugin_selector, "installed": True}],
        claude_marketplaces=[
            {
                "name": "review-validate-fix-local-main",
                "path": str(active_marketplace_root),
            }
        ],
        claude_installed_plugins=[{"id": plugin_selector, "enabled": True}],
    )

    installer.install_developer_marketplace_with_official_clis(
        active_marketplace_root=active_marketplace_root,
        codex_executable=Path("/opt/test/bin/codex"),
        claude_executable=Path("/opt/test/bin/claude"),
        command_runner=runner,
    )

    command_tails = [command[1:] for command in runner.commands]
    assert ["plugin", "remove", plugin_selector, "--json"] in command_tails
    assert ["plugin", "add", plugin_selector, "--json"] in command_tails
    assert [
        "plugin",
        "marketplace",
        "update",
        "review-validate-fix-local-main",
    ] in command_tails
    assert ["plugin", "update", plugin_selector, "--scope", "user"] in command_tails


def test_official_cli_install_removes_noncanonical_rvf_plugin_registrations(
    tmp_path: Path,
) -> None:
    """安装器必须枚举 `review-validate-fix@*` 并清掉非 canonical 的那些。

    复现本机的真实形状：canonical 与旧的 `review-validate-fix@local-codex-plugins` 同时在册。
    等值匹配单一 selector 的旧实现对旧那份结构性失明，于是每次部署只刷新 canonical、旧那份原封
    不动地活下来。
    """
    installer = _load_installer()
    active_marketplace_root = tmp_path / "active-marketplace"
    canonical_selector = "review-validate-fix@review-validate-fix-local-main"
    stale_codex_selector = "review-validate-fix@local-codex-plugins"
    stale_claude_selector = "review-validate-fix@review-validate-fix-local"
    runner = RecordingOfficialCliRunner(
        codex_marketplaces=[
            {
                "name": "review-validate-fix-local-main",
                "root": str(active_marketplace_root),
            }
        ],
        codex_installed_plugins=[
            {"pluginId": canonical_selector, "installed": True},
            {"pluginId": stale_codex_selector, "installed": True},
        ],
        claude_marketplaces=[
            {
                "name": "review-validate-fix-local-main",
                "path": str(active_marketplace_root),
            }
        ],
        claude_installed_plugins=[
            {"id": canonical_selector, "enabled": True},
            {"id": stale_claude_selector, "enabled": True},
        ],
    )

    result = installer.install_developer_marketplace_with_official_clis(
        active_marketplace_root=active_marketplace_root,
        codex_executable=Path("/opt/test/bin/codex"),
        claude_executable=Path("/opt/test/bin/claude"),
        command_runner=runner,
    )

    command_tails = [command[1:] for command in runner.commands]
    assert ["plugin", "remove", stale_codex_selector, "--json"] in command_tails
    assert [
        "plugin",
        "uninstall",
        stale_claude_selector,
        "--scope",
        "user",
    ] in command_tails
    assert result.removed_noncanonical_codex_plugin_selectors == (
        stale_codex_selector,
    )
    assert result.removed_noncanonical_claude_plugin_selectors == (
        stale_claude_selector,
    )
    # canonical 仍走正常的 remove→add / update 路径，没有被误当成残留。
    assert ["plugin", "add", canonical_selector, "--json"] in command_tails
    assert [
        "plugin",
        "update",
        canonical_selector,
        "--scope",
        "user",
    ] in command_tails


def test_official_cli_install_keeps_non_rvf_plugins_and_reports_no_removals(
    tmp_path: Path,
) -> None:
    """枚举必须严格限定在 `review-validate-fix@` 命名空间内，不得误伤别的插件。"""
    installer = _load_installer()
    active_marketplace_root = tmp_path / "active-marketplace"
    runner = RecordingOfficialCliRunner(
        codex_marketplaces=[],
        codex_installed_plugins=[
            {"pluginId": "ponytail@local-codex-plugins", "installed": True},
            # 同一个 marketplace 下的别家插件；名字前缀相近但不是 RVF。
            {"pluginId": "review-validate-fix-dashboard@x", "installed": True},
        ],
        claude_marketplaces=[],
        claude_installed_plugins=[{"id": "ponytail@somewhere", "enabled": True}],
    )

    result = installer.install_developer_marketplace_with_official_clis(
        active_marketplace_root=active_marketplace_root,
        codex_executable=Path("/opt/test/bin/codex"),
        claude_executable=Path("/opt/test/bin/claude"),
        command_runner=runner,
    )

    assert result.removed_noncanonical_codex_plugin_selectors == ()
    assert result.removed_noncanonical_claude_plugin_selectors == ()
    removed_selectors = [
        command[3]
        for command in runner.commands
        if command[1:3] in (["plugin", "remove"], ["plugin", "uninstall"])
    ]
    assert removed_selectors == []


def test_transaction_restores_previous_active_release_and_official_installation(
    tmp_path: Path,
) -> None:
    installer = _load_installer()
    home_directory = tmp_path / "home"
    active_marketplace_root = (
        home_directory
        / ".rvf/developer-plugin-marketplaces/review-validate-fix-local-main"
    )
    active_marketplace_root.mkdir(parents=True)
    (active_marketplace_root / "release-marker.txt").write_text(
        "previous\n",
        encoding="utf-8",
    )
    new_release_root = tmp_path / "new-release"
    new_release_root.mkdir()
    (new_release_root / "release-marker.txt").write_text("new\n", encoding="utf-8")
    new_release = installer.DeveloperMarketplaceRelease(
        release_root=new_release_root,
        source_repository=tmp_path / "source",
        source_commit="a" * 40,
        source_commit_count=2,
        base_plugin_version="0.1.0",
        generated_plugin_version="0.1.0-dev.2+g" + "a" * 12,
        payload_digest="digest",
    )
    runner = FailOnceDuringClaudeUpdateRunner(
        active_marketplace_root=active_marketplace_root
    )

    with pytest.raises(
        installer.DeveloperMarketplaceTransactionError,
        match="restored_previous_release",
    ) as error_info:
        installer.deploy_developer_marketplace_transaction(
            release=new_release,
            home_directory=home_directory,
            codex_executable=Path("/opt/test/bin/codex"),
            claude_executable=Path("/opt/test/bin/claude"),
            command_runner=runner,
        )

    assert error_info.value.rollback_status == "restored_previous_release"
    assert (active_marketplace_root / "release-marker.txt").read_text(
        encoding="utf-8"
    ) == "previous\n"
    claude_update_commands = [
        command
        for command in runner.commands
        if Path(command[0]).name == "claude"
        and command[1:3] == ["plugin", "update"]
    ]
    assert len(claude_update_commands) == 2
