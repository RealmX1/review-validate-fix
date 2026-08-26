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

## 入口归一之外，本 shim 还负责**出口线材翻译**（仅 UserPromptSubmit）

Stop 侧 core 已经按 host 渲染好线材（``render_stop_hook_payload`` 的
``permissionDecision:"deny"`` 分支），shim 逐字透传即可。**UPS 侧没有这层**——
``rvf_user_prompt_submit`` 是完全 host-blind 的，发的是 Claude 形状的
``hookSpecificOutput.additionalContext``，而 Kimi 二进制里 ``additionalContext`` 与
``systemMessage`` **各 0 次命中**。旧行为因此落进 Kimi UPS 特有的「stdout 回落」，把整份 JSON
（含 developer-only 的 ``systemMessage``）逐字注入模型上下文——**已在真实会话里发生过**。

翻译由 :func:`translate_core_payload_to_kimi_user_prompt_submit_wire` 收口，
developer-only 诊断由 :func:`emit_developer_only_notice` 按事件分流。两者的完整取证见各自
docstring 与 ``docs/architecture/kimi-code-cli-plugin-and-hook-runtime-forensics.md`` §12。
两条分支共用同一个判据 ``KIMI_HOOK_EVENT_NAME_USER_PROMPT_SUBMIT``——**shim 必须 import 该
常量而不是另写字面量**，否则一次拼写分叉就同时静默关掉两者（见常量 docstring）。

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


# ── 出口线材翻译（UserPromptSubmit 专用）──────────────────────────────────────

KIMI_HOOK_EVENT_NAME_USER_PROMPT_SUBMIT = "UserPromptSubmit"
"""UserPromptSubmit 事件名在 Python 侧的**唯一**事实源——shim 必须 import 它，不得各写一份。

本模块有两条分支以「``event_name`` 是否**全等**本常量」为判据：出口线材翻译（见
:func:`run_kimi_hook` 的 stdout 分流）与 developer-only 诊断的静默（见
:func:`emit_developer_only_notice`）。调用方若写裸字面量，**一次拼写分叉就让两条分支同时
静默失效**、退回「把 core stdout 逐字透传」——而那正是 Kimi 在 UserPromptSubmit 上会
把整段 stdout 当正文注入模型上下文的那条路径（含只该给开发者看的 ``systemMessage``；
取证见 :func:`translate_core_payload_to_kimi_user_prompt_submit_wire`）。这条退化
**零报错**，且现有端到端用例都是把 ``event_name`` 当参数直接喂给 :func:`run_kimi_hook`，
从不读生产 shim 里的字面量 —— 全绿也照样发现不了。

``kimi_user_prompt_submit.py`` 传本常量之后，该判据对那个 shim **恒真**，字符串的具体
取值不再参与分支，分叉在结构上不可能发生。

第三份副本在 ``.kimi-plugin/plugin.json`` 的 ``hooks[].event``：它决定 Kimi 在哪个事件上
调起这个 shim。那份分叉的后果是**该 shim 拿不到 UserPromptSubmit 的调用**（RVF 在 UPS 上
完全不触发），而不是本文这条静默退化；由
``tests/test_kimi_hook_entry_normalization.py::test_manifest_hook_entries_match_the_strict_schema``
钉住。
"""

KIMI_USER_PROMPT_SUBMIT_PERMISSION_DECISION_DENY = "deny"
"""Kimi 的 block 判据逐字是 ``permissionDecision !== "deny"`` 才 allow —— **全等比较**。

与 ``core.stop_hook_output_audience.STOP_HOOK_PERMISSION_DECISION_DENY`` 刻意各存一份：
本模块 stdlib-only、不能 import ``core``（见模块 docstring）。
``tests/test_kimi_hook_entry_normalization.py`` 断言两处字面量相同——改一处必须改另一处。
"""


