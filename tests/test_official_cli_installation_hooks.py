from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install_to_codex.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "installer_for_official_cli_hook_tests",
        INSTALLER,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _commands(payload: dict[str, object], event_name: str) -> list[str]:
    commands: list[str] = []
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return commands
    groups = hooks.get(event_name)
    if not isinstance(groups, list):
        return commands
    for group in groups:
        if not isinstance(group, dict):
            continue
        entries = group.get("hooks")
        if not isinstance(entries, list):
            continue
        commands.extend(
            entry["command"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("command"), str)
        )
    return commands


def test_configure_hooks_deduplicates_rvf_entries_and_injects_user_local_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    installer = _load_installer()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    hooks_path = tmp_path / ".codex/hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {"type": "command", "command": "python3 old/stop_hook_router.py"},
                                {"type": "command", "command": "python3 unrelated.py"},
                            ]
                        }
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {"type": "command", "command": "python3 old/rvf_user_prompt_submit.py"}
                            ]
                        }
                    ],
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    user_paths = installer.resolved_rvf_user_local_data_paths(home_directory=tmp_path)
    plugin_skill_dir = tmp_path / "active/plugins/review-validate-fix/skills/review-validate-fix"

    installer.configure_stop_hook(
        plugin_skill_dir,
        "cline-kanban",
        user_local_data_paths=user_paths,
    )
    installer.configure_user_prompt_submit_hook(
        plugin_skill_dir,
        user_local_data_paths=user_paths,
    )

    payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    stop_commands = _commands(payload, "Stop")
    prompt_commands = _commands(payload, "UserPromptSubmit")
    assert len([command for command in stop_commands if "stop_hook_router.py" in command]) == 1
    assert len(
        [command for command in prompt_commands if "rvf_user_prompt_submit.py" in command]
    ) == 1
    assert "python3 unrelated.py" in stop_commands
    for command in (stop_commands[-1], prompt_commands[-1]):
        assert f"RVF_USER_LOCAL_DATA_ROOT={user_paths.user_local_data_root}" in command
        assert f"CODEX_RVF_LOG_ROOT={user_paths.run_logs_root}" in command
        assert f"CODEX_RVF_STATE_DIR={user_paths.runtime_state_root}" in command
    assert "CODEX_RVF_FORK_MODE=cline-kanban" in stop_commands[-1]


def test_installer_has_no_manual_official_cache_or_installed_metadata_writer() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    forbidden_literals = (
        ".codex/plugins/cache",
        ".claude/plugins/cache",
        "installed_plugins.json",
        "enabledPlugins",
        "ensure_codex_plugin_enabled",
        "sync_codex_plugin_cache",
        "sync_claude_plugin",
    )
    for forbidden_literal in forbidden_literals:
        assert forbidden_literal not in source


def test_installer_cli_requires_explicit_local_main_source() -> None:
    installer = _load_installer()

    parser_error = None
    try:
        installer._parse_arguments([])
    except SystemExit as exc:
        parser_error = exc

    assert parser_error is not None
    assert parser_error.code == 2
