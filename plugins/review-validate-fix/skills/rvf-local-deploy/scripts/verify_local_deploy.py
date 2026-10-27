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
    Path("skills/review-validate-fix/scripts/task_snapshot_review_hook.py"),
    Path("skills/review-validate-fix/scripts/task_snapshot_review_hook.py"),
    Path("skills/rvf-local-deploy/SKILL.md"),
)


class VerificationError(RuntimeError):
    pass


class RegistrationUniquenessError(VerificationError):
    """宿主里存在**第二份** RVF 注册或第二棵 RVF payload 树。

    刻意与其余验证失败分成两类，因为**失败处置不同**：其余失败该回滚（部署没做成，回到部署前
    最干净），而这一类**绝不能回滚**——回滚会移除刚装好的 canonical，把机器留在「只剩残留那份」
    的更差状态。正确的 severity 是「部署做完但拒绝认证」，由人去清理残留。

    这个区别必须过得了进程边界：runner 是以子进程方式跑本脚本的，只看得见退出码与输出。所以
    `main()` 用 `REGISTRATION_UNIQUENESS_EXIT_CODE` 单独编码这一类，`rvf_local_deploy` 据此
    绕开回滚分支。子类化 `VerificationError` 是为了让「独立跑 verifier」的调用方无需改动。
    """


REGISTRATION_UNIQUENESS_EXIT_CODE = 3
"""`RegistrationUniquenessError` 的专属退出码（其余验证失败仍是 2）。

必须与 `scripts/rvf_local_deploy.py` 的
``OFFICIAL_CLI_VERIFIER_REGISTRATION_UNIQUENESS_EXIT_CODE`` 逐字一致——两边同源于一个
commit，且 `tests/test_official_cli_local_deploy_verification.py` 里有一条等值断言守着漂移。
"""


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


HOOK_TARGET_SCRIPT_SHELL_VARIABLE_NAME = "rvf_hook_target_script"
"""守卫命令串里承载目标脚本路径的 shell 变量名。

真相源是 ``core/hook_command_spawn_guard_against_missing_target_script.py`` 的同名常量。
本脚本刻意 stdlib-only、不 import ``core``（它要能在**任何**已部署 payload 上独立跑），
故自带一份副本，**两处必须同步**——``tests/test_hook_command_spawn_guard.py`` 有 gate。
"""


