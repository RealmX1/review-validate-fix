#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_to_codex.py"


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


def with_env(updates: dict[str, str | None], callback) -> None:
    original = {key: os.environ.get(key) for key in updates}
    for key, value in updates.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        callback()
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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
    assert "CODEX_RVF_FORK_MODE=gui" in command
    assert "CODEX_RVF_DEV_SYNC_COMMAND_TIMEOUT=180" in command
    assert "CODEX_RVF_STOP_HOOK_CHAIN_TIMEOUT=60" in command
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


def test_configure_stop_hook_can_write_cline_kanban_mode(tmp_path: Path) -> None:
    module = load_installer_module()

    def run_test() -> None:
        module.configure_stop_hook(
            tmp_path / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix",
            "cline-kanban",
        )

    with_fake_home(module, tmp_path, run_test)

    data = json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    matching = rvf_hooks(data)
    assert len(matching) == 1
    assert "CODEX_RVF_FORK_MODE=cline-kanban" in matching[0]["command"]


def test_configure_stop_hook_can_write_cline_kanban_connection_env(tmp_path: Path) -> None:
    module = load_installer_module()

    def run_test() -> None:
        module.configure_stop_hook(
            tmp_path / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix",
            "cline-kanban",
            cline_kanban_start_cmd="npx -y kanban@0.1.66 --no-open",
            cline_kanban_task_cmd="npx -y kanban@0.1.66 task",
            cline_kanban_start_timeout="120",
            cline_kanban_tmux_session="rvf-test-kanban",
        )

    with_fake_home(module, tmp_path, run_test)

    data = json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    matching = rvf_hooks(data)
    assert len(matching) == 1
    command = matching[0]["command"]
    assert "CODEX_RVF_FORK_MODE=cline-kanban" in command
    assert "CODEX_RVF_CLINE_KANBAN_START_CMD=" in command
    assert "kanban@0.1.66" in command
    assert "CODEX_RVF_CLINE_KANBAN_TASK_CMD=" in command
    assert "CODEX_RVF_CLINE_KANBAN_START_TIMEOUT=120" in command
    assert "CODEX_RVF_CLINE_KANBAN_TMUX_SESSION=rvf-test-kanban" in command


def test_configure_stop_hook_can_write_cline_kanban_review_options(tmp_path: Path) -> None:
    module = load_installer_module()

    def run_test() -> None:
        module.configure_stop_hook(
            tmp_path / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix",
            "cline-kanban",
            cline_kanban_base_ref="main",
            cline_kanban_auto_review_enabled="1",
            cline_kanban_auto_review_mode="pr",
            cline_kanban_start_in_plan_mode="1",
        )

    with_fake_home(module, tmp_path, run_test)

    data = json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    command = rvf_hooks(data)[0]["command"]
    assert "CODEX_RVF_CLINE_KANBAN_BASE_REF=main" in command
    assert "CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_ENABLED=1" in command
    assert "CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_MODE=pr" in command
    assert "CODEX_RVF_CLINE_KANBAN_START_IN_PLAN_MODE=1" in command


def test_configure_stop_hook_can_disable_handoff_open_and_write_ide_cmd(tmp_path: Path) -> None:
    module = load_installer_module()

    def run_test() -> None:
        module.configure_stop_hook(
            tmp_path / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix",
            open_handoff=False,
            ide_open_cmd="code -r",
        )

    with_fake_home(module, tmp_path, run_test)

    command = rvf_hooks(
        json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    )[0]["command"]
    assert "CODEX_RVF_OPEN_HANDOFF=0" in command
    assert "CODEX_RVF_IDE_OPEN_CMD='code -r'" in command


def test_main_persists_handoff_open_env(tmp_path: Path) -> None:
    module = load_installer_module()
    home = tmp_path / "home"
    plugin_parent = home / "plugins"

    def run_main() -> None:
        def call_main() -> None:
            assert module.main() == 0

        with_argv(
            [
                "install_to_codex.py",
                "--plugin-parent",
                str(plugin_parent),
                "--configure-stop-hook",
            ],
            call_main,
        )

    with_fake_home(
        module,
        home,
        lambda: with_env(
            {
                "CODEX_RVF_OPEN_HANDOFF": "0",
                "CODEX_RVF_IDE_OPEN_CMD": "code -r",
            },
            run_main,
        ),
    )

    command = rvf_hooks(
        json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    )[0]["command"]
    assert "CODEX_RVF_OPEN_HANDOFF=0" in command
    assert "CODEX_RVF_IDE_OPEN_CMD='code -r'" in command


