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
PLUGIN_SRC = ROOT / "plugins" / "review-validate-fix"
PLUGIN_SKILL_REL = Path("skills") / "review-validate-fix"
SKILL_NAME = "review-validate-fix"
PLUGIN_MANIFEST = PLUGIN_SRC / ".codex-plugin" / "plugin.json"

PRESERVE_IN_PLUGIN = {
    PLUGIN_SKILL_REL / "config" / "alternative-reviewer.json",
    PLUGIN_SKILL_REL / "state",
}
IGNORE_NAMES = {".DS_Store", "__pycache__", ".pytest_cache", ".mypy_cache", "state"}


def ignore(_: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORE_NAMES or name.endswith(".pyc")}


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


def remove_legacy_standalone_skill() -> Path | None:
    legacy = Path.home() / ".codex" / "skills" / SKILL_NAME
    if not legacy.exists() and not legacy.is_symlink():
        return None
    if legacy.is_dir() and not legacy.is_symlink():
        shutil.rmtree(legacy)
    else:
        legacy.unlink()
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
        return "local-codex-plugins"
    try:
        data = json.loads(marketplace_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "local-codex-plugins"
    name = data.get("name")
    return name if isinstance(name, str) and name.strip() else "local-codex-plugins"


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
        "name": SKILL_NAME,
        "source": {
            "source": "local",
            "path": f"./plugins/{SKILL_NAME}",
        },
        "policy": {
            "installation": "AVAILABLE",
            "authentication": "ON_INSTALL",
        },
        "category": "Coding",
    }
    for idx, plugin in enumerate(plugins):
        if plugin.get("name") == SKILL_NAME:
            plugins[idx] = entry
            break
    else:
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


def sync_codex_plugin_cache(preserve_local_config: bool) -> Path:
    cache_dir = (
        Path.home()
        / ".codex"
        / "plugins"
        / "cache"
        / marketplace_name()
        / SKILL_NAME
        / plugin_version()
    )
    copy_tree(PLUGIN_SRC, cache_dir, PRESERVE_IN_PLUGIN, preserve_local_config)
    return cache_dir


def normalize_fork_mode(value: str) -> str:
    mode = (value or "gui").strip()
    if mode in {"cline", "kanban", "ck"}:
        return "cline-kanban"
    return mode


def configure_stop_hook(
    plugin_skill_dir: Path,
    fork_mode: str = "gui",
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
    dispatcher = plugin_skill_dir / "scripts" / "codex_stop_hook_dispatcher.py"
    env_parts = [
        "CODEX_RVF_MODE=fork",
        f"CODEX_RVF_FORK_MODE={shlex.quote(fork_mode)}",
    ]
    if not open_handoff:
        env_parts.append("CODEX_RVF_OPEN_HANDOFF=0")
    ide_open_text = (ide_open_cmd or "").strip()
    if ide_open_text:
        env_parts.append(f"CODEX_RVF_IDE_OPEN_CMD={shlex.quote(ide_open_text)}")
    if fork_mode == "cline-kanban":
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
            f"python3 {shlex.quote(str(dispatcher))}",
        ]
    )
    command = " ".join(env_parts)
    entry = {
        "type": "command",
        "command": command,
        "timeout": 300,
        "statusMessage": "Review-Validate-Fix：同步插件并运行停止检查",
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
        help="plugin 父目录；默认符合 ~/.agents/plugins/marketplace.json 的 ./plugins/<name> 约定。",
    )
    parser.add_argument(
        "--replace-setup-config",
        action="store_true",
        help="覆盖本机 setup 相关配置；默认会保留 alternative-reviewer.json 和 state/。",
    )
    parser.add_argument(
        "--configure-stop-hook",
        action="store_true",
        help="更新 ~/.codex/hooks.json，让 Stop hook 先经 plugin 内 dispatcher 同步本 repo，再调用 plugin skill。",
    )
    parser.add_argument(
        "--fork-mode",
        choices=["gui", "cline-kanban", "cline", "kanban", "ck", "manual", "dry-run"],
        default="gui",
        help="与 --configure-stop-hook 配合写入 CODEX_RVF_FORK_MODE；默认 gui。",
    )
    parser.add_argument(
        "--cline-kanban-start-cmd",
        default=None,
        help="持久写入 CODEX_RVF_CLINE_KANBAN_START_CMD；默认 npx -y kanban@0.1.66 --no-open。",
    )
    parser.add_argument(
        "--cline-kanban-task-cmd",
        default=None,
        help="持久写入 CODEX_RVF_CLINE_KANBAN_TASK_CMD；默认 npx -y kanban@0.1.66 task。",
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
        dst = parent / SKILL_NAME
        removed_legacy = remove_legacy_standalone_skill()
        copy_tree(PLUGIN_SRC, dst, PRESERVE_IN_PLUGIN, preserve)
        marketplace = update_marketplace(parent)
        plugin_cache = sync_codex_plugin_cache(preserve)
        installed.append(f"plugin: {dst}")
        installed.append(f"plugin cache: {plugin_cache}")
        installed.append(f"marketplace: {marketplace}")

        if args.configure_stop_hook:
            hooks_path = configure_stop_hook(
                dst / PLUGIN_SKILL_REL,
                args.fork_mode,
                args.cline_kanban_start_cmd or os.environ.get("CODEX_RVF_CLINE_KANBAN_START_CMD"),
                args.cline_kanban_task_cmd or os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD"),
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
        if removed_legacy is not None:
            installed.append(f"removed deprecated standalone skill: {removed_legacy}")
    except Exception as exc:
        print(f"安装失败: {exc}", file=sys.stderr)
        return 2

    for item in installed:
        if item.startswith("removed "):
            print(f"已移除 {item.removeprefix('removed ')}")
        else:
            print(f"已安装 {item}")
    if preserve:
        print("已默认保留本机 setup 配置: alternative-reviewer.json 与 state/。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