def _command_path_candidates(command: str) -> list[str]:
    """从 hook 命令串里取出所有**可能是脚本路径**的 token。

    ``python3 <path>`` 的裸 token 之外还要认 ``rvf_hook_target_script=<path>;`` 这种
    赋值形态：spawn 守卫把路径搬进了 shell 变量，裸 token 不再出现。刻意只剥这一个变量名
    的前缀，不泛化到任意 ``NAME=value``——否则 ``CODEX_RVF_*`` 那些 env 值也会被当成
    候选路径，把「精确指向」削弱成「出现过就算」。
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    assignment_prefix = f"{HOOK_TARGET_SCRIPT_SHELL_VARIABLE_NAME}="
    candidates: list[str] = []
    for token in tokens:
        candidate = token.rstrip(";")
        if candidate.startswith(assignment_prefix):
            candidate = candidate[len(assignment_prefix) :]
        if candidate:
            candidates.append(candidate)
    return candidates


def _command_targets_script(command: str, expected_script_path: Path) -> bool:
    expected = str(expected_script_path.resolve())
    return any(
        candidate == expected
        or (
            candidate.startswith("/")
            and Path(candidate).expanduser().resolve() == expected_script_path.resolve()
        )
        for candidate in _command_path_candidates(command)
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


def _review_validate_fix_plugin_selectors(
    installed_plugin_entries: Any,
    *,
    plugin_selector_field_name: str,
) -> list[str]:
    """列出宿主 CLI 里所有已安装的 ``review-validate-fix@*`` selector。"""
    if not isinstance(installed_plugin_entries, list):
        return []
    selectors: list[str] = []
    for entry in installed_plugin_entries:
        if not isinstance(entry, dict):
            continue
        selector = entry.get(plugin_selector_field_name)
        if not isinstance(selector, str) or not selector.startswith(f"{PLUGIN_NAME}@"):
            continue
        if entry.get("installed") is False:
            continue
        if selector not in selectors:
            selectors.append(selector)
    return sorted(selectors)


def _require_sole_review_validate_fix_registration(
    installed_plugin_entries: Any,
    *,
    plugin_selector_field_name: str,
    host_label: str,
    cleanup_command_template: str,
) -> None:
    """断言宿主里**只有一个** RVF 插件注册，且它就是 canonical selector。

    为什么必须是唯一性而不是存在性：既有断言全是 ``next(...)`` / ``any(...)`` 形态——只要
    canonical 那份在，第二份对它完全不可见。本机因此长期同时注册着两套 RVF：会话 skill 清单里
    6 个 skill 各出现两遍，两棵不同 commit 的插件树各自跑 hook、各写各的 ``review-highwater``
    高水位，把 committed-round 漏审那类盲区重新打开。存在性断言对这一切一路绿灯。

    抛 `RegistrationUniquenessError` 而不是 `VerificationError`：这一类失败**不得触发部署回滚**
    （见该异常的 docstring）。
    """
    selectors = _review_validate_fix_plugin_selectors(
        installed_plugin_entries,
        plugin_selector_field_name=plugin_selector_field_name,
    )
    extra_selectors = [
        selector for selector in selectors if selector != PLUGIN_SELECTOR
    ]
    if not extra_selectors:
        return
    cleanup_commands = " && ".join(
        cleanup_command_template.format(selector=selector)
        for selector in extra_selectors
    )
    raise RegistrationUniquenessError(
        f"{host_label} 存在多份 RVF 插件注册: {selectors}；"
        f"canonical 只应是 {PLUGIN_SELECTOR}。"
        f"清理命令: {cleanup_commands}"
    )


def _default_command_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_synthetic_installed_hook_smoke(*, active_plugin_root: Path, deployment_source_worktree: Path, home_directory: Path, user_local_data_root: Path) -> dict[str, Any]:
    hook = active_plugin_root / "skills/review-validate-fix/scripts/task_snapshot_review_hook.py"
    synthetic_environment = {**os.environ, "HOME":str(home_directory),
        "CODEX_HOME":str(home_directory / ".codex"), "CODEX_SQLITE_HOME":str(home_directory / ".codex-sqlite"),
        "RVF_USER_LOCAL_DATA_ROOT":str(user_local_data_root), "RVF_SUPPRESS_STOP_HOOK":"1"}
    evidence = {}
    for event_name in ("Stop", "UserPromptSubmit"):
        result = subprocess.run([sys.executable,str(hook),"--event",event_name,"--host","codex"],
            input=json.dumps({"cwd":str(deployment_source_worktree),"prompt":"安装隔离验收"}),
            env=synthetic_environment,capture_output=True,text=True,timeout=15)
        _require(result.returncode == 0, f"{event_name} hook 隔离验收失败: {result.stderr}")
        evidence[event_name] = {"status":"ok","script":str(hook),"returncode":result.returncode,"stdout_emitted":bool(result.stdout.strip())}
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
    # 共享包内的 hooks/hooks.json 只供 Claude 使用。空数组仍可能回退默认文件。
    codex_manifest = _read_json(plugin_root / ".codex-plugin/plugin.json")
    _require(
        codex_manifest.get("hooks") == {"hooks": {}},
        f"{label} Codex manifest 必须显式声明空 hooks 文档，排除 Claude bundled hooks",
    )
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
    stop_router = active_plugin_root / "skills/review-validate-fix/scripts/task_snapshot_review_hook.py"
    prompt_hook = active_plugin_root / "skills/review-validate-fix/scripts/task_snapshot_review_hook.py"
    _require(
        any(_command_targets_script(command, stop_router) for command in stop_commands),
        "Stop hook 未精确指向 active marketplace router",
    )
    _require(
        any(_command_targets_script(command, prompt_hook) for command in prompt_commands),
        "UserPromptSubmit hook 未精确指向 active marketplace detector",
    )
    required_environment_paths = {"RVF_USER_LOCAL_DATA_ROOT": user_local_data_root}
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
    _require_sole_review_validate_fix_registration(
        codex_plugins,
        plugin_selector_field_name="pluginId",
        host_label="Codex",
        cleanup_command_template="codex plugin remove {selector} --json",
    )

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
    _require_sole_review_validate_fix_registration(
        claude_plugins,
        plugin_selector_field_name="id",
        host_label="Claude",
        cleanup_command_template="claude plugin uninstall {selector} --scope user",
    )

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
    # 注册被清掉、cache 树还留着 = 第二套代码仍在磁盘上；旧会话 rollout 固化的 skill 根与 hook
    # 绝对路径会继续指向它。CLI 的 remove 是否连带删 cache 并不确定，所以这里独立断言一次。
    # 刻意排在 canonical cache 的存在性断言之后：cache 整体缺失时，那条报错比这条清楚得多。
    codex_plugin_cache_roots = sorted(
        path
        for path in (home_directory / ".codex/plugins/cache").glob(f"*/{PLUGIN_NAME}")
        if path.is_dir()
    )
    if codex_plugin_cache_roots != [codex_cache_root.parent]:
        # 与上面两条注册唯一性断言同一类失败（多出来的第二套代码），因此同样**不得回滚**。
        raise RegistrationUniquenessError(
            "Codex plugin cache 里存在多份 RVF payload 树: "
            f"{[str(path) for path in codex_plugin_cache_roots]}；"
            f"canonical 只应是 {codex_cache_root.parent}。"
            "请在解除对应 plugin 注册后删除多余目录。"
        )
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
        "codex_bundled_hooks_excluded_by_manifest": True,
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
    except RegistrationUniquenessError as exc:
        # 必须排在通用 VerificationError 之前：调用方按退出码决定「回滚」还是「保留部署、拒绝认证」。
        print(
            "RVF official CLI local deploy verification FAILED (registration uniqueness): "
            f"{exc}",
            file=sys.stderr,
        )
        return REGISTRATION_UNIQUENESS_EXIT_CODE
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
