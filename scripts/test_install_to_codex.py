#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().with_name("install_to_codex.py")


def load_installer_module():
    spec = importlib.util.spec_from_file_location("rvf_install_to_codex_for_tests", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def with_fake_home(module, home: Path, callback) -> None:
    original_home = module.Path.home
    module.Path.home = classmethod(lambda cls: home)
    try:
        callback()
    finally:
        module.Path.home = original_home


def with_argv(argv: list[str], callback) -> None:
    original_argv = sys.argv
    sys.argv = argv
    try:
        callback()
    finally:
        sys.argv = original_argv


def rvf_hooks(data: dict[str, object]) -> list[dict[str, object]]:
    hooks: list[dict[str, object]] = []
    for group in data["hooks"]["Stop"]:
        for hook in group["hooks"]:
            command = hook.get("command") if isinstance(hook, dict) else None
            if isinstance(command, str) and "review-validate-fix" in command:
                hooks.append(hook)
    return hooks


def test_configure_stop_hook_deduplicates_existing_rvf_hooks(tmp_path: Path) -> None:
    module = load_installer_module()
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 /old/review-validate-fix/scripts/codex_stop_review_validate_fix.py",
                                },
                                {"type": "command", "command": "python3 /tmp/other.py"},
                            ]
                        },
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 /old/review-validate-fix/scripts/codex_stop_hook_dispatcher.py",
                                }
                            ]
                        },
                    ]
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    def run_test() -> None:
        module.configure_stop_hook(
            tmp_path / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"
        )

    with_fake_home(module, tmp_path, run_test)

    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    matching = rvf_hooks(data)
    assert len(matching) == 1
    command = matching[0]["command"]
    assert "codex_stop_hook_dispatcher.py" in command
    assert "/plugins/review-validate-fix/skills/review-validate-fix/" in command
    assert "CODEX_RVF_DEV_SYNC_COMMAND_TIMEOUT=60" in command
    assert "CODEX_RVF_STOP_HOOK_CHAIN_TIMEOUT=30" in command
    assert "python3 /tmp/other.py" in json.dumps(data)


def test_configure_stop_hook_adds_dispatcher_when_missing(tmp_path: Path) -> None:
    module = load_installer_module()
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "python3 /tmp/other.py"}]}
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def run_test() -> None:
        module.configure_stop_hook(
            tmp_path / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"
        )

    with_fake_home(module, tmp_path, run_test)

    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert len(rvf_hooks(data)) == 1
    assert "python3 /tmp/other.py" in json.dumps(data)


def test_uninstall_standalone_skill_removes_existing_dir(tmp_path: Path) -> None:
    module = load_installer_module()
    skill_dir = tmp_path / ".codex" / "skills" / "review-validate-fix"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("legacy\n", encoding="utf-8")

    assert module.uninstall_standalone_skill(skill_dir) is True
    assert not skill_dir.exists()
    assert module.uninstall_standalone_skill(skill_dir) is False


def test_copy_tree_preserves_nested_plugin_setup(tmp_path: Path) -> None:
    module = load_installer_module()
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    source_config = src / "skills" / "review-validate-fix" / "config"
    source_config.mkdir(parents=True)
    (source_config / "alternative-reviewer.json").write_text("repo\n", encoding="utf-8")
    (src / "skills" / "review-validate-fix" / "SKILL.md").write_text("repo skill\n", encoding="utf-8")
    (src / ".codex-plugin").mkdir()
    (src / ".codex-plugin" / "plugin.json").write_text("{}\n", encoding="utf-8")

    local_config = dst / "skills" / "review-validate-fix" / "config"
    local_state = dst / "skills" / "review-validate-fix" / "state"
    local_config.mkdir(parents=True)
    local_state.mkdir(parents=True)
    (local_config / "alternative-reviewer.json").write_text("local\n", encoding="utf-8")
    (local_state / "session.json").write_text("state\n", encoding="utf-8")
    (dst / "old.txt").write_text("remove\n", encoding="utf-8")

    module.copy_tree(src, dst, module.PRESERVE_IN_PLUGIN, True)

    assert (local_config / "alternative-reviewer.json").read_text(encoding="utf-8") == "local\n"
    assert (local_state / "session.json").read_text(encoding="utf-8") == "state\n"
    assert (dst / "skills" / "review-validate-fix" / "SKILL.md").read_text(encoding="utf-8") == "repo skill\n"
    assert not (dst / "old.txt").exists()


