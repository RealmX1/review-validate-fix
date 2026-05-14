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
    for group in data["hooks"].get("Stop", []):
        for hook in group["hooks"]:
            command = hook.get("command") if isinstance(hook, dict) else None
            if isinstance(command, str) and "review-validate-fix" in command:
                hooks.append(hook)
    return hooks


def rvf_user_prompt_hooks(data: dict[str, object]) -> list[dict[str, object]]:
    hooks: list[dict[str, object]] = []
    for group in data["hooks"].get("UserPromptSubmit", []):
        for hook in group.get("hooks", []):
            command = hook.get("command") if isinstance(hook, dict) else None
            if isinstance(command, str) and "rvf_user_prompt_submit.py" in command:
                hooks.append(hook)
    return hooks


def first_h1(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line
    raise AssertionError(f"missing H1: {path}")


def deployed_heading_label(module) -> str:
    return module.deploy_version_from_metadata(module.git_metadata())["heading_label"]


def assert_deployed_skill_stamps(module, plugin_root: Path, heading_label: str) -> None:
    skill_files = sorted((plugin_root / "skills").glob("*/SKILL.md"))
    assert skill_files
    for skill_md in skill_files:
        assert first_h1(skill_md).endswith(f" [deployed {heading_label}]")


def test_stamp_skill_heading_text_is_idempotent(tmp_path: Path) -> None:
    del tmp_path
    module = load_installer_module()
    source = "---\nname: demo\n---\n\n# Demo Skill\n\nBody\n"

    stamped = module.stamp_skill_heading_text(source, "abc123")
    assert "# Demo Skill [deployed abc123]\n" in stamped
    assert stamped.startswith("---\nname: demo\n---\n\n")
    assert module.stamp_skill_heading_text(stamped, "def456") == (
        "---\nname: demo\n---\n\n# Demo Skill [deployed def456]\n\nBody\n"
    )


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
    assert "codex_stop_hook_router.py" in command
    assert "CODEX_RVF_STABLE_STOP_HOOK=" in command
    assert "codex_stop_hook_dispatcher.py" in command
    assert "/plugins/review-validate-fix/skills/review-validate-fix/" in command
    assert "CODEX_RVF_FORK_MODE=auto" in command
    assert "CODEX_RVF_DEV_SYNC_COMMAND_TIMEOUT=180" in command
    assert "CODEX_RVF_STOP_HOOK_CHAIN_TIMEOUT=60" in command
    assert "python3 /tmp/other.py" in json.dumps(data)


def test_configure_user_prompt_submit_hook_deduplicates_existing_rvf_hooks(tmp_path: Path) -> None:
    module = load_installer_module()
    hooks_path = tmp_path / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "python3 /tmp/stop.py"}]}
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 /old/review-validate-fix/scripts/rvf_user_prompt_submit.py",
                                },
                                {"type": "command", "command": "python3 /tmp/other_prompt_hook.py"},
                            ]
                        },
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 /stale/review-validate-fix/scripts/rvf_user_prompt_submit.py",
                                }
                            ]
                        },
                    ],
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    def run_test() -> None:
        module.configure_user_prompt_submit_hook(
            tmp_path / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"
        )

    with_fake_home(module, tmp_path, run_test)

    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    matching = rvf_user_prompt_hooks(data)
    assert len(matching) == 1
    command = matching[0]["command"]
    assert "rvf_user_prompt_submit.py" in command
    assert str(tmp_path / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix") in command
    assert "codex_stop_hook" not in command
    assert "CODEX_RVF_MODE" not in command
    assert matching[0]["timeout"] == 5
    assert "python3 /tmp/other_prompt_hook.py" in json.dumps(data)
    assert "python3 /tmp/stop.py" in json.dumps(data)


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


def test_configure_stop_hook_can_write_kanban_followup_mode(tmp_path: Path) -> None:
    module = load_installer_module()

    def run_test() -> None:
        module.configure_stop_hook(
            tmp_path / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix",
            "kanban-message",
            cline_kanban_task_cmd="kanban --port 4567 task",
        )

    with_fake_home(module, tmp_path, run_test)

    data = json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    matching = rvf_hooks(data)
    assert len(matching) == 1
    command = matching[0]["command"]
    assert "CODEX_RVF_FORK_MODE=kanban-followup" in command
    assert "CODEX_RVF_CLINE_KANBAN_TASK_CMD=" in command


def test_configure_stop_hook_can_write_cline_kanban_connection_env(tmp_path: Path) -> None:
    module = load_installer_module()

    def run_test() -> None:
        module.configure_stop_hook(
            tmp_path / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix",
            "cline-kanban",
            cline_kanban_start_cmd="kanban --port 4567 --no-open",
            cline_kanban_task_cmd="kanban --port 4567 task",
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
    assert "kanban --port 4567 --no-open" in command
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
            cline_kanban_worktree_mode="inplace",
            cline_kanban_auto_review_enabled="1",
            cline_kanban_auto_review_mode="pr",
            cline_kanban_start_in_plan_mode="1",
        )

    with_fake_home(module, tmp_path, run_test)

    data = json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    command = rvf_hooks(data)[0]["command"]
    assert "CODEX_RVF_CLINE_KANBAN_BASE_REF=main" in command
    assert "CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE=inplace" in command
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
                "CODEX_RVF_CLINE_KANBAN_START_CMD": "kanban --port 4567 --no-open",
                "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "kanban --port 4567 task",
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


def test_main_drops_legacy_npx_kanban_defaults_from_env(tmp_path: Path) -> None:
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
    command = matching[0]["command"]
    assert "CODEX_RVF_FORK_MODE=cline-kanban" in command
    assert "CODEX_RVF_CLINE_KANBAN_START_CMD=" not in command
    assert "CODEX_RVF_CLINE_KANBAN_TASK_CMD=" not in command


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


def test_main_rejects_cline_kanban_worktree_mode_main(tmp_path: Path) -> None:
    module = load_installer_module()
    home = tmp_path / "home"
    plugin_parent = home / "plugins"

    def run_main() -> None:
        def call_main() -> None:
            try:
                module.main()
            except SystemExit as exc:
                assert exc.code == 2
            else:
                raise AssertionError("expected argparse to reject --cline-kanban-worktree-mode main")

        with_argv(
            [
                "install_to_codex.py",
                "--plugin-parent",
                str(plugin_parent),
                "--configure-stop-hook",
                "--cline-kanban-worktree-mode",
                "main",
            ],
            call_main,
        )

    with_fake_home(module, home, run_main)
    assert not (home / ".codex" / "hooks.json").exists()


def test_main_ignores_invalid_cline_kanban_worktree_mode_env(tmp_path: Path) -> None:
    module = load_installer_module()
    home = tmp_path / "home"
    plugin_parent = home / "plugins"

    def run_main() -> None:
        with_argv(
            [
                "install_to_codex.py",
                "--plugin-parent",
                str(plugin_parent),
                "--configure-stop-hook",
                "--fork-mode",
                "cline-kanban",
            ],
            module.main,
        )

    with_fake_home(
        module,
        home,
        lambda: with_env({"CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE": "main"}, run_main),
    )

    command = rvf_hooks(
        json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    )[0]["command"]
    assert "CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE=main" not in command
    assert "CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE=" not in command


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


def test_copy_tree_excludes_dev_only_paths(tmp_path: Path) -> None:
    module = load_installer_module()
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    runtime_scripts = src / "skills" / "review-validate-fix" / "scripts"
    runtime_scripts.mkdir(parents=True)
    (runtime_scripts / "codex_stop_hook_dispatcher.py").write_text("runtime\n", encoding="utf-8")
    (runtime_scripts / "install_to_codex.py").write_text("dev installer\n", encoding="utf-8")
    (runtime_scripts / "dev_only").mkdir()
    (runtime_scripts / "dev_only" / "probe.py").write_text("dev helper\n", encoding="utf-8")
    (src / "dev-only").mkdir()
    (src / "dev-only" / "notes.md").write_text("dev docs\n", encoding="utf-8")

    module.copy_tree(src, dst, module.PRESERVE_IN_PLUGIN, True)

    deployed_scripts = dst / "skills" / "review-validate-fix" / "scripts"
    assert (deployed_scripts / "codex_stop_hook_dispatcher.py").exists()
    assert not (deployed_scripts / "install_to_codex.py").exists()
    assert not (deployed_scripts / "dev_only").exists()
    assert not (dst / "dev-only").exists()


def test_ensure_codex_plugin_enabled_updates_user_config(tmp_path: Path) -> None:
    module = load_installer_module()
    home = tmp_path / "home"
    config_path = home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                'model = "gpt-5.5"',
                "",
                '[projects."/tmp/repo"]',
                'trust_level = "trusted"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with_fake_home(module, home, lambda: module.ensure_codex_plugin_enabled())

    text = config_path.read_text(encoding="utf-8")
    assert '[plugins."review-validate-fix@local-codex-plugins"]' in text
    assert "enabled = true" in text
    assert '[projects."/tmp/repo"]' in text
    assert 'trust_level = "trusted"' in text


def test_ensure_codex_plugin_enabled_writes_under_custom_marketplace(
    tmp_path: Path,
) -> None:
    module = load_installer_module()
    home = tmp_path / "home"
    marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
    marketplace_path.parent.mkdir(parents=True)
    marketplace_path.write_text(
        json.dumps({"name": "custom-codex-plugins", "plugins": []}) + "\n",
        encoding="utf-8",
    )
    config_path = home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("", encoding="utf-8")

    with_fake_home(module, home, lambda: module.ensure_codex_plugin_enabled())

    text = config_path.read_text(encoding="utf-8")
    assert '[plugins."review-validate-fix@custom-codex-plugins"]' in text
    assert "enabled = true" in text


def test_update_marketplace_replaces_old_rvf_entry_by_path(tmp_path: Path) -> None:
    module = load_installer_module()
    home = tmp_path / "home"
    marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
    marketplace_path.parent.mkdir(parents=True)
    marketplace_path.write_text(
        json.dumps(
            {
                "name": "local-codex-plugins",
                "interface": {"displayName": "Local Codex Plugins"},
                "plugins": [
                    {
                        "name": "review-validate-fix",
                        "source": {"source": "local", "path": "./plugins/review-validate-fix"},
                        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                        "category": "Coding",
                    },
                    {
                        "name": "other",
                        "source": {"source": "local", "path": "./plugins/other"},
                        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                        "category": "Coding",
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with_fake_home(module, home, lambda: module.update_marketplace(home / "plugins"))

    data = json.loads(marketplace_path.read_text(encoding="utf-8"))
    plugin_names = [plugin["name"] for plugin in data["plugins"]]
    assert plugin_names == ["other", "review-validate-fix"]
    rvf_entry = data["plugins"][1]
    assert rvf_entry["source"]["path"] == "./plugins/review-validate-fix"


def test_main_installs_plugin_and_configures_stop_hook(tmp_path: Path) -> None:
    module = load_installer_module()
    home = tmp_path / "home"
    plugin_parent = home / "plugins"
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
    assert not (plugin_skill / "scripts" / "install_to_codex.py").exists()
    assert not (cache_skill / "scripts" / "install_to_codex.py").exists()
    heading_label = deployed_heading_label(module)
    assert_deployed_skill_stamps(module, home / "plugins" / "review-validate-fix", heading_label)
    assert_deployed_skill_stamps(
        module,
        home
        / ".codex"
        / "plugins"
        / "cache"
        / "local-codex-plugins"
        / "review-validate-fix"
        / module.plugin_version(),
        heading_label,
    )
    assert "[deployed " not in (
        module.PLUGIN_SRC / "skills" / "review-validate-fix" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert (cache_skill / "scripts" / "codex_stop_review_validate_fix.py").exists()
    assert (cache_config / "alternative-reviewer.json").read_text(encoding="utf-8") == "local cache config\n"
    assert (cache_state / "run.json").read_text(encoding="utf-8") == "local cache state\n"
    hooks_data = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    matching = rvf_hooks(hooks_data)
    assert len(matching) == 1
    assert str(plugin_skill / "scripts" / "codex_stop_hook_router.py") in matching[0]["command"]
    assert str(plugin_skill / "scripts" / "codex_stop_hook_dispatcher.py") in matching[0]["command"]
    assert "CODEX_RVF_FORK_MODE=auto" in matching[0]["command"]
    assert matching[0]["statusMessage"] == "Review-Validate-Fix：选择通道并运行停止检查"
    codex_config = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert '[plugins."review-validate-fix@local-codex-plugins"]' in codex_config
    assert "enabled = true" in codex_config


def test_main_syncs_existing_claude_plugin_install(tmp_path: Path) -> None:
    module = load_installer_module()
    home = tmp_path / "home"
    plugin_parent = home / "plugins"
    settings_path = home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "enabledPlugins": {
                    "review-validate-fix@review-validate-fix-local": True,
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def run_main() -> None:
        def call_main() -> None:
            assert module.main() == 0

        with_argv(
            [
                "install_to_codex.py",
                "--plugin-parent",
                str(plugin_parent),
            ],
            call_main,
        )

    with_fake_home(module, home, run_main)

    marketplace_plugin = (
        home
        / ".claude"
        / "local-marketplaces"
        / "review-validate-fix"
        / "plugins"
        / "review-validate-fix"
    )
    cache_plugin = (
        home
        / ".claude"
        / "plugins"
        / "cache"
        / "review-validate-fix-local"
        / "review-validate-fix"
        / module.plugin_version()
    )
    for root in (marketplace_plugin, cache_plugin):
        assert (root / ".claude-plugin" / "plugin.json").exists()
        assert (root / "hooks" / "hooks.json").exists()
        assert (root / "hooks" / "stop.py").exists()
        assert (root / "commands" / "review-validate-fix.md").exists()
        assert (
            root
            / "skills"
            / "review-validate-fix"
            / "scripts"
            / "post_analyze_quiet.py"
        ).exists()

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["enabledPlugins"]["review-validate-fix@review-validate-fix-local"] is True
    assert settings["extraKnownMarketplaces"]["review-validate-fix-local"]["source"]["path"] == str(
        home / ".claude" / "local-marketplaces" / "review-validate-fix"
    )
    installed = json.loads(
        (home / ".claude" / "plugins" / "installed_plugins.json").read_text(encoding="utf-8")
    )
    record = installed["plugins"]["review-validate-fix@review-validate-fix-local"][0]
    assert record["installPath"] == str(cache_plugin)
    assert record["version"] == module.plugin_version()
    assert record["lastUpdated"]


def test_main_writes_claude_marketplace_json(tmp_path: Path) -> None:
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
                "--sync-claude-plugin",
            ],
            call_main,
        )

    with_fake_home(module, home, run_main)

    marketplace_metadata = (
        home
        / ".claude"
        / "local-marketplaces"
        / "review-validate-fix"
        / ".claude-plugin"
        / "marketplace.json"
    )
    assert marketplace_metadata.exists()
    data = json.loads(marketplace_metadata.read_text(encoding="utf-8"))
    assert data["name"] == "review-validate-fix-local"
    assert isinstance(data.get("plugins"), list) and data["plugins"], data
    first = data["plugins"][0]
    assert first["name"] == "review-validate-fix"
    assert first["source"] == "./plugins/review-validate-fix"
    src_text = (module.ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    assert marketplace_metadata.read_text(encoding="utf-8") == src_text


def test_main_can_skip_claude_plugin_sync(tmp_path: Path) -> None:
    module = load_installer_module()
    home = tmp_path / "home"
    plugin_parent = home / "plugins"
    settings_path = home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "enabledPlugins": {
                    "review-validate-fix@review-validate-fix-local": True,
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def run_main() -> None:
        def call_main() -> None:
            assert module.main() == 0

        with_argv(
            [
                "install_to_codex.py",
                "--plugin-parent",
                str(plugin_parent),
                "--skip-claude-plugin",
            ],
            call_main,
        )

    with_fake_home(module, home, run_main)

    assert not (
        home
        / ".claude"
        / "plugins"
        / "cache"
        / "review-validate-fix-local"
        / "review-validate-fix"
        / module.plugin_version()
    ).exists()
    assert not (
        home
        / ".claude"
        / "local-marketplaces"
        / "review-validate-fix"
        / "plugins"
        / "review-validate-fix"
    ).exists()


def test_main_records_deploy_log_with_rvf_context(tmp_path: Path) -> None:
    module = load_installer_module()
    home = tmp_path / "home"
    plugin_parent = home / "plugins"
    run_dir = tmp_path / "rvf-run"
    analysis_dir = run_dir / "artifacts" / "analysis"
    analysis_dir.mkdir(parents=True)
    (analysis_dir / "summary.md").write_text("# analysis\n", encoding="utf-8")
    (analysis_dir / "causality.json").write_text("{}\n", encoding="utf-8")
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "rvf-run-1",
                "status": "completed",
                "reason_code": "handoff_ready",
                "parent_thread_id": "thread-1",
                "rvf_backend": "kanban-task",
                "rvf_state_phase": "complete",
                "rvf_handoff_path": str(run_dir / "handoff.md"),
            }
        )
        + "\n",
        encoding="utf-8",
    )

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
                "CODEX_RVF_RUN_ID": "rvf-run-1",
                "CODEX_RVF_RUN_DIR": str(run_dir),
                "CODEX_SESSION_ID": "session-1",
            },
            run_main,
        ),
    )

    plugin_skill = home / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"
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
    plugin_latest = plugin_skill / "state" / "deployments" / "latest-deployment.json"
    plugin_history = plugin_skill / "state" / "deployments" / "deployments.jsonl"
    cache_latest = cache_skill / "state" / "deployments" / "latest-deployment.json"
    payload = json.loads(plugin_latest.read_text(encoding="utf-8"))

    assert plugin_history.exists()
    assert len(plugin_history.read_text(encoding="utf-8").splitlines()) == 1
    assert cache_latest.exists()
    assert payload["kind"] == "rvf-local-deploy"
    assert payload["plugin"]["name"] == "review-validate-fix"
    assert payload["plugin"]["version"] == module.plugin_version()
    assert payload["source"]["repo"] == str(module.ROOT)
    assert payload["deploy_version"]["heading_label"] == first_h1(plugin_skill / "SKILL.md").rsplit(
        "[deployed ", 1
    )[1].rstrip("]")
    assert first_h1(cache_skill / "SKILL.md").endswith(
        f" [deployed {payload['deploy_version']['heading_label']}]"
    )
    assert payload["runtime_hashes"]["plugin"]["value"]
    assert payload["runtime_hashes"]["cache"]["value"]
    assert Path(payload["destinations"]["plugin_skill"]) == plugin_skill.resolve()
    assert payload["options"]["configure_stop_hook"] is True
    assert payload["options"]["fork_mode"] == "cline-kanban"
    assert payload["rvf_sessions"]["env"]["CODEX_SESSION_ID"] == "session-1"
    assert payload["rvf_sessions"]["current_run"]["run_id"] == "rvf-run-1"
    assert payload["rvf_sessions"]["current_run"]["analysis_paths"]["summary_md"] == str(
        analysis_dir / "summary.md"
    )


def test_main_can_configure_user_prompt_submit_hook(tmp_path: Path) -> None:
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
                "--configure-user-prompt-submit-hook",
            ],
            call_main,
        )

    with_fake_home(module, home, run_main)

    plugin_skill = home / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"
    hooks_data = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    matching = rvf_user_prompt_hooks(hooks_data)
    assert len(matching) == 1
    assert str(plugin_skill / "scripts" / "rvf_user_prompt_submit.py") in matching[0]["command"]
    assert not rvf_hooks(hooks_data)


