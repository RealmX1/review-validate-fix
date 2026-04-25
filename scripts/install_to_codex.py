#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_SRC = ROOT / "skill" / "review-validate-fix"
PLUGIN_SRC = ROOT / "plugins" / "review-validate-fix"
SKILL_NAME = "review-validate-fix"

PRESERVE_IN_SKILL = {
    Path("config/alternative-reviewer.json"),
    Path("state"),
}
PRESERVE_IN_PLUGIN = {
    Path("skills/review-validate-fix/config/alternative-reviewer.json"),
    Path("skills/review-validate-fix/state"),
}
IGNORE_NAMES = {".DS_Store", "__pycache__", ".pytest_cache", ".mypy_cache", "state"}


def ignore(_: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORE_NAMES or name.endswith(".pyc")}


def is_preserved(path: Path, preserved: set[Path]) -> bool:
    for item in preserved:
        if path == item or item in path.parents:
            return True
    return False


def remove_unpreserved(dst: Path, preserved: set[Path]) -> None:
    if not dst.exists():
        return
    for child in sorted(dst.iterdir(), key=lambda p: len(p.parts), reverse=True):
        rel = child.relative_to(dst)
        if is_preserved(rel, preserved):
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def copy_tree(src: Path, dst: Path, preserved: set[Path], preserve_local_config: bool) -> None:
    if not src.exists():
        raise FileNotFoundError(src)
    dst.mkdir(parents=True, exist_ok=True)
    effective_preserve = preserved if preserve_local_config else set()
    remove_unpreserved(dst, effective_preserve)
    for child in src.iterdir():
        rel = child.relative_to(src)
        if is_preserved(rel, effective_preserve) and (dst / rel).exists():
            continue
        target = dst / rel
        if child.is_dir() and not child.is_symlink():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(child, target, ignore=ignore)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


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


def configure_stop_hook(skill_dir: Path) -> Path:
    hooks_path = Path.home() / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    if hooks_path.exists():
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    else:
        data = {"hooks": {}}

    hooks = data.setdefault("hooks", {})
    stop_groups = hooks.setdefault("Stop", [])
    command = (
        "CODEX_RVF_MODE=fork CODEX_RVF_FORK_MODE=gui python3 "
        f"{skill_dir / 'scripts' / 'codex_stop_review_validate_fix.py'}"
    )
    entry = {
        "type": "command",
        "command": command,
        "timeout": 30,
        "statusMessage": "Checking review-validate-fix gate",
    }

    replaced = False
    for group in stop_groups:
        group_hooks = group.setdefault("hooks", [])
        for index, existing in enumerate(group_hooks):
            existing_command = existing.get("command")
            if (
                isinstance(existing_command, str)
                and "codex_stop_review_validate_fix.py" in existing_command
            ):
                group_hooks[index] = entry
                replaced = True
                break
        if replaced:
            break

    if not replaced:
        stop_groups.append({"hooks": [entry]})

    hooks_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return hooks_path


def main() -> int:
    parser = argparse.ArgumentParser(description="把本仓库内容安装到 Codex skill/plugin 本机空间。")
    parser.add_argument("--as", dest="install_as", choices=["skill", "plugin", "both"], default="skill")
    parser.add_argument(
        "--skill-dir",
        default=str(Path.home() / ".codex" / "skills" / SKILL_NAME),
        help="skill 安装目标目录。",
    )
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
        help="更新 ~/.codex/hooks.json，让 Stop hook 以 Codex GUI/app-server fork 调用本 skill。",
    )
    args = parser.parse_args()

    preserve = not args.replace_setup_config
    installed: list[str] = []

    try:
        if args.install_as in {"skill", "both"}:
            dst = Path(args.skill_dir).expanduser().resolve()
            copy_tree(SKILL_SRC, dst, PRESERVE_IN_SKILL, preserve)
            installed.append(f"skill: {dst}")
            installed_skill_dir = dst
        else:
            installed_skill_dir = Path(args.skill_dir).expanduser().resolve()

        if args.install_as in {"plugin", "both"}:
            parent = Path(args.plugin_parent).expanduser().resolve()
            dst = parent / SKILL_NAME
            copy_tree(PLUGIN_SRC, dst, PRESERVE_IN_PLUGIN, preserve)
            marketplace = update_marketplace(parent)
            installed.append(f"plugin: {dst}")
            installed.append(f"marketplace: {marketplace}")

        if args.configure_stop_hook:
            hooks_path = configure_stop_hook(installed_skill_dir)
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
