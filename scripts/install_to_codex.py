#!/usr/bin/env python3
"""从干净 local main 构建 RVF marketplace，并交给宿主官方 CLI 安装。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.hook_command_spawn_guard_against_missing_target_script import (  # noqa: E402
    hook_command_guarded_against_missing_target_script,
)
from core.live_codex_sessions_holding_plugin_cache_paths import (  # noqa: E402
    live_codex_session_warning_lines,
    live_codex_sessions_holding_plugin_cache_paths,
    plugin_cache_path_prefixes_for_selectors,
)
from core.rvf_user_local_data_paths import (  # noqa: E402
    RvfUserLocalDataPaths,
    resolved_rvf_user_local_data_paths,
)


PLUGIN_NAME = "review-validate-fix"
PLUGIN_SKILL_RELATIVE_PATH = Path("skills/review-validate-fix")
DEVELOPER_MARKETPLACE_NAME = "review-validate-fix-local-main"
DEVELOPER_PLUGIN_SELECTOR = f"{PLUGIN_NAME}@{DEVELOPER_MARKETPLACE_NAME}"
DEVELOPER_MARKETPLACE_ROOT_RELATIVE_PATH = (
    Path(".rvf/developer-plugin-marketplaces") / DEVELOPER_MARKETPLACE_NAME
)
DEVELOPER_MARKETPLACE_RELEASES_ROOT_RELATIVE_PATH = (
    Path(".rvf/developer-plugin-marketplace-releases") / PLUGIN_NAME
)
RELEASE_METADATA_FILE_NAME = ".rvf-local-main-release.json"
DEPLOYMENT_RECEIPT_FILE_NAME = "latest-official-cli-installation.json"
DEPLOYMENT_RECEIPT_SCHEMA_VERSION = 2
DEPLOY_STAMP_RE = re.compile(r"\s+\[deployed [^\]]+\]$")
STABLE_BASE_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
FULL_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
LEGACY_DEFAULT_CLINE_KANBAN_ENV = {
    "CODEX_RVF_CLINE_KANBAN_START_CMD": "npx -y kanban@0.1.66 --no-open",
    "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "npx -y kanban@0.1.66 task",
}
CLINE_KANBAN_WORKTREE_MODES = {"branch", "inplace"}
BUNDLE_IGNORED_NAMES = {
    ".DS_Store",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".rvf-dev-only",
    "__pycache__",
    "check_plugin_contracts.py",
    "check_skill_contracts.sh",
    "dev-only",
    "dev_only",
    "install_to_codex.py",
}


class OfficialCliInstallationError(RuntimeError):
    pass


class DeveloperMarketplaceTransactionError(OfficialCliInstallationError):
    def __init__(self, message: str, *, rollback_status: str) -> None:
        super().__init__(f"{message}; rollback_status={rollback_status}")
        self.rollback_status = rollback_status


@dataclass(frozen=True)
class DeveloperMarketplaceRelease:
    release_root: Path
    source_repository: Path
    source_commit: str
    source_commit_count: int
    base_plugin_version: str
    generated_plugin_version: str
    payload_digest: str


@dataclass(frozen=True)
class OfficialCliInstallationResult:
    codex_marketplace_preexisting: bool
    codex_plugin_preinstalled: bool
    claude_marketplace_preexisting: bool
    claude_plugin_preinstalled: bool
    commands: tuple[tuple[str, ...], ...]
    removed_noncanonical_codex_plugin_selectors: tuple[str, ...]
    removed_noncanonical_claude_plugin_selectors: tuple[str, ...]
    live_codex_sessions_holding_plugin_cache_paths_to_be_removed: tuple[
        dict[str, Any], ...
    ] = ()
    """部署前检测到的、可能仍在运行且持有即将被删 plugin cache 路径的 Codex 会话。

    **只告警、不阻断**：判据本身有已知假阴性（liveness 用 rollout mtime 近似），而硬闸也
    不可行——Kanban 会在 codex 进程死亡后立刻重启新会话，「等所有 codex 退出」不可达。
    空列表意味着「未发现」，不等于「已确认安全」。
    """


@dataclass(frozen=True)
class DeveloperMarketplaceDeploymentTransactionResult:
    active_marketplace_root: Path
    official_cli_installation: OfficialCliInstallationResult
    rollback_status: str


@dataclass(frozen=True)
class ActiveMarketplaceReplacement:
    active_marketplace_root: Path
    previous_active_marketplace_backup_root: Path | None


OfficialCliCommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OfficialCliInstallationError(f"无法读取 JSON object: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise OfficialCliInstallationError(f"JSON 顶层必须是 object: {path}")
    return payload


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)
    return path


def generated_developer_plugin_version(
    *,
    base_version: str,
    commit_count: int,
    full_commit: str,
) -> str:
    if not STABLE_BASE_VERSION_RE.fullmatch(base_version):
        raise OfficialCliInstallationError(
            f"developer bundle 要求 X.Y.Z base version，实际为 {base_version!r}"
        )
    if commit_count < 1:
        raise OfficialCliInstallationError("Git commit count 必须为正整数")
    normalized_commit = full_commit.strip().lower()
    if not FULL_GIT_COMMIT_RE.fullmatch(normalized_commit):
        raise OfficialCliInstallationError(
            f"source commit 必须是完整 40 位小写 SHA: {full_commit!r}"
        )
    return f"{base_version}-dev.{commit_count}+g{normalized_commit[:12]}"


def _bundle_copy_ignore(_: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in BUNDLE_IGNORED_NAMES or name.endswith(".pyc")
    }


def _remove_path_if_present(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _stamp_skill_heading_text(text: str, heading_label: str) -> str:
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        newline = "\n" if line.endswith("\n") else ""
        content = line[:-1] if newline else line
        if not content.startswith("# "):
            continue
        lines[index] = (
            f"{DEPLOY_STAMP_RE.sub('', content)} [deployed {heading_label}]"
            f"{newline}"
        )
        break
    return "".join(lines)


def _stamp_generated_skill_headings(plugin_root: Path, source_commit: str) -> None:
    for skill_markdown_path in sorted((plugin_root / "skills").glob("*/SKILL.md")):
        original_text = skill_markdown_path.read_text(encoding="utf-8")
        skill_markdown_path.write_text(
            _stamp_skill_heading_text(original_text, source_commit[:12]),
            encoding="utf-8",
        )


def _patch_generated_plugin_manifests(
    plugin_root: Path,
    generated_plugin_version: str,
) -> None:
    for manifest_relative_path in (
        Path(".codex-plugin/plugin.json"),
        Path(".claude-plugin/plugin.json"),
    ):
        manifest_path = plugin_root / manifest_relative_path
        manifest = read_json_object(manifest_path)
        if manifest.get("name") != PLUGIN_NAME:
            raise OfficialCliInstallationError(
                f"plugin manifest name 不匹配: {manifest_path}"
            )
        manifest["version"] = generated_plugin_version
        write_json_atomic(manifest_path, manifest)


def _patch_generated_marketplace_manifests(
    release_root: Path,
    generated_plugin_version: str,
) -> None:
    codex_marketplace_path = release_root / ".agents/plugins/marketplace.json"
    codex_marketplace = read_json_object(codex_marketplace_path)
    codex_marketplace["name"] = DEVELOPER_MARKETPLACE_NAME
    codex_plugins = codex_marketplace.get("plugins")
    if not isinstance(codex_plugins, list) or len(codex_plugins) != 1:
        raise OfficialCliInstallationError("Codex marketplace 必须只声明 RVF plugin")
    codex_plugin_entry = codex_plugins[0]
    if not isinstance(codex_plugin_entry, dict):
        raise OfficialCliInstallationError("Codex marketplace plugin entry 必须是 object")
    codex_plugin_entry["name"] = PLUGIN_NAME
    codex_plugin_entry["source"] = {
        "source": "local",
        "path": f"./plugins/{PLUGIN_NAME}",
    }
    write_json_atomic(codex_marketplace_path, codex_marketplace)

    claude_marketplace_path = release_root / ".claude-plugin/marketplace.json"
    claude_marketplace = read_json_object(claude_marketplace_path)
    claude_marketplace["name"] = DEVELOPER_MARKETPLACE_NAME
    claude_plugins = claude_marketplace.get("plugins")
    if not isinstance(claude_plugins, list) or len(claude_plugins) != 1:
        raise OfficialCliInstallationError("Claude marketplace 必须只声明 RVF plugin")
    claude_plugin_entry = claude_plugins[0]
    if not isinstance(claude_plugin_entry, dict):
        raise OfficialCliInstallationError("Claude marketplace plugin entry 必须是 object")
    claude_plugin_entry["name"] = PLUGIN_NAME
    claude_plugin_entry["source"] = f"./plugins/{PLUGIN_NAME}"
    claude_plugin_entry["version"] = generated_plugin_version
    write_json_atomic(claude_marketplace_path, claude_marketplace)


def _is_excluded_deployment_digest_path(relative_path: Path) -> bool:
    return "__pycache__" in relative_path.parts or relative_path.suffix == ".pyc"


def deployment_file_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative_path = path.relative_to(root)
        if (
            relative_path == Path(RELEASE_METADATA_FILE_NAME)
            or _is_excluded_deployment_digest_path(relative_path)
        ):
            continue
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_developer_marketplace_release(
    *,
    source_repository: Path,
    home_directory: Path,
    source_commit: str,
    source_commit_count: int,
) -> DeveloperMarketplaceRelease:
    source_repository = source_repository.expanduser().resolve()
    home_directory = home_directory.expanduser().resolve()
    plugin_source_root = source_repository / f"plugins/{PLUGIN_NAME}"
    codex_marketplace_source = source_repository / ".agents/plugins/marketplace.json"
    claude_marketplace_source = source_repository / ".claude-plugin/marketplace.json"
    required_paths = (
        plugin_source_root,
        codex_marketplace_source,
        claude_marketplace_source,
        source_repository / "core",
        source_repository / "adapters",
        source_repository / ".rvf-pyroot",
    )
    missing_paths = [str(path) for path in required_paths if not path.exists()]
    if missing_paths:
        raise OfficialCliInstallationError(
            f"developer marketplace source 缺少必要路径: {missing_paths}"
        )

    base_plugin_manifest = read_json_object(
        plugin_source_root / ".codex-plugin/plugin.json"
    )
    base_plugin_version = base_plugin_manifest.get("version")
    if not isinstance(base_plugin_version, str):
        raise OfficialCliInstallationError("Codex plugin manifest 缺少 version")
    generated_plugin_version = generated_developer_plugin_version(
        base_version=base_plugin_version,
        commit_count=source_commit_count,
        full_commit=source_commit,
    )

    releases_root = (
        home_directory / DEVELOPER_MARKETPLACE_RELEASES_ROOT_RELATIVE_PATH
    )
    release_root = releases_root / source_commit
    if release_root.is_dir():
        metadata = read_json_object(release_root / RELEASE_METADATA_FILE_NAME)
        if metadata.get("source_commit") != source_commit:
            raise OfficialCliInstallationError(
                f"immutable release metadata 与目录 SHA 不匹配: {release_root}"
            )
        payload_digest = deployment_file_digest(release_root)
        if metadata.get("payload_digest") != payload_digest:
            raise OfficialCliInstallationError(
                f"immutable release payload digest 漂移: {release_root}"
            )
        return DeveloperMarketplaceRelease(
            release_root=release_root,
            source_repository=source_repository,
            source_commit=source_commit,
            source_commit_count=source_commit_count,
            base_plugin_version=base_plugin_version,
            generated_plugin_version=generated_plugin_version,
            payload_digest=payload_digest,
        )

    releases_root.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(
            prefix=f".{source_commit}.staging-",
            dir=str(releases_root),
        )
    )
    try:
        (staging_root / ".agents/plugins").mkdir(parents=True)
        shutil.copy2(
            codex_marketplace_source,
            staging_root / ".agents/plugins/marketplace.json",
        )
        (staging_root / ".claude-plugin").mkdir(parents=True)
        shutil.copy2(
            claude_marketplace_source,
            staging_root / ".claude-plugin/marketplace.json",
        )
        generated_plugin_root = staging_root / f"plugins/{PLUGIN_NAME}"
        shutil.copytree(
            plugin_source_root,
            generated_plugin_root,
            ignore=_bundle_copy_ignore,
        )
        for package_name in ("core", "adapters"):
            shutil.copytree(
                source_repository / package_name,
                generated_plugin_root / package_name,
                ignore=_bundle_copy_ignore,
            )
        shutil.copy2(
            source_repository / ".rvf-pyroot",
            generated_plugin_root / ".rvf-pyroot",
        )
        _remove_path_if_present(
            generated_plugin_root / PLUGIN_SKILL_RELATIVE_PATH / "state"
        )
        _remove_path_if_present(
            generated_plugin_root
            / PLUGIN_SKILL_RELATIVE_PATH
            / "config/reviewer-registry.json"
        )
        _patch_generated_plugin_manifests(
            generated_plugin_root,
            generated_plugin_version,
        )
        _patch_generated_marketplace_manifests(
            staging_root,
            generated_plugin_version,
        )
        _stamp_generated_skill_headings(generated_plugin_root, source_commit)
        payload_digest = deployment_file_digest(staging_root)
        write_json_atomic(
            staging_root / RELEASE_METADATA_FILE_NAME,
            {
                "schema_version": 1,
                "source_repository": str(source_repository),
                "source_commit": source_commit,
                "source_commit_count": source_commit_count,
                "base_plugin_version": base_plugin_version,
                "generated_plugin_version": generated_plugin_version,
                "payload_digest": payload_digest,
            },
        )
        try:
            staging_root.replace(release_root)
        except FileExistsError:
            shutil.rmtree(staging_root)
    except Exception:
        if staging_root.exists():
            shutil.rmtree(staging_root)
        raise

    return DeveloperMarketplaceRelease(
        release_root=release_root,
        source_repository=source_repository,
        source_commit=source_commit,
        source_commit_count=source_commit_count,
        base_plugin_version=base_plugin_version,
        generated_plugin_version=generated_plugin_version,
        payload_digest=payload_digest,
    )


def _prepare_active_marketplace_replacement(
    release: DeveloperMarketplaceRelease,
    *,
    home_directory: Path,
) -> ActiveMarketplaceReplacement:
    active_marketplace_root = (
        home_directory.expanduser().resolve()
        / DEVELOPER_MARKETPLACE_ROOT_RELATIVE_PATH
    )
    active_marketplace_root.parent.mkdir(parents=True, exist_ok=True)
    active_staging_root = Path(
        tempfile.mkdtemp(
            prefix=f".{DEVELOPER_MARKETPLACE_NAME}.active-staging-",
            dir=str(active_marketplace_root.parent),
        )
    )
    shutil.rmtree(active_staging_root)
    shutil.copytree(release.release_root, active_staging_root)
    previous_active_root = active_marketplace_root.with_name(
        f".{active_marketplace_root.name}.previous-{os.getpid()}"
    )
    _remove_path_if_present(previous_active_root)
    previous_active_marketplace_backup_root: Path | None = None
    if active_marketplace_root.exists():
        active_marketplace_root.replace(previous_active_root)
        previous_active_marketplace_backup_root = previous_active_root
    try:
        active_staging_root.replace(active_marketplace_root)
    except Exception:
        if previous_active_root.exists():
            previous_active_root.replace(active_marketplace_root)
        raise
    return ActiveMarketplaceReplacement(
        active_marketplace_root=active_marketplace_root,
        previous_active_marketplace_backup_root=(
            previous_active_marketplace_backup_root
        ),
    )


def _finalize_active_marketplace_replacement(
    replacement: ActiveMarketplaceReplacement,
) -> None:
    if replacement.previous_active_marketplace_backup_root is not None:
        _remove_path_if_present(replacement.previous_active_marketplace_backup_root)


def _restore_previous_active_marketplace(
    replacement: ActiveMarketplaceReplacement,
) -> bool:
    _remove_path_if_present(replacement.active_marketplace_root)
    previous_backup_root = replacement.previous_active_marketplace_backup_root
    if previous_backup_root is None or not previous_backup_root.exists():
        return False
    previous_backup_root.replace(replacement.active_marketplace_root)
    return True


def activate_developer_marketplace_release(
    release: DeveloperMarketplaceRelease,
    *,
    home_directory: Path,
) -> Path:
    replacement = _prepare_active_marketplace_replacement(
        release,
        home_directory=home_directory,
    )
    _finalize_active_marketplace_replacement(replacement)
    return replacement.active_marketplace_root


def default_official_cli_command_runner(
    command: list[str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )


def resolved_official_cli_executable(executable_name: str) -> Path:
    resolved = shutil.which(executable_name)
    if not resolved:
        raise OfficialCliInstallationError(
            f"未找到官方 {executable_name} executable；请先安装或升级宿主 CLI"
        )
    return Path(resolved).resolve()


def official_cli_version(executable: Path) -> str:
    completed = default_official_cli_command_runner([str(executable), "--version"])
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise OfficialCliInstallationError(
            f"无法读取官方 CLI version: {executable}: {detail}"
        )
    version = completed.stdout.strip() or completed.stderr.strip()
    if not version:
        raise OfficialCliInstallationError(
            f"官方 CLI version 输出为空: {executable}"
        )
    return version


def _run_official_cli_command(
    command: list[str],
    *,
    command_runner: OfficialCliCommandRunner,
    recorded_commands: list[tuple[str, ...]],
) -> str:
    recorded_commands.append(tuple(command))
    completed = command_runner(command)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise OfficialCliInstallationError(
            f"官方 CLI 命令失败 ({completed.returncode}): {command!r}: {detail}"
        )
    return completed.stdout.strip()


def _official_cli_json(
    command: list[str],
    *,
    command_runner: OfficialCliCommandRunner,
    recorded_commands: list[tuple[str, ...]],
) -> Any:
    output = _run_official_cli_command(
        command,
        command_runner=command_runner,
        recorded_commands=recorded_commands,
    )
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise OfficialCliInstallationError(
            f"官方 CLI 未返回有效 JSON: {command!r}"
        ) from exc


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


def noncanonical_review_validate_fix_plugin_selectors_in_official_cli_listing(
    installed_plugin_entries: Iterable[Any],
    *,
    plugin_selector_field_name: str,
) -> tuple[str, ...]:
    """列出「插件名是 RVF、但 selector 不是 canonical」的已安装条目。

    为什么必须**枚举**而不是等值匹配 canonical selector：安装器过去只认
    ``review-validate-fix@review-validate-fix-local-main`` 一个字符串，于是任何经**别的**
    marketplace 注册的同名插件（本机实测的 ``review-validate-fix@local-codex-plugins``）对它
    结构性不可见——每次部署都只刷新 canonical 那份，旧那份原封不动地活下来，两套 RVF 代码在同
    一台机器上并行执行、各写各的 state 根。

    ``review-validate-fix@*`` 这整个 selector 命名空间属于 RVF 自己，不存在「用户有意保留的第
    二份 RVF 安装」这种合法情形，所以枚举到的非 canonical 条目一律该被清掉。

    **刻意只针对 plugin，不连带删 marketplace**：marketplace 名（如 ``local-codex-plugins``）
    是通用容器名，可能同时托管别的插件，删掉会误伤第三方；而真正造成重复 skill 与重复 hook 的
    是**已安装的 plugin 注册**，不是「某个 marketplace 提供了 RVF」。
    """
    canonical_marketplace_prefix = f"{PLUGIN_NAME}@"
    selectors: list[str] = []
    for entry in installed_plugin_entries:
        if not isinstance(entry, dict):
            continue
        selector = entry.get(plugin_selector_field_name)
        if not isinstance(selector, str) or not selector.startswith(
            canonical_marketplace_prefix
        ):
            continue
        if selector == DEVELOPER_PLUGIN_SELECTOR:
            continue
        if entry.get("installed") is False:
            continue
        if selector not in selectors:
            selectors.append(selector)
    return tuple(sorted(selectors))


def install_developer_marketplace_with_official_clis(
    *,
    active_marketplace_root: Path,
    codex_executable: Path,
    claude_executable: Path,
    command_runner: OfficialCliCommandRunner = default_official_cli_command_runner,
) -> OfficialCliInstallationResult:
    active_marketplace_root = active_marketplace_root.expanduser().resolve()
    codex_executable_text = str(codex_executable)
    claude_executable_text = str(claude_executable)
    recorded_commands: list[tuple[str, ...]] = []

    codex_marketplace_payload = _official_cli_json(
        [codex_executable_text, "plugin", "marketplace", "list", "--json"],
        command_runner=command_runner,
        recorded_commands=recorded_commands,
    )
    codex_plugin_payload = _official_cli_json(
        [codex_executable_text, "plugin", "list", "--json"],
        command_runner=command_runner,
        recorded_commands=recorded_commands,
    )
    claude_marketplace_payload = _official_cli_json(
        [claude_executable_text, "plugin", "marketplace", "list", "--json"],
        command_runner=command_runner,
        recorded_commands=recorded_commands,
    )
    claude_plugin_payload = _official_cli_json(
        [claude_executable_text, "plugin", "list", "--json"],
        command_runner=command_runner,
        recorded_commands=recorded_commands,
    )

    codex_marketplaces = (
        codex_marketplace_payload.get("marketplaces", [])
        if isinstance(codex_marketplace_payload, dict)
        else []
    )
    codex_installed_plugins = (
        codex_plugin_payload.get("installed", [])
        if isinstance(codex_plugin_payload, dict)
        else []
    )
    claude_marketplaces = (
        claude_marketplace_payload if isinstance(claude_marketplace_payload, list) else []
    )
    claude_installed_plugins = (
        claude_plugin_payload if isinstance(claude_plugin_payload, list) else []
    )

    # 先把非 canonical 的 RVF 注册清掉，再动 canonical：否则本次部署又只刷新一份、留下另一份。
    removed_noncanonical_codex_plugin_selectors = (
        noncanonical_review_validate_fix_plugin_selectors_in_official_cli_listing(
            codex_installed_plugins,
            plugin_selector_field_name="pluginId",
        )
    )
    # 动任何 plugin cache 之前先看有没有 live 会话持有它。``codex plugin remove`` 会连带
    # 删掉整棵 payload 树，而已在运行的会话持有的是**启动时的注册快照**——2026-08-18 的
    # 286 次无上界续跑就是这么来的。只告警不阻断，理由见结果字段的 docstring。
    live_codex_sessions_holding_removed_paths = live_codex_sessions_holding_plugin_cache_paths(
        codex_home=Path.home() / ".codex",
        plugin_cache_path_prefixes=plugin_cache_path_prefixes_for_selectors(
            codex_home=Path.home() / ".codex",
            plugin_selectors=(
                *removed_noncanonical_codex_plugin_selectors,
                DEVELOPER_PLUGIN_SELECTOR,
            ),
        ),
    )
    for warning_line in live_codex_session_warning_lines(
        live_codex_sessions_holding_removed_paths
    ):
        print(warning_line, file=sys.stderr)
    for stale_selector in removed_noncanonical_codex_plugin_selectors:
        _run_official_cli_command(
            [codex_executable_text, "plugin", "remove", stale_selector, "--json"],
            command_runner=command_runner,
            recorded_commands=recorded_commands,
        )
    removed_noncanonical_claude_plugin_selectors = (
        noncanonical_review_validate_fix_plugin_selectors_in_official_cli_listing(
            claude_installed_plugins,
            plugin_selector_field_name="id",
        )
    )
    for stale_selector in removed_noncanonical_claude_plugin_selectors:
        _run_official_cli_command(
            [
                claude_executable_text,
                "plugin",
                "uninstall",
                stale_selector,
                "--scope",
                "user",
            ],
            command_runner=command_runner,
            recorded_commands=recorded_commands,
        )

    codex_marketplace_entry = next(
        (
            entry
            for entry in codex_marketplaces
            if isinstance(entry, dict)
            and entry.get("name") == DEVELOPER_MARKETPLACE_NAME
        ),
        None,
    )
    codex_plugin_entry = next(
        (
            entry
            for entry in codex_installed_plugins
            if isinstance(entry, dict)
            and entry.get("pluginId") == DEVELOPER_PLUGIN_SELECTOR
            and entry.get("installed") is not False
        ),
        None,
    )
    if codex_marketplace_entry is not None and (
        _marketplace_entry_path(codex_marketplace_entry) != active_marketplace_root
    ):
        _run_official_cli_command(
            [
                codex_executable_text,
                "plugin",
                "marketplace",
                "remove",
                DEVELOPER_MARKETPLACE_NAME,
                "--json",
            ],
            command_runner=command_runner,
            recorded_commands=recorded_commands,
        )
        codex_marketplace_entry = None
    if codex_marketplace_entry is None:
        _run_official_cli_command(
            [
                codex_executable_text,
                "plugin",
                "marketplace",
                "add",
                str(active_marketplace_root),
                "--json",
            ],
            command_runner=command_runner,
            recorded_commands=recorded_commands,
        )
    if codex_plugin_entry is not None:
        _run_official_cli_command(
            [
                codex_executable_text,
                "plugin",
                "remove",
                DEVELOPER_PLUGIN_SELECTOR,
                "--json",
            ],
            command_runner=command_runner,
            recorded_commands=recorded_commands,
        )
    _run_official_cli_command(
        [
            codex_executable_text,
            "plugin",
            "add",
            DEVELOPER_PLUGIN_SELECTOR,
            "--json",
        ],
        command_runner=command_runner,
        recorded_commands=recorded_commands,
    )
    claude_marketplace_entry = next(
        (
            entry
            for entry in claude_marketplaces
            if isinstance(entry, dict)
            and entry.get("name") == DEVELOPER_MARKETPLACE_NAME
        ),
        None,
    )
    claude_plugin_entry = next(
        (
            entry
            for entry in claude_installed_plugins
            if isinstance(entry, dict) and entry.get("id") == DEVELOPER_PLUGIN_SELECTOR
        ),
        None,
    )
    if claude_marketplace_entry is not None and (
        _marketplace_entry_path(claude_marketplace_entry) != active_marketplace_root
    ):
        _run_official_cli_command(
            [
                claude_executable_text,
                "plugin",
                "marketplace",
                "remove",
                DEVELOPER_MARKETPLACE_NAME,
            ],
            command_runner=command_runner,
            recorded_commands=recorded_commands,
        )
        claude_marketplace_entry = None
    if claude_marketplace_entry is None:
        _run_official_cli_command(
            [
                claude_executable_text,
                "plugin",
                "marketplace",
                "add",
                str(active_marketplace_root),
                "--scope",
                "user",
            ],
            command_runner=command_runner,
            recorded_commands=recorded_commands,
        )
    else:
        _run_official_cli_command(
            [
                claude_executable_text,
                "plugin",
                "marketplace",
                "update",
                DEVELOPER_MARKETPLACE_NAME,
            ],
            command_runner=command_runner,
            recorded_commands=recorded_commands,
        )
    if claude_plugin_entry is None:
        claude_plugin_action = "install"
    else:
        claude_plugin_action = "update"
    _run_official_cli_command(
        [
            claude_executable_text,
            "plugin",
            claude_plugin_action,
            DEVELOPER_PLUGIN_SELECTOR,
            "--scope",
            "user",
        ],
        command_runner=command_runner,
        recorded_commands=recorded_commands,
    )

    return OfficialCliInstallationResult(
        codex_marketplace_preexisting=codex_marketplace_entry is not None,
        codex_plugin_preinstalled=codex_plugin_entry is not None,
        claude_marketplace_preexisting=claude_marketplace_entry is not None,
        claude_plugin_preinstalled=claude_plugin_entry is not None,
        commands=tuple(recorded_commands),
        removed_noncanonical_codex_plugin_selectors=(
            removed_noncanonical_codex_plugin_selectors
        ),
        removed_noncanonical_claude_plugin_selectors=(
            removed_noncanonical_claude_plugin_selectors
        ),
        live_codex_sessions_holding_plugin_cache_paths_to_be_removed=tuple(
            {
                "session_id": finding.session_id,
                "rollout_path": str(finding.rollout_path),
                "held_plugin_cache_paths": list(finding.held_plugin_cache_paths),
            }
            for finding in live_codex_sessions_holding_removed_paths
        ),
    )


def remove_developer_marketplace_with_official_clis(
    *,
    codex_executable: Path,
    claude_executable: Path,
    command_runner: OfficialCliCommandRunner = default_official_cli_command_runner,
) -> tuple[tuple[str, ...], ...]:
    recorded_commands: list[tuple[str, ...]] = []
    codex_executable_text = str(codex_executable)
    claude_executable_text = str(claude_executable)
    codex_plugin_payload = _official_cli_json(
        [codex_executable_text, "plugin", "list", "--json"],
        command_runner=command_runner,
        recorded_commands=recorded_commands,
    )
    codex_marketplace_payload = _official_cli_json(
        [codex_executable_text, "plugin", "marketplace", "list", "--json"],
        command_runner=command_runner,
        recorded_commands=recorded_commands,
    )
    claude_plugin_payload = _official_cli_json(
        [claude_executable_text, "plugin", "list", "--json"],
        command_runner=command_runner,
        recorded_commands=recorded_commands,
    )
    claude_marketplace_payload = _official_cli_json(
        [claude_executable_text, "plugin", "marketplace", "list", "--json"],
        command_runner=command_runner,
        recorded_commands=recorded_commands,
    )
    codex_plugins = (
        codex_plugin_payload.get("installed", [])
        if isinstance(codex_plugin_payload, dict)
        else []
    )
    codex_marketplaces = (
        codex_marketplace_payload.get("marketplaces", [])
        if isinstance(codex_marketplace_payload, dict)
        else []
    )
    if any(
        isinstance(entry, dict)
        and entry.get("pluginId") == DEVELOPER_PLUGIN_SELECTOR
        for entry in codex_plugins
    ):
        _run_official_cli_command(
            [
                codex_executable_text,
                "plugin",
                "remove",
                DEVELOPER_PLUGIN_SELECTOR,
                "--json",
            ],
            command_runner=command_runner,
            recorded_commands=recorded_commands,
        )
    if any(
        isinstance(entry, dict)
        and entry.get("name") == DEVELOPER_MARKETPLACE_NAME
        for entry in codex_marketplaces
    ):
        _run_official_cli_command(
            [
                codex_executable_text,
                "plugin",
                "marketplace",
                "remove",
                DEVELOPER_MARKETPLACE_NAME,
                "--json",
            ],
            command_runner=command_runner,
            recorded_commands=recorded_commands,
        )
    if isinstance(claude_plugin_payload, list) and any(
        isinstance(entry, dict) and entry.get("id") == DEVELOPER_PLUGIN_SELECTOR
        for entry in claude_plugin_payload
    ):
        _run_official_cli_command(
            [
                claude_executable_text,
                "plugin",
                "uninstall",
                DEVELOPER_PLUGIN_SELECTOR,
                "--scope",
                "user",
            ],
            command_runner=command_runner,
            recorded_commands=recorded_commands,
        )
    if isinstance(claude_marketplace_payload, list) and any(
        isinstance(entry, dict)
        and entry.get("name") == DEVELOPER_MARKETPLACE_NAME
        for entry in claude_marketplace_payload
    ):
        _run_official_cli_command(
            [
                claude_executable_text,
                "plugin",
                "marketplace",
                "remove",
                DEVELOPER_MARKETPLACE_NAME,
            ],
            command_runner=command_runner,
            recorded_commands=recorded_commands,
        )
    return tuple(recorded_commands)


def deploy_developer_marketplace_transaction(
    *,
    release: DeveloperMarketplaceRelease,
    home_directory: Path,
    codex_executable: Path,
    claude_executable: Path,
    command_runner: OfficialCliCommandRunner = default_official_cli_command_runner,
) -> DeveloperMarketplaceDeploymentTransactionResult:
    replacement = _prepare_active_marketplace_replacement(
        release,
        home_directory=home_directory,
    )
    try:
        official_cli_installation = install_developer_marketplace_with_official_clis(
            active_marketplace_root=replacement.active_marketplace_root,
            codex_executable=codex_executable,
            claude_executable=claude_executable,
            command_runner=command_runner,
        )
    except Exception as installation_error:
        previous_release_restored = _restore_previous_active_marketplace(replacement)
        try:
            if previous_release_restored:
                install_developer_marketplace_with_official_clis(
                    active_marketplace_root=replacement.active_marketplace_root,
                    codex_executable=codex_executable,
                    claude_executable=claude_executable,
                    command_runner=command_runner,
                )
                rollback_status = "restored_previous_release"
            else:
                remove_developer_marketplace_with_official_clis(
                    codex_executable=codex_executable,
                    claude_executable=claude_executable,
                    command_runner=command_runner,
                )
                rollback_status = "removed_partial_first_install"
        except Exception as rollback_error:
            rollback_status = "rollback_failed"
            raise DeveloperMarketplaceTransactionError(
                f"official CLI install 失败: {installation_error}; "
                f"rollback 也失败: {rollback_error}",
                rollback_status=rollback_status,
            ) from installation_error
        raise DeveloperMarketplaceTransactionError(
            f"official CLI install 失败: {installation_error}",
            rollback_status=rollback_status,
        ) from installation_error
    _finalize_active_marketplace_replacement(replacement)
    return DeveloperMarketplaceDeploymentTransactionResult(
        active_marketplace_root=replacement.active_marketplace_root,
        official_cli_installation=official_cli_installation,
        rollback_status="not_required",
    )


def developer_marketplace_release_from_installation_receipt(
    installation_receipt: Mapping[str, Any],
) -> DeveloperMarketplaceRelease:
    release_root_value = installation_receipt.get("release_root")
    if not isinstance(release_root_value, str) or not release_root_value.strip():
        raise OfficialCliInstallationError(
            "previous installation receipt 缺少 immutable release_root"
        )
    release_root = Path(release_root_value).expanduser().resolve()
    metadata = read_json_object(release_root / RELEASE_METADATA_FILE_NAME)
    source_repository_value = metadata.get("source_repository")
    source_commit = metadata.get("source_commit")
    source_commit_count = metadata.get("source_commit_count")
    base_plugin_version = metadata.get("base_plugin_version")
    generated_plugin_version = metadata.get("generated_plugin_version")
    payload_digest = metadata.get("payload_digest")
    required_string_values = {
        "source_repository": source_repository_value,
        "source_commit": source_commit,
        "base_plugin_version": base_plugin_version,
        "generated_plugin_version": generated_plugin_version,
        "payload_digest": payload_digest,
    }
    invalid_keys = [
        key
        for key, value in required_string_values.items()
        if not isinstance(value, str) or not value
    ]
    if invalid_keys or not isinstance(source_commit_count, int):
        raise OfficialCliInstallationError(
            f"previous immutable release metadata 不完整: {invalid_keys}"
        )
    actual_payload_digest = deployment_file_digest(release_root)
    if actual_payload_digest != payload_digest:
        raise OfficialCliInstallationError(
            f"previous immutable release payload digest 漂移: {release_root}"
        )
    if installation_receipt.get("local_main_commit") != source_commit:
        raise OfficialCliInstallationError(
            "previous installation receipt 与 immutable release commit 不一致"
        )
    return DeveloperMarketplaceRelease(
        release_root=release_root,
        source_repository=Path(source_repository_value).expanduser().resolve(),
        source_commit=source_commit,
        source_commit_count=source_commit_count,
        base_plugin_version=base_plugin_version,
        generated_plugin_version=generated_plugin_version,
        payload_digest=payload_digest,
    )


def rollback_developer_marketplace_after_failed_validation(
    *,
    previous_installation_receipt: Mapping[str, Any] | None,
    home_directory: Path,
    codex_executable: Path,
    claude_executable: Path,
    command_runner: OfficialCliCommandRunner = default_official_cli_command_runner,
) -> dict[str, Any]:
    home_directory = home_directory.expanduser().resolve()
    active_marketplace_root = (
        home_directory / DEVELOPER_MARKETPLACE_ROOT_RELATIVE_PATH
    )
    if previous_installation_receipt is None:
        commands = remove_developer_marketplace_with_official_clis(
            codex_executable=codex_executable,
            claude_executable=claude_executable,
            command_runner=command_runner,
        )
        _remove_path_if_present(active_marketplace_root)
        user_local_data_paths = resolved_rvf_user_local_data_paths(
            home_directory=home_directory
        )
        _remove_path_if_present(
            user_local_data_paths.deployment_receipts_root
            / DEPLOYMENT_RECEIPT_FILE_NAME
        )
        return {
            "status": "rolled_back",
            "rollback_status": "removed_partial_first_install",
            "official_cli_commands": [list(command) for command in commands],
        }

    previous_release = developer_marketplace_release_from_installation_receipt(
        previous_installation_receipt
    )
    restored_transaction = deploy_developer_marketplace_transaction(
        release=previous_release,
        home_directory=home_directory,
        codex_executable=codex_executable,
        claude_executable=claude_executable,
        command_runner=command_runner,
    )
    restored_receipt = dict(previous_installation_receipt)
    restored_receipt["rollback_status"] = "restored_previous_release"
    user_local_data_paths = resolved_rvf_user_local_data_paths(
        home_directory=home_directory
    )
    write_json_atomic(
        user_local_data_paths.deployment_receipts_root
        / DEPLOYMENT_RECEIPT_FILE_NAME,
        restored_receipt,
    )
    return {
        "status": "rolled_back",
        "rollback_status": "restored_previous_release",
        "restored_local_main_commit": previous_release.source_commit,
        "restored_generated_plugin_version": (
            previous_release.generated_plugin_version
        ),
        "official_cli_commands": [
            list(command)
            for command in restored_transaction.official_cli_installation.commands
        ],
    }


def initialize_rvf_user_local_data(
    *,
    source_repository: Path,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
) -> RvfUserLocalDataPaths:
    paths = resolved_rvf_user_local_data_paths(
        environment=environment,
        home_directory=home_directory,
    )
    for directory in (
        paths.reviewer_configuration_root,
        paths.runtime_state_root,
        paths.run_logs_root,
        paths.deployment_receipts_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    merge_bundled_reviewer_harnesses_into_user_local_registry(
        bundled_registry_path=(
            source_repository
            / f"plugins/{PLUGIN_NAME}"
            / PLUGIN_SKILL_RELATIVE_PATH
            / "config/reviewer-registry.json"
        ),
        user_local_registry_path=paths.reviewer_registry_path,
    )
    return paths


def merge_bundled_reviewer_harnesses_into_user_local_registry(
    *,
    bundled_registry_path: Path,
    user_local_registry_path: Path,
) -> list[str]:
    """把 bundled registry 里 user-local 尚缺的 harness 增量并入，返回新增的 harness id。

    真相源是 **user-local** 那份 registry：bundled 那份构建时会被移出 payload
    (`_remove_path_if_present`)，verifier 还断言它不得存在于 payload 里。
    所以这里的方向是单向的——bundled 只当 seed 用。

    **只补不覆盖**：user-local 的条目是用户资产（关掉某条腿、调 priority、
    加自己的 harness），一次部署不得把它们改回出厂值。

    **补到字段一级**：只在条目一级 additive 是不够的——它挡住了「新腿进不来」，
    却挡不住「新字段进不来」。见
    :func:`backfill_bundled_fields_missing_from_existing_user_local_harness_entries`。

    **不静默跳过**：并入前先比对 schema_version。版本对不上时宁可让部署响亮地
    失败，也不能跳过——跳过的后果是新评审腿永远进不来，而部署照常报成功，
    没有任何一处会察觉（本仓反复栽过的假绿形状）。
    """
    if not bundled_registry_path.is_file():
        raise OfficialCliInstallationError(
            f"缺少 bundled reviewer registry: {bundled_registry_path}"
        )
    bundled_registry = read_json_object(bundled_registry_path)
    bundled_harnesses = bundled_registry.get("harnesses")
    if not isinstance(bundled_harnesses, dict):
        raise OfficialCliInstallationError(
            f"bundled reviewer registry 缺少 harnesses object: {bundled_registry_path}"
        )

    if not user_local_registry_path.exists():
        user_local_registry_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled_registry_path, user_local_registry_path)
        return sorted(bundled_harnesses)

    user_local_registry = read_json_object(user_local_registry_path)
    user_local_harnesses = user_local_registry.get("harnesses")
    if not isinstance(user_local_harnesses, dict):
        raise OfficialCliInstallationError(
            "user-local reviewer registry 缺少 harnesses object: "
            f"{user_local_registry_path}"
        )
    if user_local_registry.get("schema_version") != bundled_registry.get(
        "schema_version"
    ):
        raise OfficialCliInstallationError(
            "reviewer registry schema_version 不一致，拒绝并入新 harness："
            f"user-local={user_local_registry.get('schema_version')!r} "
            f"bundled={bundled_registry.get('schema_version')!r} "
            f"({user_local_registry_path})。请先人工迁移该文件再重跑部署。"
        )

    newly_added_harness_ids = sorted(
        harness_id
        for harness_id in bundled_harnesses
        if harness_id not in user_local_harnesses
    )
    for harness_id in newly_added_harness_ids:
        user_local_harnesses[harness_id] = bundled_harnesses[harness_id]
    backfilled_field_names_by_harness_id = (
        backfill_bundled_fields_missing_from_existing_user_local_harness_entries(
            bundled_harnesses=bundled_harnesses,
            user_local_harnesses=user_local_harnesses,
            harness_ids_added_by_this_merge=frozenset(newly_added_harness_ids),
        )
    )
    if not newly_added_harness_ids and not backfilled_field_names_by_harness_id:
        return []
    user_local_registry["harnesses"] = user_local_harnesses
    write_json_atomic(user_local_registry_path, user_local_registry)
    return newly_added_harness_ids


def backfill_bundled_fields_missing_from_existing_user_local_harness_entries(
    *,
    bundled_harnesses: Mapping[str, Any],
    user_local_harnesses: dict[str, Any],
    harness_ids_added_by_this_merge: frozenset[str] = frozenset(),
) -> dict[str, list[str]]:
    """给**已存在**的 harness 条目补上「出厂后来新增、user-local 从未有过」的字段。

    就地改写 ``user_local_harnesses``，返回 ``{harness_id: [被补上的字段名]}``。

    **为什么条目级 additive 不够**：它只挡住「新腿进不来」，挡不住「新字段进不来」。
    一台已部署过的机器，其 cursor / claude_code / codex 三条腿是首次部署那天的形状；
    之后往出厂条目里加的字段（第一例是
    ``underlying_provider_account_quota_pool_identity``）对它们永远不可见，
    于是依赖该字段的选腿约束（同额度池互斥）在生产路径上**静默失效**——
    部署照常成功、双评审照常跑两条腿，没有任何一处会察觉。这与当初「新腿永远
    进不来」是同一个假绿形状，只是降了一级粒度。

    **判据是「键在不在」而不是「值等不等于出厂值」**：键不存在 ⇒ 用户从未对它表达过
    意见（那天这个字段还不存在），补进去不算改用户的值；键存在 ⇒ 用户资产，值一律
    原样保留，包括显式改过的值、显式置空的值、以及恰好等于出厂值的值。

    **只补顶层键**：出厂条目全是标量字段。递归合并会引入「用户改过的嵌套结构被部分
    覆盖」这种更难解释的语义，等真出现嵌套字段时再单独设计。
    """
    backfilled_field_names_by_harness_id: dict[str, list[str]] = {}
    for harness_id, bundled_spec in bundled_harnesses.items():
        if harness_id in harness_ids_added_by_this_merge:
            continue
        user_local_spec = user_local_harnesses.get(harness_id)
        if not isinstance(bundled_spec, dict) or not isinstance(user_local_spec, dict):
            continue
        backfilled_field_names = sorted(
            field_name
            for field_name in bundled_spec
            if field_name not in user_local_spec
        )
        if not backfilled_field_names:
            continue
        for field_name in backfilled_field_names:
            user_local_spec[field_name] = bundled_spec[field_name]
        backfilled_field_names_by_harness_id[harness_id] = backfilled_field_names
    return backfilled_field_names_by_harness_id


def legacy_default_cline_kanban_env_value(name: str, value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or LEGACY_DEFAULT_CLINE_KANBAN_ENV.get(name) == text:
        return None
    return text


def normalized_cline_kanban_worktree_mode(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    return text if text in CLINE_KANBAN_WORKTREE_MODES else None


def normalize_fork_mode(value: str) -> str:
    mode = (value or "auto").strip()
    if mode in {"cline", "kanban", "ck"}:
        return "cline-kanban"
    if mode in {"kanban-message", "kanban-inject"}:
        return "kanban-followup"
    return mode


def _read_hooks_document(hooks_path: Path) -> dict[str, Any]:
    if not hooks_path.exists():
        return {"hooks": {}}
    payload = read_json_object(hooks_path)
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        payload["hooks"] = {}
    return payload


def configure_stop_hook(
    plugin_skill_dir: Path,
    fork_mode: str = "auto",
    cline_kanban_start_cmd: str | None = None,
    cline_kanban_task_cmd: str | None = None,
    cline_kanban_start_timeout: str | None = None,
    cline_kanban_tmux_session: str | None = None,
    cline_kanban_base_ref: str | None = None,
    cline_kanban_worktree_mode: str | None = None,
    cline_kanban_auto_review_enabled: str | None = None,
    cline_kanban_auto_review_mode: str | None = None,
    cline_kanban_start_in_plan_mode: str | None = None,
    *,
    user_local_data_paths: RvfUserLocalDataPaths | None = None,
) -> Path:
    fork_mode = normalize_fork_mode(fork_mode)
    cline_kanban_worktree_mode = normalized_cline_kanban_worktree_mode(
        cline_kanban_worktree_mode
    )
    paths = user_local_data_paths or resolved_rvf_user_local_data_paths()
    hooks_path = Path.home() / ".codex/hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_hooks_document(hooks_path)
    hooks = data.setdefault("hooks", {})
    stop_groups = hooks.setdefault("Stop", [])
    if not isinstance(stop_groups, list):
        stop_groups = []
        hooks["Stop"] = stop_groups
    router = plugin_skill_dir / "scripts/stop_hook_router.py"
    stable_dispatcher = plugin_skill_dir / "scripts/stop_hook_dispatcher.py"
    dev_dispatcher = (
        ROOT
        / f"plugins/{PLUGIN_NAME}"
        / PLUGIN_SKILL_RELATIVE_PATH
        / "scripts/stop_hook_dispatcher.py"
    )
    env_parts = [
        f"RVF_USER_LOCAL_DATA_ROOT={shlex.quote(str(paths.user_local_data_root))}",
        f"CODEX_RVF_LOG_ROOT={shlex.quote(str(paths.run_logs_root))}",
        f"CODEX_RVF_STATE_DIR={shlex.quote(str(paths.runtime_state_root))}",
        f"CODEX_RVF_INSTALLED_SKILL_DIR={shlex.quote(str(plugin_skill_dir))}",
        "CODEX_RVF_MODE=fork",
        f"CODEX_RVF_FORK_MODE={shlex.quote(fork_mode)}",
        f"CODEX_RVF_STABLE_STOP_HOOK={shlex.quote(str(stable_dispatcher))}",
        f"CODEX_RVF_DEV_STOP_HOOK={shlex.quote(str(dev_dispatcher))}",
    ]
    if fork_mode in {"auto", "cline-kanban", "kanban-followup"}:
        for name, value in (
            ("CODEX_RVF_CLINE_KANBAN_START_CMD", cline_kanban_start_cmd),
            ("CODEX_RVF_CLINE_KANBAN_TASK_CMD", cline_kanban_task_cmd),
            ("CODEX_RVF_CLINE_KANBAN_START_TIMEOUT", cline_kanban_start_timeout),
            ("CODEX_RVF_CLINE_KANBAN_TMUX_SESSION", cline_kanban_tmux_session),
            ("CODEX_RVF_CLINE_KANBAN_BASE_REF", cline_kanban_base_ref),
            ("CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE", cline_kanban_worktree_mode),
            (
                "CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_ENABLED",
                cline_kanban_auto_review_enabled,
            ),
            ("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_MODE", cline_kanban_auto_review_mode),
            (
                "CODEX_RVF_CLINE_KANBAN_START_IN_PLAN_MODE",
                cline_kanban_start_in_plan_mode,
            ),
        ):
            text = (value or "").strip()
            if text:
                env_parts.append(f"{name}={shlex.quote(text)}")
    env_parts.extend(
        [
            "CODEX_RVF_DEV_SYNC_COMMAND_TIMEOUT=180",
            "CODEX_RVF_STOP_HOOK_CHAIN_TIMEOUT=60",
            f"CODEX_RVF_DEV_REPO={shlex.quote(str(ROOT))}",
        ]
    )
    entry = {
        "type": "command",
        "command": hook_command_guarded_against_missing_target_script(
            target_script_shell_literal=shlex.quote(str(router)),
            hook_event_name="Stop",
            environment_assignment_prefix=" ".join(env_parts),
            # env 前缀只作用于 python3 那一条命令，面包屑分支读不到它烘焙的
            # RVF_USER_LOCAL_DATA_ROOT，故把已解析的根显式传进守卫。
            hook_target_missing_breadcrumb_directory_shell_literal=shlex.quote(
                str(paths.user_local_data_root)
            ),
        ),
        "timeout": 300,
        "statusMessage": "Review-Validate-Fix：选择通道并运行停止检查",
    }
    cleaned_groups: list[dict[str, Any]] = []
    target_group: dict[str, Any] | None = None
    for raw_group in stop_groups:
        if not isinstance(raw_group, dict):
            continue
        group = raw_group
        group_hooks = group.get("hooks")
        if not isinstance(group_hooks, list):
            group["hooks"] = []
            cleaned_groups.append(group)
            continue
        kept_hooks: list[Any] = []
        removed_rvf_hook = False
        for existing in group_hooks:
            command = existing.get("command") if isinstance(existing, dict) else None
            if isinstance(command, str) and any(
                marker in command
                for marker in (
                    "stop_review_validate_fix.py",
                    "stop_hook_dispatcher.py",
                    "stop_hook_router.py",
                )
            ):
                removed_rvf_hook = True
                if target_group is None:
                    target_group = group
                continue
            kept_hooks.append(existing)
        if removed_rvf_hook and target_group is group:
            kept_hooks.append(entry)
        group["hooks"] = kept_hooks
        if kept_hooks:
            cleaned_groups.append(group)
    if target_group is None:
        cleaned_groups.append({"hooks": [entry]})
    hooks["Stop"] = cleaned_groups
    write_json_atomic(hooks_path, data)
    return hooks_path


KIMI_CODE_HOME_ENVIRONMENT_VARIABLE = "KIMI_CODE_HOME"
KIMI_DEFAULT_HOME_RELATIVE_TO_USER_HOME = ".kimi-code"

# Kimi 的 `installed.json` 里 `source` 是**安装来源种类的枚举**，不是路径——路径归
# `originalSource`。取证：`resolveInstallSource()` 返回 `{kind: "local-path"|"zip-url"|"github", path}`，
# `recordFrom()` 落的是 `source: input.source ?? "local-path"`（即那个 kind），`persist()` 原样写盘。
# 写错的代价在 0.38.0 起是**响亮**的：`pluginService.listPlugins` 的出站 zod 校验里
# `source: enum([...])` / `github: pluginGithubMetadataSchema.optional()`，一条不合规就让整个
# `/plugins` 面板报 "output validation failed" 打不开（hook 派发走 enabledHooks()、不过这道校验，
# 所以这是一个「hook 还在跑、UI 全挂」的偏斜故障，别按「插件没装上」去排查）。
KIMI_PLUGIN_REGISTRY_INSTALL_SOURCE_KIND_LOCAL_PATH = "local-path"

# 承载 record 的 **document 信封**同样有形状：Kimi 的 `persist()` 逐字写
# `writeInstalled(home, {version: 1, plugins: [...]})`，它的空 registry 常量也是
# `{version: 1, plugins: []}`（0.38.0 里 agent-core 与 agent-core-v2 两份实现一致）。
# 今天 `readInstalled()` 只校验 `Array.isArray(parsed.plugins)` ⇒ 缺 `version` **不报错**；
# 但那正是 `source` / `github` 那两条真缺陷的同款形状——旧版容忍、新版加一道校验就变硬失败。
# 所以全新 registry 补齐它；**已存在的 document 保留原有 version**，见下面写入处的注释。
KIMI_PLUGIN_REGISTRY_DOCUMENT_SCHEMA_VERSION = 1


def resolve_kimi_code_home(
    *,
    environment: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
) -> Path:
    environment = os.environ if environment is None else environment
    override = environment.get(KIMI_CODE_HOME_ENVIRONMENT_VARIABLE)
    if override:
        return Path(override).expanduser()
    base = home_directory if home_directory is not None else Path.home()
    return Path(base) / KIMI_DEFAULT_HOME_RELATIVE_TO_USER_HOME


def verify_kimi_plugin_registry_after_write(
    registry_path: Path,
    *,
    expected_plugin_record: Mapping[str, Any],
    expected_other_plugin_ids: Iterable[Any],
) -> dict[str, Any]:
    """读回刚写的 ``installed.json``，确认落盘的确实是我们写的那份。

    存在的理由见 ``configure_kimi_plugin_registration`` 的「丢更新竞争」段：这条写入无锁无 CAS，
    运行中的 Kimi 会用它内存里的旧 registry 全量重写。复验抓的是**写入时点**的三类真实故障：
    没落盘、我们自己的 merge 写错、以及在 ``replace`` 与复验之间发生的覆盖。

    返回的 ``registry_sha256`` 是给事后对账的锚：``kimi_plugin_manifest_activation_state_check.py``
    在任意后续时刻再算一次，对不上就说明注册在部署之后被覆盖掉了。它**不**证明注册此刻还在——
    无锁写入给不出那种保证。
    """
    registry_bytes = registry_path.read_bytes()
    try:
        verified_document = json.loads(registry_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OfficialCliInstallationError(
            f"{registry_path} 写后复验失败：读回的内容不是合法 JSON（{exc}）"
        ) from exc
    verified_records = verified_document.get("plugins")
    if not isinstance(verified_records, list):
        raise OfficialCliInstallationError(
            f"{registry_path} 写后复验失败：读回的文档里没有 plugins 列表"
        )
    verified_plugin_ids = [
        record.get("id") for record in verified_records if isinstance(record, dict)
    ]
    landed_record = next(
        (
            record
            for record in verified_records
            if isinstance(record, dict)
            and record.get("id") == expected_plugin_record["id"]
        ),
        None,
    )
    if landed_record != dict(expected_plugin_record):
        raise OfficialCliInstallationError(
            f"{registry_path} 写后复验失败：我们写入的 {expected_plugin_record['id']} 记录"
            f"不在读回内容里（读到 {landed_record!r}）。最可能的成因是运行中的 Kimi 用内存里的"
            "旧 registry 全量重写了它——请退出 Kimi 后重试部署，或重试后执行 /plugins reload"
        )
    missing_other_plugin_ids = sorted(
        str(plugin_id)
        for plugin_id in set(expected_other_plugin_ids) - set(verified_plugin_ids)
    )
    if missing_other_plugin_ids:
        raise OfficialCliInstallationError(
            f"{registry_path} 写后复验失败：用户原有插件被掀掉了 {missing_other_plugin_ids}"
        )
    return {
        "registry_sha256": hashlib.sha256(registry_bytes).hexdigest(),
        "plugin_ids": sorted(str(plugin_id) for plugin_id in verified_plugin_ids),
    }


def configure_kimi_plugin_registration(
    active_marketplace_root: Path,
    *,
    kimi_code_home: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """把 RVF 注册进 Kimi 的 ``plugins/installed.json``（幂等 additive merge）。

    **为什么是「写文件」而不是调 CLI**：Kimi 的插件注册在四条跨进程通道上全部不可达——
    `kimi --help` 无 `plugin` 子命令、`kimi web` 的 REST 路由表与 WebSocket frame 全集里
    没有任何 plugin 操作、`AcpServer` 的 28 个方法含 plugin 的为 0。`installPlugin` 只是
    TUI 与 kernel 之间的进程内 RPC。手改 `installed.json` + `/plugins reload` 是 Kimi
    **官方认可**的路径（它自己的错误文案逐字这么写）。

    **两条必须守住的约束**：

    1. **幂等 additive merge。** 全量重写会掀掉用户装的其它插件。这里只替换/插入
       ``id == review-validate-fix`` 那一条。
    2. **``state`` 写不进去。** ``persist()`` 只落 9 个字段，``state`` / ``manifest`` /
       ``diagnostics`` 都是加载时派生的内存值。所以本函数**不写** state，调用方也**不得**
       靠读它来确认生效——生效与否由 ``kimi_plugin_manifest_activation_state_check.py``
       独立复算（「安装器写完了」永远不等于「注册生效了」）。

    ⚠️ **丢更新竞争**：Kimi 在内存里持有完整 registry，并以固定 ``installed.json.tmp``
    **全量重写**，没有跨进程锁也没有 CAS ⇒ 运行中的 TUI/web 会覆盖掉我们刚写的内容。

    本函数**刻意不做进程探测**：「有没有 Kimi 在跑」两个方向都判不准——误判成「在运行」会让
    部署无谓失败，判成「没在跑」也拦不住下一秒才启动的实例；那种信号进了 receipt 也不构成
    可对账的凭据。实际兜底是三层，全部可验证：

    1. **写后复验**（本函数实现）：``replace`` 之后立刻读回，比对我们那条记录是否逐字落盘、
       别人插件的 id 集合是否原样保留；不符即 fail closed。复验结果连同读回内容的 sha256
       进 receipt 的 ``post_write_verification``。
    2. ``requires_manual_activation_commands_in_order``：让用户显式收敛内存态。**必须两条、
       且顺序固定**，因为它们收敛的是两层不同的缓存（取证自 0.38.0 二进制）：
       ``/plugins reload`` 只把 ``installed.json`` 重新 materialize 进**进程级**
       ``PluginManager``；而 skill 是**会话级**的——``loadSkills()`` 只在 session 构造时跑一次
       （``this.skillsReady = this.loadSkills()``），已有会话不会重扫。所以还要 ``/reload``
       （或 ``/new``）重建会话，新的 skill 才进得去。Kimi 自己的 TUI 常量逐字就是这个意思：
       ``PLUGIN_RELOAD_HINT = "Run /new or /reload to apply plugin changes."``
       ⚠️ 只提示 ``/plugins reload`` 会让用户看到「插件在册、Skills 却是空的」而无从下手。
    3. ``kimi_plugin_manifest_activation_state_check.py`` 事后**独立复算**生效状态；把当时
       文件的 sha256 与 1 记下的那个一比，就能看出注册有没有被运行中的 Kimi 覆盖掉。

    复验是**时点观测**，不是「注册一定还在」的保证——无锁写入本来就给不出那种保证，这一点
    必须照直写，否则读者会把 receipt 当成比它更强的证据。

    ⚠️ **状态根只能用默认值**：Kimi 的 ``enabledHooks()`` 只注入 ``KIMI_CODE_HOME`` 与
    ``KIMI_PLUGIN_ROOT`` 两个变量，而 hook 命令写在**进 payload 的静态** manifest 里、
    不能带本机绝对路径 ⇒ Kimi 侧的 hook 子进程拿不到 Codex 侧那串显式 ``RVF_*`` env 前缀，
    只能落到 ``~/.rvf/user-local-data`` 默认根。单机部署下这就是正确值；但如果将来要支持
    非默认状态根，得改 manifest 的生成方式（而不是在这里塞 env）。
    """
    kimi_home = kimi_code_home or resolve_kimi_code_home()
    plugin_root = active_marketplace_root / f"plugins/{PLUGIN_NAME}"
    registry_path = kimi_home / "plugins" / "installed.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)

    existing_document: dict[str, Any] = {}
    if registry_path.is_file():
        try:
            loaded = json.loads(registry_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_document = loaded
        except json.JSONDecodeError as exc:
            # Kimi 对非法 JSON 是抛错（整个 plugin store 加载失败），不是当空 registry。
            # 直接覆盖会掩盖用户那边的问题，所以 fail closed。
            raise OfficialCliInstallationError(
                f"{registry_path} 不是合法 JSON（{exc}）；请先修复它再重试部署"
            ) from exc

    existing_records = existing_document.get("plugins")
    if not isinstance(existing_records, list):
        existing_records = []

    timestamp = now or utc_now()
    previous = next(
        (
            record
            for record in existing_records
            if isinstance(record, dict) and record.get("id") == PLUGIN_NAME
        ),
        None,
    )
    record = {
        "id": PLUGIN_NAME,
        "root": str(plugin_root),
        # 枚举，不是路径——见 KIMI_PLUGIN_REGISTRY_INSTALL_SOURCE_KIND_LOCAL_PATH 的取证注释。
        "source": KIMI_PLUGIN_REGISTRY_INSTALL_SOURCE_KIND_LOCAL_PATH,
        "enabled": True,
        # 首次注册才记 installedAt；重复部署保留原值，让「装了多久」这个事实不被刷掉。
        "installedAt": (previous or {}).get("installedAt") or timestamp,
        "updatedAt": timestamp,
        "originalSource": str(plugin_root),
    }
    # `capabilities`（per-plugin 的 MCP 开关等）是**用户态**：Kimi 自己 install 时写的是
    # `existing?.capabilities`，全新安装即 undefined ⇒ 键不落盘。这里照抄那个语义——有就原样保留
    # （否则重装会把用户关掉的 MCP server 又打开），没有就**不写这个键**，而不是写 `[]`。
    previous_capabilities = (previous or {}).get("capabilities")
    if previous_capabilities is not None:
        record["capabilities"] = previous_capabilities
    # `github` 刻意**不写**：它只对 kind=="github" 的安装有值，本地路径安装在 Kimi 那边是
    # undefined ⇒ JSON 里根本没这个键。写 `null` 会被 `.optional()` 拒收（zod 接受「缺省」，
    # 不接受 null），正是 0.38.0 下 `/plugins` 报错的另一半。
    merged_records = [
        existing
        for existing in existing_records
        if not (isinstance(existing, dict) and existing.get("id") == PLUGIN_NAME)
    ]
    merged_records.append(record)
    # 用 `setdefault` 而不是直接赋值：全新 registry 从空 dict 出发 ⇒ 补上宿主那个顶层
    # `version`（否则落盘信封只有 `plugins`，与 writeInstalled() 写的形状对不上）；而**已存在**
    # 的 document 原样保留它自己的值。后者是刻意的：如果用户或未来版本的 Kimi 已经把 registry
    # 升到更高的 schema 版本，这条 additive merge 把它压回 1 就是拿「兼容性修复」制造真降级。
    existing_document.setdefault(
        "version", KIMI_PLUGIN_REGISTRY_DOCUMENT_SCHEMA_VERSION
    )
    existing_document["plugins"] = merged_records

    temporary_path = registry_path.with_suffix(".json.rvf-tmp")
    temporary_path.write_text(
        json.dumps(existing_document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary_path.replace(registry_path)

    post_write_verification = verify_kimi_plugin_registry_after_write(
        registry_path,
        expected_plugin_record=record,
        expected_other_plugin_ids=[
            existing.get("id")
            for existing in merged_records
            if isinstance(existing, dict) and existing.get("id") != PLUGIN_NAME
        ],
    )

    return {
        "kimi_code_home": str(kimi_home),
        "installed_registry_path": str(registry_path),
        "plugin_root": str(plugin_root),
        "was_already_registered": previous is not None,
        "preserved_other_plugin_count": len(merged_records) - 1,
        # 两条命令**都要跑，且缺一不可**——它们收敛的是两个不同的东西：
        "requires_manual_activation_commands_in_order": [
            "/plugins reload",
            "/reload",
        ],
        "post_write_verification": post_write_verification,
    }


def kimi_plugin_registration_outcome_for_deployment_receipt(
    active_marketplace_root: Path,
    *,
    enabled: bool,
    register_kimi_plugin: Callable[[Path], dict[str, Any]] = (
        configure_kimi_plugin_registration
    ),
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """跑 Kimi 注册，把结果**降级成回执字段**；返回 ``(注册回执, 失败回执)``，本函数永不抛。

    Kimi 注册是部署链的最后一环：调用它的时候，Codex/Claude 的 marketplace 事务、两个官方
    CLI 的安装、以及 Stop hook 配置都已经不可回滚地完成了，只差写部署回执。而它对损坏的
    ``installed.json`` 是显式 raise（另有 mkdir/write 的 OSError 面），``main()`` 的 except
    只打印并 ``return 2``——既不回滚已提交的部署，也**不写回执**。

    于是一个第三方产品的坏文件（甚至根本没装 Kimi 的用户手里一份损坏的
    ``~/.kimi-code/plugins/installed.json``）就能让「Claude/Codex 实际已部署新版、回执仍停在
    旧版」。回执是本仓判 deploy-lag 的**唯一凭据**，停在旧版比 Kimi 没注册上危险得多：前者
    让整套「先核验部署版本再断言因果」的纪律读到假事实，后者只是第三个 host 少一个 hook。

    所以这一环非致命：失败写进回执里**独立的** ``kimi_plugin_registration_failure`` 字段
    （不是把成功字段填成空，那样区分不了「没启用」和「失败了」），并打到 stderr。
    """
    if not enabled:
        return None, None
    try:
        return register_kimi_plugin(active_marketplace_root), None
    except Exception as kimi_registration_error:  # noqa: BLE001 — 见 docstring：这一环必须非致命
        failure = {
            "error_type": type(kimi_registration_error).__name__,
            "message": str(kimi_registration_error),
        }
        print(
            "RVF Kimi plugin registration FAILED（非致命；Codex/Claude 部署与部署回执不受影响）: "
            f"{kimi_registration_error}",
            file=sys.stderr,
        )
        return None, failure


def configure_user_prompt_submit_hook(
    plugin_skill_dir: Path,
    *,
    user_local_data_paths: RvfUserLocalDataPaths | None = None,
) -> Path:
    paths = user_local_data_paths or resolved_rvf_user_local_data_paths()
    hooks_path = Path.home() / ".codex/hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_hooks_document(hooks_path)
    hooks = data.setdefault("hooks", {})
    prompt_groups = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(prompt_groups, list):
        prompt_groups = []
    detector = plugin_skill_dir / "scripts/rvf_user_prompt_submit.py"
    entry = {
        "type": "command",
        "command": hook_command_guarded_against_missing_target_script(
            target_script_shell_literal=shlex.quote(str(detector)),
            hook_event_name="UserPromptSubmit",
            environment_assignment_prefix=" ".join(
                [
                    f"RVF_USER_LOCAL_DATA_ROOT={shlex.quote(str(paths.user_local_data_root))}",
                    f"CODEX_RVF_LOG_ROOT={shlex.quote(str(paths.run_logs_root))}",
                    f"CODEX_RVF_STATE_DIR={shlex.quote(str(paths.runtime_state_root))}",
                    f"CODEX_RVF_INSTALLED_SKILL_DIR={shlex.quote(str(plugin_skill_dir))}",
                ]
            ),
            # 同 configure_stop_hook：面包屑根必须显式传，不能指望 ambient 环境变量。
            hook_target_missing_breadcrumb_directory_shell_literal=shlex.quote(
                str(paths.user_local_data_root)
            ),
        ),
        "timeout": 5,
    }
    cleaned_groups: list[dict[str, Any]] = []
    target_group: dict[str, Any] | None = None
    for raw_group in prompt_groups:
        if not isinstance(raw_group, dict):
            continue
        group = raw_group
        group_hooks = group.get("hooks")
        if not isinstance(group_hooks, list):
            group["hooks"] = []
            cleaned_groups.append(group)
            continue
        kept_hooks: list[Any] = []
        removed_rvf_hook = False
        for existing in group_hooks:
            command = existing.get("command") if isinstance(existing, dict) else None
            if isinstance(command, str) and "rvf_user_prompt_submit.py" in command:
                removed_rvf_hook = True
                if target_group is None:
                    target_group = group
                continue
            kept_hooks.append(existing)
        if removed_rvf_hook and target_group is group:
            kept_hooks.append(entry)
        group["hooks"] = kept_hooks
        if kept_hooks:
            cleaned_groups.append(group)
    if target_group is None:
        cleaned_groups.append({"hooks": [entry]})
    hooks["UserPromptSubmit"] = cleaned_groups
    write_json_atomic(hooks_path, data)
    return hooks_path


def _git_text(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise OfficialCliInstallationError(
            f"git {' '.join(arguments)} 失败: {detail}"
        )
    return completed.stdout.strip()


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-source-worktree", type=Path)
    parser.add_argument("--source-local-main-commit")
    parser.add_argument(
        "--rollback-to-installation-receipt",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--rollback-remove-current-installation",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--configure-stop-hook", action="store_true")
    parser.add_argument("--configure-user-prompt-submit-hook", action="store_true")
    parser.add_argument(
        "--configure-kimi-plugin-registration",
        action="store_true",
        help=(
            "把 RVF 注册进 Kimi 的 plugins/installed.json（幂等 additive merge）。"
            "Kimi 没有任何可自动化的 plugin CLI/RPC 入口，手改该文件 + /plugins reload "
            "是官方认可路径；写完仍须由 kimi_plugin_manifest_activation_state_check.py 独立复验。"
        ),
    )
    parser.add_argument("--fork-mode", default="auto")
    parser.add_argument("--cline-kanban-start-cmd")
    parser.add_argument("--cline-kanban-task-cmd")
    parser.add_argument("--cline-kanban-start-timeout")
    parser.add_argument("--cline-kanban-tmux-session")
    parser.add_argument("--cline-kanban-base-ref")
    parser.add_argument("--cline-kanban-worktree-mode", choices=sorted(CLINE_KANBAN_WORKTREE_MODES))
    parser.add_argument("--cline-kanban-auto-review-enabled")
    parser.add_argument("--cline-kanban-auto-review-mode")
    parser.add_argument("--cline-kanban-start-in-plan-mode")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rollback_requested = bool(
        args.rollback_to_installation_receipt
        or args.rollback_remove_current_installation
    )
    if (
        args.rollback_to_installation_receipt
        and args.rollback_remove_current_installation
    ):
        parser.error("rollback mode 只能选择恢复上一 release 或移除首次安装之一")
    source_arguments_present = bool(
        args.deployment_source_worktree or args.source_local_main_commit
    )
    if rollback_requested and source_arguments_present:
        parser.error("rollback mode 不接受 local-main source 参数")
    if not rollback_requested and (
        args.deployment_source_worktree is None
        or not args.source_local_main_commit
    ):
        parser.error(
            "deployment mode 必须同时提供 --deployment-source-worktree "
            "与 --source-local-main-commit"
        )
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_arguments(argv)
    home_directory = Path.home().resolve()
    try:
        if (
            args.rollback_to_installation_receipt
            or args.rollback_remove_current_installation
        ):
            codex_executable = resolved_official_cli_executable("codex")
            claude_executable = resolved_official_cli_executable("claude")
            previous_installation_receipt = (
                read_json_object(
                    args.rollback_to_installation_receipt.expanduser().resolve()
                )
                if args.rollback_to_installation_receipt
                else None
            )
            rollback_result = rollback_developer_marketplace_after_failed_validation(
                previous_installation_receipt=previous_installation_receipt,
                home_directory=home_directory,
                codex_executable=codex_executable,
                claude_executable=claude_executable,
            )
            if args.json:
                print(json.dumps(rollback_result, ensure_ascii=False, indent=2))
            else:
                print(
                    "RVF official CLI installation rollback OK: "
                    f"{rollback_result['rollback_status']}"
                )
            return 0

        source_repository = args.deployment_source_worktree.expanduser().resolve()
        actual_commit = _git_text(source_repository, "rev-parse", "HEAD")
        expected_commit = _git_text(
            source_repository,
            "rev-parse",
            "--verify",
            f"{args.source_local_main_commit}^{{commit}}",
        )
        if actual_commit != expected_commit:
            raise OfficialCliInstallationError(
                "installer source commit 与 local main HEAD 不一致"
            )
        source_commit_count = int(
            _git_text(source_repository, "rev-list", "--count", actual_commit)
        )
        user_local_data_paths = initialize_rvf_user_local_data(
            source_repository=source_repository,
            home_directory=home_directory,
        )
        release = build_developer_marketplace_release(
            source_repository=source_repository,
            home_directory=home_directory,
            source_commit=actual_commit,
            source_commit_count=source_commit_count,
        )
        codex_executable = resolved_official_cli_executable("codex")
        claude_executable = resolved_official_cli_executable("claude")
        official_cli_versions = {
            "codex": official_cli_version(codex_executable),
            "claude": official_cli_version(claude_executable),
        }
        validation = default_official_cli_command_runner(
            [str(claude_executable), "plugin", "validate", str(release.release_root)]
        )
        if validation.returncode != 0:
            raise OfficialCliInstallationError(
                "Claude 官方 manifest validation 失败: "
                f"{validation.stderr.strip() or validation.stdout.strip()}"
            )
        deployment_transaction = deploy_developer_marketplace_transaction(
            release=release,
            home_directory=home_directory,
            codex_executable=codex_executable,
            claude_executable=claude_executable,
        )
        active_marketplace_root = deployment_transaction.active_marketplace_root
        official_cli_result = deployment_transaction.official_cli_installation
        plugin_skill_dir = (
            active_marketplace_root
            / f"plugins/{PLUGIN_NAME}"
            / PLUGIN_SKILL_RELATIVE_PATH
        )
        hooks_path: Path | None = None
        if args.configure_stop_hook:
            hooks_path = configure_stop_hook(
                plugin_skill_dir,
                args.fork_mode,
                args.cline_kanban_start_cmd
                or legacy_default_cline_kanban_env_value(
                    "CODEX_RVF_CLINE_KANBAN_START_CMD",
                    os.environ.get("CODEX_RVF_CLINE_KANBAN_START_CMD"),
                ),
                args.cline_kanban_task_cmd
                or legacy_default_cline_kanban_env_value(
                    "CODEX_RVF_CLINE_KANBAN_TASK_CMD",
                    os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD"),
                ),
                args.cline_kanban_start_timeout
                or os.environ.get("CODEX_RVF_CLINE_KANBAN_START_TIMEOUT"),
                args.cline_kanban_tmux_session
                or os.environ.get("CODEX_RVF_CLINE_KANBAN_TMUX_SESSION"),
                args.cline_kanban_base_ref
                or os.environ.get("CODEX_RVF_CLINE_KANBAN_BASE_REF"),
                args.cline_kanban_worktree_mode
                or os.environ.get("CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE"),
                args.cline_kanban_auto_review_enabled
                or os.environ.get("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_ENABLED"),
                args.cline_kanban_auto_review_mode
                or os.environ.get("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_MODE"),
                args.cline_kanban_start_in_plan_mode
                or os.environ.get("CODEX_RVF_CLINE_KANBAN_START_IN_PLAN_MODE"),
                user_local_data_paths=user_local_data_paths,
            )
        if args.configure_user_prompt_submit_hook:
            hooks_path = configure_user_prompt_submit_hook(
                plugin_skill_dir,
                user_local_data_paths=user_local_data_paths,
            )
        (
            kimi_plugin_registration,
            kimi_plugin_registration_failure,
        ) = kimi_plugin_registration_outcome_for_deployment_receipt(
            active_marketplace_root,
            enabled=args.configure_kimi_plugin_registration,
        )
        receipt = {
            "schema_version": DEPLOYMENT_RECEIPT_SCHEMA_VERSION,
            "status": "installed",
            "installed_at": utc_now(),
            "deployment_source_worktree": str(source_repository),
            "local_main_commit": actual_commit,
            "source_commit_count": source_commit_count,
            "generated_plugin_version": release.generated_plugin_version,
            "payload_digest": release.payload_digest,
            "release_root": str(release.release_root),
            "active_marketplace_root": str(active_marketplace_root),
            "marketplace_identifier": DEVELOPER_MARKETPLACE_NAME,
            # 只记「写了什么」，绝不记「生效了」——Kimi 的 state 不落盘，生效与否必须由
            # kimi_plugin_manifest_activation_state_check.py 独立复算。
            "kimi_plugin_registration": kimi_plugin_registration,
            "kimi_plugin_registration_failure": kimi_plugin_registration_failure,
            "plugin_selector": DEVELOPER_PLUGIN_SELECTOR,
            "codex_executable": str(codex_executable),
            "claude_executable": str(claude_executable),
            "official_cli_versions": official_cli_versions,
            "official_cli_commands": [
                list(command) for command in official_cli_result.commands
            ],
            "removed_noncanonical_plugin_selectors": {
                "codex": list(
                    official_cli_result.removed_noncanonical_codex_plugin_selectors
                ),
                "claude": list(
                    official_cli_result.removed_noncanonical_claude_plugin_selectors
                ),
            },
            "live_codex_sessions_holding_plugin_cache_paths_to_be_removed": [
                dict(finding)
                for finding in (
                    official_cli_result
                    .live_codex_sessions_holding_plugin_cache_paths_to_be_removed
                )
            ],
            "hooks_path": str(hooks_path) if hooks_path else None,
            "user_local_data_root": str(user_local_data_paths.user_local_data_root),
            "rollback_status": deployment_transaction.rollback_status,
        }
        receipt_path = write_json_atomic(
            user_local_data_paths.deployment_receipts_root
            / DEPLOYMENT_RECEIPT_FILE_NAME,
            receipt,
        )
        receipt["receipt_path"] = str(receipt_path)
    except Exception as exc:
        print(f"RVF official CLI installation FAILED: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    else:
        print(
            "RVF official CLI installation OK: "
            f"{receipt['generated_plugin_version']} @ {actual_commit[:12]}"
        )
        # 非致命≠可以静默：Kimi 这一环失败时，人类摘要里必须看得见，否则「降级」就变成了隐瞒。
        if receipt.get("kimi_plugin_registration_failure"):
            print(
                "  ⚠️ Kimi 插件注册未完成（非致命，Codex/Claude 已部署且回执已写）: "
                f"{receipt['kimi_plugin_registration_failure']['message']}",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
