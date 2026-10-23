#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import importlib.util
from pathlib import Path
from unittest.mock import patch
from _rvf_test_support.registry_completeness_guard import assert_every_defined_test_is_registered


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
    / "stop_hook_dispatcher.py"
)
ROUTER_SCRIPT = SCRIPT.with_name("stop_hook_router.py")


def load_dispatcher_module():
    spec = importlib.util.spec_from_file_location("rvf_stop_hook_dispatcher_for_tests", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run(cmd: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q"], cwd=path)
    return path


def write_fake_dev_scripts(repo: Path, marker: Path, *, fail_sync: bool = False) -> None:
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    contract_body = (
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker / 'sync-ran')!r}).write_text('sync\\n', encoding='utf-8')\n"
    )
    if fail_sync:
        contract_body += "sys.exit(7)\n"
    (scripts / "check_plugin_contracts.py").write_text(contract_body, encoding="utf-8")
    (scripts / "install_to_codex.py").write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker / 'install-ran')!r}).write_text("
        "'install ' + ' '.join(sys.argv[1:]) + '\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )


def write_timing_dev_scripts(repo: Path, marker: Path) -> None:
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "check_plugin_contracts.py").write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib\n"
        f"pathlib.Path({str(marker / 'sync-ran')!r}).write_text('sync\\n', encoding='utf-8')\n"
        "report_path = pathlib.Path(os.environ['RVF_CONTRACT_TIMING_REPORT'])\n"
        "report_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "report_path.write_text(json.dumps({\n"
        "    'version': 1,\n"
        "    'kind': 'plugin-contract-timing',\n"
        "    'duration_seconds': 2.5,\n"
        "    'measured_work_duration_seconds': 4.0,\n"
        "    'returncode': 0,\n"
        "    'groups': [{'name': 'tests', 'duration_seconds': 2.0}],\n"
        "    'slowest_step': {\n"
        "        'label': 'tests: stop_review_validate_fix',\n"
        "        'duration_seconds': 2.0,\n"
        "        'percentage_of_total': 80.0,\n"
        "    },\n"
        "}) + '\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (scripts / "install_to_codex.py").write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker / 'install-ran')!r}).write_text("
        "'install ' + ' '.join(sys.argv[1:]) + '\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )


def write_env_check_dev_scripts(repo: Path, marker: Path) -> None:
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    body = (
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        # RVF env 命名空间横跨 CODEX_RVF_ 与去-codex 后的裸 RVF_。继承自父会话的任一前缀
        # 变量都判脏；但 dispatcher 显式注入给 dev-sync 子进程的 RVF_ 变量是合法的
        # （如 contract-check 步骤的 RVF_CONTRACT_TIMING_REPORT），不算泄漏。
        "allowed = {'RVF_CONTRACT_TIMING_REPORT'}\n"
        "leaked = [k for k in os.environ if (k.startswith('CODEX_RVF_') or k.startswith('RVF_')) and k not in allowed]\n"
        "if leaked:\n"
        "    sys.stderr.write('leaked RVF env into dev-sync child: ' + ','.join(sorted(leaked)) + chr(10))\n"
        "    sys.exit(9)\n"
        f"pathlib.Path({str(marker)!r}).write_text('clean\\n', encoding='utf-8')\n"
    )
    (scripts / "check_plugin_contracts.py").write_text(body, encoding="utf-8")
    (scripts / "install_to_codex.py").write_text(body, encoding="utf-8")


def write_fake_installed_hook(path: Path, marker: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "raw = sys.stdin.read()\n"
        f"pathlib.Path({str(marker / 'hook-input.json')!r}).write_text(raw, encoding='utf-8')\n"
        "print(json.dumps({'continue': True, 'systemMessage': 'real hook ran'}))\n",
        encoding="utf-8",
    )


def write_fake_router_target(path: Path, marker: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "raw = sys.stdin.read()\n"
        f"pathlib.Path({str(marker / f'{label}-input.json')!r}).write_text(raw, encoding='utf-8')\n"
        f"pathlib.Path({str(marker / f'{label}-env.json')!r}).write_text(json.dumps({{'selected_channel': os.environ.get('CODEX_RVF_SELECTED_CHANNEL'), 'dev_sync': os.environ.get('CODEX_RVF_DEV_SYNC'), 'dev_sync_install': os.environ.get('CODEX_RVF_DEV_SYNC_INSTALL'), 'state_dir': os.environ.get('CODEX_RVF_SESSION_HOOK_STATE_DIR')}}), encoding='utf-8')\n"
        f"print(json.dumps({{'continue': True, 'systemMessage': '{label} hook ran'}}))\n",
        encoding="utf-8",
    )


def write_fake_notifier(path: Path, log: Path) -> Path:
    """假 terminal-notifier：把每次调用的 argv 以 JSON 行追加到 log。"""
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(log)!r}).open('a', encoding='utf-8')."
        "write(json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def write_fake_tmux(path: Path) -> Path:
    """假 tmux：记录 argv 到 FAKE_TMUX_CALLS，按 FAKE_TMUX_RETURNCODE 退出。

    不真正执行被包裹的 shell（不启动 analyze agent），让 detached 线程在测试里
    只走 launcher 的 prompt/status/lock 落盘，不产生真实后台进程。
    """
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "calls = os.environ.get('FAKE_TMUX_CALLS')\n"
        "if calls:\n"
        "    with open(calls, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "raise SystemExit(int(os.environ.get('FAKE_TMUX_RETURNCODE', '0')))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


# 进程级默认假 tmux：invoke_result()/invoke_router() 在测试未显式覆盖时用它。
_DEFAULT_FAKE_TMUX = write_fake_tmux(Path(tempfile.gettempdir()) / "rvf_dispatcher_default_fake_tmux.py")


def _write_noop_notifier(path: Path) -> Path:
    path.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# 进程级默认假 notifier：确保没有测试会在真机弹出真实 OS 通知。
_DEFAULT_FAKE_NOTIFIER = _write_noop_notifier(
    Path(tempfile.gettempdir()) / "rvf_dispatcher_default_fake_notifier.py"
)


def write_assistant_handoff_transcript(path: Path, handoff: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": f"完成。\nRVF_HANDOFF_FILE: {handoff}",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_assistant_plan_transcript(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": "<proposed_plan>\n# Plan\n</proposed_plan>",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_plan_then_normal_transcript(path: Path) -> Path:
    records = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": "<proposed_plan>\n# Plan\n</proposed_plan>",
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "execute it"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": "Implementation complete.",
            },
        },
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def write_codex_goal_transcript(
    path: Path,
    *,
    status: str | None = "active",
    originator: str | None = "codex-tui",
    cli_version: str | None = "0.130.0",
    subagent: bool = False,
    continuation: bool = True,
    user_mentions_continuation: bool = False,
) -> Path:
    meta: dict[str, object] = {"id": "codex-goal-session", "cwd": str(path.parent)}
    if originator is not None:
        meta["originator"] = originator
    if cli_version is not None:
        meta["cli_version"] = cli_version
    if subagent:
        meta["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": "parent-session"}}}

    records = [{"type": "session_meta", "payload": meta}]
    if continuation:
        records.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": "Continue working toward the active thread goal.\n\nObjective: test",
                },
            }
        )
    if user_mentions_continuation:
        records.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "Please test literal Continue working toward the active thread goal text.",
                },
            }
        )
    if status is not None:
        records.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "thread_goal_updated",
                    "thread_id": "codex-goal-session",
                    "turn_id": "turn",
                    "goal": {
                        "threadId": "codex-goal-session",
                        "objective": "test",
                        "status": status,
                        "tokensUsed": 0,
                    },
                },
            }
        )
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def write_failing_installed_hook(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('boom', file=sys.stderr)\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )


def write_slow_installed_hook(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import time\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )


