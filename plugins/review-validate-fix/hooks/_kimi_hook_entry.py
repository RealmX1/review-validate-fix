#!/usr/bin/env python3
"""Kimi Code CLI hook 入口的单一契约（事件归一 + 转发骨架）。

与 ``_claude_hook_entry`` 同纪律：**stdlib-only、不 import ``core`` / ``adapters``**。
hook 是最该 fail-open 的面，不给它加 vendored import 的失败模式。

## 为什么 Kimi 的 shim 比 Claude 的厚

Claude 的 shim 是纯转发；Kimi 的必须先把事件补全，否则 RVF 连「当前仓库是哪个」都不知道。
Kimi 传给 hook 的 Stop 事件**只有四个字段**（实测自二进制：``triggerInner`` 构造
``{hookEventName, sessionId, cwd, ...args.inputData}``，再经 ``toHookInputData`` 的
``camelToSnake`` 转成 snake_case；Stop 的 ``args.inputData`` 只有 ``stopHookActive``）：

```json
{"hook_event_name": "Stop", "session_id": "session_<uuid>", "cwd": "…", "stop_hook_active": false}
```

**没有 ``transcript_path``。** 而 ``detect_transcript_format`` 正是靠它判 host —— 缺了它，
RVF 会回落 Codex 兜底，于是给 Kimi 发 ``decision:block``，被 Kimi 的 ``looseObject``
静默吞掉 ⇒ **RVF 在 Kimi 上完全失声、零报错**。所以合成 ``transcript_path`` 不是锦上添花。

本 shim 做三件归一，每件都有独立的失败后果：

| 归一 | 来源 | 缺了会怎样 |
| --- | --- | --- |
| ``transcript_path`` | 会话名册定位 ``agents/main/wire.jsonl`` | host 判成 Codex ⇒ 静默失声（见上） |
| ``cwd`` | ``state.json`` 的 ``workDir`` | Kimi 的 ``cwd`` 可能是空串（``this.options.cwd ?? ""``）⇒ RVF 认不出目标仓库 |
| ``last_user_message`` | ``state.json`` 的 ``lastPrompt`` | 触发字面量检测拿不到用户这一轮说了什么 |

## 退出码恒 0 是硬要求，不是风格

Kimi 的 ``resultFromExitCode`` 只认两个码：``0``（解析 stdout）与 ``2``（block，reason 取
**stderr**）。**其他任意退出码都会让 stdout 被完全丢弃、结果 allow、且无任何报错。**
所以崩溃、超时、非法 JSON、核心脚本不存在——一律 return 0。

``exit 2`` 那条通道本仓刻意不用（理由见
``core/stop_hook_output_audience/stop_hook_output_audience_contract.py`` 的模块 docstring），
并由 gate ``AUD-6`` 结构性封死。

## 布局解析是 ``adapters/kimi/session_layout.py`` 的**副本**

与 ``_claude_hook_entry`` 自带一份 Codex 路径判据副本同理（那里注释也写着「两处改动必须
同步」）：本模块不能 import ``adapters``，故复刻最小必要的布局解析。
``tests/test_kimi_hook_entry_normalization.py`` 断言两处行为等价——改一处必须改另一处。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

KIMI_CODE_HOME_ENVIRONMENT_VARIABLE = "KIMI_CODE_HOME"
DEFAULT_KIMI_CODE_HOME_DIRECTORY_NAME = ".kimi-code"
SESSIONS_DIRECTORY_NAME = "sessions"
SESSION_INDEX_FILE_NAME = "session_index.jsonl"
SESSION_DIRECTORY_NAME_PREFIX = "session_"
WORKING_DIRECTORY_BUCKET_DIRECTORY_NAME_PREFIX = "wd_"
SESSION_STATE_FILE_NAME = "state.json"
AGENTS_DIRECTORY_NAME = "agents"
AGENT_WIRE_TRANSCRIPT_FILE_NAME = "wire.jsonl"
MAIN_AGENT_IDENTIFIER = "main"

KIMI_SOURCE_PROVIDER = "kimi-code"
CLINE_KANBAN_AGENT_IDENTIFIER = "kimi"


def emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


# ── 布局解析（adapters/kimi/session_layout.py 的 stdlib-only 副本）────────────


def resolved_kimi_code_home_directory(environment: dict | None = None) -> Path:
    env = os.environ if environment is None else environment
    override = (env.get(KIMI_CODE_HOME_ENVIRONMENT_VARIABLE) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / DEFAULT_KIMI_CODE_HOME_DIRECTORY_NAME


def normalized_session_directory_name(session_id: str) -> str:
    trimmed = (session_id or "").strip()
    if not trimmed:
        return ""
    if trimmed.startswith(SESSION_DIRECTORY_NAME_PREFIX):
        return trimmed
    return f"{SESSION_DIRECTORY_NAME_PREFIX}{trimmed}"


def resolve_session_directory(session_id: str, environment: dict | None = None) -> Path | None:
    """先查 ``session_index.jsonl``（快），未命中或指向不存在的路径就扫桶（准）。"""
    name = normalized_session_directory_name(session_id)
    if not name:
        return None
    home = resolved_kimi_code_home_directory(environment)
    try:
        raw = (home / SESSION_INDEX_FILE_NAME).read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if normalized_session_directory_name(str(entry.get("sessionId") or "")) != name:
            continue
        directory = entry.get("sessionDir")
        if directory and Path(str(directory)).is_dir():
            return Path(str(directory))
    sessions_root = home / SESSIONS_DIRECTORY_NAME
    if not sessions_root.is_dir():
        return None
    try:
        buckets = sorted(sessions_root.iterdir())
    except OSError:
        return None
    for bucket in buckets:
        if not bucket.is_dir():
            continue
        if not bucket.name.startswith(WORKING_DIRECTORY_BUCKET_DIRECTORY_NAME_PREFIX):
            continue
        candidate = bucket / name
        if candidate.is_dir():
            return candidate
    return None


def resolve_agent_wire_transcript_path(
    session_id: str, agent_id: str = MAIN_AGENT_IDENTIFIER, environment: dict | None = None
) -> Path | None:
    session_directory = resolve_session_directory(session_id, environment)
    if session_directory is None:
        return None
    candidate = (
        session_directory
        / AGENTS_DIRECTORY_NAME
        / (agent_id or MAIN_AGENT_IDENTIFIER)
        / AGENT_WIRE_TRANSCRIPT_FILE_NAME
    )
    return candidate if candidate.is_file() else None


def read_session_state(session_id: str, environment: dict | None = None) -> dict | None:
    session_directory = resolve_session_directory(session_id, environment)
    if session_directory is None:
        return None
    try:
        state = json.loads(
            (session_directory / SESSION_STATE_FILE_NAME).read_text(
                encoding="utf-8", errors="replace"
            )
        )
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


# ── 归属与归一 ────────────────────────────────────────────────────────────────


def owns_invocation(event: dict) -> bool:
    """本 shim 是否拥有这次调用（**正向**判据，不是排除法）。

    刻意不做 ``is_foreign_invocation`` 那种「排除 Codex」的守卫：那道守卫存在是因为
    Codex 的 plugin loader 会把 plugin-packaged ``hooks/hooks.json`` 也加载一遍，造成
    同一事件双触发。Kimi 走的是**它自己的** ``.kimi-plugin/plugin.json``，与 Claude /
    Codex 之间不存在同类双加载——给不存在的故障加守卫只会增加误判面。

    正向判据：事件带 Kimi 特征（有 ``session_id`` 且没有别家 host 的 stamp）。
    """
    if not isinstance(event, dict):
        return False
    source = event.get("source")
    if isinstance(source, dict):
        provider = source.get("provider")
        if isinstance(provider, str) and provider and provider != KIMI_SOURCE_PROVIDER:
            # 已被别的 host 入口打过标 ⇒ 不是我们的调用。
            return False
    return bool(str(event.get("session_id") or "").strip())


def normalize_kimi_event(event: dict, *, event_name: str, environment: dict | None = None) -> dict:
    """把 Kimi 的稀疏事件补成 RVF 核心能消费的形状。**任何一步失败都只是少补一个字段。**"""
    normalized = dict(event)
    normalized.setdefault("hook_event_name", event_name)
    normalized.setdefault("source", {"provider": KIMI_SOURCE_PROVIDER, "plugin": "review-validate-fix"})

    session_id = str(normalized.get("session_id") or "").strip()
    state: dict | None = None
    if session_id:
        if not normalized.get("transcript_path"):
            wire = resolve_agent_wire_transcript_path(session_id, environment=environment)
            if wire is not None:
                # 两个键都写：`transcript_path` 是 RVF 的主键，`session_file` 已在
                # SESSION_PATH_KEYS 里，双写让路径证据不依赖单一键名。
                normalized["transcript_path"] = str(wire)
                normalized.setdefault("session_file", str(wire))
        state = read_session_state(session_id, environment=environment)

    if not normalized.get("cwd"):
        work_dir = (state or {}).get("workDir")
        if isinstance(work_dir, str) and work_dir.strip():
            normalized["cwd"] = work_dir
        else:
            normalized["cwd"] = os.getcwd()

    if not normalized.get("last_user_message"):
        last_prompt = (state or {}).get("lastPrompt")
        if isinstance(last_prompt, str) and last_prompt.strip():
            normalized["last_user_message"] = last_prompt

    return normalized


def run_kimi_hook(
    *,
    event_name: str,
    core_script: tuple[str, ...],
    timeout_env: str,
    default_timeout: str,
    silent_success: bool,
) -> int:
    """读 Kimi hook event（stdin），归一后转发给 plugin 内的 RVF 核心。

    **恒返回 0。** 见模块 docstring「退出码恒 0 是硬要求」。
    """
    label = f"Kimi {event_name} hook"

    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        emit({"continue": True, "systemMessage": f"review-validate-fix {label} skipped: invalid JSON input."})
        return 0
    if not isinstance(event, dict):
        event = {}

    if not owns_invocation(event):
        return 0

    try:
        event = normalize_kimi_event(event, event_name=event_name)
    except Exception as exc:  # noqa: BLE001 - 归一失败也必须 fail open
        emit({"continue": True, "systemMessage": f"review-validate-fix {label} normalization failed: {type(exc).__name__}: {exc}"})
        return 0

    env = os.environ.copy()
    env.setdefault("CODEX_RVF_CLINE_KANBAN_AGENT_ID", CLINE_KANBAN_AGENT_IDENTIFIER)
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
        emit({"continue": True, "systemMessage": f"review-validate-fix {label} failed before dispatch: {type(exc).__name__}: {exc}"})
        return 0

    if completed.stdout.strip():
        sys.stdout.write(completed.stdout)
        return 0

    if silent_success:
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