def translate_core_payload_to_kimi_user_prompt_submit_wire(
    payload: object,
) -> dict[str, object] | None:
    """把 core 的 **Claude 形状** UPS 出口翻成 Kimi 认的线材（``None`` = 什么都不发）。

    ## 为什么必须翻译（取证自 Kimi 0.38.0 二进制）

    core 的 ``rvf_user_prompt_submit._render_hook_payload`` 是 host-blind 的，产出的顶层键只有
    ``continue`` / ``systemMessage`` / ``hookSpecificOutput.{hookEventName,additionalContext}`` /
    ``decision``+``reason``。而 Kimi 认得的字段只有：

    ```js
    HookSpecificOutputSchema = looseObject({ message, permissionDecision, permissionDecisionReason });
    HookJsonOutputSchema     = looseObject({ message, hookSpecificOutput });
    ```

    ``additionalContext`` 与 ``systemMessage`` 在整个二进制里**各 0 次命中**。于是原先的逐字透传
    落进 ``looseObject`` 的未命名区，``structuredOutput`` 解析出的 ``message`` 为 ``undefined``，
    接着命中 UPS 特有的**回落**：

    ```js
    function userPromptHookMessage(result) {
      if (result.timedOut === true || (result.exitCode !== void 0 && result.exitCode !== 0)) return;
      const message = result.message?.trim();
      if (message !== void 0 && message.length > 0) return message;
      const stdout = result.stdout?.trim();          // ← 整段原始 stdout 当正文
      return stdout === void 0 || stdout.length === 0 ? void 0 : stdout;
    }
    ```

    产物经 ``<hook_result hook_event="UserPromptSubmit">…</hook_result>`` **同时**
    ``appendUserMessage`` 进模型上下文、并 ``emitEvent({type:"hook.result"})`` 让 TUI 渲染。
    ⇒ 旧行为等于把整份 JSON（含 developer-only 的 ``systemMessage``）连引号带 ``\\n`` 转义
    逐字喂给模型。**2026-08-25T21:28:57Z 已在真实会话里发生过一次**，实据见
    ``docs/architecture/kimi-code-cli-plugin-and-hook-runtime-forensics.md`` §12。

    ## 三条映射

    | core 发的 | Kimi 线材 | 理由 |
    | --- | --- | --- |
    | ``decision:"block"`` + ``reason`` | ``hookSpecificOutput.{permissionDecision:"deny", permissionDecisionReason}`` | Kimi 唯一的 block 判据；顶层 ``decision`` 从不被读取 |
    | ``hookSpecificOutput.additionalContext`` | 顶层 ``message`` | ``structuredOutput`` 读 ``message ?? hookSpecificOutput?.message`` |
    | 只有 ``systemMessage`` / ``continue`` | ``None``（静默） | developer-only 绝不驱动 agent；Kimi 没有「开发者可见但不喂模型」的通道 |

    block 优先于 additionalContext：撤回投递的硬中和路径（``_apply_withdrawn_dispatch_hard_neutralization``）
    两者都发，而 Kimi 一次只能走一条。``reason`` → ``permissionDecisionReason`` 是字段语义 1:1
    的映射；``reason`` 缺席时才退到 ``additionalContext``，不做拼接（未经实测的形状不发）。

    ⚠️ **刻意不发 ``hookEventName``**：Kimi 的内层 schema 只命名
    ``message`` / ``permissionDecision`` / ``permissionDecisionReason``。这与
    ``core.stop_hook_output_audience.render_stop_hook_payload`` 的 Kimi 分支同一条纪律。
    """
    if not isinstance(payload, dict):
        return None

    additional_context: str | None = None
    hook_specific_output = payload.get("hookSpecificOutput")
    if isinstance(hook_specific_output, dict):
        candidate = hook_specific_output.get("additionalContext")
        if isinstance(candidate, str) and candidate.strip():
            additional_context = candidate

    if payload.get("decision") == "block":
        reason = payload.get("reason")
        if not (isinstance(reason, str) and reason.strip()):
            reason = additional_context
        if not (isinstance(reason, str) and reason.strip()):
            # block 没有任何可投递的正文 ⇒ 发出去只会得到 Kimi 的兜底文案
            # "Blocked by UserPromptSubmit hook"，那是纯噪声。宁可不发。
            return None
        return {
            "hookSpecificOutput": {
                "permissionDecision": KIMI_USER_PROMPT_SUBMIT_PERMISSION_DECISION_DENY,
                "permissionDecisionReason": reason,
            }
        }

    if additional_context is not None:
        return {"message": additional_context}

    return None