def main() -> int:
    tests = [
        test_stamp_skill_heading_text_is_idempotent,
        test_configure_stop_hook_deduplicates_existing_rvf_hooks,
        test_configure_user_prompt_submit_hook_deduplicates_existing_rvf_hooks,
        test_configure_stop_hook_adds_dispatcher_when_missing,
        test_configure_stop_hook_can_write_cline_kanban_mode,
        test_configure_stop_hook_can_write_kanban_followup_mode,
        test_configure_stop_hook_can_write_cline_kanban_connection_env,
        test_configure_stop_hook_can_write_cline_kanban_review_options,
        test_configure_stop_hook_can_disable_handoff_open_and_write_ide_cmd,
        test_main_persists_handoff_open_env,
        test_main_persists_cline_connection_env,
        test_main_drops_legacy_npx_kanban_defaults_from_env,
        test_main_persists_cline_review_options,
        test_main_rejects_cline_kanban_worktree_mode_main,
        test_copy_tree_preserves_nested_plugin_setup,
        test_copy_tree_excludes_dev_only_paths,
        test_ensure_codex_plugin_enabled_updates_user_config,
        test_ensure_codex_plugin_enabled_writes_under_custom_marketplace,
        test_update_marketplace_replaces_old_rvf_entry_by_path,
        test_main_installs_plugin_and_configures_stop_hook,
        test_main_syncs_existing_claude_plugin_install,
        test_main_writes_claude_marketplace_json,
        test_main_can_skip_claude_plugin_sync,
        test_main_records_deploy_log_with_rvf_context,
        test_main_can_configure_user_prompt_submit_hook,
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for test in tests:
            test(root / test.__name__)
    print("install_to_codex tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
