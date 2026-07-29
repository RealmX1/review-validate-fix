#!/usr/bin/env python3
"""验证由 Codex/Claude Code 官方 CLI 安装的 local-main RVF release。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping


PLUGIN_NAME = "review-validate-fix"
MARKETPLACE_NAME = "review-validate-fix-local-main"
PLUGIN_SELECTOR = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
RELEASE_METADATA_FILE_NAME = ".rvf-local-main-release.json"
REQUIRED_PLUGIN_RELATIVE_PATHS = (
    Path(".codex-plugin/plugin.json"),
    Path(".claude-plugin/plugin.json"),
    Path(".rvf-pyroot"),
    Path("core/__init__.py"),
    Path("adapters/__init__.py"),
    Path("skills/review-validate-fix/SKILL.md"),
    Path("skills/review-validate-fix/scripts/stop_hook_router.py"),
    Path("skills/review-validate-fix/scripts/rvf_user_prompt_submit.py"),
    Path("skills/rvf-local-deploy/SKILL.md"),
)


class VerificationError(RuntimeError):
    pass


OfficialCliCommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"无法读取 JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VerificationError(f"JSON 顶层必须是 object: {path}")
    return payload


def _is_excluded_deployment_digest_path(relative_path: Path) -> bool:
    return "__pycache__" in relative_path.parts or relative_path.suffix == ".pyc"


def _directory_digest(root: Path, *, exclude_release_metadata: bool = False) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative_path = path.relative_to(root)
        if _is_excluded_deployment_digest_path(relative_path):
            continue
        if exclude_release_metadata and relative_path == Path(RELEASE_METADATA_FILE_NAME):
            continue
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _hook_commands_for_event(payload: Any, event_name: str) -> list[str]:
    if not isinstance(payload, dict):
        return []
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return []
    groups = hooks.get(event_name)
    if not isinstance(groups, list):
        return []
    commands: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        entries = group.get("hooks")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            command = entry.get("command") if isinstance(entry, dict) else None
            if isinstance(command, str):
                commands.append(command)
    return commands


def _command_targets_script(command: str, expected_script_path: Path) -> bool:
    expected = str(expected_script_path.resolve())
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return any(
        token == expected
        or (
            token.startswith("/")
            and Path(token).expanduser().resolve() == expected_script_path.resolve()
        )
        for token in tokens
    )


def _command_injects_resolved_environment_paths(
    command: str,
    expected_paths: Mapping[str, Path],
) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    injected_values: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            continue
        name, value = token.split("=", 1)
        if name in expected_paths:
            injected_values[name] = value
    return all(
        name in injected_values
        and Path(injected_values[name]).expanduser().resolve()
        == expected_path.expanduser().resolve()
        for name, expected_path in expected_paths.items()
    )


def _marketplace_entry_path(entry: Mapping[str, Any]) -> Path | None:
    for key in ("root", "path", "installLocation"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser().resolve()
    marketplace_source = entry.get("marketplaceSource")
    if isinstance(marketplace_source, dict):
        source = marketplace_source.get("source")
        if isinstance(source, str) and source.strip():
            return Path(source).expanduser().resolve()
    return None


def _default_command_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_synthetic_installed_hook_smoke(
    *,
    active_plugin_root: Path,
    deployment_source_worktree: Path,
    home_directory: Path,
    user_local_data_root: Path,
) -> dict[str, Any]:
    stop_router = (
        active_plugin_root
        / "skills/review-validate-fix/scripts/stop_hook_router.py"
    )
    stop_dispatcher = (
        active_plugin_root
        / "skills/review-validate-fix/scripts/stop_hook_dispatcher.py"
    )
    prompt_hook = (
        active_plugin_root
        / "skills/review-validate-fix/scripts/rvf_user_prompt_submit.py"
    )
    synthetic_environment = {
        **os.environ,
        "HOME": str(home_directory),
        "RVF_USER_LOCAL_DATA_ROOT": str(user_local_data_root),
        "CODEX_RVF_LOG_ROOT": str(user_local_data_root / "run-logs"),
        "CODEX_RVF_STATE_DIR": str(user_local_data_root / "runtime-state"),
        "CODEX_RVF_INSTALLED_SKILL_DIR": str(
            active_plugin_root / "skills/review-validate-fix"
        ),
        "CODEX_RVF_SESSION_HOOK_STATE_DIR": str(
            user_local_data_root / "runtime-state/session-hook"
        ),
        "CODEX_RVF_STABLE_STOP_HOOK": str(stop_dispatcher),
        "CODEX_RVF_DEV_SYNC": "0",
        "RVF_SUPPRESS": "1",
        "RVF_SUPPRESS_STOP_HOOK": "1",
    }
    smoke_cases = (
        (
            "codex_stop_router",
            stop_router,
            {
                "hook_event_name": "Stop",
                "cwd": str(deployment_source_worktree),
                "session_id": "rvf-local-deploy-synthetic-smoke",
                "last_user_message": "RVF local deploy synthetic hook smoke",
            },
        ),
        (
            "codex_user_prompt_submit",
            prompt_hook,
            {
                "hook_event_name": "UserPromptSubmit",
                "cwd": str(deployment_source_worktree),
                "session_id": "rvf-local-deploy-synthetic-smoke",
                "prompt": "RVF local deploy synthetic hook smoke",
            },
        ),
    )
    evidence: dict[str, Any] = {}
    for label, script_path, event in smoke_cases:
        completed = subprocess.run(
            [sys.executable, str(script_path)],
            input=json.dumps(event),
            cwd=deployment_source_worktree,
            env=synthetic_environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        detail = completed.stderr.strip() or completed.stdout.strip()
        _require(
            completed.returncode == 0,
            f"{label} synthetic hook smoke 失败: {detail}",
        )
        evidence[label] = {
            "status": "ok",
            "script": str(script_path),
            "returncode": completed.returncode,
            "stdout_emitted": bool(completed.stdout.strip()),
        }
    return evidence


def _official_cli_json(
    command: list[str],
    *,
    command_runner: OfficialCliCommandRunner,
) -> Any:
    completed = command_runner(command)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise VerificationError(f"官方 CLI 查询失败: {command!r}: {detail}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"官方 CLI 查询未返回 JSON: {command!r}") from exc


def _query_official_cli_state(
    receipt: Mapping[str, Any],
    *,
    command_runner: OfficialCliCommandRunner,
) -> dict[str, Any]:
    codex_executable = receipt.get("codex_executable") or shutil.which("codex")
    claude_executable = receipt.get("claude_executable") or shutil.which("claude")
    _require(isinstance(codex_executable, str) and codex_executable, "未找到 Codex CLI")
    _require(isinstance(claude_executable, str) and claude_executable, "未找到 Claude CLI")
    return {
        "codex_marketplaces": _official_cli_json(
            [codex_executable, "plugin", "marketplace", "list", "--json"],
            command_runner=command_runner,
        ),
        "codex_plugins": _official_cli_json(
            [codex_executable, "plugin", "list", "--json"],
            command_runner=command_runner,
        ),
        "claude_marketplaces": _official_cli_json(
            [claude_executable, "plugin", "marketplace", "list", "--json"],
            command_runner=command_runner,
        ),
        "claude_plugins": _official_cli_json(
            [claude_executable, "plugin", "list", "--json"],
            command_runner=command_runner,
        ),
    }


def _verify_plugin_payload_root(plugin_root: Path, *, label: str) -> None:
    _require(plugin_root.is_dir(), f"{label} plugin payload 不存在: {plugin_root}")
    missing = [
        str(relative_path)
        for relative_path in REQUIRED_PLUGIN_RELATIVE_PATHS
        if not (plugin_root / relative_path).is_file()
    ]
    _require(not missing, f"{label} plugin payload 缺少必要文件: {missing}")
    _require(
        not (plugin_root / "skills/review-validate-fix/state").exists(),
        f"{label} plugin payload 不得包含可变 state",
    )
    _require(
        not (
            plugin_root
            / "skills/review-validate-fix/config/reviewer-registry.json"
        ).exists(),
        f"{label} plugin payload 不得包含可变 reviewer registry",
    )


def verify_local_deploy(
    *,
    installation_receipt_path: Path,
    hooks_path: Path,
    home_directory: Path,
    official_cli_state: Mapping[str, Any] | None = None,
    command_runner: OfficialCliCommandRunner = _default_command_runner,
) -> dict[str, Any]:
    installation_receipt_path = installation_receipt_path.expanduser().resolve()
    hooks_path = hooks_path.expanduser().resolve()
    home_directory = home_directory.expanduser().resolve()
    receipt = _read_json(installation_receipt_path)
    _require(receipt.get("status") == "installed", "installation receipt status 不是 installed")
    _require(
        receipt.get("marketplace_identifier") == MARKETPLACE_NAME,
        "installation receipt marketplace identifier 不匹配",
    )
    _require(
        receipt.get("plugin_selector") == PLUGIN_SELECTOR,
        "installation receipt plugin selector 不匹配",
    )
    generated_plugin_version = receipt.get("generated_plugin_version")
    _require(
        isinstance(generated_plugin_version, str) and generated_plugin_version,
        "installation receipt 缺少 generated plugin version",
    )
    active_marketplace_root = Path(str(receipt.get("active_marketplace_root"))).expanduser().resolve()
    release_root = Path(str(receipt.get("release_root"))).expanduser().resolve()
    user_local_data_root = Path(str(receipt.get("user_local_data_root"))).expanduser().resolve()
    deployment_source_worktree = (
        Path(str(receipt.get("deployment_source_worktree"))).expanduser().resolve()
    )
    _require(
        deployment_source_worktree.is_dir(),
        "installation receipt deploy 源 worktree 不存在",
    )
    active_plugin_root = active_marketplace_root / f"plugins/{PLUGIN_NAME}"
    _verify_plugin_payload_root(active_plugin_root, label="active marketplace")
    _require(release_root.is_dir(), f"immutable release 不存在: {release_root}")
    release_metadata = _read_json(release_root / RELEASE_METADATA_FILE_NAME)
    _require(
        release_metadata.get("source_commit") == receipt.get("local_main_commit"),
        "release source commit 与 receipt 不一致",
    )
    _require(
        release_metadata.get("generated_plugin_version") == generated_plugin_version,
        "release generated version 与 receipt 不一致",
    )
    release_digest = _directory_digest(release_root, exclude_release_metadata=True)
    active_digest = _directory_digest(
        active_marketplace_root,
        exclude_release_metadata=True,
    )
    _require(release_digest == receipt.get("payload_digest"), "release payload digest 漂移")
    _require(active_digest == release_digest, "active marketplace 与 immutable release 内容不一致")

    reviewer_registry_path = (
        user_local_data_root
        / "reviewer-configuration/reviewer-registry.json"
    )
    runtime_state_root = user_local_data_root / "runtime-state"
    run_logs_root = user_local_data_root / "run-logs"
    deployment_receipts_root = user_local_data_root / "deployment-receipts"
    _require(reviewer_registry_path.is_file(), "user-local reviewer registry 不存在")
    for required_directory in (
        runtime_state_root,
        run_logs_root,
        deployment_receipts_root,
    ):
        _require(required_directory.is_dir(), f"user-local data directory 不存在: {required_directory}")

    hooks = _read_json(hooks_path)
    stop_commands = _hook_commands_for_event(hooks, "Stop")
    prompt_commands = _hook_commands_for_event(hooks, "UserPromptSubmit")
    stop_router = active_plugin_root / "skills/review-validate-fix/scripts/stop_hook_router.py"
    prompt_hook = active_plugin_root / "skills/review-validate-fix/scripts/rvf_user_prompt_submit.py"
    _require(
        any(_command_targets_script(command, stop_router) for command in stop_commands),
        "Stop hook 未精确指向 active marketplace router",
    )
    _require(
        any(_command_targets_script(command, prompt_hook) for command in prompt_commands),
        "UserPromptSubmit hook 未精确指向 active marketplace detector",
    )
    required_environment_paths = {
        "RVF_USER_LOCAL_DATA_ROOT": user_local_data_root,
        "CODEX_RVF_LOG_ROOT": run_logs_root,
        "CODEX_RVF_STATE_DIR": runtime_state_root,
        "CODEX_RVF_INSTALLED_SKILL_DIR": (
            active_plugin_root / "skills/review-validate-fix"
        ),
    }
    for event_name, commands in (
        ("Stop", stop_commands),
        ("UserPromptSubmit", prompt_commands),
    ):
        _require(
            any(
                _command_injects_resolved_environment_paths(
                    command,
                    required_environment_paths,
                )
                for command in commands
            ),
            f"{event_name} hook 未注入统一 user-local data roots",
        )

    state = (
        dict(official_cli_state)
        if official_cli_state is not None
        else _query_official_cli_state(receipt, command_runner=command_runner)
    )
    codex_marketplace_payload = state.get("codex_marketplaces")
    codex_marketplaces = (
        codex_marketplace_payload.get("marketplaces", [])
        if isinstance(codex_marketplace_payload, dict)
        else []
    )
    codex_marketplace = next(
        (
            entry
            for entry in codex_marketplaces
            if isinstance(entry, dict) and entry.get("name") == MARKETPLACE_NAME
        ),
        None,
    )
    _require(codex_marketplace is not None, "Codex marketplace 未注册")
    _require(
        _marketplace_entry_path(codex_marketplace) == active_marketplace_root,
        "Codex marketplace source 未指向 active release",
    )
    codex_plugin_payload = state.get("codex_plugins")
    codex_plugins = (
        codex_plugin_payload.get("installed", [])
        if isinstance(codex_plugin_payload, dict)
        else []
    )
    codex_plugin = next(
        (
            entry
            for entry in codex_plugins
            if isinstance(entry, dict) and entry.get("pluginId") == PLUGIN_SELECTOR
        ),
        None,
    )
    _require(codex_plugin is not None, "Codex 缺少目标 plugin selector")
    _require(codex_plugin.get("version") == generated_plugin_version, "Codex plugin version 不匹配")
    _require(codex_plugin.get("enabled") is True, "Codex plugin 未启用")

    claude_marketplaces = state.get("claude_marketplaces")
    claude_marketplace = next(
        (
            entry
            for entry in claude_marketplaces
            if isinstance(claude_marketplaces, list)
            and isinstance(entry, dict)
            and entry.get("name") == MARKETPLACE_NAME
        ),
        None,
    ) if isinstance(claude_marketplaces, list) else None
    _require(claude_marketplace is not None, "Claude marketplace 未注册")
    _require(
        _marketplace_entry_path(claude_marketplace) == active_marketplace_root,
        "Claude marketplace source 未指向 active release",
    )
    claude_plugins = state.get("claude_plugins")
    claude_plugin = next(
        (
            entry
            for entry in claude_plugins
            if isinstance(claude_plugins, list)
            and isinstance(entry, dict)
            and entry.get("id") == PLUGIN_SELECTOR
        ),
        None,
    ) if isinstance(claude_plugins, list) else None
    _require(claude_plugin is not None, "Claude 缺少目标 plugin selector")
    _require(claude_plugin.get("version") == generated_plugin_version, "Claude plugin version 不匹配")
    _require(claude_plugin.get("enabled") is True, "Claude plugin 未启用")

    codex_cache_root = (
        home_directory
        / f".codex/plugins/cache/{MARKETPLACE_NAME}/{PLUGIN_NAME}"
        / generated_plugin_version
    )
    claude_install_path = claude_plugin.get("installPath")
    _require(
        isinstance(claude_install_path, str) and claude_install_path,
        "Claude plugin listing 缺少 installPath",
    )
    claude_cache_root = Path(claude_install_path).expanduser().resolve()
    _verify_plugin_payload_root(codex_cache_root, label="Codex official cache")
    _verify_plugin_payload_root(claude_cache_root, label="Claude official cache")
    active_plugin_digest = _directory_digest(active_plugin_root)
    _require(
        _directory_digest(codex_cache_root) == active_plugin_digest,
        "Codex official cache 与 active plugin payload 不一致",
    )
    _require(
        _directory_digest(claude_cache_root) == active_plugin_digest,
        "Claude official cache 与 active plugin payload 不一致",
    )
    synthetic_hook_smoke = _run_synthetic_installed_hook_smoke(
        active_plugin_root=active_plugin_root,
        deployment_source_worktree=deployment_source_worktree,
        home_directory=home_directory,
        user_local_data_root=user_local_data_root,
    )
    cli_versions = receipt.get("official_cli_versions")
    _require(
        isinstance(cli_versions, dict)
        and isinstance(cli_versions.get("codex"), str)
        and bool(cli_versions.get("codex"))
        and isinstance(cli_versions.get("claude"), str)
        and bool(cli_versions.get("claude")),
        "installation receipt 缺少官方 CLI version evidence",
    )

    return {
        "status": "ok",
        "deployment_source_worktree": receipt.get("deployment_source_worktree"),
        "local_main_commit": receipt.get("local_main_commit"),
        "generated_plugin_version": generated_plugin_version,
        "payload_digest": release_digest,
        "active_marketplace_root": str(active_marketplace_root),
        "user_local_data_root": str(user_local_data_root),
        "official_cli": {
            "codex": {
                "installed": True,
                "marketplace": MARKETPLACE_NAME,
                "plugin_selector": PLUGIN_SELECTOR,
                "cache_root": str(codex_cache_root),
            },
            "claude": {
                "installed": True,
                "marketplace": MARKETPLACE_NAME,
                "plugin_selector": PLUGIN_SELECTOR,
                "cache_root": str(claude_cache_root),
            },
        },
        "cache_payloads_match_active_release": True,
        "hooks_verified": ["Stop", "UserPromptSubmit"],
        "synthetic_hook_smoke": synthetic_hook_smoke,
        "official_cli_versions": dict(cli_versions),
    }


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation-receipt", type=Path, required=True)
    parser.add_argument("--hooks-path", type=Path, default=Path.home() / ".codex/hooks.json")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_arguments(argv)
    try:
        result = verify_local_deploy(
            installation_receipt_path=args.installation_receipt,
            hooks_path=args.hooks_path,
            home_directory=args.home,
        )
    except VerificationError as exc:
        print(f"RVF official CLI local deploy verification FAILED: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            "RVF official CLI local deploy verification OK: "
            f"{result['generated_plugin_version']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