def test_migrate_legacy_skill_setup_copies_missing_config_and_state(tmp_path: Path) -> None:
    module = load_installer_module()
    legacy = tmp_path / "legacy"
    plugin_skill = tmp_path / "plugin" / "skills" / "review-validate-fix"
    legacy_config = legacy / "config"
    legacy_state = legacy / "state" / "session-hook"
    legacy_config.mkdir(parents=True)
    legacy_state.mkdir(parents=True)
    (legacy_config / "alternative-reviewer.json").write_text("legacy config\n", encoding="utf-8")
    (legacy_state / "thread.json").write_text("legacy state\n", encoding="utf-8")

    migrated = module.migrate_legacy_skill_setup(legacy, plugin_skill, True)

    assert migrated == ["config/alternative-reviewer.json", "state/"]
    assert (
        plugin_skill / "config" / "alternative-reviewer.json"
    ).read_text(encoding="utf-8") == "legacy config\n"
    assert (
        plugin_skill / "state" / "session-hook" / "thread.json"
    ).read_text(encoding="utf-8") == "legacy state\n"


def test_migrate_legacy_skill_setup_preserves_existing_plugin_setup(tmp_path: Path) -> None:
    module = load_installer_module()
    legacy = tmp_path / "legacy"
    plugin_skill = tmp_path / "plugin" / "skills" / "review-validate-fix"
    (legacy / "config").mkdir(parents=True)
    (legacy / "state").mkdir(parents=True)
    (legacy / "config" / "alternative-reviewer.json").write_text("legacy config\n", encoding="utf-8")
    (legacy / "state" / "existing.json").write_text("legacy state\n", encoding="utf-8")
    (legacy / "state" / "missing.json").write_text("missing state\n", encoding="utf-8")
    (plugin_skill / "config").mkdir(parents=True)
    (plugin_skill / "state").mkdir(parents=True)
    (plugin_skill / "config" / "alternative-reviewer.json").write_text("plugin config\n", encoding="utf-8")
    (plugin_skill / "state" / "existing.json").write_text("plugin state\n", encoding="utf-8")

    migrated = module.migrate_legacy_skill_setup(legacy, plugin_skill, True)

    assert migrated == ["state/"]
    assert (
        plugin_skill / "config" / "alternative-reviewer.json"
    ).read_text(encoding="utf-8") == "plugin config\n"
    assert (plugin_skill / "state" / "existing.json").read_text(encoding="utf-8") == "plugin state\n"
    assert (plugin_skill / "state" / "missing.json").read_text(encoding="utf-8") == "missing state\n"


def test_main_accepts_legacy_as_skill_and_migrates_before_uninstall(tmp_path: Path) -> None:
    module = load_installer_module()
    home = tmp_path / "home"
    skill_dir = home / ".codex" / "skills" / "review-validate-fix"
    (skill_dir / "config").mkdir(parents=True)
    (skill_dir / "state" / "session-hook").mkdir(parents=True)
    (skill_dir / "config" / "alternative-reviewer.json").write_text("legacy config\n", encoding="utf-8")
    (skill_dir / "state" / "session-hook" / "thread.json").write_text("legacy state\n", encoding="utf-8")

    def run_main() -> None:
        def call_main() -> None:
            assert module.main() == 0

        with_argv(
            [
                "install_to_codex.py",
                "--as",
                "skill",
                "--plugin-parent",
                str(home / "plugins"),
                "--installed-skill-dir",
                str(skill_dir),
                "--configure-stop-hook",
            ],
            call_main,
        )

    with_fake_home(module, home, run_main)

    plugin_skill = home / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"
    assert not skill_dir.exists()
    assert (
        plugin_skill / "config" / "alternative-reviewer.json"
    ).read_text(encoding="utf-8") == "legacy config\n"
    assert (
        plugin_skill / "state" / "session-hook" / "thread.json"
    ).read_text(encoding="utf-8") == "legacy state\n"
    hooks_data = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    matching = rvf_hooks(hooks_data)
    assert len(matching) == 1
    assert str(plugin_skill / "scripts" / "codex_stop_hook_dispatcher.py") in matching[0]["command"]


def main() -> int:
    tests = [
        test_configure_stop_hook_deduplicates_existing_rvf_hooks,
        test_configure_stop_hook_adds_dispatcher_when_missing,
        test_uninstall_standalone_skill_removes_existing_dir,
        test_copy_tree_preserves_nested_plugin_setup,
        test_migrate_legacy_skill_setup_copies_missing_config_and_state,
        test_migrate_legacy_skill_setup_preserves_existing_plugin_setup,
        test_main_accepts_legacy_as_skill_and_migrates_before_uninstall,
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for test in tests:
            test(root / test.__name__)
    print("install_to_codex tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
