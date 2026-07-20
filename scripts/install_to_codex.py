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
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
    if not paths.reviewer_registry_path.exists():
        bundled_registry = (
            source_repository
            / f"plugins/{PLUGIN_NAME}"
            / PLUGIN_SKILL_RELATIVE_PATH
            / "config/reviewer-registry.json"
        )
        if not bundled_registry.is_file():
            raise OfficialCliInstallationError(
                f"缺少 bundled reviewer registry: {bundled_registry}"
            )
        shutil.copy2(bundled_registry, paths.reviewer_registry_path)
    return paths


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
            f"python3 {shlex.quote(str(router))}",
        ]
    )
    entry = {
        "type": "command",
        "command": " ".join(env_parts),
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
        "command": " ".join(
            [
                f"RVF_USER_LOCAL_DATA_ROOT={shlex.quote(str(paths.user_local_data_root))}",
                f"CODEX_RVF_LOG_ROOT={shlex.quote(str(paths.run_logs_root))}",
                f"CODEX_RVF_STATE_DIR={shlex.quote(str(paths.runtime_state_root))}",
                f"CODEX_RVF_INSTALLED_SKILL_DIR={shlex.quote(str(plugin_skill_dir))}",
                f"python3 {shlex.quote(str(detector))}",
            ]
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
    parser.add_argument("--source-local-main-worktree", type=Path)
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
        args.source_local_main_worktree or args.source_local_main_commit
    )
    if rollback_requested and source_arguments_present:
        parser.error("rollback mode 不接受 local-main source 参数")
    if not rollback_requested and (
        args.source_local_main_worktree is None
        or not args.source_local_main_commit
    ):
        parser.error(
            "deployment mode 必须同时提供 --source-local-main-worktree "
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

        source_repository = args.source_local_main_worktree.expanduser().resolve()
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
        receipt = {
            "schema_version": DEPLOYMENT_RECEIPT_SCHEMA_VERSION,
            "status": "installed",
            "installed_at": utc_now(),
            "local_main_worktree": str(source_repository),
            "local_main_commit": actual_commit,
            "source_commit_count": source_commit_count,
            "generated_plugin_version": release.generated_plugin_version,
            "payload_digest": release.payload_digest,
            "release_root": str(release.release_root),
            "active_marketplace_root": str(active_marketplace_root),
            "marketplace_identifier": DEVELOPER_MARKETPLACE_NAME,
            "plugin_selector": DEVELOPER_PLUGIN_SELECTOR,
            "codex_executable": str(codex_executable),
            "claude_executable": str(claude_executable),
            "official_cli_versions": official_cli_versions,
            "official_cli_commands": [
                list(command) for command in official_cli_result.commands
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
