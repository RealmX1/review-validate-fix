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
