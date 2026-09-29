"""``rvf`` 快捷入口 shim 的落点、内容与「该怎么调用它」的解析（host 中性叶子）。

为什么需要一个 PATH 上的单一入口：横幅、回执与 SKILL.md 过去各写各的——横幅渲染
``python3 <部署绝对路径>/rvf_agent_decision_cli.py``，回执却写一个 PATH 上根本不存在的
``rvf-agent``。agent 照抄回执 ⇒ command not found ⇒ 反复 ``--help`` 摸索，这是 RVF 执行段
请求数虚高的来源之一。

落点：部署（``scripts/rvf_local_deploy.py``）把 shim 写到 ``~/.rvf/bin/rvf``（RVF 自有目录），
再在 ``~/.local/bin/rvf`` 放一个指向它的符号链接（该目录在 Claude Code / Codex 的 shell PATH
上都常见）。渲染侧**不假设** PATH 一定可用：``which rvf`` 解析到的必须是本 shim（按所有权标记
核对，避免把别人的同名命令当成自己的），否则退到 shim 绝对路径，再退到入口脚本绝对路径。

本模块不 import host SDK、不 spawn 子进程，可被 hook 与 CLI 两侧无环 import。
"""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
from typing import Mapping

RVF_COMMAND_LINE_SHORTCUT_COMMAND_NAME = "rvf"
RVF_COMMAND_LINE_SHORTCUT_ENTRYPOINT_SCRIPT_FILENAME = "rvf_command_line_shortcut_entrypoint.py"
RVF_COMMAND_LINE_SHORTCUT_SHIM_OWNERSHIP_MARKER = (
    "RVF_COMMAND_LINE_SHORTCUT_SHIM_INSTALLED_BY_RVF_LOCAL_DEPLOY"
)
RVF_COMMAND_LINE_SHORTCUT_SHIM_DIRECTORY_RELATIVE_TO_HOME = Path(".rvf") / "bin"
RVF_COMMAND_LINE_SHORTCUT_USER_PATH_LINK_DIRECTORY_RELATIVE_TO_HOME = Path(".local") / "bin"
# shim 头部探测所有权标记时最多读这么多字节（shim 本身只有几百字节）。
_SHIM_OWNERSHIP_PROBE_MAX_BYTES = 4096


def rvf_command_line_shortcut_shim_path(home: Path | None = None) -> Path:
    base = home if home is not None else Path.home()
    return (
        base.expanduser()
        / RVF_COMMAND_LINE_SHORTCUT_SHIM_DIRECTORY_RELATIVE_TO_HOME
        / RVF_COMMAND_LINE_SHORTCUT_COMMAND_NAME
    )


def rvf_command_line_shortcut_user_path_link_path(home: Path | None = None) -> Path:
    base = home if home is not None else Path.home()
    return (
        base.expanduser()
        / RVF_COMMAND_LINE_SHORTCUT_USER_PATH_LINK_DIRECTORY_RELATIVE_TO_HOME
        / RVF_COMMAND_LINE_SHORTCUT_COMMAND_NAME
    )


def render_rvf_command_line_shortcut_shim_script(
    *, entrypoint_script_path: Path, python_executable: str
) -> str:
    return (
        "#!/bin/sh\n"
        f"# {RVF_COMMAND_LINE_SHORTCUT_SHIM_OWNERSHIP_MARKER}\n"
        "# 由 scripts/rvf_local_deploy.py 生成；每次部署整份覆盖，请勿手改。\n"
        f"exec {shlex.quote(python_executable)} {shlex.quote(str(entrypoint_script_path))} \"$@\"\n"
    )


def is_rvf_owned_command_line_shortcut_shim(path: Path) -> bool:
    try:
        with Path(path).open("rb") as handle:
            head = handle.read(_SHIM_OWNERSHIP_PROBE_MAX_BYTES)
    except OSError:
        return False
    return RVF_COMMAND_LINE_SHORTCUT_SHIM_OWNERSHIP_MARKER.encode("ascii") in head


def resolve_rvf_command_line_shortcut_invocation(
    *,
    fallback_entrypoint_script_path: Path,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> str:
    """渲染给 agent 的命令前缀：``rvf`` > shim 绝对路径 > ``python3 <入口脚本绝对路径>``。"""
    environment = environ if environ is not None else os.environ
    on_path = shutil.which(RVF_COMMAND_LINE_SHORTCUT_COMMAND_NAME, path=environment.get("PATH"))
    if on_path and is_rvf_owned_command_line_shortcut_shim(Path(on_path)):
        return RVF_COMMAND_LINE_SHORTCUT_COMMAND_NAME
    shim_path = rvf_command_line_shortcut_shim_path(home)
    if shim_path.is_file() and is_rvf_owned_command_line_shortcut_shim(shim_path):
        return shlex.quote(str(shim_path))
    return f"python3 {shlex.quote(str(Path(fallback_entrypoint_script_path).resolve()))}"


__all__ = [
    "RVF_COMMAND_LINE_SHORTCUT_COMMAND_NAME",
    "RVF_COMMAND_LINE_SHORTCUT_ENTRYPOINT_SCRIPT_FILENAME",
    "RVF_COMMAND_LINE_SHORTCUT_SHIM_OWNERSHIP_MARKER",
    "is_rvf_owned_command_line_shortcut_shim",
    "render_rvf_command_line_shortcut_shim_script",
    "resolve_rvf_command_line_shortcut_invocation",
    "rvf_command_line_shortcut_shim_path",
    "rvf_command_line_shortcut_user_path_link_path",
]