def test_main_persists_cline_connection_env(tmp_path: Path) -> None:
    module = load_installer_module()
    home = tmp_path / "home"
    plugin_parent = home / "plugins"

    def run_main() -> None:
        def call_main() -> None:
            assert module.main() == 0

        with_argv(
            [
                "install_to_codex.py",
                "--plugin-parent",
                str(plugin_parent),
                "--configure-stop-hook",
                "--fork-mode",
                "cline-kanban",
            ],
            call_main,
        )

    with_fake_home(
        module,
        home,
        lambda: with_env(
            {
                "CODEX_RVF_CLINE_KANBAN_START_CMD": "npx -y kanban@0.1.66 --no-open",
                "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "npx -y kanban@0.1.66 task",
            },
            run_main,
        ),
    )

    data = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    matching = rvf_hooks(data)
    assert len(matching) == 1
    assert "CODEX_RVF_FORK_MODE=cline-kanban" in matching[0]["command"]
    assert "CODEX_RVF_CLINE_KANBAN_START_CMD=" in matching[0]["command"]
    assert "CODEX_RVF_CLINE_KANBAN_TASK_CMD=" in matching[0]["command"]


def test_main_persists_cline_review_options(tmp_path: Path) -> None:
    module = load_installer_module()
    home = tmp_path / "home"
    plugin_parent = home / "plugins"

    def run_main() -> None:
        def call_main() -> None:
            assert module.main() == 0

        with_argv(
            [
                "install_to_codex.py",
                "--plugin-parent",
                str(plugin_parent),
                "--configure-stop-hook",
                "--fork-mode",
                "cline-kanban",
            ],
            call_main,
        )

    with_fake_home(
        module,
        home,
        lambda: with_env(
            {
                "CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_ENABLED": "1",
                "CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_MODE": "commit",
                "CODEX_RVF_CLINE_KANBAN_START_IN_PLAN_MODE": "1",
            },
            run_main,
        ),
    )

    command = rvf_hooks(
        json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    )[0]["command"]
    assert "CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_ENABLED=1" in command
    assert "CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_MODE=commit" in command
    assert "CODEX_RVF_CLINE_KANBAN_START_IN_PLAN_MODE=1" in command


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


def test_main_installs_plugin_and_configures_stop_hook(tmp_path: Path) -> None:
    module = load_installer_module()
    home = tmp_path / "home"
    plugin_parent = home / "plugins"
    legacy_skill = home / ".codex" / "skills" / "review-validate-fix"
    legacy_skill.mkdir(parents=True)
    (legacy_skill / "SKILL.md").write_text("legacy standalone\n", encoding="utf-8")
    cache_skill = (
        home
        / ".codex"
        / "plugins"
        / "cache"
        / "local-codex-plugins"
        / "review-validate-fix"
        / module.plugin_version()
        / "skills"
        / "review-validate-fix"
    )
    cache_config = cache_skill / "config"
    cache_state = cache_skill / "state"
    cache_config.mkdir(parents=True)
    cache_state.mkdir(parents=True)
    (cache_skill / "SKILL.md").write_text("stale cached skill\n", encoding="utf-8")
    (cache_config / "alternative-reviewer.json").write_text("local cache config\n", encoding="utf-8")
    (cache_state / "run.json").write_text("local cache state\n", encoding="utf-8")

    def run_main() -> None:
        def call_main() -> None:
            assert module.main() == 0

        with_argv(
            [
                "install_to_codex.py",
                "--plugin-parent",
                str(plugin_parent),
                "--configure-stop-hook",
            ],
            call_main,
        )

    with_fake_home(module, home, run_main)

    plugin_skill = home / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"
    assert (plugin_skill / "SKILL.md").exists()
    assert (plugin_skill / "scripts" / "codex_stop_review_validate_fix.py").exists()
    assert (cache_skill / "SKILL.md").read_text(encoding="utf-8") == (
        module.PLUGIN_SRC / "skills" / "review-validate-fix" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert (cache_skill / "scripts" / "codex_stop_review_validate_fix.py").exists()
    assert (cache_config / "alternative-reviewer.json").read_text(encoding="utf-8") == "local cache config\n"
    assert (cache_state / "run.json").read_text(encoding="utf-8") == "local cache state\n"
    assert not legacy_skill.exists()
    hooks_data = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    matching = rvf_hooks(hooks_data)
    assert len(matching) == 1
    assert str(plugin_skill / "scripts" / "codex_stop_hook_dispatcher.py") in matching[0]["command"]
    assert matching[0]["statusMessage"] == "Review-Validate-Fix：同步插件并运行停止检查"


def main() -> int:
    tests = [
        test_configure_stop_hook_deduplicates_existing_rvf_hooks,
        test_configure_stop_hook_adds_dispatcher_when_missing,
        test_configure_stop_hook_can_write_cline_kanban_mode,
        test_configure_stop_hook_can_write_cline_kanban_connection_env,
        test_configure_stop_hook_can_write_cline_kanban_review_options,
        test_configure_stop_hook_can_disable_handoff_open_and_write_ide_cmd,
        test_main_persists_handoff_open_env,
        test_main_persists_cline_connection_env,
        test_main_persists_cline_review_options,
        test_copy_tree_preserves_nested_plugin_setup,
        test_main_installs_plugin_and_configures_stop_hook,
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for test in tests:
            test(root / test.__name__)
    print("install_to_codex tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
