#!/usr/bin/env python3
"""行为 gate：Kimi hook 入口的事件归一与 fail-open 契约。

Kimi 传给 hook 的 Stop 事件**只有四个字段**（实测自二进制：``triggerInner`` 构造
``{hookEventName, sessionId, cwd, ...args.inputData}``，经 ``toHookInputData`` 的
``camelToSnake`` 转成 snake_case；Stop 的 ``args.inputData`` 只有 ``stopHookActive``，
且其触发分支的前置条件是 ``if (!stopHookContinuationUsed)`` ⇒ 该值恒 ``false``）：

```json
{"hook_event_name": "Stop", "session_id": "session_<uuid>", "cwd": "…", "stop_hook_active": false}
```

护的不变量：

* **ENTRY-1（合成 transcript_path）** —— 事件里**没有** ``transcript_path``，而
  ``detect_transcript_format`` 正是靠它判 host。缺了它 RVF 会兜底 Codex ⇒ 给 Kimi 发
  ``decision:block`` ⇒ 被 ``looseObject`` 静默吞掉 ⇒ **完全失声、零报错**。
* **ENTRY-2（恢复 cwd）** —— Kimi 的 ``cwd`` 可能是空串（``this.options.cwd ?? ""``），
  必须由 ``state.json`` 的 ``workDir`` 补。
* **ENTRY-3（注入 last_user_message）** —— 由 ``state.json`` 的 ``lastPrompt`` 补。
* **ENTRY-4（退出码恒 0）** —— Kimi 的 ``resultFromExitCode`` 只认 ``0`` 与 ``2``；其他任意
  码都会让 stdout 被完全丢弃并 allow 且无报错。崩溃 / 超时 / 非法 JSON / 核心不存在一律 0。
* **ENTRY-5（正向归属）** —— 用正向 ``owns_invocation`` 而不是排除式 foreign 守卫：那道守卫
  存在是因为 Codex 会双加载 plugin-packaged ``hooks.json``；Kimi 走自己的 manifest，不存在
  同类双加载，给不存在的故障加守卫只会增加误判面。
* **ENTRY-6（副本不漂移）** —— shim 内的布局解析是 ``adapters/kimi/session_layout.py`` 的
  stdlib-only 副本（hook 不能 import ``adapters``）。两处行为必须一致。
* **ENTRY-7（UPS 出口线材翻译）** —— core 的 UPS 出口是 host-blind 的 Claude 形状，而 Kimi
  二进制里 ``additionalContext`` 与 ``systemMessage`` **各 0 次命中**；UPS 又是唯一会把
  stdout **回落成正文注入**的事件（``userPromptHookMessage`` 在 ``message`` 缺席时 return
  整段 stdout），且注入与 TUI 渲染取的是**同一份** ``hookResult``。⇒ shim 必须在出口翻成
  Kimi 线材，developer-only 一律静默。2026-08-25T21:28:57Z 已真实泄漏过一次，取证见
  ``docs/architecture/kimi-code-cli-plugin-and-hook-runtime-forensics.md`` §12。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "plugins" / "review-validate-fix" / "hooks"
KIMI_ENTRY = HOOKS_DIR / "_kimi_hook_entry.py"
KIMI_STOP_SHIM = HOOKS_DIR / "kimi_stop.py"
KIMI_MANIFEST = ROOT / "plugins" / "review-validate-fix" / ".kimi-plugin" / "plugin.json"


def _load_entry():
    spec = importlib.util.spec_from_file_location("_kimi_hook_entry_under_test", KIMI_ENTRY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _build_session(root: Path, *, session_id: str = "session_abc", work_dir: str = "/some/repo",
                   last_prompt: str = "刚说的这句") -> Path:
    session_dir = root / "sessions" / "wd_proj_hash" / session_id
    wire = session_dir / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True, exist_ok=True)
    wire.write_text(json.dumps({"type": "metadata", "protocol_version": "1.4"}) + "\n",
                    encoding="utf-8")
    (session_dir / "state.json").write_text(
        json.dumps({"workDir": work_dir, "lastPrompt": last_prompt,
                    "agents": {"main": {"homedir": str(wire.parent), "type": "main",
                                        "parentAgentId": None}}}),
        encoding="utf-8",
    )
    return wire


# ── ENTRY-1 / 2 / 3：三项归一 ─────────────────────────────────────────────────


def test_normalization_synthesizes_transcript_path_and_restores_cwd_and_last_message(
    tmp_path: Path,
) -> None:
    entry = _load_entry()
    home = tmp_path / "kimi-home"
    wire = _build_session(home)
    env = {"KIMI_CODE_HOME": str(home)}

    normalized = entry.normalize_kimi_event(
        {"hook_event_name": "Stop", "session_id": "session_abc", "cwd": "",
         "stop_hook_active": False},
        event_name="Stop",
        environment=env,
    )

    assert normalized["transcript_path"] == str(wire), (
        "缺 transcript_path ⇒ host 判成 Codex ⇒ 发 decision:block ⇒ Kimi 静默 allow ⇒ 完全失声"
    )
    assert normalized["session_file"] == str(wire), "双写让路径证据不依赖单一键名"
    assert normalized["cwd"] == "/some/repo", "Kimi 的 cwd 可能是空串，必须由 state.json 的 workDir 补"
    assert normalized["last_user_message"] == "刚说的这句"
    assert normalized["source"]["provider"] == "kimi-code"
    assert normalized["hook_event_name"] == "Stop"


def test_normalization_never_overwrites_fields_the_event_already_carries(tmp_path: Path) -> None:
    entry = _load_entry()
    home = tmp_path / "kimi-home"
    _build_session(home)
    normalized = entry.normalize_kimi_event(
        {"hook_event_name": "Stop", "session_id": "session_abc", "cwd": "/explicit/cwd",
         "transcript_path": "/explicit/transcript.jsonl", "last_user_message": "显式给的"},
        event_name="Stop",
        environment={"KIMI_CODE_HOME": str(home)},
    )
    assert normalized["cwd"] == "/explicit/cwd"
    assert normalized["transcript_path"] == "/explicit/transcript.jsonl"
    assert normalized["last_user_message"] == "显式给的"


def test_normalization_degrades_gracefully_when_the_session_is_unknown(tmp_path: Path) -> None:
    """定位不到会话时只是少补字段，不抛、不造假路径。"""
    entry = _load_entry()
    normalized = entry.normalize_kimi_event(
        {"hook_event_name": "Stop", "session_id": "session_gone", "cwd": ""},
        event_name="Stop",
        environment={"KIMI_CODE_HOME": str(tmp_path / "empty-home")},
    )
    assert "transcript_path" not in normalized, "拿不到就不写——写一个不存在的路径比不写更糟"
    assert normalized["cwd"], "cwd 兜底到 os.getcwd()，不留空"
    assert "last_user_message" not in normalized


# ── ENTRY-5：正向归属 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("event", "owned"),
    [
        ({"session_id": "session_abc"}, True),
        ({"session_id": "session_abc", "source": {"provider": "kimi-code"}}, True),
        ({"session_id": "session_abc", "source": {"provider": "claude-code"}}, False),
        ({"session_id": ""}, False),
        ({}, False),
    ],
    ids=["bare", "own-stamp", "foreign-stamp", "empty-id", "no-id"],
)
def test_owns_invocation_is_a_positive_judgement(event: dict, owned: bool) -> None:
    assert _load_entry().owns_invocation(event) is owned


# ── ENTRY-4：退出码恒 0 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("unknown-session", '{"hook_event_name":"Stop","session_id":"session_nope","cwd":""}'),
        ("no-session-id", '{"hook_event_name":"Stop"}'),
        ("invalid-json", "not json at all"),
        ("empty-input", ""),
        ("non-dict-json", "[1,2,3]"),
    ],
)
def test_stop_shim_always_exits_zero(tmp_path: Path, label: str, payload: str) -> None:
    """非 0 非 2 的退出码会让 Kimi 丢弃 stdout 并 allow —— RVF 因此完全失声且零报错。"""
    completed = subprocess.run(
        [sys.executable, str(KIMI_STOP_SHIM)],
        input=payload, capture_output=True, text=True,
        env={**_isolated_env(tmp_path)},
    )
    assert completed.returncode == 0, (label, completed.returncode, completed.stderr[:400])


def test_stop_shim_exits_zero_even_when_the_core_script_is_missing(tmp_path: Path) -> None:
    """核心脚本不存在（部署残缺 / 版本目录被轮换掉）时也必须 exit 0。

    本仓吃过这个亏：一份死掉的 hook 注册让 Codex 每次 Stop 都 spawn 一个不存在的 stop.py，
    而 host 把那个 Errno 2 当成了驱动 agent 的指令，无上界续跑 286 次。
    """
    entry = _load_entry()
    rc_probe = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(HOOKS_DIR)!r});"
         "from _kimi_hook_entry import run_kimi_hook;"
         "sys.exit(run_kimi_hook(event_name='Stop',"
         "  core_script=('definitely','not','here.py'),"
         "  timeout_env='KIMI_RVF_STOP_HOOK_TIMEOUT', default_timeout='5',"
         "  silent_success=False))"],
        input='{"hook_event_name":"Stop","session_id":"session_x"}',
        capture_output=True, text=True, env=_isolated_env(tmp_path),
    )
    assert rc_probe.returncode == 0, (rc_probe.returncode, rc_probe.stderr[:400])
    assert entry is not None


def test_stop_shim_exits_zero_when_the_subprocess_itself_fails(tmp_path: Path) -> None:
    """覆盖 ``except Exception`` 那条 **spawn 失败** 分支（超时属于它）。

    ⚠️ 这条用例是先验真红时补上的：原本以为「核心脚本不存在」能覆盖它，实测不能——
    那种情形 ``subprocess.run`` **成功**起了解释器、由解释器自己报错返回非 0，走的是正常
    返回路径。于是把 ``except`` 分支的 ``return 0`` 改成 ``return 1`` 时整套测试仍然全绿，
    即那条分支当时是**零覆盖**的。超时是能真正让 ``subprocess.run`` 抛出来的路径。
    """
    env = _isolated_env(tmp_path)
    env["KIMI_RVF_STOP_HOOK_TIMEOUT"] = "0.001"
    home = tmp_path / "kimi-home"
    _build_session(home)
    env["KIMI_CODE_HOME"] = str(home)
    completed = subprocess.run(
        [sys.executable, str(KIMI_STOP_SHIM)],
        input='{"hook_event_name":"Stop","session_id":"session_abc"}',
        capture_output=True, text=True, env=env,
    )
    assert completed.returncode == 0, (completed.returncode, completed.stderr[:400])
    assert "failed before dispatch" in completed.stdout, completed.stdout
    assert '"continue": true' in completed.stdout, "spawn 失败必须 fail open"


def _isolated_env(tmp_path: Path) -> dict:
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith(("RVF_", "CODEX_RVF_", "KIMI_"))}
    env["KIMI_CODE_HOME"] = str(tmp_path / "isolated-kimi-home")
    env["RVF_HOME"] = str(tmp_path / "isolated-rvf-home")
    env["HOME"] = str(tmp_path / "isolated-home")
    return env


# ── ENTRY-6：shim 内副本与 session_layout.py 不漂移 ───────────────────────────


def test_layout_copy_in_the_shim_matches_session_layout(tmp_path: Path) -> None:
    """两处行为必须一致——shim 不能 import ``adapters``，所以只能靠 gate 钉住等价性。"""
    entry = _load_entry()
    home = tmp_path / "kimi-home"
    wire = _build_session(home)
    env = {"KIMI_CODE_HOME": str(home)}

    reference = json.loads(
        subprocess.run(
            [sys.executable, "-c",
             f"import json, sys; sys.path.insert(0, {str(ROOT)!r});"
             f"sys.path.insert(0, {str(ROOT / 'plugins' / 'review-validate-fix' / 'skills' / 'review-validate-fix' / 'scripts')!r});"
             "from adapters.kimi.session_layout import ("
             "  resolved_kimi_code_home_directory, normalized_session_directory_name,"
             "  resolve_session_directory, resolve_agent_wire_transcript_path, read_session_state)\n"
             f"env = {env!r}\n"
             "print(json.dumps({\n"
             "  'home': str(resolved_kimi_code_home_directory(env)),\n"
             "  'norm_bare': normalized_session_directory_name('abc'),\n"
             "  'norm_prefixed': normalized_session_directory_name('session_abc'),\n"
             "  'session_dir': str(resolve_session_directory('session_abc', env) or ''),\n"
             "  'session_dir_bare': str(resolve_session_directory('abc', env) or ''),\n"
             "  'wire': str(resolve_agent_wire_transcript_path('session_abc', 'main', env) or ''),\n"
             "  'missing': str(resolve_agent_wire_transcript_path('session_gone', 'main', env) or ''),\n"
             "  'state_work_dir': (read_session_state('session_abc', env) or {}).get('workDir'),\n"
             "}))"],
            capture_output=True, text=True, check=True,
        ).stdout
    )

    from_shim = {
        "home": str(entry.resolved_kimi_code_home_directory(env)),
        "norm_bare": entry.normalized_session_directory_name("abc"),
        "norm_prefixed": entry.normalized_session_directory_name("session_abc"),
        "session_dir": str(entry.resolve_session_directory("session_abc", env) or ""),
        "session_dir_bare": str(entry.resolve_session_directory("abc", env) or ""),
        "wire": str(entry.resolve_agent_wire_transcript_path("session_abc", "main", env) or ""),
        "missing": str(entry.resolve_agent_wire_transcript_path("session_gone", "main", env) or ""),
        "state_work_dir": (entry.read_session_state("session_abc", env) or {}).get("workDir"),
    }
    assert from_shim == reference, (
        f"shim 内的布局解析副本与 adapters/kimi/session_layout.py 已漂移：\n"
        f"  shim={from_shim}\n  layout={reference}"
    )
    assert from_shim["wire"] == str(wire)


# ── manifest 形状 ────────────────────────────────────────────────────────────


def test_manifest_hook_entries_match_the_strict_schema() -> None:
    """``HookDefSchema`` 是 ``.strict()``：多写一个字段 ⇒ 该 hook 被**静默丢弃**、
    ``state`` 仍报 ``ok``。plugin 看起来健康而 Stop hook 根本不存在——这是假绿。"""
    manifest = json.loads(KIMI_MANIFEST.read_text(encoding="utf-8"))
    allowed = {"event", "matcher", "command", "timeout"}
    events = set()
    for hook in manifest["hooks"]:
        extra = set(hook) - allowed
        assert not extra, f"多写的字段会让该 hook 被静默丢弃：{extra}"
        assert hook["command"].strip()
        assert 1 <= hook["timeout"] <= 600, hook
        events.add(hook["event"])
    assert {"Stop", "UserPromptSubmit"} <= events, events


def test_kimi_entries_never_leak_into_the_claude_hooks_json() -> None:
    """结构性护栏：Codex 的 plugin loader 会把 plugin-packaged ``hooks.json`` 也加载一遍，
    Kimi 条目混进去就是三重触发。"""
    raw = (HOOKS_DIR / "hooks.json").read_text(encoding="utf-8")
    assert "kimi" not in raw.lower(), "Kimi 的 hook 必须只走 .kimi-plugin/plugin.json"


# ── ENTRY-7：UPS 出口线材翻译 ────────────────────────────────────────────────
#
# core 的 rvf_user_prompt_submit 是完全 host-blind 的，发 Claude 形状的
# hookSpecificOutput.additionalContext + 顶层 systemMessage。而 Kimi 0.38.0 二进制里
# additionalContext 与 systemMessage **各 0 次命中**，UPS 又是唯一会把 stdout 回落成正文
# 注入的事件（userPromptHookMessage 在 message 缺席时 return 整段 stdout），产物同时进
# 模型上下文（appendUserMessage）与 TUI（emitEvent type:"hook.result"）。
# 2026-08-25T21:28:57Z 已在真实会话里把一条 developer-only 诊断连 JSON 转义一起注入过。


CORE_AUDIENCE_CONTRACT = (
    ROOT / "core" / "stop_hook_output_audience" / "stop_hook_output_audience_contract.py"
)


def test_additional_context_becomes_the_top_level_message_kimi_actually_reads() -> None:
    """``structuredOutput`` 读的是 ``message ?? hookSpecificOutput?.message``。

    发 ``additionalContext`` 等于什么都没发——它落在 ``looseObject`` 未命名区，
    然后整段 stdout 被当正文注入。
    """
    entry = _load_entry()
    wire = entry.translate_core_payload_to_kimi_user_prompt_submit_wire(
        {
            "continue": True,
            "systemMessage": "RVF: 开发者旁注，绝不能进模型上下文。",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "决策点正文：请显式 start 或 decline。",
            },
        }
    )
    assert wire == {"message": "决策点正文：请显式 start 或 decline。"}, wire
    assert "additionalContext" not in json.dumps(wire, ensure_ascii=False)
    assert "systemMessage" not in json.dumps(wire, ensure_ascii=False), (
        "developer-only 的 systemMessage 必须被丢掉——Kimi 的注入与渲染是同一份 hookResult，"
        "带上它就是把诊断逐字喂给模型。"
    )


def test_decision_block_becomes_hook_specific_output_permission_decision_deny() -> None:
    """Kimi 唯一的 block 判据是 ``hookSpecificOutput.permissionDecision === "deny"``。

    顶层 ``decision`` 从不被读取 ⇒ 撤回投递的「硬中和」在 Kimi 上原本必定失效。
    """
    entry = _load_entry()
    wire = entry.translate_core_payload_to_kimi_user_prompt_submit_wire(
        {
            "continue": True,
            "decision": "block",
            "reason": "RVF 已撤回该 dispatch token 对应的那轮 review。",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "完整禁令正文（reason 缺席时才用它）。",
            },
            "systemMessage": "RVF: 已中和。",
        }
    )
    assert wire == {
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": "RVF 已撤回该 dispatch token 对应的那轮 review。",
        }
    }, wire
    assert "decision" not in wire, "顶层 decision 是 Codex/Claude 线材，Kimi 不读"
    assert "hookEventName" not in wire["hookSpecificOutput"], (
        "Kimi 内层 schema 只命名 message / permissionDecision / permissionDecisionReason；"
        "未经实测的多余键不发（与 render_stop_hook_payload 的 Kimi 分支同纪律）。"
    )


def test_block_without_reason_falls_back_to_additional_context() -> None:
    """``reason`` → ``permissionDecisionReason`` 是 1:1 映射；缺席才退到 additionalContext。

    两者都空时返回 ``None``：发出去只会拿到 Kimi 的兜底文案
    ``"Blocked by UserPromptSubmit hook"``，那是纯噪声。
    """
    entry = _load_entry()
    fallback = entry.translate_core_payload_to_kimi_user_prompt_submit_wire(
        {
            "decision": "block",
            "hookSpecificOutput": {"additionalContext": "退到这条。"},
        }
    )
    assert fallback["hookSpecificOutput"]["permissionDecisionReason"] == "退到这条。"
    assert entry.translate_core_payload_to_kimi_user_prompt_submit_wire(
        {"decision": "block", "reason": "   "}
    ) is None


def test_developer_only_payload_translates_to_silence() -> None:
    """失败 / 降级 / skip / 诊断一律不得驱动 agent。

    Kimi 没有「开发者可见但不喂模型」的通道（``appendUserMessage`` 与 ``emitEvent`` 取的是
    **同一份** hookResult）⇒ developer-only 的唯一正确渲染就是静默。
    """
    entry = _load_entry()
    for payload in (
        {"continue": True, "systemMessage": "review-validate-fix Kimi UserPromptSubmit hook exited 1."},
        {"continue": True},
        {},
        None,
        "not a dict",
        {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "   "}},
    ):
        assert entry.translate_core_payload_to_kimi_user_prompt_submit_wire(payload) is None, payload


def test_developer_only_notice_is_silent_on_ups_but_still_emitted_on_stop(capsys) -> None:
    """同一条诊断在两个事件上的正确出口相反。

    Stop：Kimi 只读 block 判据、整段丢弃 allow-stdout ⇒ 发 ``systemMessage`` 无害。
    UPS：会被回落成正文注入 ⇒ 必须静默。
    """
    entry = _load_entry()

    entry.emit_developer_only_notice("boom", event_name="UserPromptSubmit")
    assert capsys.readouterr().out == "", "UPS 上的 developer-only 诊断必须一个字都不输出"

    entry.emit_developer_only_notice("boom", event_name="Stop")
    emitted = json.loads(capsys.readouterr().out)
    assert emitted == {"continue": True, "systemMessage": "boom"}, emitted


def test_deny_literal_is_the_same_in_the_shim_copy_and_the_core_contract() -> None:
    """``permissionDecision`` 是 JS 全等比较，拼错即**静默降级 allow**、零报错。

    shim 不能 import ``core``（stdlib-only 纪律），所以字面量各存一份；这条钉住两处不漂移。
    """
    entry = _load_entry()
    assert entry.KIMI_USER_PROMPT_SUBMIT_PERMISSION_DECISION_DENY == "deny"
    core_source = CORE_AUDIENCE_CONTRACT.read_text(encoding="utf-8")
    assert 'STOP_HOOK_PERMISSION_DECISION_DENY = "deny"' in core_source, (
        "core 侧的 deny 字面量已改动，shim 的副本必须同步。"
    )


def _run_hook_with_fake_core(
    entry, monkeypatch, *, event_name: str, core_stdout: str, returncode: int = 0
) -> str:
    """驱动 ``run_kimi_hook`` 的 stdout 分支，把核心脚本换成一份假回执。"""
    import io
    import types

    monkeypatch.setattr(
        entry,
        "subprocess",
        types.SimpleNamespace(
            run=lambda *a, **kw: types.SimpleNamespace(
                stdout=core_stdout, stderr="", returncode=returncode
            )
        ),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "hook_event_name": event_name, "session_id": "session_abc", "cwd": "/tmp",
    })))
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    rc = entry.run_kimi_hook(
        event_name=event_name,
        core_script=("skills", "review-validate-fix", "scripts", "whatever.py"),
        timeout_env="KIMI_RVF_TEST_TIMEOUT",
        default_timeout="5",
        silent_success=event_name == "UserPromptSubmit",
    )
    assert rc == 0, rc
    return captured.getvalue()


def test_ups_shim_emits_kimi_wire_and_never_the_raw_core_json(monkeypatch) -> None:
    """端到端：核心发 Claude 形状，shim 出口必须已是 Kimi 线材。"""
    entry = _load_entry()
    out = _run_hook_with_fake_core(
        entry, monkeypatch,
        event_name="UserPromptSubmit",
        core_stdout=json.dumps({
            "continue": True,
            "systemMessage": "=== RVF USER-PROMPT HOOK ===\n开发者旁注",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "决策点正文",
            },
        }, ensure_ascii=False),
    )
    assert json.loads(out) == {"message": "决策点正文"}, out
    assert "USER-PROMPT HOOK" not in out, (
        "这正是 2026-08-25 真实会话里发生过的泄漏形态：整份 JSON 被回落成正文注入模型。"
    )


def test_ups_shim_drops_non_json_core_stdout(monkeypatch) -> None:
    """核心的杂音（stray print / traceback）透传等于把它注入模型上下文。

    丢弃仍然 fail-open —— prompt 照常继续，只是没有注入。
    """
    entry = _load_entry()
    out = _run_hook_with_fake_core(
        entry, monkeypatch, event_name="UserPromptSubmit",
        core_stdout="Traceback (most recent call last):\n  RuntimeError: boom\n",
    )
    assert out == "", out


def test_stop_shim_still_passes_core_stdout_through_verbatim(monkeypatch) -> None:
    """回归护栏：Stop 侧 core 已由 ``render_stop_hook_payload`` 渲染好 Kimi 线材，
    shim **不得**再翻译一遍。"""
    entry = _load_entry()
    core_stdout = json.dumps({
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": "继续收尾当前 RVF。",
        },
        "systemMessage": "RVF: 刻意注入，非失败。",
    }, ensure_ascii=False)
    out = _run_hook_with_fake_core(
        entry, monkeypatch, event_name="Stop", core_stdout=core_stdout
    )
    assert out == core_stdout, out
