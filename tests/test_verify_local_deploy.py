from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install_to_codex.py"
VERIFIER = (
    ROOT
    / "plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py"
)


def load_verifier():
    spec = importlib.util.spec_from_file_location("rvf_verify_local_deploy_for_tests", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_into(
    tmp_path: Path,
    *,
    configure_ups: bool,
    sync_claude: bool = False,
) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    argv = [
        sys.executable,
        str(INSTALLER),
        "--plugin-parent",
        str(home / "plugins"),
        "--configure-stop-hook",
    ]
    argv.append("--sync-claude-plugin" if sync_claude else "--skip-claude-plugin")
    if configure_ups:
        argv.append("--configure-user-prompt-submit-hook")
    subprocess.run(
        argv,
        cwd=ROOT,
        env={**os.environ, "HOME": str(home)},
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return (
        home / "plugins/review-validate-fix",
        home / ".codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0",
        home / ".codex/hooks.json",
    )


def test_verify_local_deploy_accepts_complete_agentic_overhaul_payload(tmp_path: Path) -> None:
    verifier = load_verifier()
    plugin_root, cache_root, hooks_path = install_into(tmp_path, configure_ups=True)
    result = verifier.verify_local_deploy(
        repo=ROOT,
        plugin_root=plugin_root,
        cache_root=cache_root,
        hooks_path=hooks_path,
    )
    assert result["status"] == "ok"
    assert result["source_head"] == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    assert len(result["payloads"]) == 2
    assert all(payload["smoke_count"] == 3 for payload in result["payloads"])


def test_verify_local_deploy_rejects_missing_user_prompt_submit_hook(tmp_path: Path) -> None:
    verifier = load_verifier()
    plugin_root, cache_root, hooks_path = install_into(tmp_path, configure_ups=False)
    with pytest.raises(verifier.VerificationError, match="UserPromptSubmit hook"):
        verifier.verify_local_deploy(
            repo=ROOT,
            plugin_root=plugin_root,
            cache_root=cache_root,
            hooks_path=hooks_path,
        )


def test_verify_local_deploy_rejects_missing_user_prompt_submit_script(tmp_path: Path) -> None:
    verifier = load_verifier()
    plugin_root, cache_root, hooks_path = install_into(tmp_path, configure_ups=True)
    (plugin_root / "skills/review-validate-fix/scripts/rvf_user_prompt_submit.py").unlink()

    with pytest.raises(verifier.VerificationError, match="payload 缺文件"):
        verifier.verify_local_deploy(
            repo=ROOT,
            plugin_root=plugin_root,
            cache_root=cache_root,
            hooks_path=hooks_path,
        )


def test_verify_local_deploy_rejects_ups_hook_under_wrong_event(tmp_path: Path) -> None:
    verifier = load_verifier()
    plugin_root, cache_root, hooks_path = install_into(tmp_path, configure_ups=True)
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    ups_groups = hooks["hooks"]["UserPromptSubmit"]
    hooks["hooks"]["Stop"].extend(ups_groups)
    hooks["hooks"]["UserPromptSubmit"] = []
    hooks_path.write_text(json.dumps(hooks), encoding="utf-8")

    with pytest.raises(verifier.VerificationError, match="UserPromptSubmit hook"):
        verifier.verify_local_deploy(
            repo=ROOT,
            plugin_root=plugin_root,
            cache_root=cache_root,
            hooks_path=hooks_path,
        )


def test_verify_local_deploy_rejects_hook_path_substring_only(tmp_path: Path) -> None:
    verifier = load_verifier()
    plugin_root, cache_root, hooks_path = install_into(tmp_path, configure_ups=True)
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    expected = str(
        plugin_root / "skills/review-validate-fix/scripts/rvf_user_prompt_submit.py"
    )
    for group in hooks["hooks"]["UserPromptSubmit"]:
        for entry in group.get("hooks", []):
            command = entry.get("command")
            if isinstance(command, str) and expected in command:
                entry["command"] = command.replace(expected, f"{expected}.backup")
    hooks_path.write_text(json.dumps(hooks), encoding="utf-8")

    with pytest.raises(verifier.VerificationError, match="UserPromptSubmit hook"):
        verifier.verify_local_deploy(
            repo=ROOT,
            plugin_root=plugin_root,
            cache_root=cache_root,
            hooks_path=hooks_path,
        )


def test_verify_local_deploy_checks_synced_claude_payloads(tmp_path: Path) -> None:
    verifier = load_verifier()
    plugin_root, cache_root, hooks_path = install_into(
        tmp_path,
        configure_ups=True,
        sync_claude=True,
    )

    result = verifier.verify_local_deploy(
        repo=ROOT,
        plugin_root=plugin_root,
        cache_root=cache_root,
        hooks_path=hooks_path,
    )

    assert len(result["payloads"]) == 4


def test_verify_local_deploy_rejects_stale_claude_cache(tmp_path: Path) -> None:
    verifier = load_verifier()
    plugin_root, cache_root, hooks_path = install_into(
        tmp_path,
        configure_ups=True,
        sync_claude=True,
    )
    latest = json.loads(
        (
            plugin_root
            / "skills/review-validate-fix/state/deployments/latest-deployment.json"
        ).read_text(encoding="utf-8")
    )
    claude_cache = Path(latest["destinations"]["claude_plugin_cache"])
    stale_script = (
        claude_cache
        / "skills/review-validate-fix/scripts/rvf_user_prompt_submit.py"
    )
    stale_script.write_text("# stale\n", encoding="utf-8")

    with pytest.raises(verifier.VerificationError, match="Claude.*内容不一致"):
        verifier.verify_local_deploy(
            repo=ROOT,
            plugin_root=plugin_root,
            cache_root=cache_root,
            hooks_path=hooks_path,
        )
