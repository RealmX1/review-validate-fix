#!/usr/bin/env python3
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR_NAME = "review-validate-fix"
PLUGIN_NAME = "review-validate-fix"
PLUGIN_SRC = ROOT / "plugins" / PLUGIN_DIR_NAME
PLUGIN_SKILL_REL = Path("skills") / "review-validate-fix"
DEFAULT_MARKETPLACE_NAME = "local-codex-plugins"
PLUGIN_MANIFEST = PLUGIN_SRC / ".codex-plugin" / "plugin.json"
CLAUDE_MARKETPLACE_SRC = ROOT / ".claude-plugin" / "marketplace.json"
CLAUDE_MARKETPLACE_NAME = "review-validate-fix-local"
CLAUDE_PLUGIN_CONFIG_ID = f"{PLUGIN_DIR_NAME}@{CLAUDE_MARKETPLACE_NAME}"
CLAUDE_MARKETPLACE_ROOT_REL = Path(".claude") / "local-marketplaces" / PLUGIN_DIR_NAME
CLAUDE_PLUGIN_REL = Path("plugins") / PLUGIN_DIR_NAME
CLAUDE_CACHE_ROOT_REL = (
    Path(".claude")
    / "plugins"
    / "cache"
    / CLAUDE_MARKETPLACE_NAME
    / PLUGIN_DIR_NAME
)
DEPLOY_LOG_REL = Path("state") / "deployments"
DEPLOY_HISTORY_FILE = "deployments.jsonl"
DEPLOY_LATEST_FILE = "latest-deployment.json"
DEPLOY_LOG_VERSION = 1
DEPLOY_STAMP_RE = re.compile(r"\s+\[deployed [^\]]+\]$")
LEGACY_DEFAULT_CLINE_KANBAN_ENV = {
    "CODEX_RVF_CLINE_KANBAN_START_CMD": "npx -y kanban@0.1.66 --no-open",
    "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "npx -y kanban@0.1.66 task",
}
CLINE_KANBAN_WORKTREE_MODES = {"branch", "inplace"}

PRESERVE_IN_PLUGIN = {
    PLUGIN_SKILL_REL / "config" / "reviewer-registry.json",
    PLUGIN_SKILL_REL / "state",
}
IGNORE_NAMES = {".DS_Store", "__pycache__", ".pytest_cache", ".mypy_cache", "state"}
DEV_ONLY_NAMES = {
    ".rvf-dev-only",
    "dev-only",
    "dev_only",
    "check_plugin_contracts.py",
    "check_skill_contracts.sh",
    "install_to_codex.py",
}


def ignore(_: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in IGNORE_NAMES or name in DEV_ONLY_NAMES or name.endswith(".pyc")
    }


def is_dev_only_path(path: Path) -> bool:
    return any(part in DEV_ONLY_NAMES for part in path.parts)


def is_preserved(path: Path, preserved: set[Path]) -> bool:
    for item in preserved:
        if path == item or item in path.parents:
            return True
    return False


def contains_preserved(path: Path, preserved: set[Path]) -> bool:
    for item in preserved:
        if path == item or path in item.parents:
            return True
    return False


