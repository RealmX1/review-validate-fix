#!/usr/bin/env python3
"""Claude Code hook 入口的单一 host-ownership 契约（S3 / handoff G）。

`stop.py` 与 `user_prompt_submit.py` 这两个 Claude 插件 hook 入口此前各自持有
一份**逐字复制**的 ``_is_codex_invocation`` 守卫 + 几乎相同的
stdin→normalize→subprocess→fail-open 骨架。本模块把它们收敛为单一来源：

- ``is_foreign_invocation``：本 Claude 入口**只拥有 Claude 调用**。背景：Codex
  plugin loader 把 plugin-packaged ``hooks/hooks.json`` 也当成 hooks 源加载
  （见 ``~/.codex/config.toml`` ``[hooks.state.
  review-validate-fix@local-codex-plugins:hooks/hooks.json:...]``），与
  ``~/.codex/hooks.json`` 里 ``install_to_codex.py`` 注册的 RVF entry 平行执行
  → 同一 Codex 事件触发两次 RVF。一旦有正向证据判定调用方是 Codex 就让本
  入口静默 no-op，由 Codex 端那个直调核心的 entry 独自处理 → **同一事件每
  host 恰好处理一次**。
- ``run_claude_hook``：两入口共享的转发流程，按 event 名 / core 脚本 / 超时
  环境变量 / 静默成功语义参数化（stop 在空输出时报「completed without
  payload」；UPS 静默成功，仅核心非零退出时报信）。

**刻意 stdlib-only、无 ``core`` / ``adapters`` 依赖**：hook 是最该 fail-open 的
安全面，不给它加 vendored import 失败模式。本模块作为 sibling 被两入口经
same-dir ``sys.path`` 自举 import（与 ``subagent_capture`` / ``rvf_analyze_thread``
的自举同款思路，但这里只 import 同目录文件、不触 ``_rvf_pyroot``）。

**未做（deferred，非本切片范围）**：彻底删运行期守卫的「config 级单次注册」
（让 Codex plugin loader 不注册 bundled ``hooks.json``，使本守卫可整体删除、
``rg is_foreign_invocation hooks/`` 命中 0）需在 live ``~/.codex`` 实测
plugin-loader 行为（plan Risk #14「先实测确认抑制 bundled 双源、再删守卫」），
且受「勿污染真实 ~/.codex」约束。故本切片只做守卫**单源化**（去掉两份复制），
零守卫的结构性消除留作后续。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


# plugin root：优先 Claude Code 注入的 ``CLAUDE_PLUGIN_ROOT``；回退到本模块
# 的父目录（``hooks/``）的上一级 = plugin payload 根（与两入口原计算一致，
# 本模块与 stop.py/ups.py 同在 ``hooks/`` 下，``parents[1]`` 同值）。
PLUGIN_ROOT = Path(
    os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parents[1])
).resolve()


def emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def is_foreign_invocation(event: dict[str, object]) -> bool:
    """判断本次 hook 是否被非 Claude（Codex）host 旁路触发（应当 no-op）。

    检测策略（保守，仅在正向证据时返回 True）：
    1. 事件里任一会话路径键（``transcript_path`` / ``conversation_path`` /
       ``session_path`` / ``session_file``）落在 ``/.codex/sessions/`` 下
       → Codex 转写文件路径，确诊 Codex。
    2. 兜底：未匹配则返回 False（按 Claude 跑，杜绝把 Claude 误判成 Codex
       而失声）。
    """
    for key in ("transcript_path", "conversation_path", "session_path", "session_file"):
        value = event.get(key)
        if isinstance(value, str) and "/.codex/sessions/" in value:
            return True
    return False


def run_claude_hook(
    *,
    event_name: str,
    core_script: tuple[str, ...],
    timeout_env: str,
    default_timeout: str,
    silent_success: bool,
) -> int:
    """读 Claude hook event（stdin），normalize 后转发给 plugin 内的 RVF 核心。

    - ``event_name``：Claude 事件名（``"Stop"`` / ``"UserPromptSubmit"``），用作
      ``hook_event_name`` 默认值与诊断消息标签（``Claude <event_name> hook``）。
    - ``core_script``：相对 ``PLUGIN_ROOT`` 的核心脚本路径分量元组。
    - ``timeout_env`` / ``default_timeout``：subprocess 超时秒数的 env 键与默认值。
    - ``silent_success``：True 时空输出 + 退出码 0 静默（不报信，让 prompt
      原样继续，仅核心非零退出且有 stderr 时报信）；False 时空输出总报
      「completed without payload」/ exit 码。

    Hooks 必须 fail open：任何失败都打印 ``{"continue": true, ...}`` 让用户的
    动作永不被阻塞。
    """
    label = f"Claude {event_name} hook"

    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        emit(
            {
                "continue": True,
                "systemMessage": f"review-validate-fix {label} skipped: invalid JSON input.",
            }
        )
        return 0

    if not isinstance(event, dict):
        event = {}

    if is_foreign_invocation(event):
        # 非 Claude host 通过各自 installer 注册的 entry 直调核心；这个
        # plugin-packaged shim 是 Claude Code 专用。静默退出防止同一事件
        # 触发两次 RVF。
        return 0

    event.setdefault("source", {"provider": "claude-code", "plugin": "review-validate-fix"})
    event.setdefault("hook_event_name", event_name)
    if not event.get("cwd"):
        event["cwd"] = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    env = os.environ.copy()
    env.setdefault("CODEX_RVF_CLINE_KANBAN_AGENT_ID", "claude")
    env.setdefault("CODEX_RVF_LOG_ROOT", str(Path.home() / ".claude" / "rvf"))
    env.setdefault("CODEX_RVF_DEV_SYNC", "0")

    core_path = PLUGIN_ROOT.joinpath(*core_script)
    try:
        completed = subprocess.run(
            [sys.executable, str(core_path)],
            input=json.dumps(event, ensure_ascii=False),
            capture_output=True,
            text=True,
            env=env,
            timeout=float(env.get(timeout_env, default_timeout)),
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - hooks must fail open.
        emit(
            {
                "continue": True,
                "systemMessage": (
                    f"review-validate-fix {label} failed before dispatch: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }
        )
        return 0

    if completed.stdout.strip():
        sys.stdout.write(completed.stdout)
        return 0

    if silent_success:
        # 静默成功是常态（无 dispatch token / marker / manual trigger）：成功
        # 时不输出，让 prompt 原样继续。仅在核心非零退出且有诊断 stderr
        # 时报信。
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()
            message = f"review-validate-fix {label} exited {completed.returncode}."
            if detail:
                message += f" stderr={detail[:500]}"
            emit({"continue": True, "systemMessage": message})
        return 0

    detail = (completed.stderr or "").strip()
    message = f"review-validate-fix {label} completed without payload."
    if completed.returncode != 0:
        message = f"review-validate-fix {label} exited {completed.returncode}."
    if detail:
        message += f" stderr={detail[:500]}"
    emit({"continue": True, "systemMessage": message})
    return 0
