#!/usr/bin/env python3
"""S2-invoke：headless 子代理调用向量的 host 分派 seam。

证明调用侧 host 特定的 argv 构造已下沉到 ``adapters/<host>/subagent.py`` 的
``build_analyze_command``、归一返回 ``core.subagents.InvokeCommand``，而「按 host
选哪个 adapter」的分派留在 skill facade ``rvf_analyze_thread.build_analyze_command``
——与观测侧 ``subagent_capture`` 的分派形态对称。同时锁定 facade 输出与重构前
**字节一致**（``(argv, uses_stdin)`` tuple，bin 经 facade 解析后透传 adapter）。
"""

from __future__ import annotations

from pathlib import Path

from _rvf_test_support.loader import load_script_module as _load

# rvf_analyze_thread 自举 SCRIPT_DIR 上 sys.path，故经 canonical loader 加载即可
# 触发其 `import _rvf_pyroot` → repo root 上 sys.path，使下方 adapters/core 可 import。
_load("rvf_analyze_thread")

from adapters.codex.subagent import build_analyze_command as codex_build_analyze_command
from adapters.claude_code.subagent import (
    build_analyze_command as claude_build_analyze_command,
)
from core.subagents import InvokeCommand
from core.subagents.models import InvokeCommand as InvokeCommandFromModels


# adapter 的当前硬编码向量（去掉 bin 首元素）。Claude 侧 permission-mode 故意是
# bypassPermissions——见 adapters/claude_code/subagent.py docstring 引用的线上证据
# （acceptEdits 在 headless 会让 Read/Bash 全被拒、agent 干净退 0 但零产出）。
_CODEX_TAIL = ["--ask-for-approval", "never", "--sandbox", "workspace-write", "exec", "-"]
_CLAUDE_TAIL = ["-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "bypassPermissions"]


def test_invoke_command_reexported_from_package() -> None:
    # __init__ re-export 与 models 定义为同一类，避免双定义漂移。
    assert InvokeCommand is InvokeCommandFromModels


def test_codex_adapter_builds_exec_command() -> None:
    cmd = codex_build_analyze_command(codex_bin="codex")
    assert isinstance(cmd, InvokeCommand)
    assert cmd.uses_stdin is True
    assert cmd.argv == ["codex", *_CODEX_TAIL]
    # 末元素 `-` = 从 stdin 读 prompt；workspace-write 允许 Edit 产物。
    assert cmd.argv[-1] == "-"
    assert "--sandbox" in cmd.argv and "workspace-write" in cmd.argv


def test_claude_adapter_builds_stream_json_command() -> None:
    cmd = claude_build_analyze_command(claude_bin="claude")
    assert isinstance(cmd, InvokeCommand)
    assert cmd.uses_stdin is True
    assert cmd.argv == ["claude", *_CLAUDE_TAIL]
    # analyze agent 必须能解析 $rvf-analyze slash command 并 Edit 文件。
    assert "--disable-slash-commands" not in cmd.argv
    assert "bypassPermissions" in cmd.argv
    # acceptEdits 是被禁掉的旧值，绝不能回退。
    assert "acceptEdits" not in cmd.argv


def test_adapters_honor_supplied_bin() -> None:
    # bin 由 facade 解析后传入；adapter 不耦合 CODEX_RVF_*_BIN env 约定。
    assert codex_build_analyze_command(codex_bin="/opt/codex").argv[0] == "/opt/codex"
    assert claude_build_analyze_command(claude_bin="/opt/claude").argv[0] == "/opt/claude"


def test_facade_dispatches_by_host_and_preserves_tuple_shape() -> None:
    module = _load("rvf_analyze_thread")
    codex_argv, codex_stdin = module.build_analyze_command(module.HOST_CODEX)
    claude_argv, claude_stdin = module.build_analyze_command(module.HOST_CLAUDE)
    # facade 仍返回 list（非 tuple），下游 launch_detached_analyze_thread 无需改动。
    assert isinstance(codex_argv, list) and isinstance(claude_argv, list)
    assert codex_stdin is True and claude_stdin is True
    assert codex_argv[1:] == _CODEX_TAIL
    assert claude_argv[1:] == _CLAUDE_TAIL
    # bin 首元素由 codex_bin()/claude_bin() 解析；用 Path(...).name 比对（与
    # test_review_support_scripts 的 gold-standard 同款），对 CODEX_RVF_*_BIN 设为
    # 绝对路径的环境稳健，不会假红。
    assert Path(codex_argv[0]).name == "codex"
    assert Path(claude_argv[0]).name == "claude"


def test_facade_unknown_host_falls_back_to_codex() -> None:
    module = _load("rvf_analyze_thread")
    fallback_argv, fallback_stdin = module.build_analyze_command("totally-unknown-host")
    codex_argv, _ = module.build_analyze_command(module.HOST_CODEX)
    assert fallback_argv == codex_argv
    assert fallback_stdin is True