def legacy_default_cline_kanban_env_value(name: str, value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if LEGACY_DEFAULT_CLINE_KANBAN_ENV.get(name) == text:
        return None
    return text


def normalized_cline_kanban_worktree_mode(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    if text not in CLINE_KANBAN_WORKTREE_MODES:
        return None
    return text


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_text(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_metadata() -> dict[str, Any]:
    status_short = (run_text(["git", "status", "--short"]) or "").splitlines()
    return {
        "repo": str(ROOT),
        "head": run_text(["git", "rev-parse", "HEAD"]),
        "branch": run_text(["git", "branch", "--show-current"]),
        "describe": run_text(["git", "describe", "--always", "--dirty", "--tags"]),
        "status_short": status_short,
        "dirty": bool(status_short),
    }


def deploy_version_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    head = metadata.get("head")
    source_head = head if isinstance(head, str) and head.strip() else None
    short_head = source_head[:12] if source_head else "unknown"
    dirty = bool(metadata.get("dirty"))
    heading_label = f"{short_head}-dirty" if dirty else short_head
    return {
        "source_head": source_head,
        "source_short_head": short_head,
        "dirty": dirty,
        "heading_label": heading_label,
    }


def stamp_skill_heading_text(text: str, heading_label: str) -> str:
    lines = text.splitlines(keepends=True)
    if not lines:
        return text
    for index, line in enumerate(lines):
        newline = ""
        content = line
        if content.endswith("\r\n"):
            newline = "\r\n"
            content = content[:-2]
        elif content.endswith("\n"):
            newline = "\n"
            content = content[:-1]
        if not content.startswith("# "):
            continue
        base = DEPLOY_STAMP_RE.sub("", content)
        lines[index] = f"{base} [deployed {heading_label}]{newline}"
        return "".join(lines)
    return text


def stamp_skill_heading(path: Path, heading_label: str) -> bool:
    text = path.read_text(encoding="utf-8")
    stamped = stamp_skill_heading_text(text, heading_label)
    if stamped == text:
        return False
    path.write_text(stamped, encoding="utf-8")
    return True


def stamp_deployed_skill_headings(plugin_roots: list[Path], deploy_version: dict[str, Any]) -> list[Path]:
    heading_label = str(deploy_version.get("heading_label") or "unknown")
    stamped: list[Path] = []
    for root in plugin_roots:
        skills_dir = root / "skills"
        if not skills_dir.is_dir():
            continue
        for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
            if stamp_skill_heading(skill_md, heading_label):
                stamped.append(skill_md)
    return stamped


def read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def deployment_file_digest(root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    if not root.exists():
        return {
            "algorithm": "sha256",
            "value": None,
            "file_count": 0,
            "byte_count": 0,
            "root": str(root),
        }
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rel = path.relative_to(root)
        if is_dev_only_path(rel) or is_preserved(rel, PRESERVE_IN_PLUGIN):
            continue
        if any(part in IGNORE_NAMES for part in rel.parts) or path.name.endswith(".pyc"):
            continue
        data = path.read_bytes()
        digest.update(str(rel).encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
        file_count += 1
        byte_count += len(data)
    return {
        "algorithm": "sha256",
        "value": digest.hexdigest(),
        "file_count": file_count,
        "byte_count": byte_count,
        "root": str(root),
        "excluded": ["state/", "config/reviewer-registry.json", "dev-only paths", "*.pyc"],
    }


def summarize_rvf_run(run_dir: Path | None) -> dict[str, Any] | None:
    if run_dir is None:
        return None
    summary_path = run_dir.expanduser() / "summary.json"
    summary = read_json_object(summary_path)
    if summary is None:
        return {"run_dir": str(run_dir.expanduser()), "summary_path": str(summary_path), "available": False}
    keys = [
        "run_id",
        "status",
        "reason_code",
        "started_at",
        "ended_at",
        "updated_at",
        "parent_thread_id",
        "parent_session_id",
        "session_id",
        "rvf_backend",
        "rvf_state_phase",
        "rvf_handoff_path",
    ]
    compact = {key: summary.get(key) for key in keys if summary.get(key) is not None}
    compact["run_dir"] = str(run_dir.expanduser())
    compact["summary_path"] = str(summary_path)
    compact["available"] = True
    analysis_dir = run_dir.expanduser() / "artifacts" / "analysis"
    if analysis_dir.exists():
        compact["analysis_paths"] = {
            "summary_md": str(analysis_dir / "summary.md"),
            "causality_json": str(analysis_dir / "causality.json"),
        }
    return compact


def latest_run_from_root(root: Path) -> dict[str, Any] | None:
    latest_path = root.expanduser() / "latest.json"
    latest = read_json_object(latest_path)
    if latest is None:
        return None
    payload = {
        "latest_path": str(latest_path),
        "pointer": latest,
    }
    summary_path = latest.get("summary_path")
    if isinstance(summary_path, str) and summary_path.strip():
        payload["summary"] = summarize_rvf_run(Path(summary_path).expanduser().parent)
    return payload


def rvf_session_context(plugin_skill_dir: Path) -> dict[str, Any]:
    env_keys = [
        "CODEX_RVF_RUN_ID",
        "CODEX_RVF_RUN_DIR",
        "RVF_RUN_ID",
        "RVF_RUN_DIR",
        "CODEX_RVF_LOG_ROOT",
        "CODEX_RVF_STATE_DIR",
        "CODEX_SESSION_ID",
        "CODEX_THREAD_ID",
    ]
    env = {key: os.environ[key] for key in env_keys if os.environ.get(key)}
    run_dir_text = env.get("CODEX_RVF_RUN_DIR") or env.get("RVF_RUN_DIR")
    roots: list[Path] = []
    for key in ("CODEX_RVF_LOG_ROOT", "CODEX_RVF_STATE_DIR"):
        value = os.environ.get(key)
        if value and value.strip():
            roots.append(Path(value).expanduser())
    roots.append(plugin_skill_dir / "state")

    seen: set[str] = set()
    latest_runs = []
    for root in roots:
        root_key = str(root)
        if root_key in seen:
            continue
        seen.add(root_key)
        latest = latest_run_from_root(root)
        if latest is not None:
            latest_runs.append(latest)
    return {
        "env": env,
        "current_run": summarize_rvf_run(Path(run_dir_text).expanduser()) if run_dir_text else None,
        "latest_runs": latest_runs,
    }


def build_deploy_log_entry(
    *,
    dst: Path,
    plugin_cache: Path,
    claude_paths: dict[str, Path] | None,
    source_metadata: dict[str, Any],
    deploy_version: dict[str, Any],
    configure_stop_hook_enabled: bool,
    configure_user_prompt_submit_hook_enabled: bool,
    fork_mode: str,
    preserve_local_config: bool,
    replace_setup_config: bool,
) -> dict[str, Any]:
    plugin_skill_dir = dst / PLUGIN_SKILL_REL
    cache_skill_dir = plugin_cache / PLUGIN_SKILL_REL
    return {
        "version": DEPLOY_LOG_VERSION,
        "kind": "rvf-local-deploy",
        "deployed_at": utc_now(),
        "plugin": {
            "name": PLUGIN_NAME,
            "directory_name": PLUGIN_DIR_NAME,
            "version": plugin_version(),
        },
        "source": source_metadata,
        "deploy_version": deploy_version,
        "runtime_hashes": {
            "plugin": deployment_file_digest(dst),
            "cache": deployment_file_digest(plugin_cache),
        },
        "destinations": {
            "plugin": str(dst),
            "plugin_skill": str(plugin_skill_dir),
            "plugin_cache": str(plugin_cache),
            "cache_skill": str(cache_skill_dir),
            "codex_config": str(Path.home() / ".codex" / "config.toml"),
            "hooks": str(Path.home() / ".codex" / "hooks.json"),
            "marketplace": str(Path.home() / ".agents" / "plugins" / "marketplace.json"),
            "claude_plugin_marketplace": str(claude_paths["marketplace"]) if claude_paths else None,
            "claude_marketplace_metadata": (
                str(claude_paths["marketplace_metadata"]) if claude_paths else None
            ),
            "claude_plugin_cache": str(claude_paths["cache"]) if claude_paths else None,
            "claude_settings": str(claude_paths["settings"]) if claude_paths else None,
            "claude_installed_plugins": str(claude_paths["installed_plugins"]) if claude_paths else None,
        },
        "options": {
            "configure_stop_hook": configure_stop_hook_enabled,
            "configure_user_prompt_submit_hook": configure_user_prompt_submit_hook_enabled,
            "sync_claude_plugin": claude_paths is not None,
            "fork_mode": normalize_fork_mode(fork_mode),
            "preserve_local_config": preserve_local_config,
            "replace_setup_config": replace_setup_config,
        },
        "rvf_sessions": rvf_session_context(plugin_skill_dir),
    }


def write_deploy_log(entry: dict[str, Any], skill_dirs: list[Path]) -> list[Path]:
    written: list[Path] = []
    for skill_dir in skill_dirs:
        log_dir = skill_dir / DEPLOY_LOG_REL
        log_dir.mkdir(parents=True, exist_ok=True)
        history = log_dir / DEPLOY_HISTORY_FILE
        with history.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        latest = log_dir / DEPLOY_LATEST_FILE
        tmp = latest.with_name(f".{latest.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(latest)
        written.append(history)
        written.append(latest)
    return written


def remove_unpreserved(dst: Path, preserved: set[Path], base: Path = Path()) -> None:
    if not dst.exists():
        return
    for child in sorted(dst.iterdir(), key=lambda p: len(p.parts), reverse=True):
        rel = base / child.name
        if is_preserved(rel, preserved):
            continue
        if child.is_dir() and not child.is_symlink() and contains_preserved(rel, preserved):
            remove_unpreserved(child, preserved, rel)
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def merge_tree(src: Path, dst: Path, preserved: set[Path], base: Path = Path()) -> None:
    for child in src.iterdir():
        if child.name in IGNORE_NAMES or child.name.endswith(".pyc"):
            continue
        rel = base / child.name
        if is_dev_only_path(rel):
            continue
        target = dst / child.name
        if is_preserved(rel, preserved) and target.exists():
            continue
        if child.is_dir() and not child.is_symlink():
            if target.exists() and not target.is_dir():
                target.unlink()
            if target.exists():
                merge_tree(child, target, preserved, rel)
            else:
                shutil.copytree(child, target, ignore=ignore)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


def copy_tree(src: Path, dst: Path, preserved: set[Path], preserve_local_config: bool) -> None:
    if not src.exists():
        raise FileNotFoundError(src)
    dst.mkdir(parents=True, exist_ok=True)
    effective_preserve = preserved if preserve_local_config else set()
    remove_unpreserved(dst, effective_preserve)
    merge_tree(src, dst, effective_preserve)


def vendor_pyroot(dst: Path) -> None:
    """把 repo-root ``core/`` + ``adapters/`` + ``.rvf-pyroot`` 哨兵 vendor 进部署
    payload 根 ``dst``。

    源真相留在 repo 顶层（``core/`` / ``adapters/`` 不在 ``PLUGIN_SRC`` 内，
    ``copy_tree`` 永远不会带上它们）；部署时把它们内嵌进 payload，使部署后的
    scripts 能 ``import core.* / adapters.*``——靠 ``_rvf_pyroot.py`` 哨兵自底向上
    定位本 ``dst`` 根。漏掉这步 → 部署后运行期 ``ModuleNotFoundError``，而 repo
    测试仍全绿（漂移），所以 ``deploy_payload`` 把它与 ``copy_tree`` 构造上绑死。
    """
    for sub in ("core", "adapters"):
        src_pkg = ROOT / sub
        if not src_pkg.is_dir():
            raise FileNotFoundError(src_pkg)
        dst_pkg = dst / sub
        if dst_pkg.exists():
            shutil.rmtree(dst_pkg)
        shutil.copytree(src_pkg, dst_pkg, ignore=ignore)
    shutil.copyfile(ROOT / ".rvf-pyroot", dst / ".rvf-pyroot")


def deploy_payload(src: Path, dst: Path, preserved: set[Path], preserve_local_config: bool) -> None:
    """部署 plugin payload 的**单一收口**：``copy_tree`` + ``vendor_pyroot``。

    所有「从源 plugin 树产出一份新 payload」的路径都必须走这里，确保 core/adapters/
    哨兵随 payload 一并 vendor。下游再从已 vendored 的 ``dst`` 拷贝（codex cache /
    claude marketplace+cache）即自动继承，无需重复 vendor。
    """
    copy_tree(src, dst, preserved, preserve_local_config)
    vendor_pyroot(dst)


def copy_missing_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_file():
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return
    for child in src.rglob("*"):
        rel = child.relative_to(src)
        target = dst / rel
        if child.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


def copy_legacy_config_if_safe(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    repo_default = PLUGIN_SRC / PLUGIN_SKILL_REL / "config" / "reviewer-registry.json"
    should_copy = not dst.exists()
    if not should_copy and repo_default.exists():
        try:
            should_copy = dst.read_bytes() == repo_default.read_bytes()
        except OSError:
            should_copy = False
    if should_copy:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def plugin_version() -> str:
    data = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"plugin manifest missing version: {PLUGIN_MANIFEST}")
    return version


def claude_marketplace_root() -> Path:
    return Path.home() / CLAUDE_MARKETPLACE_ROOT_REL


def claude_marketplace_plugin_path() -> Path:
    return claude_marketplace_root() / CLAUDE_PLUGIN_REL


def claude_cache_plugin_path() -> Path:
    return Path.home() / CLAUDE_CACHE_ROOT_REL / plugin_version()


def claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def claude_installed_plugins_path() -> Path:
    return Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def claude_plugin_enabled_or_installed() -> bool:
    settings = read_json_object(claude_settings_path()) or {}
    enabled_plugins = settings.get("enabledPlugins")
    if isinstance(enabled_plugins, dict) and enabled_plugins.get(CLAUDE_PLUGIN_CONFIG_ID) is True:
        return True
    marketplaces = settings.get("extraKnownMarketplaces")
    if isinstance(marketplaces, dict) and CLAUDE_MARKETPLACE_NAME in marketplaces:
        return True

    installed = read_json_object(claude_installed_plugins_path()) or {}
    plugins = installed.get("plugins")
    if isinstance(plugins, dict) and CLAUDE_PLUGIN_CONFIG_ID in plugins:
        return True

    if claude_marketplace_plugin_path().exists():
        return True
    cache_root = Path.home() / CLAUDE_CACHE_ROOT_REL
    return cache_root.exists()


def update_claude_settings() -> Path:
    path = claude_settings_path()
    data = read_json_object(path) or {}
    enabled = data.setdefault("enabledPlugins", {})
    if not isinstance(enabled, dict):
        enabled = {}
        data["enabledPlugins"] = enabled
    enabled[CLAUDE_PLUGIN_CONFIG_ID] = True

    marketplaces = data.setdefault("extraKnownMarketplaces", {})
    if not isinstance(marketplaces, dict):
        marketplaces = {}
        data["extraKnownMarketplaces"] = marketplaces
    marketplaces[CLAUDE_MARKETPLACE_NAME] = {
        "source": {
            "source": "directory",
            "path": str(claude_marketplace_root()),
        }
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def update_claude_installed_plugins(cache_plugin: Path) -> Path:
    path = claude_installed_plugins_path()
    data = read_json_object(path) or {}
    data["version"] = 2
    plugins = data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        plugins = {}
        data["plugins"] = plugins

    existing_entries = plugins.get(CLAUDE_PLUGIN_CONFIG_ID)
    existing = (
        existing_entries[0]
        if isinstance(existing_entries, list)
        and existing_entries
        and isinstance(existing_entries[0], dict)
        else {}
    )
    now = utc_now()
    record = dict(existing)
    record.update(
        {
            "scope": record.get("scope") or "user",
            "installPath": str(cache_plugin),
            "version": plugin_version(),
            "installedAt": record.get("installedAt") or now,
            "lastUpdated": now,
        }
    )
    plugins[CLAUDE_PLUGIN_CONFIG_ID] = [record]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def sync_claude_marketplace_metadata() -> Path:
    if not CLAUDE_MARKETPLACE_SRC.is_file():
        raise FileNotFoundError(
            f"missing source marketplace metadata: {CLAUDE_MARKETPLACE_SRC}"
        )
    dst = claude_marketplace_root() / ".claude-plugin" / "marketplace.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CLAUDE_MARKETPLACE_SRC, dst)
    return dst


def sync_claude_plugin(plugin_src: Path, preserve_local_config: bool) -> dict[str, Path]:
    marketplace_plugin = claude_marketplace_plugin_path()
    cache_plugin = claude_cache_plugin_path()
    copy_tree(plugin_src, marketplace_plugin, PRESERVE_IN_PLUGIN, preserve_local_config)
    copy_tree(plugin_src, cache_plugin, PRESERVE_IN_PLUGIN, preserve_local_config)
    marketplace_metadata = sync_claude_marketplace_metadata()
    settings = update_claude_settings()
    installed_plugins = update_claude_installed_plugins(cache_plugin)
    return {
        "marketplace": marketplace_plugin,
        "marketplace_metadata": marketplace_metadata,
        "cache": cache_plugin,
        "settings": settings,
        "installed_plugins": installed_plugins,
    }


def marketplace_name() -> str:
    marketplace_path = Path.home() / ".agents" / "plugins" / "marketplace.json"
    if not marketplace_path.exists():
        return DEFAULT_MARKETPLACE_NAME
    try:
        data = json.loads(marketplace_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DEFAULT_MARKETPLACE_NAME
    name = data.get("name")
    return name if isinstance(name, str) and name.strip() else DEFAULT_MARKETPLACE_NAME


def plugin_config_id() -> str:
    return f"{PLUGIN_NAME}@{marketplace_name()}"


def ensure_codex_plugin_enabled() -> Path:
    config_path = Path.home() / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    header = f'[plugins."{plugin_config_id()}"]'
    header_line = f"{header}\n"
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True) if config_path.exists() else []
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == header:
            start = index
            break

    if start is None:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.extend([header_line, "enabled = true\n"])
    else:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            stripped = lines[index].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                end = index
                break

        enabled_index: int | None = None
        for index in range(start + 1, end):
            key = lines[index].split("#", 1)[0].split("=", 1)[0].strip()
            if key == "enabled":
                enabled_index = index
                break
        if enabled_index is None:
            lines.insert(end, "enabled = true\n")
        else:
            lines[enabled_index] = "enabled = true\n"

    config_path.write_text("".join(lines), encoding="utf-8")
    return config_path


def update_marketplace(plugin_parent: Path) -> Path:
    marketplace_path = Path.home() / ".agents" / "plugins" / "marketplace.json"
    marketplace_path.parent.mkdir(parents=True, exist_ok=True)
    if marketplace_path.exists():
        data = json.loads(marketplace_path.read_text(encoding="utf-8"))
    else:
        data = {
            "name": "local-codex-plugins",
            "interface": {"displayName": "Local Codex Plugins"},
            "plugins": [],
        }

    data.setdefault("name", "local-codex-plugins")
    data.setdefault("interface", {}).setdefault("displayName", "Local Codex Plugins")
    plugins = data.setdefault("plugins", [])
    entry = {
        "name": PLUGIN_NAME,
        "source": {
            "source": "local",
            "path": f"./plugins/{PLUGIN_DIR_NAME}",
        },
        "policy": {
            "installation": "AVAILABLE",
            "authentication": "ON_INSTALL",
        },
        "category": "Coding",
    }
    source_path = entry["source"]["path"]
    plugins[:] = [
        plugin
        for plugin in plugins
        if not (
            plugin.get("name") == PLUGIN_NAME
            or (
                isinstance(plugin.get("source"), dict)
                and plugin["source"].get("path") == source_path
            )
        )
    ]
    plugins.append(entry)

    marketplace_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    expected_parent = Path.home()
    if plugin_parent.resolve() != (expected_parent / "plugins").resolve():
        print(
            "提示: marketplace 使用 ./plugins/review-validate-fix；"
            f"当前 plugin 安装目录为 {plugin_parent}，请确认 Codex 的本机 marketplace 解析规则。",
            file=sys.stderr,
        )
    return marketplace_path


def sync_codex_plugin_cache(plugin_src: Path, preserve_local_config: bool) -> Path:
    cache_dir = (
        Path.home()
        / ".codex"
        / "plugins"
        / "cache"
        / marketplace_name()
        / PLUGIN_NAME
        / plugin_version()
    )
    copy_tree(plugin_src, cache_dir, PRESERVE_IN_PLUGIN, preserve_local_config)
    if preserve_local_config:
        copy_legacy_config_if_safe(
            plugin_src / PLUGIN_SKILL_REL / "config" / "reviewer-registry.json",
            cache_dir / PLUGIN_SKILL_REL / "config" / "reviewer-registry.json",
        )
        copy_missing_tree(
            plugin_src / PLUGIN_SKILL_REL / "state",
            cache_dir / PLUGIN_SKILL_REL / "state",
        )
    return cache_dir


def normalize_fork_mode(value: str) -> str:
    mode = (value or "auto").strip()
    if mode in {"cline", "kanban", "ck"}:
        return "cline-kanban"
    if mode in {"kanban-message", "kanban-inject"}:
        return "kanban-followup"
    return mode


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
) -> Path:
    fork_mode = normalize_fork_mode(fork_mode)
    cline_kanban_worktree_mode = normalized_cline_kanban_worktree_mode(cline_kanban_worktree_mode)
    hooks_path = Path.home() / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    if hooks_path.exists():
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    else:
        data = {"hooks": {}}

    hooks = data.setdefault("hooks", {})
    stop_groups = hooks.setdefault("Stop", [])
    router = plugin_skill_dir / "scripts" / "codex_stop_hook_router.py"
    stable_dispatcher = plugin_skill_dir / "scripts" / "codex_stop_hook_dispatcher.py"
    dev_dispatcher = ROOT / "plugins" / PLUGIN_DIR_NAME / PLUGIN_SKILL_REL / "scripts" / "codex_stop_hook_dispatcher.py"
    env_parts = [
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
            ("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_ENABLED", cline_kanban_auto_review_enabled),
            ("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_MODE", cline_kanban_auto_review_mode),
            ("CODEX_RVF_CLINE_KANBAN_START_IN_PLAN_MODE", cline_kanban_start_in_plan_mode),
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
    command = " ".join(env_parts)
    entry = {
        "type": "command",
        "command": command,
        "timeout": 300,
        "statusMessage": "Review-Validate-Fix：选择通道并运行停止检查",
    }

    target_group: dict[str, object] | None = None
    cleaned_stop_groups: list[dict[str, object]] = []
    for group in stop_groups:
        if not isinstance(group, dict):
            continue
        group_hooks = group.get("hooks")
        if not isinstance(group_hooks, list):
            group["hooks"] = []
            cleaned_stop_groups.append(group)
            continue

        kept_hooks = []
        removed_rvf_hook = False
        for existing in group_hooks:
            command_value = existing.get("command") if isinstance(existing, dict) else None
            is_rvf_hook = (
                isinstance(command_value, str)
                and (
                    "codex_stop_review_validate_fix.py" in command_value
                    or "codex_stop_hook_dispatcher.py" in command_value
                    or "codex_stop_hook_router.py" in command_value
                )
            )
            if is_rvf_hook:
                removed_rvf_hook = True
                if target_group is None:
                    target_group = group
                continue
            kept_hooks.append(existing)

        if removed_rvf_hook and target_group is group:
            kept_hooks.append(entry)
        group["hooks"] = kept_hooks
        if kept_hooks:
            cleaned_stop_groups.append(group)

    hooks["Stop"] = cleaned_stop_groups
    if target_group is None:
        cleaned_stop_groups.append({"hooks": [entry]})

    hooks_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return hooks_path


def configure_user_prompt_submit_hook(plugin_skill_dir: Path) -> Path:
    hooks_path = Path.home() / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    if hooks_path.exists():
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    else:
        data = {"hooks": {}}

    hooks = data.setdefault("hooks", {})
    prompt_groups = hooks.setdefault("UserPromptSubmit", [])
    detector = plugin_skill_dir / "scripts" / "rvf_user_prompt_submit.py"
    entry = {
        "type": "command",
        "command": f"python3 {shlex.quote(str(detector))}",
        "timeout": 5,
    }

    target_group: dict[str, object] | None = None
    cleaned_prompt_groups: list[dict[str, object]] = []
    for group in prompt_groups:
        if not isinstance(group, dict):
            continue
        group_hooks = group.get("hooks")
        if not isinstance(group_hooks, list):
            group["hooks"] = []
            cleaned_prompt_groups.append(group)
            continue

        kept_hooks = []
        removed_rvf_hook = False
        for existing in group_hooks:
            command_value = existing.get("command") if isinstance(existing, dict) else None
            is_rvf_hook = isinstance(command_value, str) and "rvf_user_prompt_submit.py" in command_value
            if is_rvf_hook:
                removed_rvf_hook = True
                if target_group is None:
                    target_group = group
                continue
            kept_hooks.append(existing)

        if removed_rvf_hook and target_group is group:
            kept_hooks.append(entry)
        group["hooks"] = kept_hooks
        if kept_hooks:
            cleaned_prompt_groups.append(group)

    hooks["UserPromptSubmit"] = cleaned_prompt_groups
    if target_group is None:
        cleaned_prompt_groups.append({"hooks": [entry]})

    hooks_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return hooks_path


def main() -> int:
    parser = argparse.ArgumentParser(description="把本仓库 plugin 安装到 Codex 本机 plugin 空间。")
    parser.add_argument(
        "--plugin-parent",
        default=str(Path.home() / "plugins"),
        help="plugin 父目录；默认安装到 ~/plugins/review-validate-fix 并由 marketplace entry review-validate-fix 指向。",
    )
    parser.add_argument(
        "--replace-setup-config",
        action="store_true",
        help="覆盖本机 setup 相关配置；默认会保留 reviewer-registry.json 和 state/。",
    )
    parser.add_argument(
        "--configure-stop-hook",
        action="store_true",
        help="更新 ~/.codex/hooks.json，让 Stop hook 先经 plugin 内 router 选择 stable/dev channel，再调用对应 dispatcher。",
    )
    parser.add_argument(
        "--configure-user-prompt-submit-hook",
        action="store_true",
        help="更新 ~/.codex/hooks.json，为 RVF dispatch token 注册 UserPromptSubmit detector；不启动 review workflow。",
    )
    parser.add_argument(
        "--sync-claude-plugin",
        action="store_true",
        help="强制同步 Claude Code local marketplace/cache，并启用 review-validate-fix@review-validate-fix-local。",
    )
    parser.add_argument(
        "--skip-claude-plugin",
        action="store_true",
        help="跳过 Claude Code plugin 同步；默认在检测到已有 Claude RVF 安装时自动同步。",
    )
    parser.add_argument(
        "--fork-mode",
        choices=[
            "gui",
            "auto",
            "cline-kanban",
            "cline",
            "kanban",
            "ck",
            "kanban-followup",
            "kanban-message",
            "kanban-inject",
            "manual",
            "dry-run",
        ],
        default="auto",
        help="与 --configure-stop-hook 配合写入 CODEX_RVF_FORK_MODE；默认 auto。",
    )
    parser.add_argument(
        "--cline-kanban-start-cmd",
        default=None,
        help="持久写入 CODEX_RVF_CLINE_KANBAN_START_CMD；默认 kanban --no-open。",
    )
    parser.add_argument(
        "--cline-kanban-task-cmd",
        default=None,
        help="持久写入 CODEX_RVF_CLINE_KANBAN_TASK_CMD；默认 kanban task。",
    )
    parser.add_argument(
        "--cline-kanban-start-timeout",
        default=None,
        help="持久写入 CODEX_RVF_CLINE_KANBAN_START_TIMEOUT；默认 90。",
    )
    parser.add_argument(
        "--cline-kanban-tmux-session",
        default=None,
        help="持久写入 CODEX_RVF_CLINE_KANBAN_TMUX_SESSION；默认 cline-kanban-3484。",
    )
    parser.add_argument(
        "--cline-kanban-base-ref",
        default=None,
        help="持久写入 CODEX_RVF_CLINE_KANBAN_BASE_REF；未提供时 Stop hook 使用当前 HEAD。",
    )
    parser.add_argument(
        "--cline-kanban-worktree-mode",
        choices=["branch", "inplace"],
        default=None,
        help="持久写入 CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE；默认 branch。",
    )
    parser.add_argument(
        "--cline-kanban-auto-review-enabled",
        default=None,
        help="持久写入 CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_ENABLED；默认 0。",
    )
    parser.add_argument(
        "--cline-kanban-auto-review-mode",
        choices=["commit", "pr", "move_to_trash"],
        default=None,
        help="持久写入 CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_MODE；仅 auto-review enabled 时传给 Kanban。",
    )
    parser.add_argument(
        "--cline-kanban-start-in-plan-mode",
        default=None,
        help="持久写入 CODEX_RVF_CLINE_KANBAN_START_IN_PLAN_MODE；默认 0。",
    )
    args = parser.parse_args()

    preserve = not args.replace_setup_config
    installed: list[str] = []

    try:
        parent = Path(args.plugin_parent).expanduser().resolve()
        dst = parent / PLUGIN_DIR_NAME
        # A：唯一主动 vendor 点——产出 dst 同时把 core/adapters/哨兵内嵌。
        deploy_payload(PLUGIN_SRC, dst, PRESERVE_IN_PLUGIN, preserve)
        marketplace = update_marketplace(parent)
        plugin_config = ensure_codex_plugin_enabled()
        # B：codex cache 从已 vendored 的 dst 拷贝，自动继承 vendoring。
        plugin_cache = sync_codex_plugin_cache(dst, preserve)
        sync_claude = (not args.skip_claude_plugin) and (
            args.sync_claude_plugin or claude_plugin_enabled_or_installed()
        )
        # C/D：claude marketplace+cache 也从 dst 拷贝（单 vendor 点收口），自动继承。
        claude_paths = sync_claude_plugin(dst, preserve) if sync_claude else None
        source_metadata = git_metadata()
        deploy_version = deploy_version_from_metadata(source_metadata)
        stamp_roots = [dst, plugin_cache]
        if claude_paths is not None:
            stamp_roots.extend([claude_paths["marketplace"], claude_paths["cache"]])
        stamped_skill_paths = stamp_deployed_skill_headings(stamp_roots, deploy_version)
        installed.append(f"plugin: {dst}")
        installed.append(f"Codex plugin enabled: {plugin_config}")
        installed.append(f"plugin cache: {plugin_cache}")
        if claude_paths is not None:
            installed.append(f"Claude plugin marketplace: {claude_paths['marketplace']}")
            installed.append(f"Claude marketplace metadata: {claude_paths['marketplace_metadata']}")
            installed.append(f"Claude plugin cache: {claude_paths['cache']}")
            installed.append(f"Claude settings: {claude_paths['settings']}")
            installed.append(f"Claude installed plugins: {claude_paths['installed_plugins']}")
        installed.append(
            "deploy version stamp: "
            f"{deploy_version['heading_label']} ({len(stamped_skill_paths)} SKILL.md files)"
        )
        installed.append(f"marketplace: {marketplace}")

        if args.configure_stop_hook:
            hooks_path = configure_stop_hook(
                dst / PLUGIN_SKILL_REL,
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
                args.cline_kanban_start_timeout or os.environ.get("CODEX_RVF_CLINE_KANBAN_START_TIMEOUT"),
                args.cline_kanban_tmux_session or os.environ.get("CODEX_RVF_CLINE_KANBAN_TMUX_SESSION"),
                args.cline_kanban_base_ref or os.environ.get("CODEX_RVF_CLINE_KANBAN_BASE_REF"),
                args.cline_kanban_worktree_mode
                or normalized_cline_kanban_worktree_mode(
                    os.environ.get("CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE")
                ),
                args.cline_kanban_auto_review_enabled or os.environ.get("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_ENABLED"),
                args.cline_kanban_auto_review_mode or os.environ.get("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_MODE"),
                args.cline_kanban_start_in_plan_mode or os.environ.get("CODEX_RVF_CLINE_KANBAN_START_IN_PLAN_MODE"),
            )
            installed.append(f"stop hook: {hooks_path}")
        if args.configure_user_prompt_submit_hook:
            hooks_path = configure_user_prompt_submit_hook(dst / PLUGIN_SKILL_REL)
            installed.append(f"user prompt submit hook: {hooks_path}")
        deploy_log_paths = write_deploy_log(
            build_deploy_log_entry(
                dst=dst,
                plugin_cache=plugin_cache,
                claude_paths=claude_paths,
                source_metadata=source_metadata,
                deploy_version=deploy_version,
                configure_stop_hook_enabled=args.configure_stop_hook,
                configure_user_prompt_submit_hook_enabled=args.configure_user_prompt_submit_hook,
                fork_mode=args.fork_mode,
                preserve_local_config=preserve,
                replace_setup_config=args.replace_setup_config,
            ),
            [dst / PLUGIN_SKILL_REL, plugin_cache / PLUGIN_SKILL_REL],
        )
        installed.append(
            "deploy log: "
            + ", ".join(str(path) for path in deploy_log_paths if path.name == DEPLOY_LATEST_FILE)
        )
    except Exception as exc:
        print(f"安装失败: {exc}", file=sys.stderr)
        return 2

    for item in installed:
        print(f"已安装 {item}")
    if preserve:
        print("已默认保留本机 setup 配置: reviewer-registry.json 与 state/。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
