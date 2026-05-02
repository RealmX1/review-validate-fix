#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR_NAME = "review-validate-fix"
PLUGIN_NAME = "rvf"
PLUGIN_SRC = ROOT / "plugins" / PLUGIN_DIR_NAME
PLUGIN_SKILL_REL = Path("skills") / "review-validate-fix"
SKILL_NAME = "review-validate-fix"
DEFAULT_MARKETPLACE_NAME = "local-codex-plugins"
PLUGIN_MANIFEST = PLUGIN_SRC / ".codex-plugin" / "plugin.json"
LEGACY_DEFAULT_CLINE_KANBAN_ENV = {
    "CODEX_RVF_CLINE_KANBAN_START_CMD": "npx -y kanban@0.1.66 --no-open",
    "CODEX_RVF_CLINE_KANBAN_TASK_CMD": "npx -y kanban@0.1.66 task",
}

PRESERVE_IN_PLUGIN = {
    PLUGIN_SKILL_REL / "config" / "alternative-reviewer.json",
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
    repo_default = PLUGIN_SRC / PLUGIN_SKILL_REL / "config" / "alternative-reviewer.json"
    should_copy = not dst.exists()
    if not should_copy and repo_default.exists():
        try:
            should_copy = dst.read_bytes() == repo_default.read_bytes()
        except OSError:
            should_copy = False
    if should_copy:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def remove_legacy_codex_skill_dir(plugin_skill_dir: Path, preserve_local_config: bool) -> Path | None:
    legacy = Path.home() / ".codex" / "skills" / SKILL_NAME
    if not legacy.exists() and not legacy.is_symlink():
        return None
    if preserve_local_config:
        copy_legacy_config_if_safe(
            legacy / "config" / "alternative-reviewer.json",
            plugin_skill_dir / "config" / "alternative-reviewer.json",
        )
        copy_missing_tree(legacy / "state", plugin_skill_dir / "state")
    if legacy.is_symlink() or legacy.is_file():
        legacy.unlink()
    else:
        shutil.rmtree(legacy)
    return legacy


def plugin_version() -> str:
    data = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"plugin manifest missing version: {PLUGIN_MANIFEST}")
    return version


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


def legacy_plugin_config_ids() -> set[str]:
    return {
        f"{SKILL_NAME}@{DEFAULT_MARKETPLACE_NAME}",
        f"{SKILL_NAME}@{marketplace_name()}",
    }


def remove_plugin_sections(lines: list[str], plugin_ids: set[str]) -> list[str]:
    output: list[str] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        matched = False
        for plugin_id in plugin_ids:
            if stripped == f'[plugins."{plugin_id}"]':
                matched = True
                index += 1
                while index < len(lines):
                    next_stripped = lines[index].strip()
                    if next_stripped.startswith("[") and next_stripped.endswith("]"):
                        break
                    index += 1
                break
        if matched:
            while output and not output[-1].strip():
                output.pop()
            if index < len(lines) and output and output[-1].strip():
                output.append("\n")
            continue
        output.append(lines[index])
        index += 1
    return output


def ensure_codex_plugin_enabled() -> Path:
    config_path = Path.home() / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    header = f'[plugins."{plugin_config_id()}"]'
    header_line = f"{header}\n"
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True) if config_path.exists() else []
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines = remove_plugin_sections(lines, legacy_plugin_config_ids())

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
            plugin.get("name") in {PLUGIN_NAME, SKILL_NAME}
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
            plugin_src / PLUGIN_SKILL_REL / "config" / "alternative-reviewer.json",
            cache_dir / PLUGIN_SKILL_REL / "config" / "alternative-reviewer.json",
        )
        copy_missing_tree(
            plugin_src / PLUGIN_SKILL_REL / "state",
            cache_dir / PLUGIN_SKILL_REL / "state",
        )
    return cache_dir