def emit_developer_only_notice(message: str, *, event_name: str) -> None:
    """developer-only 诊断出口：Stop 上照发，**UserPromptSubmit 上一律静默**。

    Stop 侧 Kimi 只读 block 判据、整段丢弃 allow-stdout ⇒ 发 ``systemMessage`` 无害
    （``adapters/kimi/README.md`` 已记：RVF 自己的 ledger 是 Kimi 上唯一的开发者可见面）。

    UPS 侧则相反：它是 Kimi 唯一会把 hook stdout **回落成正文注入**的事件（见
    :func:`translate_core_payload_to_kimi_user_prompt_submit_wire` 的取证），所以在 UPS 上发一条
    只有 ``systemMessage`` 的诊断 = 把失败/降级信息逐字塞进模型上下文。而 Kimi 的注入与渲染是
    **同一份** ``hookResult``（``appendUserMessage`` 与 ``emitEvent`` 都取它），没有「只给开发者看」
    的分支 ⇒ 唯一正确的渲染就是静默。
    """
    if event_name == KIMI_HOOK_EVENT_NAME_USER_PROMPT_SUBMIT:
        return
    emit({"continue": True, "systemMessage": message})


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
        emit_developer_only_notice(
            f"review-validate-fix {label} skipped: invalid JSON input.", event_name=event_name
        )
        return 0
    if not isinstance(event, dict):
        event = {}

    if not owns_invocation(event):
        return 0

    try:
        event = normalize_kimi_event(event, event_name=event_name)
    except Exception as exc:  # noqa: BLE001 - 归一失败也必须 fail open
        emit_developer_only_notice(
            f"review-validate-fix {label} normalization failed: {type(exc).__name__}: {exc}",
            event_name=event_name,
        )
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
        emit_developer_only_notice(
            f"review-validate-fix {label} failed before dispatch: {type(exc).__name__}: {exc}",
            event_name=event_name,
        )
        return 0

    if completed.stdout.strip():
        if event_name == KIMI_HOOK_EVENT_NAME_USER_PROMPT_SUBMIT:
            # UPS 是唯一会把 stdout 回落成正文注入的事件 ⇒ 必须翻成 Kimi 认的线材，
            # 绝不逐字透传。非法 JSON 同样丢弃：那是 core 的杂音（stray print / traceback），
            # 透传等于把它注入模型上下文。丢弃仍然 fail-open——prompt 照常继续。
            try:
                core_payload = json.loads(completed.stdout)
            except json.JSONDecodeError:
                core_payload = None
            wire = translate_core_payload_to_kimi_user_prompt_submit_wire(core_payload)
            if wire is not None:
                emit(wire)
            return 0
        sys.stdout.write(completed.stdout)
        return 0

    if silent_success:
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()
            message = f"review-validate-fix {label} exited {completed.returncode}."
            if detail:
                message += f" stderr={detail[:500]}"
            emit_developer_only_notice(message, event_name=event_name)
        return 0

    detail = (completed.stderr or "").strip()
    message = f"review-validate-fix {label} completed without payload."
    if completed.returncode != 0:
        message = f"review-validate-fix {label} exited {completed.returncode}."
    if detail:
        message += f" stderr={detail[:500]}"
    emit_developer_only_notice(message, event_name=event_name)
    return 0