def write_user_transcript(path: Path, repo: Path, session_id: str = "session") -> Path:
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": str(repo)},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "status only"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_apply_patch_transcript(path: Path, repo: Path, rel_path: str) -> Path:
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {rel_path}\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** End Patch\n"
    )
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "session-with-edit", "cwd": str(repo)},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "input": patch,
                    "call_id": "call_patch",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_timestamped_apply_patch_transcript(
    path: Path,
    repo: Path,
    rel_path: str,
    *,
    timestamp: str = "2020-01-01T00:00:00Z",
) -> Path:
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {rel_path}\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** End Patch\n"
    )
    records = [
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {"id": "session-with-edit", "cwd": str(repo)},
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": patch,
                "call_id": "call_patch",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def invoke_result(
    event: dict[str, object],
    *,
    dev_repo: Path | None,
    hook: Path,
    state: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("CODEX_RVF_") or key.startswith("RVF_") or key.startswith("KANBAN_") or key.startswith("CLINE_KANBAN_"):
            env.pop(key, None)
    if dev_repo is not None:
        env["CODEX_RVF_DEV_REPO"] = str(dev_repo)
    env["HOME"] = str(state / "home")
    env["CODEX_RVF_INSTALLED_STOP_HOOK"] = str(hook)
    env["CODEX_RVF_LOG_ROOT"] = str(state)
    # 见上：session-hook 只认 CODEX_RVF_STATE_DIR，与真实安装一致地两个都设。
    env["CODEX_RVF_STATE_DIR"] = str(state)
    if extra_env:
        env.update(extra_env)
    # 默认假 tmux：避免触发 handoff→advisory 的测试在后台真的拉起 analyze agent。
    env.setdefault("RVF_TMUX_BIN", str(_DEFAULT_FAKE_TMUX))
    env.setdefault("RVF_TERMINAL_NOTIFIER_BIN", str(_DEFAULT_FAKE_NOTIFIER))
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
    )


def invoke(
    event: dict[str, object],
    *,
    dev_repo: Path | None,
    hook: Path,
    state: Path,
    extra_env: dict[str, str] | None = None,
) -> str:
    completed = invoke_result(
        event,
        dev_repo=dev_repo,
        hook=hook,
        state=state,
        extra_env=extra_env,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def invoke_router(
    event: dict[str, object],
    *,
    stable_hook: Path,
    dev_hook: Path | None,
    state: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in (
        "CODEX_RVF_STABLE_STOP_HOOK",
        "CODEX_RVF_DEV_STOP_HOOK",
        "CODEX_RVF_DEV_REPO",
        "CODEX_RVF_SESSION_HOOK_STATE_DIR",
        "CODEX_RVF_STATE_DIR",
        "CODEX_RVF_LOG_ROOT",
        "CODEX_RVF_SELECTED_CHANNEL",
        "CODEX_RVF_DEV_SYNC",
        "CODEX_RVF_DEV_SYNC_INSTALL",
    ):
        env.pop(key, None)
    env["CODEX_RVF_STABLE_STOP_HOOK"] = str(stable_hook)
    if dev_hook is not None:
        env["CODEX_RVF_DEV_STOP_HOOK"] = str(dev_hook)
    env["CODEX_RVF_LOG_ROOT"] = str(state)
    # session-hook 状态目录只认 CODEX_RVF_STATE_DIR，**不会**回落到 LOG_ROOT
    # （回落会让 reviewer 子进程 source 过 review-env.sh 之后把会话状态写进
    # 某个 run 的日志目录）。真实安装（install_to_codex.py）两个都设，这里同理；
    # 本进程不改 HOME，漏设会让 router 写到真实的 ~/.rvf 下去。
    env["CODEX_RVF_STATE_DIR"] = str(state)
    if extra_env:
        env.update(extra_env)
    env.setdefault("RVF_TMUX_BIN", str(_DEFAULT_FAKE_TMUX))
    env.setdefault("RVF_TERMINAL_NOTIFIER_BIN", str(_DEFAULT_FAKE_NOTIFIER))
    return subprocess.run(
        [sys.executable, str(ROUTER_SCRIPT)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
    )


def invoke_router_stdout(
    event: dict[str, object],
    *,
    stable_hook: Path,
    dev_hook: Path | None,
    state: Path,
    extra_env: dict[str, str] | None = None,
) -> str:
    completed = invoke_router(
        event,
        stable_hook=stable_hook,
        dev_hook=dev_hook,
        state=state,
        extra_env=extra_env,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def latest_summary(state: Path) -> dict[str, object]:
    pointer = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    assert set(pointer) >= {"run_id", "summary_path", "events_path", "status", "reason_code"}
    assert Path(str(pointer["events_path"])).exists()
    return json.loads(Path(str(pointer["summary_path"])).read_text(encoding="utf-8"))


def test_router_defaults_to_dev_when_dev_terms_apply(tmp_path: Path) -> None:
    """dev terms 满足时 router 默认走 dev 通道。

    "dev terms 满足" = ``CODEX_RVF_DEV_STOP_HOOK`` 或 ``CODEX_RVF_DEV_REPO``
    解析出一个真实存在的 target file。这里 ``dev_hook`` 已经写入磁盘，
    ``invoke_router`` 也把 ``CODEX_RVF_DEV_STOP_HOOK`` 注入 env。
    """
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    stable_hook = tmp_path / "stable" / "hook.py"
    dev_hook = tmp_path / "dev" / "hook.py"
    write_fake_router_target(stable_hook, marker, "stable")
    write_fake_router_target(dev_hook, marker, "dev")

    stdout = invoke_router_stdout(
        {
            "cwd": str(repo),
            "session_id": "router-default-session",
            "last_user_message": "ordinary work in RVF repo",
        },
        stable_hook=stable_hook,
        dev_hook=dev_hook,
        state=tmp_path / "state",
        extra_env={"CODEX_RVF_DEV_REPO": str(repo)},
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "dev hook ran"
    assert (marker / "dev-input.json").exists()
    assert not (marker / "stable-input.json").exists()
    dev_env = json.loads((marker / "dev-env.json").read_text(encoding="utf-8"))
    assert dev_env["selected_channel"] == "dev"
    assert dev_env["dev_sync"] == "1"
    assert dev_env["dev_sync_install"] is None


def test_router_defaults_to_stable_when_dev_terms_do_not_apply(tmp_path: Path) -> None:
    """dev terms 不满足时 router 默认走 stable（``DEFAULT_CHANNEL`` fallback）。

    "dev terms 不满足" = 既没有 ``CODEX_RVF_DEV_STOP_HOOK``，也没有
    ``CODEX_RVF_DEV_REPO``（这里通过传 ``dev_hook=None`` 让
    ``invoke_router`` 不注入 ``CODEX_RVF_DEV_STOP_HOOK``）。
    """
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    stable_hook = tmp_path / "stable" / "hook.py"
    write_fake_router_target(stable_hook, marker, "stable")

    stdout = invoke_router_stdout(
        {
            "cwd": str(repo),
            "session_id": "router-stable-fallback",
            "last_user_message": "ordinary work in RVF repo",
        },
        stable_hook=stable_hook,
        dev_hook=None,
        state=tmp_path / "state",
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "stable hook ran"
    assert (marker / "stable-input.json").exists()
    assert not (marker / "dev-input.json").exists()
    stable_env = json.loads((marker / "stable-env.json").read_text(encoding="utf-8"))
    assert stable_env["selected_channel"] == "stable"
    assert stable_env["dev_sync"] == "0"
    assert stable_env["dev_sync_install"] is None


def test_router_channel_dev_marker_routes_current_and_later_stops(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    marker = tmp_path / "marker"
    marker.mkdir()
    stable_hook = tmp_path / "stable" / "hook.py"
    dev_hook = tmp_path / "dev" / "hook.py"
    write_fake_router_target(stable_hook, marker, "stable")
    write_fake_router_target(dev_hook, marker, "dev")
    state = tmp_path / "state"

    stdout = invoke_router_stdout(
        {
            "cwd": str(repo),
            "session_id": "router-dev-session",
            "last_user_message": "RVF_STOP_HOOK_CHANNEL: dev",
        },
        stable_hook=stable_hook,
        dev_hook=dev_hook,
        state=state,
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "dev hook ran"
    assert (marker / "dev-input.json").exists()
    session_state = json.loads(
        (state / "session-hook" / "router-dev-session.json").read_text(encoding="utf-8")
    )
    assert session_state["channel"] == "dev"
    dev_env = json.loads((marker / "dev-env.json").read_text(encoding="utf-8"))
    assert dev_env["selected_channel"] == "dev"
    assert dev_env["dev_sync"] == "1"
    assert dev_env["dev_sync_install"] is None

    (marker / "dev-input.json").unlink()
    stdout = invoke_router_stdout(
        {
            "cwd": str(repo),
            "session_id": "router-dev-session",
            "last_user_message": "ordinary later stop",
        },
        stable_hook=stable_hook,
        dev_hook=dev_hook,
        state=state,
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "dev hook ran"
    assert (marker / "dev-input.json").exists()


def test_router_channel_default_clears_session_marker(tmp_path: Path) -> None:
    """``default`` 指令清除 session marker，本次后续走 auto-resolved default。

    auto-resolved default 由 ``default_channel()`` 决定：dev terms 满足时
    走 dev，否则走 stable。本测试的 fixture 同时配置了 stable + dev hook
    （dev terms 满足）——所以 ``default`` 清除 marker 后 resolved channel
    应当是 dev，而非以前硬编码的 stable。
    """
    repo = init_repo(tmp_path / "repo")
    marker = tmp_path / "marker"
    marker.mkdir()
    stable_hook = tmp_path / "stable" / "hook.py"
    dev_hook = tmp_path / "dev" / "hook.py"
    write_fake_router_target(stable_hook, marker, "stable")
    write_fake_router_target(dev_hook, marker, "dev")
    state = tmp_path / "state"

    invoke_router_stdout(
        {
            "cwd": str(repo),
            "session_id": "router-default-clear",
            "last_user_message": "RVF_STOP_HOOK_CHANNEL: dev",
        },
        stable_hook=stable_hook,
        dev_hook=dev_hook,
        state=state,
    )
    stdout = invoke_router_stdout(
        {
            "cwd": str(repo),
            "session_id": "router-default-clear",
            "last_user_message": "RVF_STOP_HOOK_CHANNEL: default",
        },
        stable_hook=stable_hook,
        dev_hook=dev_hook,
        state=state,
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "dev hook ran"
    assert not (state / "session-hook" / "router-default-clear.json").exists()


def test_router_channel_status_reports_gate_and_channel(tmp_path: Path) -> None:
    state = tmp_path / "state"
    session_state = state / "session-hook" / "router-status.json"
    session_state.parent.mkdir(parents=True)
    session_state.write_text(
        json.dumps({"session_id": "router-status", "enabled": False, "channel": "dev"}) + "\n",
        encoding="utf-8",
    )

    stdout = invoke_router_stdout(
        {
            "cwd": str(tmp_path),
            "session_id": "router-status",
            "last_user_message": "RVF_STOP_HOOK_CHANNEL: status",
        },
        stable_hook=tmp_path / "stable" / "hook.py",
        dev_hook=tmp_path / "dev" / "hook.py",
        state=state,
    )

    payload = json.loads(stdout)
    assert "reason=session_hook_channel_status" in payload["systemMessage"]
    summary = latest_summary(state)
    assert summary["reason_code"] == "session_hook_channel_status"
    assert "channel 状态为 dev" in str(summary["message"])
    assert "gate=disabled" in str(summary["message"])
    assert summary["selected_channel"] == "dev"
    assert summary["session_hook_gate_state"] == "disabled"


def test_dev_repo_main_session_syncs_before_running_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    event = {
        "cwd": str(repo),
        "session_id": "parent-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
    }
    stdout = invoke(event, dev_repo=repo, hook=hook, state=tmp_path / "state")

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_handoff_marker_opens_before_dev_sync_or_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    handoff = tmp_path / "state" / "runs" / "rvf-child" / "artifacts" / "handoff.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("# handoff\n", encoding="utf-8")
    notifier_log = tmp_path / "notify.log"
    notifier = write_fake_notifier(tmp_path / "fake_notifier.py", notifier_log)

    event = {
        "cwd": str(repo),
        "session_id": "child-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
        "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
    }
    stdout = invoke(
        event,
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
        extra_env={"RVF_TERMINAL_NOTIFIER_BIN": str(notifier)},
    )

    payload = json.loads(stdout)
    assert "reason=handoff_file_ready" in payload["systemMessage"]
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()
    calls = [
        json.loads(line)
        for line in notifier_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(calls) == 1
    summary = latest_summary(tmp_path / "state")
    assert summary["handoff_path"] == str(handoff.resolve())


def _ensure_initial_commit(repo: Path) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if head.returncode == 0:
        return
    run(["git", "config", "user.email", "rvf@example.com"], cwd=repo)
    run(["git", "config", "user.name", "RVF"], cwd=repo)
    seed = repo / ".rvf-seed"
    seed.write_text("seed\n", encoding="utf-8")
    run(["git", "add", str(seed)], cwd=repo)
    run(["git", "commit", "-q", "-m", "seed"], cwd=repo)


def _seed_finalize_run_dir(
    *,
    state: Path,
    repo: Path,
    run_id: str = "rvf-child",
) -> tuple[Path, Path]:
    _ensure_initial_commit(repo)
    run_dir = state / "runs" / run_id
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    handoff = artifacts / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "started",
                "reason_code": "test",
                "repo": str(repo),
                "events_path": str(run_dir / "events.jsonl"),
                "artifacts_dir": str(artifacts),
                "run_dir": str(run_dir),
            }
        ),
        encoding="utf-8",
    )
    snapshot_module = load_workspace_snapshot_module()
    (artifacts / "before-workspace-snapshot.json").write_text(
        json.dumps(snapshot_module.capture(repo), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_dir, handoff


def load_workspace_snapshot_module():
    import importlib.util as _il

    script = (
        Path(__file__).resolve().parents[1]
        / "plugins"
        / "review-validate-fix"
        / "skills"
        / "review-validate-fix"
        / "scripts"
        / "workspace_snapshot.py"
    )
    spec = _il.spec_from_file_location("rvf_workspace_snapshot_for_tests", script)
    assert spec is not None and spec.loader is not None
    module = _il.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_same_session_transcript_with_marker(path: Path, repo: Path) -> Path:
    records = [
        {"type": "session_meta", "payload": {"id": "child-session", "cwd": str(repo)}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "background work"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "ack"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "go $review-validate-fix",
            },
        },
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "running rvf"}},
    ]
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


def _seed_bound_automatic_closeout_receipt_and_synthetic_registration(run_dir: Path, repo: Path, rvf_home: Path) -> dict[str, str]:
    from _rvf_test_support.loader import load_script_module

    receipt = {
        "decision_token": "0123456789abcdef",
        "session_id": "child-session",
        "worktree": str(repo.resolve()),
    }
    # 完成通知消费正式开审时的不可变绑定；夹具也经真实 bridge 建立该生命周期前提。
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    with patch.dict(os.environ, {"RVF_HOME": str(rvf_home)}):
        load_script_module("rvf_automatic_closeout_bridge").bind_run(
            receipt, run_dir, run_id=summary["run_id"],
        )
    # 此成功夹具显式声明合成注册事实；缺记录不得获得自动通知资格。
    (run_dir / "artifacts" / "automatic-closeout-registration.json").write_text(json.dumps({
        "run_id": summary["run_id"], "decision_token": receipt["decision_token"], "status": "registered",
    }), encoding="utf-8")
    return receipt


def test_handoff_marker_finalizes_run_artifacts_same_session(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    state = tmp_path / "state"
    run_dir, handoff = _seed_finalize_run_dir(state=state, repo=repo)
    rvf_home = tmp_path / "rvf-home"
    receipt = _seed_bound_automatic_closeout_receipt_and_synthetic_registration(run_dir, repo, rvf_home)
    transcript = _write_same_session_transcript_with_marker(
        tmp_path / "rollout.jsonl",
        repo,
    )
    # mutate workspace so workspace_diff has a real change
    (repo / ".rvf-seed").write_text("seed\nchanged\n", encoding="utf-8")
    fake_tmux = write_fake_tmux(tmp_path / "fake_tmux.py")
    tmux_calls = tmp_path / "tmux-calls.jsonl"

    event = {
        "cwd": str(repo),
        "session_id": "child-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
        "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
    }
    stdout = invoke(
        event,
        dev_repo=repo,
        hook=hook,
        state=state,
        extra_env={
            "RVF_HOME": str(rvf_home),
            "RVF_TMUX_BIN": str(fake_tmux),
            "FAKE_TMUX_CALLS": str(tmux_calls),
        },
    )

    # finalize artifacts in the seeded run_dir
    lock = run_dir / "artifacts" / ".finalize.lock"
    assert lock.exists(), "finalize lock should be written"
    traj_dir = run_dir / "artifacts" / "trajectory"
    pre = traj_dir / "pre-rvf" / "rollout.jsonl"
    post = traj_dir / "rvf" / "rollout.jsonl"
    assert pre.exists() and post.exists()
    assert pre.read_bytes() + post.read_bytes() == transcript.read_bytes()
    distilled = traj_dir / "rvf" / "trajectory.jsonl"
    assert distilled.exists()
    diff_json = run_dir / "artifacts" / "workspace-diff.json"
    assert diff_json.exists()
    payload = json.loads(diff_json.read_text(encoding="utf-8"))
    assert payload["status"] == "complete"
    paths = {item["path"]: item["op"] for item in payload["changed_paths"]}
    assert paths.get(".rvf-seed") == "modified"
    summary_payload = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    finalized = json.loads(lock.read_text(encoding="utf-8"))
    assert finalized == summary_payload["finalize"]
    assert finalized["decision_kind"] == "handoff"
    assert finalized["errors"] == []
    assert finalized["completed_at"]
    notification_path = rvf_home / "automatic-closeout-completion-notifications" / f"{receipt['decision_token']}.json"
    completion = json.loads(notification_path.read_text(encoding="utf-8"))
    assert completion == {
        "schema": "rvf-automatic-closeout-completion/v1",
        **receipt,
        "run_id": run_dir.name,
        "run_dir": str(run_dir.resolve()),
        "handoff_path": str(handoff.resolve()),
        "handoff_sha256": hashlib.sha256(handoff.read_bytes()).hexdigest(),
    }
    payload = json.loads(stdout)
    assert "rvf_analyze=thread_launched" in payload["systemMessage"]
    hook_summary = latest_summary(state)
    assert hook_summary["rvf_analyze_status"] == "thread-launched"
    assert hook_summary["rvf_analyze_thread_launch_status"] == "launched"
    assert hook_summary["rvf_analyze_run_dir"] == str(run_dir.resolve())
    # 假 tmux 收到 detached new-session 调用。
    call = json.loads(tmux_calls.read_text(encoding="utf-8").splitlines()[0])
    assert call["argv"][:3] == ["new-session", "-d", "-s"]


def test_handoff_marker_surfaces_finalize_record_errors(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    state = tmp_path / "state"
    run_dir, handoff = _seed_finalize_run_dir(state=state, repo=repo)
    rvf_home = tmp_path / "rvf-home"
    _seed_bound_automatic_closeout_receipt_and_synthetic_registration(run_dir, repo, rvf_home)
    transcript = _write_same_session_transcript_with_marker(
        tmp_path / "rollout.jsonl",
        repo,
    )
    analysis_path = run_dir / "artifacts" / "analysis"
    analysis_path.write_text("blocks analysis scaffold directory\n", encoding="utf-8")

    event = {
        "cwd": str(repo),
        "session_id": "child-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
        "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
    }
    stdout = invoke(
        event,
        dev_repo=repo,
        hook=hook,
        state=state,
        extra_env={"RVF_HOME": str(rvf_home)},
    )

    payload = json.loads(stdout)
    assert "finalize_errors=1" in payload["systemMessage"]
    hook_summary = latest_summary(state)
    assert hook_summary["finalize_status"] == "warning"
    assert hook_summary["finalize_error_count"] == 1
    assert hook_summary["finalized_run_dir"] == str(run_dir.resolve())
    assert hook_summary["finalize_errors"][0]["stage"] == "analysis_scaffold"
    run_summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert run_summary["finalize"]["errors"][0]["stage"] == "analysis_scaffold"
    assert not (rvf_home / "automatic-closeout-completion-notifications").exists()
    events = [
        json.loads(line)
        for line in Path(str(hook_summary["events_path"])).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(event["event"] == "finalize_completed_with_errors" for event in events)


def test_handoff_marker_finalizes_run_artifacts_forked_session(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    state = tmp_path / "state"
    run_dir, handoff = _seed_finalize_run_dir(
        state=state, repo=repo, run_id="rvf-forked"
    )
    parent_transcript = tmp_path / "parent.jsonl"
    parent_transcript.write_text(
        json.dumps(
            {"type": "event_msg", "payload": {"type": "user_message", "message": "parent context"}}
        )
        + "\n",
        encoding="utf-8",
    )
    child_transcript = tmp_path / "child.jsonl"
    child_transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "forked-child"}}),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "go $review-validate-fix",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "artifacts" / "origin.json").write_text(
        json.dumps(
            {
                "session_id": "parent-session",
                "transcript_path": str(parent_transcript),
            }
        ),
        encoding="utf-8",
    )

    event = {
        "cwd": str(repo),
        "session_id": "forked-child",
        "turn_id": "turn",
        "hook_event_name": "Stop",
        "transcript_path": str(child_transcript),
        "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
    }
    invoke(
        event,
        dev_repo=repo,
        hook=hook,
        state=state,
    )

    pre = run_dir / "artifacts" / "trajectory" / "pre-rvf" / "rollout.jsonl"
    post = run_dir / "artifacts" / "trajectory" / "rvf" / "rollout.jsonl"
    assert pre.read_bytes() == parent_transcript.read_bytes()
    assert post.read_bytes() == child_transcript.read_bytes()
    pre_manifest = json.loads(
        (run_dir / "artifacts" / "trajectory" / "pre-rvf" / "manifest.json").read_text(encoding="utf-8")
    )
    assert pre_manifest["source_kind"] == "forked-source-full"


def test_rvf_analyze_followup_trigger_skips_dispatcher_sync(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    stdout = invoke(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "last_user_message": "$rvf-analyze /tmp/run\n\nRVF_KANBAN_ANALYZE_TRIGGER",
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
    )

    payload = json.loads(stdout)
    assert "reason=rvf_analyze_followup_trigger_turn" in payload["systemMessage"]
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()


def test_plan_operation_skips_before_dev_sync_or_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    transcript = write_assistant_plan_transcript(tmp_path / "session.jsonl")
    state = tmp_path / "state"

    completed = invoke_result(
        {
            "cwd": str(repo),
            "session_id": "parent-session",
            "turn_id": "turn",
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "last_assistant_message": "<proposed_plan>\n# Plan\n</proposed_plan>",
        },
        dev_repo=repo,
        hook=hook,
        state=state,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=plan_operation" in payload["systemMessage"]
    assert completed.stderr == ""
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()
    summary = latest_summary(state)
    assert summary["status"] == "skipped"
    assert summary["reason_code"] == "plan_operation"


def test_codex_goal_mode_skips_before_dev_sync_or_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    transcript = write_codex_goal_transcript(tmp_path / "session.jsonl", status="active")
    state = tmp_path / "state"

    completed = invoke_result(
        {
            "cwd": str(repo),
            "session_id": "codex-goal-session",
            "turn_id": "turn",
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "last_assistant_message": "Implementation complete.",
        },
        dev_repo=repo,
        hook=hook,
        state=state,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=codex_goal_mode" in payload["systemMessage"]
    assert completed.stderr == ""
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()
    summary = latest_summary(state)
    assert summary["status"] == "skipped"
    assert summary["reason_code"] == "codex_goal_mode"
    assert summary["goal_status"] == "active"
    assert summary["temporary_fix"] is True


def test_non_codex_goal_like_transcript_runs_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    transcript = write_codex_goal_transcript(
        tmp_path / "session.jsonl",
        originator="Claude Code",
        cli_version=None,
        status="active",
    )
    event = {
        "cwd": str(repo),
        "session_id": "not-codex-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
    }

    stdout = invoke(event, dev_repo=None, hook=hook, state=tmp_path / "state")

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_codex_goal_mode_subagent_runs_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    transcript = write_codex_goal_transcript(tmp_path / "session.jsonl", status="active", subagent=True)
    event = {
        "cwd": str(repo),
        "session_id": "codex-goal-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
    }

    stdout = invoke(event, dev_repo=None, hook=hook, state=tmp_path / "state")

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_codex_user_text_goal_marker_without_status_runs_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    transcript = write_codex_goal_transcript(
        tmp_path / "session.jsonl",
        status=None,
        continuation=False,
        user_mentions_continuation=True,
    )
    event = {
        "cwd": str(repo),
        "session_id": "codex-goal-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
    }

    stdout = invoke(event, dev_repo=None, hook=hook, state=tmp_path / "state")

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_codex_completed_goal_runs_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    transcript = write_codex_goal_transcript(tmp_path / "session.jsonl", status="complete")
    event = {
        "cwd": str(repo),
        "session_id": "codex-goal-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
    }

    stdout = invoke(event, dev_repo=None, hook=hook, state=tmp_path / "state")

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_literal_plan_markers_in_completion_do_not_skip_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    event = {
        "cwd": str(repo),
        "session_id": "parent-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
        "last_assistant_message": (
            "Implementation complete. Documented literal markers "
            "<proposed_plan> and </proposed_plan> for regression coverage."
        ),
    }
    stdout = invoke(event, dev_repo=repo, hook=hook, state=tmp_path / "state")

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_prior_plan_output_does_not_suppress_future_turn(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    transcript = write_plan_then_normal_transcript(tmp_path / "session.jsonl")

    event = {
        "cwd": str(repo),
        "session_id": "parent-session",
        "turn_id": "turn-after-plan",
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
        "last_assistant_message": "Implementation complete.",
        "mode": "plan",
        "source": {"agent_mode": "plan"},
    }
    stdout = invoke(
        event,
        dev_repo=None,
        hook=hook,
        state=tmp_path / "state",
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_session_hook_off_still_syncs_before_running_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    event = {
        "cwd": str(repo),
        "session_id": "parent-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
        "last_user_message": "RVF_STOP_HOOK: off",
    }
    stdout = invoke(event, dev_repo=repo, hook=hook, state=tmp_path / "state")

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_dev_channel_sync_never_installs_plugin(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    event = {
        "cwd": str(repo),
        "session_id": "parent-session",
        "turn_id": "turn",
        "hook_event_name": "Stop",
    }
    stdout = invoke(event, dev_repo=repo, hook=hook, state=tmp_path / "state")

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_non_matching_repo_runs_installed_hook_without_sync(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    other = init_repo(tmp_path / "other")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    stdout = invoke(
        {"cwd": str(other), "hook_event_name": "Stop"},
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert (marker / "hook-input.json").exists()


def test_subagent_stop_runs_installed_hook_without_sync(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    transcript = tmp_path / "subagent.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    stdout = invoke(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()


def test_suppress_env_skips_before_sync_and_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    completed = invoke_result(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "session_id": "headless-child",
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
        extra_env={"RVF_SUPPRESS_STOP_HOOK": "1"},
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=suppressed" in payload["systemMessage"]
    assert "summary=" in payload["systemMessage"]
    summary = latest_summary(tmp_path / "state")
    assert summary["status"] == "skipped"
    assert summary["reason_code"] == "suppressed"
    assert completed.stderr == ""
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()


def test_suppress_env_skips_handoff_marker_before_opening(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    notifier_log = tmp_path / "notify.log"
    notifier = write_fake_notifier(tmp_path / "fake_notifier.py", notifier_log)

    completed = invoke_result(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "session_id": "headless-child",
            "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
        extra_env={
            "RVF_SUPPRESS_STOP_HOOK": "1",
            "RVF_TERMINAL_NOTIFIER_BIN": str(notifier),
        },
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=suppressed" in payload["systemMessage"]
    assert "summary=" in payload["systemMessage"]
    summary = latest_summary(tmp_path / "state")
    assert summary["status"] == "skipped"
    assert summary["reason_code"] == "suppressed"
    assert not notifier_log.exists()
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()


def test_sync_failure_skips_installed_hook_to_avoid_stale_fork(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker, fail_sync=True)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    state = tmp_path / "state"

    completed = invoke_result(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=repo,
        hook=hook,
        state=state,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=sync_command_failed" in payload["systemMessage"]
    assert completed.stderr == ""
    assert (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()
    summary = latest_summary(state)
    assert summary["status"] == "failed"
    assert summary["reason_code"] == "sync_command_failed"


def test_dev_sync_registers_contract_check_timing_report(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_timing_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    state = tmp_path / "state"

    stdout = invoke(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=repo,
        hook=hook,
        state=state,
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    summary = latest_summary(state)
    report_path = Path(str(summary["contract_check_timing_report_path"]))
    assert report_path.is_file()
    assert summary["contract_check_timing"]["slowest_step"]["label"] == (
        "tests: stop_review_validate_fix"
    )
    assert summary["contract_check_timing"]["measured_work_duration_seconds"] == 4.0
    assert summary["dev_sync_steps"][0]["paths"]["timing_report"] == str(report_path)


def test_installed_hook_failure_blocks_instead_of_continuing(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_failing_installed_hook(hook)

    completed = invoke_result(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=None,
        hook=hook,
        state=tmp_path / "state",
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=installed_hook_failed" in payload["systemMessage"]
    assert completed.stderr == ""


def test_missing_installed_hook_blocks_instead_of_continuing(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    hook = tmp_path / "missing" / "stop_review_validate_fix.py"

    completed = invoke_result(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=None,
        hook=hook,
        state=tmp_path / "state",
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=installed_hook_failed" in payload["systemMessage"]
    assert completed.stderr == ""


def test_installed_hook_timeout_blocks_instead_of_continuing(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_slow_installed_hook(hook)

    completed = invoke_result(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=None,
        hook=hook,
        state=tmp_path / "state",
        extra_env={"CODEX_RVF_STOP_HOOK_CHAIN_TIMEOUT": "1"},
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=installed_hook_timeout" in payload["systemMessage"]
    assert completed.stderr == ""


def test_dev_repo_without_session_owned_dirty_skips_sync_and_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    (repo / "background.txt").write_text("background\n", encoding="utf-8")
    transcript = write_user_transcript(tmp_path / "session.jsonl", repo)
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    completed = invoke_result(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    # Slice 3 reason-code rename: structured reason_code is the new name; the
    # legacy substring is preserved as the `reason_code_legacy_alias` summary
    # field but does NOT appear in systemMessage on the dispatcher path.
    assert "reason=no_unassigned_review_scope" in payload["systemMessage"]
    summary = latest_summary(tmp_path / "state")
    assert summary["reason_code"] == "no_unassigned_review_scope"
    assert summary.get("reason_code_legacy_alias") == "no_session_owned_dirty"
    assert completed.stderr == ""
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()


def test_dispatcher_falls_back_to_legacy_when_tracker_disabled(tmp_path: Path) -> None:
    """`RVF_TRACKER_DISABLE=1` keeps Phase-0 reason codes
    (`no_session_owned_dirty`) flowing through the dispatcher gate so
    disable-mode users see no behavior change."""
    repo = init_repo(tmp_path / "rvf")
    (repo / "background.txt").write_text("background\n", encoding="utf-8")
    transcript = write_user_transcript(tmp_path / "session.jsonl", repo)
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    completed = invoke_result(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
        extra_env={"RVF_TRACKER_DISABLE": "1"},
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=no_session_owned_dirty" in payload["systemMessage"]
    summary = latest_summary(tmp_path / "state")
    assert summary["reason_code"] == "no_session_owned_dirty"
    assert not (marker / "sync-ran").exists()


def test_session_hook_control_forwards_without_session_owned_dirty(tmp_path: Path) -> None:
    for action in ("off", "on", "status"):
        root = tmp_path / action
        repo = init_repo(root / "rvf")
        (repo / "background.txt").write_text("background\n", encoding="utf-8")
        transcript = write_user_transcript(root / "session.jsonl", repo)
        marker = root / "marker"
        marker.mkdir()
        write_fake_dev_scripts(repo, marker)
        hook = root / "installed" / "stop_review_validate_fix.py"
        write_fake_installed_hook(hook, marker)
        event = {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "session_id": f"session-{action}",
            "transcript_path": str(transcript),
            "last_user_message": f"RVF_STOP_HOOK: {action}",
        }

        stdout = invoke(
            event,
            dev_repo=repo,
            hook=hook,
            state=root / "state",
        )

        payload = json.loads(stdout)
        assert payload["systemMessage"] == "real hook ran"
        assert not (marker / "sync-ran").exists()
        assert not (marker / "install-ran").exists()
        assert json.loads((marker / "hook-input.json").read_text(encoding="utf-8")) == event


def test_session_manifest_failure_skips_sync_and_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    transcript = tmp_path / "bad-session.jsonl"
    transcript.write_bytes(b"\xff")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    state = tmp_path / "state"

    completed = invoke_result(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
        dev_repo=repo,
        hook=hook,
        state=state,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=session_manifest_failed" in payload["systemMessage"]
    assert completed.stderr == ""
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()
    summary = latest_summary(state)
    assert summary["status"] == "skipped"
    assert summary["reason_code"] == "session_manifest_failed"


def test_provided_missing_transcript_skips_sync_and_installed_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    missing_transcript = tmp_path / "missing-session.jsonl"
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)
    state = tmp_path / "state"

    completed = invoke_result(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "transcript_path": str(missing_transcript),
        },
        dev_repo=repo,
        hook=hook,
        state=state,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=transcript_unavailable" in payload["systemMessage"]
    assert completed.stderr == ""
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()
    summary = latest_summary(state)
    assert summary["status"] == "skipped"
    assert summary["reason_code"] == "transcript_unavailable"


def test_dev_repo_with_session_owned_dirty_syncs_and_runs_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    (repo / "owned.txt").write_text("new\n", encoding="utf-8")
    transcript = write_apply_patch_transcript(tmp_path / "session.jsonl", repo, "owned.txt")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    stdout = invoke(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert (marker / "hook-input.json").exists()


def test_committed_session_edit_with_later_same_path_dirty_skips_tracker_gate(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    _ensure_initial_commit(repo)
    (repo / "owned.txt").write_text("new\n", encoding="utf-8")
    run(["git", "add", "owned.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "commit session edit"], cwd=repo)
    (repo / "owned.txt").write_text("background\n", encoding="utf-8")
    transcript = write_timestamped_apply_patch_transcript(tmp_path / "session.jsonl", repo, "owned.txt")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    completed = invoke_result(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=no_unassigned_review_scope" in payload["systemMessage"]
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()


def test_committed_session_edit_with_later_same_path_dirty_skips_legacy_gate(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    _ensure_initial_commit(repo)
    (repo / "owned.txt").write_text("new\n", encoding="utf-8")
    run(["git", "add", "owned.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "commit session edit"], cwd=repo)
    (repo / "owned.txt").write_text("background\n", encoding="utf-8")
    transcript = write_timestamped_apply_patch_transcript(tmp_path / "session.jsonl", repo, "owned.txt")
    marker = tmp_path / "marker"
    marker.mkdir()
    write_fake_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, marker)

    completed = invoke_result(
        {
            "cwd": str(repo),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
        extra_env={"RVF_TRACKER_DISABLE": "1"},
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["continue"] is True
    assert "reason=no_session_owned_dirty" in payload["systemMessage"]
    assert not (marker / "sync-ran").exists()
    assert not (marker / "install-ran").exists()
    assert not (marker / "hook-input.json").exists()


def test_should_sync_session_scope_emits_session_manifest_failed_when_refresh_fails(
    tmp_path: Path,
) -> None:
    """`should_sync_session_scope` 必须在 `refresh_global_diff_tracker` 因
    build_manifest 异常返回 sentinel 时 fail-loud 返回
    `session_manifest_failed`，而不是继续走 allocator dry-run（后者会因为
    空 session_units 把 manifest 失败误判为 `no_unassigned_review_scope`，
    导致 dispatcher 错误地跳过 dev-sync 与 installed hook）。"""
    module = load_dispatcher_module()
    repo = init_repo(tmp_path / "rvf")
    transcript = write_user_transcript(tmp_path / "session.jsonl", repo)

    # 直接加载 stop_review_validate_fix 模块，把它的 `build_manifest`
    # 替换成立即抛错的 stub。dispatcher 在 should_sync_session_scope 里用
    # `from stop_review_validate_fix import refresh_global_diff_tracker`
    # 后调用，所以替换该模块全局即可生效。
    skill_scripts = SCRIPT.parent
    sys.path.insert(0, str(skill_scripts))
    try:
        import stop_review_validate_fix as hook_module
    finally:
        # 保留 sys.path 修改：dispatcher 也会用同一前缀路径。
        pass
    original_build_manifest = hook_module.build_manifest

    def _raise(*args, **kwargs):
        raise RuntimeError("boom: synthetic build_manifest failure")

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    os.environ["CODEX_RVF_LOG_ROOT"] = str(state)
    old_disable = os.environ.pop("RVF_TRACKER_DISABLE", None)
    try:
        hook_module.build_manifest = _raise  # type: ignore[assignment]
        ledger = hook_module.start_run(
            "stop-hook-dispatcher-test", repo=str(repo), cwd=str(repo)
        )
        event = {"cwd": str(repo), "transcript_path": str(transcript)}
        synced, message, reason_code = module.should_sync_session_scope(
            event, repo, ledger
        )
    finally:
        hook_module.build_manifest = original_build_manifest  # type: ignore[assignment]
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
        if old_disable is not None:
            os.environ["RVF_TRACKER_DISABLE"] = old_disable

    assert synced is False
    assert reason_code == "session_manifest_failed"
    assert "session manifest failed" in message
    # ledger 应记录 dev-sync 端的 session_scope_failed 与上游
    # tracker_refresh_failed，二者都用 session_manifest_failed reason code。
    raw = ledger.events_path.read_text(encoding="utf-8") if ledger.events_path.exists() else ""
    assert "tracker_refresh_failed" in raw
    assert "session_scope_failed" in raw
    assert raw.count("session_manifest_failed") >= 2


def test_coerce_text_handles_timeout_bytes() -> None:
    module = load_dispatcher_module()
    assert module.coerce_text(b"\xffstdout") == "�stdout"
    assert module.coerce_text(None) == ""


def test_sync_subprocesses_do_not_inherit_rvf_runtime_env(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "rvf")
    marker = tmp_path / "clean-env"
    hook_marker = tmp_path / "hook"
    hook_marker.mkdir()
    write_env_check_dev_scripts(repo, marker)
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, hook_marker)

    stdout = invoke(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=repo,
        hook=hook,
        state=tmp_path / "state",
        extra_env={
            "CODEX_RVF_FORK_MODE": "dry-run",
            "CODEX_RVF_STATE_DIR": "/tmp/rvf-test",
            # 去-codex S8 把中性运行时 env 改名为裸 RVF_；这些继承自父会话的值必须同样被
            # sync_child_env 剥离。用 inert path-config 变量当探针：dispatcher 不消费它们
            # （无 RunLedger/suppress 等控制语义）、run_step 也不会把它们当合法注入重新加入，
            # 故能干净地探测「裸 RVF_ 是否泄漏进 dev-sync 子进程」。
            "RVF_TMUX_BIN": "/tmp/probe-tmux",
            "RVF_TERMINAL_NOTIFIER_BIN": "/tmp/probe-notifier",
        },
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    assert marker.exists()


def test_dev_sync_step_specs_resolve_repo_level_dev_scripts(tmp_path: Path) -> None:
    module = load_dispatcher_module()
    repo = tmp_path / "rvf"

    specs = module.dev_sync_step_specs(repo)

    assert [spec[0] for spec in specs] == ["contract-check"]
    assert specs[0][1] == (repo / "scripts" / "check_plugin_contracts.py").resolve()


def test_dev_sync_step_specs_never_include_an_installer(tmp_path: Path) -> None:
    module = load_dispatcher_module()
    repo = tmp_path / "rvf"
    specs = module.dev_sync_step_specs(repo)

    assert [spec[0] for spec in specs] == ["contract-check"]
    assert specs[0][1] == (repo / "scripts" / "check_plugin_contracts.py").resolve()
    assert all(spec[1].name != "install_to_codex.py" for spec in specs)


def test_hook_config_drops_legacy_npx_kanban_defaults(tmp_path: Path) -> None:
    module = load_dispatcher_module()
    home = tmp_path / "home"
    hooks_path = home / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "CODEX_RVF_MODE=fork "
                                        "CODEX_RVF_FORK_MODE=cline-kanban "
                                        "CODEX_RVF_CLINE_KANBAN_START_CMD='npx -y kanban@0.1.66 --no-open' "
                                        "CODEX_RVF_CLINE_KANBAN_TASK_CMD='npx -y kanban@0.1.66 task' "
                                        "CODEX_RVF_CLINE_KANBAN_BASE_REF=main "
                                        f"python3 {SCRIPT}"
                                    ),
                                }
                            ]
                        }
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    original_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = str(home)
        hook_env = module.hook_config_from_hooks_json()
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home

    assert hook_env["CODEX_RVF_FORK_MODE"] == "cline-kanban"
    assert hook_env["CODEX_RVF_CLINE_KANBAN_BASE_REF"] == "main"
    assert "CODEX_RVF_CLINE_KANBAN_START_CMD" not in hook_env
    assert "CODEX_RVF_CLINE_KANBAN_TASK_CMD" not in hook_env


def test_hook_config_extracts_router_env_when_current_dispatcher_is_second_target(
    tmp_path: Path,
) -> None:
    module = load_dispatcher_module()
    home = tmp_path / "home"
    hooks_path = home / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    stable_dispatcher = tmp_path / "stable" / "stop_hook_dispatcher.py"
    router = tmp_path / "stable" / "stop_hook_router.py"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "CODEX_RVF_MODE=fork "
                                        "CODEX_RVF_FORK_MODE=cline-kanban "
                                        f"CODEX_RVF_STABLE_STOP_HOOK={stable_dispatcher} "
                                        f"CODEX_RVF_DEV_STOP_HOOK={SCRIPT} "
                                        "CODEX_RVF_CLINE_KANBAN_BASE_REF=main "
                                        f"python3 {router}"
                                    ),
                                }
                            ]
                        }
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    original_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = str(home)
        hook_env = module.hook_config_from_hooks_json()
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home

    assert hook_env["CODEX_RVF_FORK_MODE"] == "cline-kanban"
    assert hook_env["CODEX_RVF_STABLE_STOP_HOOK"] == str(stable_dispatcher)
    assert hook_env["CODEX_RVF_DEV_STOP_HOOK"] == str(SCRIPT)
    assert hook_env["CODEX_RVF_CLINE_KANBAN_BASE_REF"] == "main"


def test_installed_hook_receives_hooks_json_mode_over_stale_cached_env(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    marker = tmp_path / "marker"
    marker.mkdir()
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    hook.parent.mkdir(parents=True)
    hook.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(marker / 'hook-env.json')!r}).write_text(json.dumps({{'mode': os.environ.get('CODEX_RVF_FORK_MODE'), 'start_cmd': os.environ.get('CODEX_RVF_CLINE_KANBAN_START_CMD'), 'task_cmd': os.environ.get('CODEX_RVF_CLINE_KANBAN_TASK_CMD'), 'base_ref': os.environ.get('CODEX_RVF_CLINE_KANBAN_BASE_REF')}}), encoding='utf-8')\n"
        "print(json.dumps({'continue': True, 'systemMessage': 'real hook ran'}))\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    hooks_path = home / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "CODEX_RVF_MODE=fork "
                                        "CODEX_RVF_FORK_MODE=cline-kanban "
                                        "CODEX_RVF_CLINE_KANBAN_START_CMD='kanban --port 4567 --no-open' "
                                        "CODEX_RVF_CLINE_KANBAN_TASK_CMD='kanban --port 4567 task' "
                                        "CODEX_RVF_CLINE_KANBAN_BASE_REF=main "
                                        f"python3 {SCRIPT}"
                                    ),
                                }
                            ]
                        }
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    stdout = invoke(
        {"cwd": str(repo), "hook_event_name": "Stop"},
        dev_repo=None,
        hook=hook,
        state=tmp_path / "state",
        extra_env={
            "HOME": str(home),
            "CODEX_RVF_FORK_MODE": "gui",
            "CODEX_RVF_CLINE_KANBAN_BASE_REF": "stale-branch",
        },
    )

    payload = json.loads(stdout)
    assert payload["systemMessage"] == "real hook ran"
    hook_env = json.loads((marker / "hook-env.json").read_text(encoding="utf-8"))
    assert hook_env == {
        "mode": "cline-kanban",
        "start_cmd": "kanban --port 4567 --no-open",
        "task_cmd": "kanban --port 4567 task",
        "base_ref": "main",
    }



# ---------------------------------------------------------------------------
# f03b5：Codex 的 handoff 出口（本 dispatcher 的 handoff 分支）必须与 Claude 出口一样清掉
# 「已被 agent 接受、仍在途」的 RVF run 凭证（kanban-followup-in-progress marker），否则之后
# 每一次 Stop 都会读到残留凭证、对一轮早已 handoff + finalize 的 run 发「仍在进行且未 handoff」。
# ---------------------------------------------------------------------------


def load_kanban_followup_lock_module_for_dispatcher_tests():
    # 按生产里的模块名 import（模块内 dataclass 需要在 sys.modules 里找到自己）。
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    import kanban_followup_lock

    return kanban_followup_lock


def _isolated_rvf_runtime_state_env_for_dispatcher_tests(state: Path) -> dict[str, str]:
    """把 RVF 全部可变状态根指到 ``state`` 之下：本组测试会走到真实 installed hook，任何一根
    漏设都会写进真实 ``~/.rvf``（live infra，假绿）。"""
    return {
        "RVF_HOME": str(state / "rvf-home"),
        "RVF_USER_LOCAL_DATA_ROOT": str(state / "rvf-user-local-data"),
        "RVF_KANBAN_FOLLOWUP_LOCK_ROOT": str(state / "kanban-followup-in-progress"),
        "RVF_PROVIDER_HEALTH_CHECK": "0",
        "CODEX_RVF_FORK_MODE": "off",
        "CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET": "2",
    }


def _arm_codex_rvf_start_in_flight_receipt_for_dispatcher_tests(
    *,
    state: Path,
    repo: Path,
    session_id: str,
    run_id: str,
    run_dir: Path,
    delivered_transcript_path: Path,
) -> Path:
    """按 Codex ``rvf start`` 的形态写 session-keyed 在途凭证（task_id=None）。"""
    lock_module = load_kanban_followup_lock_module_for_dispatcher_tests()
    marker_path = lock_module.write_marker(
        task_id=None,
        session_id=session_id,
        run_id=run_id,
        run_dir=str(run_dir),
        repo=str(repo),
        cwd=str(repo),
        decision_token="bca5ac470e8d9f18",
        delivered_session_id=session_id,
        delivered_transcript_path=str(delivered_transcript_path),
        state=lock_module.STATE_IN_PROGRESS,
        root=state,
    )
    assert marker_path is not None and marker_path.exists()
    return marker_path


def _write_codex_rollout_for_dispatcher_tests(
    path: Path,
    *,
    thread_id: str,
    repo: Path,
    user_messages: list[str],
    assistant_message: str | None = None,
    history_base: dict[str, object] | None = None,
) -> Path:
    session_meta: dict[str, object] = {"id": thread_id, "cwd": str(repo)}
    if history_base is not None:
        session_meta["history_mode"] = "paginated"
        session_meta["history_base"] = history_base
    records: list[dict[str, object]] = [{"type": "session_meta", "payload": session_meta}]
    for message in user_messages:
        records.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": message}],
                },
            }
        )
        records.append({"type": "event_msg", "payload": {"type": "user_message", "message": message}})
    if assistant_message is not None:
        records.append({"type": "event_msg", "payload": {"type": "agent_message", "message": assistant_message}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return path


def _ledger_events_of_latest_stop(state: Path) -> list[dict[str, object]]:
    pointer = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    return [
        json.loads(line)
        for line in Path(str(pointer["events_path"])).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _invoke_codex_handoff_stop_through_dispatcher(
    tmp_path: Path,
    *,
    repo: Path,
    state: Path,
    session_id: str,
    handoff: Path,
    hook: Path,
) -> str:
    transcript = _write_codex_rollout_for_dispatcher_tests(
        tmp_path / f"rollout-2026-10-04T21-03-33-{session_id}.jsonl",
        thread_id=session_id,
        repo=repo,
        user_messages=["实现完成", "RVF 发起决策点 · RVF_DECISION_TOKEN=bca5ac470e8d9f18"],
        assistant_message=f"RVF_HANDOFF_FILE: {handoff}",
    )
    return invoke(
        {
            "cwd": str(repo),
            "session_id": session_id,
            "turn_id": "turn-handoff",
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "last_assistant_message": f"RVF_HANDOFF_FILE: {handoff}",
        },
        dev_repo=None,
        hook=hook,
        state=state,
        extra_env=_isolated_rvf_runtime_state_env_for_dispatcher_tests(state),
    )


def test_dispatcher_handoff_clears_in_progress_marker_of_handed_off_run(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    state = tmp_path / "state"
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, tmp_path)
    run_dir, handoff = _seed_finalize_run_dir(state=state, repo=repo, run_id="rvf-handed-off")
    session_id = "01a10703-34b8-72d0-9bd8-976ad362b12a"
    marker_path = _arm_codex_rvf_start_in_flight_receipt_for_dispatcher_tests(
        state=state,
        repo=repo,
        session_id=session_id,
        run_id="rvf-handed-off",
        run_dir=run_dir,
        delivered_transcript_path=tmp_path / "delivered.jsonl",
    )

    stdout = _invoke_codex_handoff_stop_through_dispatcher(
        tmp_path, repo=repo, state=state, session_id=session_id, handoff=handoff, hook=hook
    )

    payload = json.loads(stdout)
    assert payload.get("decision") != "block", payload
    assert (run_dir / "artifacts" / ".finalize.lock").exists()
    assert not marker_path.exists(), "Codex handoff 出口必须清掉本 run 的在途凭证"
    cleared = [
        e for e in _ledger_events_of_latest_stop(state) if e.get("event") == "kanban_followup_in_progress_cleared"
    ]
    assert len(cleared) == 1, cleared
    assert cleared[0]["removed_kanban_followup_in_progress_marker_paths"] == [str(marker_path)]
    assert not (tmp_path / "hook-input.json").exists(), "handoff 分支不应再转交 installed hook"


def test_dispatcher_repeated_handoff_is_idempotent(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    state = tmp_path / "state"
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, tmp_path)
    run_dir, handoff = _seed_finalize_run_dir(state=state, repo=repo, run_id="rvf-handed-off")
    session_id = "01a10799-e6a2-79e0-b526-04d5c662f819"
    marker_path = _arm_codex_rvf_start_in_flight_receipt_for_dispatcher_tests(
        state=state,
        repo=repo,
        session_id=session_id,
        run_id="rvf-handed-off",
        run_dir=run_dir,
        delivered_transcript_path=tmp_path / "delivered.jsonl",
    )

    first_payload = json.loads(
        _invoke_codex_handoff_stop_through_dispatcher(
            tmp_path, repo=repo, state=state, session_id=session_id, handoff=handoff, hook=hook
        )
    )
    assert not marker_path.exists()
    finalize_lock_after_first = (run_dir / "artifacts" / ".finalize.lock").read_text(encoding="utf-8")

    second_payload = json.loads(
        _invoke_codex_handoff_stop_through_dispatcher(
            tmp_path, repo=repo, state=state, session_id=session_id, handoff=handoff, hook=hook
        )
    )

    assert second_payload.get("decision") != "block", second_payload
    assert set(second_payload) == set(first_payload)
    assert not marker_path.exists()
    assert (run_dir / "artifacts" / ".finalize.lock").read_text(encoding="utf-8") == finalize_lock_after_first
    second_events = _ledger_events_of_latest_stop(state)
    assert not [e for e in second_events if e.get("event") == "kanban_followup_in_progress_cleared"], second_events
    assert not [e for e in second_events if e.get("event") == "finalize_failed"], second_events


def test_dispatcher_handoff_keeps_in_progress_marker_of_different_run(tmp_path: Path) -> None:
    """同一会话里凭证属于**另一轮** run（例如本次 handoff 的是旧 run 的残留标记）：不得误清。"""
    repo = init_repo(tmp_path / "repo")
    state = tmp_path / "state"
    hook = tmp_path / "installed" / "stop_review_validate_fix.py"
    write_fake_installed_hook(hook, tmp_path)
    _run_dir, handoff = _seed_finalize_run_dir(state=state, repo=repo, run_id="rvf-handed-off")
    other_run_dir = state / "runs" / "rvf-other-in-flight"
    other_run_dir.mkdir(parents=True)
    session_id = "01a10aaa-0000-7000-8000-000000000001"
    marker_path = _arm_codex_rvf_start_in_flight_receipt_for_dispatcher_tests(
        state=state,
        repo=repo,
        session_id=session_id,
        run_id="rvf-other-in-flight",
        run_dir=other_run_dir,
        delivered_transcript_path=tmp_path / "delivered.jsonl",
    )

    _invoke_codex_handoff_stop_through_dispatcher(
        tmp_path, repo=repo, state=state, session_id=session_id, handoff=handoff, hook=hook
    )

    assert marker_path.exists(), "别的 run 的在途凭证不得被本次 handoff 误清"
    events = _ledger_events_of_latest_stop(state)
    assert not [e for e in events if e.get("event") == "kanban_followup_in_progress_cleared"], events
    kept = [e for e in events if e.get("event") == "kanban_followup_in_progress_marker_kept_for_other_run"]
    assert len(kept) == 1, [e.get("event") for e in events]
    assert kept[0]["handed_off_run_id"] == "rvf-handed-off"
    assert kept[0]["kept_kanban_followup_in_progress_markers"][0]["run_id"] == "rvf-other-in-flight"


def test_dispatcher_handoff_then_rewind_then_commit_stop_does_not_reengage(tmp_path: Path) -> None:
    """端到端（W1 task 863b8 时间线）：Codex handoff → 用户 rewind（同 thread 另起分段文件）→
    要求 commit + base-branch-sync → Stop。这次 Stop 经 dispatcher 转交**真实** installed hook，
    绝不能再发「仍在进行且未 handoff」。"""
    repo = init_repo(tmp_path / "repo")
    state = tmp_path / "state"
    real_installed_hook = SCRIPT.with_name("stop_review_validate_fix.py")
    run_dir, handoff = _seed_finalize_run_dir(state=state, repo=repo, run_id="rvf-handed-off")
    session_id = "01a10703-34b8-72d0-9bd8-976ad362b12a"
    marker_path = _arm_codex_rvf_start_in_flight_receipt_for_dispatcher_tests(
        state=state,
        repo=repo,
        session_id=session_id,
        run_id="rvf-handed-off",
        run_dir=run_dir,
        delivered_transcript_path=tmp_path / f"rollout-2026-10-04T21-03-33-{session_id}.jsonl",
    )
    _invoke_codex_handoff_stop_through_dispatcher(
        tmp_path, repo=repo, state=state, session_id=session_id, handoff=handoff, hook=real_installed_hook
    )
    assert (run_dir / "artifacts" / ".finalize.lock").exists()

    # rewind 之后同一 thread 另起分段文件；工作树仍脏（commit 之前的 Stop / base-sync 期间的 Stop）。
    (repo / "feature.txt").write_text("rvf reviewed change\n", encoding="utf-8")
    rewound_segment = _write_codex_rollout_for_dispatcher_tests(
        tmp_path / f"rollout-2026-10-05T01-09-18-{session_id}_01a107e3-0000-7000-8000-000000000000.jsonl",
        thread_id=session_id,
        repo=repo,
        user_messages=["RVF found no issue. Commit and $base-branch-sync"],
        assistant_message="已提交并完成 base-branch-sync。",
        history_base={"thread_id": session_id, "end_ordinal_exclusive": 1, "end_byte_offset": 1},
    )
    completed = invoke_result(
        {
            "cwd": str(repo),
            "session_id": session_id,
            "turn_id": "turn-after-rewind",
            "hook_event_name": "Stop",
            "transcript_path": str(rewound_segment),
            "stop_hook_active": False,
            "last_assistant_message": "已提交并完成 base-branch-sync。",
        },
        dev_repo=None,
        hook=real_installed_hook,
        state=state,
        extra_env=_isolated_rvf_runtime_state_env_for_dispatcher_tests(state),
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
    assert payload.get("decision") != "block" or "仍在进行且未 handoff" not in str(payload.get("reason")), payload
    assert "仍在进行且未 handoff" not in completed.stdout, completed.stdout
    assert not marker_path.exists()
    nudged = [
        path
        for path in (state / "runs").rglob("events.jsonl")
        if "kanban_followup_in_progress_nudged" in path.read_text(encoding="utf-8")
    ]
    assert not nudged, nudged

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    args = parser.parse_args()
    if args.shard_count < 1:
        raise SystemExit("--shard-count must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise SystemExit("--shard-index must be in [0, shard-count)")

    tests = [
        # f03b5：Codex handoff 出口清本 run 的在途凭证；重复 handoff 幂等；不误清别的 run；
        # handoff → rewind → commit/base-sync → Stop 不再被续跑提示驱动。
        test_dispatcher_handoff_clears_in_progress_marker_of_handed_off_run,
        test_dispatcher_repeated_handoff_is_idempotent,
        test_dispatcher_handoff_keeps_in_progress_marker_of_different_run,
        test_dispatcher_handoff_then_rewind_then_commit_stop_does_not_reengage,
        test_router_defaults_to_dev_when_dev_terms_apply,
        test_router_defaults_to_stable_when_dev_terms_do_not_apply,
        test_router_channel_dev_marker_routes_current_and_later_stops,
        test_router_channel_default_clears_session_marker,
        test_router_channel_status_reports_gate_and_channel,
        test_dev_repo_main_session_syncs_before_running_installed_hook,
        test_handoff_marker_opens_before_dev_sync_or_installed_hook,
        test_handoff_marker_finalizes_run_artifacts_same_session,
        test_handoff_marker_surfaces_finalize_record_errors,
        test_handoff_marker_finalizes_run_artifacts_forked_session,
        test_rvf_analyze_followup_trigger_skips_dispatcher_sync,
        test_plan_operation_skips_before_dev_sync_or_installed_hook,
        test_codex_goal_mode_skips_before_dev_sync_or_installed_hook,
        test_non_codex_goal_like_transcript_runs_installed_hook,
        test_codex_goal_mode_subagent_runs_installed_hook,
        test_codex_user_text_goal_marker_without_status_runs_installed_hook,
        test_codex_completed_goal_runs_installed_hook,
        test_literal_plan_markers_in_completion_do_not_skip_hook,
        test_prior_plan_output_does_not_suppress_future_turn,
        test_session_hook_off_still_syncs_before_running_installed_hook,
        test_dev_channel_sync_never_installs_plugin,
        test_non_matching_repo_runs_installed_hook_without_sync,
        test_subagent_stop_runs_installed_hook_without_sync,
        test_suppress_env_skips_before_sync_and_installed_hook,
        test_suppress_env_skips_handoff_marker_before_opening,
        test_sync_failure_skips_installed_hook_to_avoid_stale_fork,
        test_dev_sync_registers_contract_check_timing_report,
        test_installed_hook_failure_blocks_instead_of_continuing,
        test_missing_installed_hook_blocks_instead_of_continuing,
        test_installed_hook_timeout_blocks_instead_of_continuing,
        test_dev_repo_without_session_owned_dirty_skips_sync_and_hook,
        test_dispatcher_falls_back_to_legacy_when_tracker_disabled,
        test_session_hook_control_forwards_without_session_owned_dirty,
        test_session_manifest_failure_skips_sync_and_installed_hook,
        test_provided_missing_transcript_skips_sync_and_installed_hook,
        test_dev_repo_with_session_owned_dirty_syncs_and_runs_hook,
        test_committed_session_edit_with_later_same_path_dirty_skips_tracker_gate,
        test_committed_session_edit_with_later_same_path_dirty_skips_legacy_gate,
        test_should_sync_session_scope_emits_session_manifest_failed_when_refresh_fails,
        test_coerce_text_handles_timeout_bytes,
        test_sync_subprocesses_do_not_inherit_rvf_runtime_env,
        test_dev_sync_step_specs_resolve_repo_level_dev_scripts,
        test_dev_sync_step_specs_never_include_an_installer,
        test_hook_config_drops_legacy_npx_kanban_defaults,
        test_hook_config_extracts_router_env_when_current_dispatcher_is_second_target,
        test_installed_hook_receives_hooks_json_mode_over_stale_cached_env,
    ]
    assert_every_defined_test_is_registered(
        globals(), {test.__name__ for test in tests}, source_path=__file__,
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        selected = [
            test
            for index, test in enumerate(tests)
            if args.shard_count <= 1 or index % args.shard_count == args.shard_index
        ]
        for test in selected:
            if test is test_coerce_text_handles_timeout_bytes:
                test()
            else:
                test(root / test.__name__)
    suffix = (
        f" shard {args.shard_index + 1}/{args.shard_count}"
        if args.shard_count > 1
        else ""
    )
    print(f"codex stop hook dispatcher tests OK{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