def remove_legacy_plugin_cache() -> Path | None:
    legacy_cache = Path.home() / ".codex" / "plugins" / "cache" / marketplace_name() / SKILL_NAME
    if not legacy_cache.exists() and not legacy_cache.is_symlink():
        return None
    if legacy_cache.is_symlink() or legacy_cache.is_file():
        legacy_cache.unlink()
    else:
        shutil.rmtree(legacy_cache)
    return legacy_cache


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
    cline_kanban_auto_review_enabled: str | None = None,
    cline_kanban_auto_review_mode: str | None = None,
    cline_kanban_start_in_plan_mode: str | None = None,
    open_handoff: bool = True,
    ide_open_cmd: str | None = None,
) -> Path:
    fork_mode = normalize_fork_mode(fork_mode)
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
    if not open_handoff:
        env_parts.append("CODEX_RVF_OPEN_HANDOFF=0")
    ide_open_text = (ide_open_cmd or "").strip()
    if ide_open_text:
        env_parts.append(f"CODEX_RVF_IDE_OPEN_CMD={shlex.quote(ide_open_text)}")
    if fork_mode in {"auto", "cline-kanban", "kanban-followup"}:
        for name, value in (
            ("CODEX_RVF_CLINE_KANBAN_START_CMD", cline_kanban_start_cmd),
            ("CODEX_RVF_CLINE_KANBAN_TASK_CMD", cline_kanban_task_cmd),
            ("CODEX_RVF_CLINE_KANBAN_START_TIMEOUT", cline_kanban_start_timeout),
            ("CODEX_RVF_CLINE_KANBAN_TMUX_SESSION", cline_kanban_tmux_session),
            ("CODEX_RVF_CLINE_KANBAN_BASE_REF", cline_kanban_base_ref),
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


def main() -> int:
    parser = argparse.ArgumentParser(description="把本仓库 plugin 安装到 Codex 本机 plugin 空间。")
    parser.add_argument(
        "--plugin-parent",
        default=str(Path.home() / "plugins"),
        help="plugin 父目录；默认安装到 ~/plugins/review-validate-fix 并由 marketplace entry rvf 指向。",
    )
    parser.add_argument(
        "--replace-setup-config",
        action="store_true",
        help="覆盖本机 setup 相关配置；默认会保留 alternative-reviewer.json 和 state/。",
    )
    parser.add_argument(
        "--configure-stop-hook",
        action="store_true",
        help="更新 ~/.codex/hooks.json，让 Stop hook 先经 plugin 内 router 选择 stable/dev channel，再调用对应 dispatcher。",
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
        help="持久写入 CODEX_RVF_CLINE_KANBAN_TMUX_SESSION；默认 rvf-cline-kanban。",
    )
    parser.add_argument(
        "--cline-kanban-base-ref",
        default=None,
        help="持久写入 CODEX_RVF_CLINE_KANBAN_BASE_REF；未提供时 Stop hook 使用当前 HEAD。",
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
    parser.add_argument(
        "--no-open-handoff",
        action="store_true",
        help="持久写入 CODEX_RVF_OPEN_HANDOFF=0，关闭 RVF 完成时自动打开 handoff.md。",
    )
    parser.add_argument(
        "--ide-open-cmd",
        default=None,
        help="持久写入 CODEX_RVF_IDE_OPEN_CMD；用于指定打开 handoff.md 的 coding agent IDE 命令。",
    )
    args = parser.parse_args()

    preserve = not args.replace_setup_config
    installed: list[str] = []

    try:
        parent = Path(args.plugin_parent).expanduser().resolve()
        dst = parent / PLUGIN_DIR_NAME
        copy_tree(PLUGIN_SRC, dst, PRESERVE_IN_PLUGIN, preserve)
        marketplace = update_marketplace(parent)
        plugin_config = ensure_codex_plugin_enabled()
        removed_legacy_skill = remove_legacy_codex_skill_dir(dst / PLUGIN_SKILL_REL, preserve)
        plugin_cache = sync_codex_plugin_cache(dst, preserve)
        removed_legacy_plugin_cache = remove_legacy_plugin_cache()
        installed.append(f"plugin: {dst}")
        installed.append(f"Codex plugin enabled: {plugin_config}")
        installed.append(f"plugin cache: {plugin_cache}")
        if removed_legacy_skill:
            installed.append(f"removed legacy Codex skill directory: {removed_legacy_skill}")
        if removed_legacy_plugin_cache:
            installed.append(f"removed legacy Codex plugin cache: {removed_legacy_plugin_cache}")
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
                args.cline_kanban_auto_review_enabled or os.environ.get("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_ENABLED"),
                args.cline_kanban_auto_review_mode or os.environ.get("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_MODE"),
                args.cline_kanban_start_in_plan_mode or os.environ.get("CODEX_RVF_CLINE_KANBAN_START_IN_PLAN_MODE"),
                not args.no_open_handoff
                and os.environ.get("CODEX_RVF_OPEN_HANDOFF", "").strip().lower()
                not in {"0", "false", "no", "n", "off", "disabled"},
                args.ide_open_cmd or os.environ.get("CODEX_RVF_IDE_OPEN_CMD"),
            )
            installed.append(f"stop hook: {hooks_path}")
    except Exception as exc:
        print(f"安装失败: {exc}", file=sys.stderr)
        return 2

    for item in installed:
        print(f"已安装 {item}")
    if preserve:
        print("已默认保留本机 setup 配置: alternative-reviewer.json 与 state/。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
