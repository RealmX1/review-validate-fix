"""验证共享 RVF payload 的宿主归属；只列举 hooks，绝不启动线程或运行真实 hook。"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from test_official_cli_developer_plugin_installation import (
    ROOT,
    RecordingOfficialCliRunner,
    _load_installer,
)
from test_official_cli_local_deploy_verification import VERIFIER_PATH, _load_module


SELECTOR = "review-validate-fix@review-validate-fix-local-main"


def _build_isolated_release(home: Path, commit_digit: str = "b"):
    installer = _load_installer()
    # worktree 可含指向真实运行 state 的忽略项；仅把受跟踪源码复制到构建输入。
    source = home / "isolated-source-repository"
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "plugins/review-validate-fix", "core", "adapters",
         ".rvf-pyroot", ".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json"],
        cwd=ROOT,
    ).decode().split("\0")
    for relative in filter(None, tracked):
        original = ROOT / relative
        if original.is_file():
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(original.read_bytes())
            target.chmod(original.stat().st_mode & 0o777)
    release = installer.build_developer_marketplace_release(
        source_repository=source, home_directory=home,
        source_commit=commit_digit * 40, source_commit_count=405,
    )
    return installer, release, release.release_root / "plugins/review-validate-fix"


def test_official_installation_does_not_write_bundled_hook_states(tmp_path, monkeypatch):
    home = tmp_path / "isolated-account-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    installer, release, _ = _build_isolated_release(home)
    configuration = home / ".codex/config.toml"
    configuration.parent.mkdir(parents=True)
    original = '[hooks.state."unrelated:stop:0:0"]\nenabled = false\ntrusted_hash = "user-choice"\n'
    configuration.write_text(original)
    runner = RecordingOfficialCliRunner(
        codex_marketplaces=[], codex_installed_plugins=[],
        claude_marketplaces=[], claude_installed_plugins=[],
    )
    for _ in range(2):
        installer.install_developer_marketplace_with_official_clis(
            active_marketplace_root=release.release_root,
            codex_executable=Path("/fixture/codex"),
            claude_executable=Path("/fixture/claude"), command_runner=runner,
        )
        assert configuration.read_text() == original


@pytest.mark.parametrize("override", [None, [], {"hooks": {"Stop": []}}])
def test_deployment_verifier_rejects_unexpected_codex_hook_sources(tmp_path, override):
    _, _, plugin = _build_isolated_release(tmp_path / "isolated-account-home")
    verifier = _load_module("verifier_for_hook_source_ownership", VERIFIER_PATH)
    verifier._verify_plugin_payload_root(plugin, label="fixture")
    manifest_path = plugin / ".codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("hooks", None)
    if override is not None:
        manifest["hooks"] = override
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(verifier.VerificationError, match="Codex.*hooks"):
        verifier._verify_plugin_payload_root(plugin, label="fixture")


def _nested_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nested_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_objects(child)


def _read_isolated_codex_lists(codex, home, workspace):
    incoming = queue.Queue()
    environment = {
        "HOME": str(home), "CODEX_HOME": str(home / ".codex"),
        "PATH": os.defpath, "TMPDIR": str(workspace), "LANG": "en_US.UTF-8",
    }
    with (workspace / "codex-list-stderr.log").open("w") as stderr:
        process = subprocess.Popen(
            [codex, "app-server", "-c",
             'hooks.PreToolUse=[{hooks=[{type="command",command="/usr/bin/true"}]}]'],
            cwd=workspace, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            text=True, bufsize=1,
        )
        def collect():
            for line in process.stdout:
                incoming.put(json.loads(line))
            incoming.put(None)
        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            results = []
            for ident, (method, params) in enumerate([
                ("initialize", {"clientInfo": {"name": "rvf-hook-ownership-test", "version": "1"},
                                "capabilities": {"experimentalApi": True}}),
                ("hooks/list", {"cwds": [str(workspace)]}),
                ("skills/list", {"cwds": [str(workspace)], "forceReload": True}),
            ]):
                process.stdin.write(json.dumps({"id": ident, "method": method, "params": params}) + "\n")
                process.stdin.flush()
                while True:
                    message = incoming.get(timeout=20)
                    assert message is not None, "Codex app-server exited before returning lists"
                    if message.get("id") == ident:
                        assert "error" not in message, message
                        results.append(message["result"])
                        break
                if ident == 0:
                    process.stdin.write('{"method":"initialized","params":{}}\n')
                    process.stdin.flush()
            return results[1:]
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            reader.join(timeout=2)
            process.stdout.close()


def test_real_codex_lists_only_global_rvf_hooks_and_preserves_review(tmp_path, monkeypatch):
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("Codex CLI unavailable; real plugin discovery needs the host binary")
    home = (tmp_path / "isolated-account-home").resolve()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    installer, release, _ = _build_isolated_release(home)
    active_marketplace = installer.activate_developer_marketplace_release(release, home_directory=home)
    plugin = active_marketplace / "plugins/review-validate-fix"
    user_paths = installer.initialize_rvf_user_local_data(source_repository=ROOT, home_directory=home)
    for configure in (installer.configure_stop_hook, installer.configure_user_prompt_submit_hook):
        configure(plugin / "skills/review-validate-fix", user_local_data_paths=user_paths)
    hook_file = home / ".codex/hooks.json"
    definitions = json.loads(hook_file.read_text())
    definitions["hooks"]["SessionStart"] = [
        {"hooks": [{"type": "command", "command": "/usr/bin/true"}]}
    ]
    hook_file.write_text(json.dumps(definitions))
    configuration = home / ".codex/config.toml"
    config_text = ('check_for_update_on_startup = false\n[features]\nhooks = true\n'
                   f'[plugins."{SELECTOR}"]\nenabled = true\n')
    # 历史三条停用状态不应凭空生成定义，也不应被迁移成新信任。
    for event in ["stop", "user_prompt_submit", "post_tool_use"]:
        config_text += f'[hooks.state."{SELECTOR}:hooks/hooks.json:{event}:0:0"]\nenabled = false\n'
    configuration.write_text(config_text)
    cache = home / ".codex/plugins/cache/review-validate-fix-local-main/review-validate-fix"
    workspace = tmp_path.resolve()
    for commit_digit in ["b", "c"]:
        _, release, payload = _build_isolated_release(home, commit_digit)
        installer.activate_developer_marketplace_release(release, home_directory=home)
        if cache.exists():
            shutil.rmtree(cache)
        shutil.copytree(payload, cache / release.generated_plugin_version)
        for _ in range(2):  # 重复加载和更新后新宿主均不得恢复 bundled 来源。
            hooks, skills = _read_isolated_codex_lists(codex, home, workspace)
            handlers = [r for r in _nested_objects(hooks) if "key" in r and "trustStatus" in r]
            assert len(handlers) == 4, handlers
            assert sum(r["key"].startswith(str(hook_file)) for r in handlers) == 3
            assert sum("/<session-flags>/" in r["key"] for r in handlers) == 1
            assert all(r["trustStatus"] == "untrusted" for r in handlers)
            rvf_skills = [r for r in _nested_objects(skills) if r.get("pluginId") == SELECTOR and "name" in r]
            assert len(rvf_skills) == 6 and all(r["enabled"] for r in rvf_skills)
            assert configuration.read_text() == config_text
        claude_manifest = json.loads((payload / ".claude-plugin/plugin.json").read_text())
        assert "hooks" not in claude_manifest
        assert (payload / "hooks/hooks.json").read_bytes() == (ROOT / "plugins/review-validate-fix/hooks/hooks.json").read_bytes()
    # 在隔离账户模拟用户信任已有定义；随后改变 timeout 必须再次失去信任。
    for handler in handlers:
        config_text += f'[hooks.state.{json.dumps(handler["key"])}]\ntrusted_hash = {json.dumps(handler["currentHash"])}\n'
    configuration.write_text(config_text)
    trusted_hooks, _ = _read_isolated_codex_lists(codex, home, workspace)
    assert all(r["trustStatus"] == "trusted" for r in _nested_objects(trusted_hooks) if "trustStatus" in r)
    definitions = json.loads(hook_file.read_text())
    definitions["hooks"]["Stop"][0]["hooks"][0]["timeout"] += 1
    hook_file.write_text(json.dumps(definitions))
    changed_hooks, _ = _read_isolated_codex_lists(codex, home, workspace)
    changed_states = [r["trustStatus"] for r in _nested_objects(changed_hooks) if "trustStatus" in r]
    assert len(changed_states) == 4 and changed_states.count("trusted") == 3, changed_states
