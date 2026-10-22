#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import concurrent.futures
import datetime
import functools
import inspect
import json
import importlib.util
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from _rvf_test_support.kanban_followup_lock_marker_name import in_progress_lock_marker_name
from _rvf_test_support.registry_completeness_guard import (
    assert_every_defined_test_is_registered,
    registered_names_from_case_tuples,
)


# 注册表完整性守卫的豁免名单（唯一合法出口）：仅放确属暂时隔离、暂时跑不过的测试，
# 每项须附 `# quarantined: <原因+issue>`。当前为空 = 严格模式：任何 def test_* 漏登记即红。
INTENTIONALLY_UNREGISTERED: frozenset[str] = frozenset()


ROOT = Path(__file__).resolve().parents[1]
CHECK_SKILL_CONTRACTS = ROOT / "scripts" / "check_skill_contracts.sh"
CHECK_PLUGIN_CONTRACTS = ROOT / "scripts" / "check_plugin_contracts.py"
SCRIPT_DIR = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
)
BUILD_PACKET = SCRIPT_DIR / "build_review_packet.py"
CHECK_REVIEW_OUTPUT = SCRIPT_DIR / "check_review_output.py"
WRITE_REVIEW_RESULT = SCRIPT_DIR / "write_review_result.py"
CHECK_REVIEW_RESULT = SCRIPT_DIR / "check_review_result.py"
COMMAND_LOCK = SCRIPT_DIR / "command_lock.py"
PREPARE_REVIEW_RUN = SCRIPT_DIR / "prepare_review_run.py"
RUN_ALTERNATIVE_REVIEWER = SCRIPT_DIR / "run_alternative_reviewer.py"
CURSOR_STREAM_TOOL_LAYER_HEALTH = SCRIPT_DIR / "cursor_stream_tool_layer_health.py"
VERIFY_CURSOR_TOOL_LAYER = SCRIPT_DIR / "verify_cursor_tool_layer.py"
CANCEL_RVF_RUN = SCRIPT_DIR / "cancel_rvf_run.py"
CLINE_KANBAN_CLIENT = SCRIPT_DIR / "cline_kanban_client.py"
APPLY_WORKTREE_BOOTSTRAP = SCRIPT_DIR / "apply_worktree_bootstrap.py"
SESSION_MANIFEST = ROOT / "core" / "session_scope_allocation" / "session_change_manifest.py"
DIAGNOSE_STOP_HOOK_SCOPE = SCRIPT_DIR / "diagnose_stop_hook_scope.py"
RUN_LEDGER = ROOT / "core" / "run_ledger" / "run_ledger.py"
RVF_HANDOFF = SCRIPT_DIR / "rvf_handoff.py"
RVF_PREP_FILE = SCRIPT_DIR / "rvf_prep_file.py"
RVF_DECISION_TOKEN_STORE = SCRIPT_DIR / "rvf_decision_token_store.py"
RVF_USER_PROMPT_SUBMIT = SCRIPT_DIR / "rvf_user_prompt_submit.py"
KANBAN_FOLLOWUP_LOCK = SCRIPT_DIR / "kanban_followup_lock.py"
AWAITING_DISPATCHED_AGENT_MARKER = SCRIPT_DIR / "rvf_awaiting_dispatched_agent_marker.py"
RVF_POST_TOOL_USE = SCRIPT_DIR / "rvf_post_tool_use.py"
HOOKS_JSON = (
    ROOT / "plugins" / "review-validate-fix" / "hooks" / "hooks.json"
)

for _name in tuple(os.environ):
    if _name.startswith("CODEX_RVF_") or _name.startswith("RVF_"):
        os.environ.pop(_name, None)

# 铁律：测试绝不写真实 ``~/.rvf``（live infra ⇒ 假绿 + 污染用户状态）。上面那轮清扫把
# ``RVF_HOME`` 家族的**专用** env（``RVF_WITHDRAWN_DISPATCH_TOMBSTONE_ROOT`` /
# ``RVF_AGENTIC_INITIATION_GUARDIAN_ROOT`` / 决策 token store / 注入尝试 ledger /
# kanban-followup 锁 …）连同 ``RVF_HOME`` 一起摘掉了，于是它们共同的解析链
# 「显式参数 > 专用 env > ``$RVF_HOME`` > ``~/.rvf``」会直接落到最后一档的真实根。本文件的
# 测试是**逐个手工 set/restore** 环境的——只要有一个新调用点落进这一族、而某个测试没跟着加
# 隔离，就会静默写真实根（实测：``inspect_user_prompt_submit`` 见到 dispatch token 会抢
# ``consumed`` 终态，把 tombstone 落进 ``~/.rvf/rvf-withdrawn-dispatch-tokens/``）。
#
# 隔离必须挂在**模块导入期**，而不是 ``main()`` 里：本文件同时也能被 bare pytest 直接收集
# （``python3 -m pytest tests/test_review_support_scripts.py``），那条入口根本不经过
# ``main()``，放在 ``main()`` 的隔离对它完全失效。导入期是手写 runner 与 bare pytest 两条
# 入口唯一的公共上游。``--jobs`` 的 ``ProcessPoolExecutor`` worker 两种启动方式也都被覆盖：
# fork 直接继承 ``os.environ``，spawn 会重新 import 本模块、各自再建一个隔离根。
#
# 用**强制赋值**而非 ``setdefault``：开发者 shell 里可能正带着指向真实根的 ``RVF_HOME``。
# 个别测试仍可在自己作用域内 set/restore 覆盖本默认值，那条既有行为不受影响。
_ISOLATED_RVF_HOME_FOR_TESTS = tempfile.mkdtemp(prefix="rvf-test-home-")
os.environ["RVF_HOME"] = _ISOLATED_RVF_HOME_FOR_TESTS
# ``RVF_HOME`` 管不到 ``user-local-data``（run-logs / diff-tracker / runtime-state 另有自己的根
# ``RVF_USER_LOCAL_DATA_ROOT``，缺省 ``~/.rvf/user-local-data``）。继承本进程环境的子进程——
# prepare_review_run / run_alternative_reviewer / command_lock 的用例大多直接 ``env=None``——
# 此前因此把 RunLedger 写进真实 ``run-logs/runs/``（F5·H5b 普查：一轮套件 180 个文件）。
# 设这一档而不是 ``CODEX_RVF_LOG_ROOT``：后者优先级更高，会遮住专测「run-logs 从
# RVF_USER_LOCAL_DATA_ROOT 派生」的用例。
os.environ["RVF_USER_LOCAL_DATA_ROOT"] = str(
    Path(_ISOLATED_RVF_HOME_FOR_TESTS) / "user-local-data"
)
atexit.register(shutil.rmtree, _ISOLATED_RVF_HOME_FOR_TESTS, ignore_errors=True)


def load_alternative_reviewer_module():
    spec = importlib.util.spec_from_file_location("rvf_run_alternative_reviewer", RUN_ALTERNATIVE_REVIEWER)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load run_alternative_reviewer module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cursor_tool_layer_health_module():
    spec = importlib.util.spec_from_file_location(
        "rvf_cursor_stream_tool_layer_health", CURSOR_STREAM_TOOL_LAYER_HEALTH
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load cursor_stream_tool_layer_health module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cancel_rvf_run_module():
    spec = importlib.util.spec_from_file_location("rvf_cancel_rvf_run", CANCEL_RVF_RUN)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load cancel_rvf_run module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_run_ledger_module():
    spec = importlib.util.spec_from_file_location("rvf_run_ledger_for_tests", RUN_LEDGER)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load core.run_ledger.run_ledger module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_rvf_prep_file_module():
    spec = importlib.util.spec_from_file_location("rvf_prep_file", RVF_PREP_FILE)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_prep_file module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_prepare_review_run_module():
    """加载 prepare_review_run 模块（进程内直调 prepare_run_from_prep_file 等）。

    与 load_rvf_agent_decision_cli_module 同构：它 top-level import 兄弟脚本
    （rvf_prep_file / rvf_bootstrap_gate）与 core.*（经 _rvf_pyroot），故先保证 ROOT +
    SCRIPT_DIR 在 sys.path 上。
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("prepare_review_run", PREPARE_REVIEW_RUN)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load prepare_review_run module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # 本测试簇只验证既有状态机；真实宿主/Git 分界由独立集成测试覆盖。
    module.require_review_start_boundary_for_prepare = lambda **kwargs: {"test_scope": "prepare-unit"}
    # 专用状态机夹具维持绑定器的 Path 返回契约；真实双向绑定由隔离 Git 集成套件验证。
    module.bind_review_start_boundary_to_one_run = lambda receipt, run_id, run_dir: (run_id, Path(run_dir) if run_dir is not None else None)
    return module


def load_rvf_bootstrap_gate_module():
    """加载 rvf_bootstrap_gate 模块（pre-review local-base bootstrap 的等待状态机）。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "rvf_bootstrap_gate", SCRIPT_DIR / "rvf_bootstrap_gate.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_bootstrap_gate module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_rvf_decision_token_store_module():
    # 与 load_rvf_prep_file_module 同构：spec 载入 + sys.modules 注册（dataclass 在 3.9 下做
    # ClassVar 检测时需要模块出现在 sys.modules，否则 _is_type 取不到模块命名空间而报错）。
    spec = importlib.util.spec_from_file_location("rvf_decision_token_store", RVF_DECISION_TOKEN_STORE)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_decision_token_store module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_rvf_user_prompt_submit_module():
    # Ensure rvf_prep_file and other dependencies are importable from SCRIPT_DIR
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    load_rvf_prep_file_module()
    spec = importlib.util.spec_from_file_location(
        "rvf_user_prompt_submit", RVF_USER_PROMPT_SUBMIT
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_user_prompt_submit module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_kanban_followup_lock_module():
    # kanban_followup_lock 依赖 ``from core.run_ledger.run_ledger import safe_token``，需 SCRIPT_DIR 在 path 上（其自带 _rvf_pyroot bootstrap 解析 pyroot）。
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "kanban_followup_lock", KANBAN_FOLLOWUP_LOCK
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load kanban_followup_lock module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_awaiting_dispatched_agent_marker_module():
    # rvf_awaiting_dispatched_agent_marker 依赖 ``from rvf_logging import safe_token`` 与
    # ``from kanban_followup_lock import ...``，需 SCRIPT_DIR 在 path 上。
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "rvf_awaiting_dispatched_agent_marker", AWAITING_DISPATCHED_AGENT_MARKER
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_awaiting_dispatched_agent_marker module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_check_plugin_contracts_module():
    spec = importlib.util.spec_from_file_location(
        "rvf_check_plugin_contracts",
        CHECK_PLUGIN_CONTRACTS,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load check_plugin_contracts module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cline_kanban_client_module():
    spec = importlib.util.spec_from_file_location("rvf_cline_kanban_client", CLINE_KANBAN_CLIENT)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load cline_kanban_client module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_rvf_post_tool_use_module():
    spec = importlib.util.spec_from_file_location("rvf_post_tool_use", RVF_POST_TOOL_USE)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_post_tool_use module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_rvf_analyze_thread_module():
    # rvf_analyze_thread top-level imports core.run_ledger.run_ledger / trajectory_distill，且
    # launch_detached_analyze_thread 会 lazy import rvf_analyze_advisory，全部需
    # 要 SCRIPT_DIR 在 sys.path 上才能解析；以真实模块名注册进 sys.modules，让
    # advisory 的 `from rvf_analyze_thread import ...` 命中同一实例。
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "rvf_analyze_thread", SCRIPT_DIR / "rvf_analyze_thread.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_analyze_thread module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_fake_tmux_script(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "calls = os.environ.get('FAKE_TMUX_CALLS')\n"
        "if calls:\n"
        "    with open(calls, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "stderr = os.environ.get('FAKE_TMUX_STDERR')\n"
        "if stderr:\n"
        "    sys.stderr.write(stderr)\n"
        "raise SystemExit(int(os.environ.get('FAKE_TMUX_RETURNCODE', '0')))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def write_subcommand_aware_tmux_script(path: Path) -> Path:
    """fake tmux：记录调用，并按子命令分别返回退出码——

    - ``has-session`` → ``FAKE_TMUX_HAS_SESSION_RETURNCODE``（默认 0，即「存活」）
    - 其余（``new-session`` 等）→ ``FAKE_TMUX_RETURNCODE``（默认 0）

    供 FU-2 staleness 测试构造「has-session 报死 + new-session 成功」等组合
    （uniform 的 write_fake_tmux_script 无法区分子命令）。
    """
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "calls = os.environ.get('FAKE_TMUX_CALLS')\n"
        "if calls:\n"
        "    with open(calls, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(json.dumps({'argv': argv}) + '\\n')\n"
        "sub = argv[0] if argv else ''\n"
        "if sub == 'has-session':\n"
        "    raise SystemExit(int(os.environ.get('FAKE_TMUX_HAS_SESSION_RETURNCODE', '0')))\n"
        "raise SystemExit(int(os.environ.get('FAKE_TMUX_RETURNCODE', '0')))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class _AnalyzeLedgerStub:
    run_id = "rvf-unit"

    def env(self) -> dict[str, str]:
        return {}


def _seed_analysis_payload(run_dir: Path) -> dict[str, str]:
    analysis_dir = run_dir / "artifacts" / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    summary = analysis_dir / "summary.md"
    causality = analysis_dir / "causality.json"
    summary.write_text("# scaffold\n<!-- TODO(rvf-analyze): fill -->\n", encoding="utf-8")
    causality.write_text(json.dumps({"issues": []}) + "\n", encoding="utf-8")
    return {
        "run_dir": str(run_dir),
        "summary_md_path": str(summary),
        "causality_json_path": str(causality),
    }


def test_rvf_analyze_thread_builds_claude_command(_root: Path | None = None) -> None:
    module = load_rvf_analyze_thread_module()
    argv, uses_stdin = module.build_analyze_command(module.HOST_CLAUDE)
    assert uses_stdin is True
    assert Path(argv[0]).name == "claude"
    assert "-p" in argv
    assert "--permission-mode" in argv and "bypassPermissions" in argv
    assert "--output-format" in argv and "stream-json" in argv
    # analyze agent 必须能解析 $rvf-analyze slash command 并 Edit 文件。
    assert "--disable-slash-commands" not in argv


def test_rvf_analyze_thread_builds_codex_command(_root: Path | None = None) -> None:
    module = load_rvf_analyze_thread_module()
    argv, uses_stdin = module.build_analyze_command(module.HOST_CODEX)
    assert uses_stdin is True
    assert Path(argv[0]).name == "codex"
    assert "exec" in argv
    assert argv[-1] == "-"
    assert "--sandbox" in argv and "workspace-write" in argv
    # 未知 host 兜底到 codex 向量。
    fallback_argv, _ = module.build_analyze_command("totally-unknown-host")
    assert fallback_argv == argv


def test_rvf_analyze_thread_select_host(root: Path) -> None:
    module = load_rvf_analyze_thread_module()
    root.mkdir(parents=True, exist_ok=True)
    claude_t = root / "claude.jsonl"
    claude_t.write_text(json.dumps({"type": "user", "message": {"role": "user"}}) + "\n", encoding="utf-8")
    codex_t = root / "codex.jsonl"
    codex_t.write_text(json.dumps({"type": "session_meta", "payload": {"id": "s"}}) + "\n", encoding="utf-8")

    before_override_expiry = datetime.datetime(2026, 7, 10, 11, 59, tzinfo=datetime.timezone.utc)
    after_override_expiry = datetime.datetime(2026, 7, 10, 12, 1, tzinfo=datetime.timezone.utc)

    override = module.temporary_force_codex_analysis_host_override(before_override_expiry)
    assert override is not None
    assert override["forced_host"] == module.HOST_CODEX
    assert override["expires_after_requested_date"] == "2026-07-09"
    assert override["expiry_timezone_policy"] == "covers_end_of_2026_07_09_across_us_timezones"

    assert (
        module.select_host({"transcript_path": str(claude_t)}, now_utc=before_override_expiry)
        == module.HOST_CODEX
    )
    assert (
        module.select_host({"transcript_path": str(claude_t)}, now_utc=after_override_expiry)
        == module.HOST_CLAUDE
    )
    assert (
        module.select_host({"transcript_path": str(codex_t)}, now_utc=after_override_expiry)
        == module.HOST_CODEX
    )
    # transcript 缺失 → 兜底 codex。
    assert module.select_host({}, now_utc=after_override_expiry) == module.HOST_CODEX
    assert (
        module.select_host(
            {"transcript_path": str(root / "missing.jsonl")},
            now_utc=after_override_expiry,
        )
        == module.HOST_CODEX
    )


def test_rvf_analyze_thread_status_file_schema(root: Path) -> None:
    module = load_rvf_analyze_thread_module()
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / "runs" / "rvf-unit"
    analysis = _seed_analysis_payload(run_dir)
    analysis_dir = Path(analysis["summary_md_path"]).parent
    fake_tmux = write_fake_tmux_script(root / "fake_tmux.py")
    tmux_calls = root / "tmux-calls.jsonl"

    saved = {k: os.environ.get(k) for k in ("RVF_TMUX_BIN", "FAKE_TMUX_CALLS", "FAKE_TMUX_RETURNCODE")}
    os.environ["RVF_TMUX_BIN"] = str(fake_tmux)
    os.environ["FAKE_TMUX_CALLS"] = str(tmux_calls)
    os.environ["FAKE_TMUX_RETURNCODE"] = "0"
    try:
        result = module.launch_detached_analyze_thread(
            event={},
            ledger=_AnalyzeLedgerStub(),
            analysis=analysis,
            finalize_record=None,
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert result["launch_status"] == "launched"
    assert result["tmux_session"] == "rvf-analyze-rvf-unit"

    status = json.loads((analysis_dir / ".analyze-thread.status.json").read_text(encoding="utf-8"))
    expected_keys = {
        "schema_version",
        "run_dir",
        "run_name",
        "host",
        "tmux_session",
        "command",
        "pid",
        "started_at",
        "armed_at",
        "returncode",
        "finished_at",
        "launch_status",
        "error",
    }
    assert expected_keys <= set(status)
    assert status["schema_version"] == 1
    assert status["launch_status"] == "launched"
    # 锁与冻结 prompt 落盘；fake tmux 恰好被调一次。
    assert (analysis_dir / ".analyze-thread.lock").exists()
    assert (analysis_dir / ".analyze-thread.prompt.md").exists()
    assert len(tmux_calls.read_text(encoding="utf-8").splitlines()) == 1
    # 切忌触碰 summary/causality（COMPLETE 判定靠它们 mtime）。
    assert "TODO(rvf-analyze)" in Path(analysis["summary_md_path"]).read_text(encoding="utf-8")


def test_rvf_analyze_thread_lock_blocks_second_launch(root: Path) -> None:
    module = load_rvf_analyze_thread_module()
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / "runs" / "rvf-unit"
    analysis = _seed_analysis_payload(run_dir)
    fake_tmux = write_fake_tmux_script(root / "fake_tmux.py")
    tmux_calls = root / "tmux-calls.jsonl"

    saved = {k: os.environ.get(k) for k in ("RVF_TMUX_BIN", "FAKE_TMUX_CALLS", "FAKE_TMUX_RETURNCODE")}
    os.environ["RVF_TMUX_BIN"] = str(fake_tmux)
    os.environ["FAKE_TMUX_CALLS"] = str(tmux_calls)
    os.environ["FAKE_TMUX_RETURNCODE"] = "0"
    try:
        first = module.launch_detached_analyze_thread(
            event={}, ledger=_AnalyzeLedgerStub(), analysis=analysis, finalize_record=None
        )
        second = module.launch_detached_analyze_thread(
            event={}, ledger=_AnalyzeLedgerStub(), analysis=analysis, finalize_record=None
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert first["launch_status"] == "launched"
    # 每 run O_EXCL 锁：第二次命中 already_running，不再启动 tmux。
    # 第二次会先 has-session 探活（session 存活 → already_running），故按 new-session 计数。
    recorded = [json.loads(line)["argv"] for line in tmux_calls.read_text(encoding="utf-8").splitlines()]
    assert second["launch_status"] == "already_running"
    assert len([a for a in recorded if a[:1] == ["new-session"]]) == 1, recorded


def load_check_review_output_module():
    spec = importlib.util.spec_from_file_location(
        "rvf_check_review_output", CHECK_REVIEW_OUTPUT
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load check_review_output module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


from independent_review_start_boundary_fixture import run_subprocess_with_independent_review_start_boundary

def run(
    cmd: list[str],
    cwd: Path | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or completed.stdout.strip() or f"{cmd[0]} failed")
    return completed



def run_with_independent_review_start_boundary(cmd, cwd=None, input_text=None, env=None):
    """既有 prepare CLI 正向回归显式补齐新契约；独立 gate 反例仍直接运行生产入口。"""
    if len(cmd) > 1 and Path(cmd[1]).name == "prepare_review_run.py" and "--repo" in cmd:
        from independent_review_start_boundary_fixture import environment_with_synthetic_independent_review_start_boundary
        def argument(name):
            return cmd[cmd.index(name) + 1] if name in cmd else None
        env = environment_with_synthetic_independent_review_start_boundary(
            repo=Path(argument("--repo")), environment=env,
            run_id=argument("--rvf-run-id"), run_dir=argument("--rvf-run-dir"))
    return run(cmd, cwd=cwd, input_text=input_text, env=env)

def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_rvf_prep_file_round_trip_and_sweep(tmp_path: Path) -> None:
    prep = load_rvf_prep_file_module()
    root = tmp_path / "prep-root"
    now = prep.parse_timestamp("2026-05-07T00:00:00Z")

    written = prep.write_prep_file(
        {
            "origin_session_id": "session-a",
            "origin_repo": str(tmp_path / "repo"),
            "target_flow": "flow-2-branch",
            "rvf_run": {
                "run_id": "rvf-20260507T000000Z-test",
                "tracker_scope_hash": "sha256:abc",
            },
        },
        root=root,
        token="0123456789abcdef",
        now=now,
        ttl_seconds=300,
    )

    assert written.token == "0123456789abcdef"
    assert written.path == root / "0123456789abcdef.json"
    assert written.payload["schema_version"] == 1
    assert written.payload["created_at"] == "2026-05-07T00:00:00Z"
    assert written.payload["expires_at"] == "2026-05-07T00:05:00Z"
    assert written.path.stat().st_mode & 0o777 == 0o600
    assert written.path.parent.stat().st_mode & 0o777 == 0o700

    try:
        prep.write_prep_file(
            {"origin_session_id": "session-collision", "origin_repo": str(tmp_path)},
            root=root,
            token="0123456789abcdef",
            now=prep.parse_timestamp("2026-05-07T00:00:10Z"),
            ttl_seconds=300,
        )
    except prep.PrepFileError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected explicit token collision to fail")
    assert json.loads(written.path.read_text(encoding="utf-8"))["origin_session_id"] == "session-a"

    stale = prep.write_prep_file(
        {"origin_session_id": "old-session", "origin_repo": str(tmp_path)},
        root=root,
        token="bbbbbbbbbbbbbbbb",
        now=prep.parse_timestamp("2026-05-07T00:00:00Z"),
        ttl_seconds=1,
    )
    reused = prep.write_prep_file(
        {"origin_session_id": "new-session", "origin_repo": str(tmp_path)},
        root=root,
        token="bbbbbbbbbbbbbbbb",
        now=prep.parse_timestamp("2026-05-07T00:00:02Z"),
        ttl_seconds=1000,
    )
    assert reused.path == stale.path
    assert reused.payload["origin_session_id"] == "new-session"

    existing_generated = prep.write_prep_file(
        {"origin_session_id": "generated-existing", "origin_repo": str(tmp_path)},
        root=root,
        token="ffffffffffffffff",
        now=now,
        ttl_seconds=1000,
    )
    original_generate_token = prep.generate_token
    generated_tokens = iter(["ffffffffffffffff", "eeeeeeeeeeeeeeee"])
    try:
        prep.generate_token = lambda: next(generated_tokens)
        generated = prep.write_prep_file(
            {"origin_session_id": "generated-retry", "origin_repo": str(tmp_path)},
            root=root,
            now=now,
            ttl_seconds=1000,
        )
    finally:
        prep.generate_token = original_generate_token
    assert existing_generated.path.exists()
    assert generated.token == "eeeeeeeeeeeeeeee"
    assert generated.payload["origin_session_id"] == "generated-retry"

    valid = prep.read_prep_file(
        "0123456789abcdef",
        root=root,
        now=prep.parse_timestamp("2026-05-07T00:01:00Z"),
    )
    assert valid.status == "valid"
    assert valid.payload["origin_session_id"] == "session-a"

    updated = prep.update_prep_file(
        written,
        {
            "target_worktree": str(tmp_path / "task-worktree"),
            "target_kanban_task_id": "task-123",
        },
    )
    assert updated.path == written.path
    assert updated.payload["created_at"] == "2026-05-07T00:00:00Z"
    assert updated.payload["expires_at"] == "2026-05-07T00:05:00Z"
    assert updated.payload["target_worktree"] == str(tmp_path / "task-worktree")
    valid_updated = prep.read_prep_file(
        "0123456789abcdef",
        root=root,
        now=prep.parse_timestamp("2026-05-07T00:01:00Z"),
    )
    assert valid_updated.status == "valid"
    assert valid_updated.payload["target_kanban_task_id"] == "task-123"

    for protected_key in ("created_at", "expires_at", "token", "schema_version"):
        try:
            prep.update_prep_file(updated, {protected_key: "bad"})
        except prep.PrepFileError as exc:
            assert protected_key in str(exc)
        else:
            raise AssertionError(f"expected PrepFileError for protected field {protected_key}")

    expired = prep.read_prep_file(
        "0123456789abcdef",
        root=root,
        now=prep.parse_timestamp("2026-05-07T00:06:00Z"),
    )
    assert expired.status == "expired"

    removed = prep.sweep_stale(
        root=root,
        now=prep.parse_timestamp("2026-05-07T00:06:00Z"),
    )
    assert removed == [written.path]
    assert not written.path.exists()


def test_rvf_prep_file_revive_expired_restamps_ttl(tmp_path: Path) -> None:
    """FU-1：写 prep → now 越过 expiry（read 为 expired 但带回 payload）→ revive 续期 →
    再 read 为 valid，且非时间戳字段原样保留；缺 payload 的 lookup 不可 revive。"""
    prep = load_rvf_prep_file_module()
    root = tmp_path / "prep-root"
    t0 = prep.parse_timestamp("2026-05-07T00:00:00Z")
    written = prep.write_prep_file(
        {
            "origin_session_id": "session-revive",
            "rvf_run": {"run_id": "rvf-revive", "run_dir": str(tmp_path / "run")},
        },
        root=root,
        token="0123456789abcdef",
        now=t0,
        ttl_seconds=300,
    )
    after = prep.parse_timestamp("2026-05-07T00:10:00Z")  # 越过 00:05:00 expiry
    expired = prep.read_prep_file(written.token, root=root, now=after)
    assert expired.status == "expired"
    assert expired.payload is not None  # read 对 expired 仍带回 payload

    revived = prep.revive_prep_file(expired, root=root, now=after, ttl_seconds=300)
    assert revived.path == written.path  # 就地重写同一 token 文件
    assert revived.payload["created_at"] == "2026-05-07T00:10:00Z"
    assert revived.payload["expires_at"] == "2026-05-07T00:15:00Z"
    assert revived.payload["origin_session_id"] == "session-revive"  # 非时间戳字段保留
    assert revived.payload["rvf_run"]["run_id"] == "rvf-revive"

    again = prep.read_prep_file(
        written.token, root=root, now=prep.parse_timestamp("2026-05-07T00:12:00Z")
    )
    assert again.status == "valid", again  # 续期后回到 valid

    missing = prep.read_prep_file("aaaaaaaaaaaaaaaa", root=root, now=after)
    assert missing.status == "missing" and missing.payload is None
    try:
        prep.revive_prep_file(missing, root=root, now=after)
    except prep.PrepFileError:
        pass
    else:
        raise AssertionError("expected revive of payload-less lookup to fail")


def test_kanban_followup_marker_family_roots_follow_rvf_home_when_no_dedicated_root_is_set(
    tmp_path: Path,
) -> None:
    """F5·H5b：三族 marker 的落盘根必须和 RVF_HOME 家族其余成员一样认 ``$RVF_HOME``。

    本文件在模块导入期把 ``RVF_HOME`` 强制指向 tmp，注释里承诺的解析链是「显式参数 > 专用
    env > ``$RVF_HOME`` > ``~/.rvf``」——但这三族此前跳过了 ``$RVF_HOME`` 一档，于是
    ``rvf_agent_decision_cli.run_start`` 这类不传 ``lock_root`` 的调用（cad8824 引入的 bootstrap
    agent CLI 测试）把 in-progress 锁写进了开发者真实的 ``~/.rvf/kanban-followup-in-progress/``
    （2026-09-30 实测 872 个里 854 个是测试残留，当天仍在写）。那个目录正是 Stop 读侧判断
    「有没有 RVF 在途」的地方。
    """
    k = load_kanban_followup_lock_module()
    rvf_home = tmp_path / "isolated-rvf-home"
    saved = {name: os.environ.get(name) for name in ("RVF_HOME", k.LOCK_ROOT_ENV)}
    try:
        os.environ["RVF_HOME"] = str(rvf_home)
        os.environ.pop(k.LOCK_ROOT_ENV, None)
        assert k._root() == rvf_home / k.SUBDIR_NAME
        assert k._pending_root() == rvf_home / k.PENDING_SUBDIR_NAME
        assert k._decision_point_delivery_intent_root() == (
            rvf_home / k.DECISION_POINT_DELIVERY_INTENT_SUBDIR_NAME
        )
        # 专用 env 仍然优先于 RVF_HOME（既有的测试 / 生产覆盖手段不受影响）。
        dedicated = tmp_path / "dedicated-lock-root"
        os.environ[k.LOCK_ROOT_ENV] = str(dedicated)
        assert k._root() == dedicated
        assert k._pending_root() == dedicated / k.PENDING_SUBDIR_NAME
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_kanban_followup_pending_marker_round_trip(tmp_path: Path) -> None:
    """dispatched-unconfirmed(pending) marker：write/read/clear(token 防误清)/stale/与 in-progress 物理隔离。

    pending 记录「dispatch 已发、尚未确认落地」；UPS arm 落地时按 token 清，超时则下次 Stop 据其重投。
    """
    k = load_kanban_followup_lock_module()
    root = tmp_path / "lock-root"
    path = k.write_pending_marker(
        task_id="bf042",
        session_id="s1",
        run_id="run-x",
        run_dir=str(tmp_path / "run-x"),
        repo="/repo",
        cwd="/repo",
        token="deadbeefdeadbeef",
        delivery_channel="terminal",
        message_id="terminal:bf042:run-x",
        root=root,
    )
    assert path is not None and path.exists()
    # 与 in-progress marker 物理隔离：pending 落在独立子目录，不会同名互相覆盖。
    assert path.parent.name == k.PENDING_SUBDIR_NAME
    marker = k.read_pending_marker(task_id="bf042", root=root)
    assert marker is not None
    assert marker["state"] == k.PENDING_STATE == "dispatched_unconfirmed"
    assert marker["token"] == "deadbeefdeadbeef"
    assert marker["delivery_channel"] == "terminal"
    assert k.pending_status(marker) == k.STATUS_ACTIVE
    # token 防误清：一条迟到的旧投递确认（不同 token）不得清掉这把新 pending。
    assert k.clear_pending_marker(task_id="bf042", token="other", root=root) == []
    assert k.read_pending_marker(task_id="bf042", root=root) is not None
    # 正确 token 清掉。
    removed = k.clear_pending_marker(task_id="bf042", token="deadbeefdeadbeef", root=root)
    assert removed == [str(path)]
    assert not path.exists()
    assert k.read_pending_marker(task_id="bf042", root=root) is None
    # stale 判定：TTL=0 → 立即过期。
    prev_ttl = os.environ.get(k.PENDING_TTL_ENV)
    os.environ[k.PENDING_TTL_ENV] = "0"
    try:
        k.write_pending_marker(
            task_id="t2",
            session_id=None,
            run_id="r",
            run_dir="d",
            repo=None,
            cwd=None,
            token="t",
            delivery_channel="terminal",
            root=root,
        )
        stale = k.read_pending_marker(task_id="t2", root=root)
        assert k.pending_status(stale) == k.STATUS_STALE
    finally:
        if prev_ttl is None:
            os.environ.pop(k.PENDING_TTL_ENV, None)
        else:
            os.environ[k.PENDING_TTL_ENV] = prev_ttl


def test_kanban_followup_iter_pending_and_stamp_notified(tmp_path: Path) -> None:
    """S0/S0b：pending 新快照字段 + iter_pending_markers 跨 task 枚举 + stamp_pending_notified 保 token。

    stamp 必须 read-merge-write 且**原样保留 token**，否则会破坏 clear_pending_marker 的 token
    防误清 guard（一条迟到的旧投递确认会因 token 不再匹配而无法清掉 marker → 永久误锁）。
    """
    k = load_kanban_followup_lock_module()
    root = tmp_path / "lock-root"
    pa = k.write_pending_marker(
        task_id="taskA", session_id="sA", run_id="rA", run_dir=str(tmp_path / "rA"),
        repo="/repo", cwd="/repo", token="aaaaaaaaaaaaaaaa", delivery_channel="terminal",
        kanban_project_path="/repo", kanban_task_title="标题A",
        kanban_task_title_source="cline_kanban_task_env",
        origin_transcript_path=str(tmp_path / "tA.jsonl"), root=root,
    )
    pb = k.write_pending_marker(
        task_id="taskB", session_id="sB", run_id="rB", run_dir=str(tmp_path / "rB"),
        repo="/repo2", cwd="/repo2", token="bbbbbbbbbbbbbbbb", delivery_channel="terminal",
        kanban_project_path="/repo2", kanban_task_title="标题B",
        origin_transcript_path=None, root=root,
    )
    assert pa and pb
    # S0 快照字段持久化。
    ma = k.read_pending_marker(task_id="taskA", root=root)
    assert ma["kanban_project_path"] == "/repo"
    assert ma["kanban_task_title"] == "标题A"
    assert ma["kanban_task_title_source"] == "cline_kanban_task_env"
    assert ma["origin_transcript_path"] == str(tmp_path / "tA.jsonl")
    # iter 枚举两个 task（顺序无关），每个 payload 注入 _marker_path。
    markers = k.iter_pending_markers(root=root)
    assert {m.get("kanban_task_id") for m in markers} == {"taskA", "taskB"}
    assert all(m.get("_marker_path") for m in markers)
    # stamp 盖 last_notified_at 且保留 token。
    assert k.stamp_pending_notified(task_id="taskA", token="aaaaaaaaaaaaaaaa", root=root) is True
    after = k.read_pending_marker(task_id="taskA", root=root)
    assert after.get("last_notified_at")
    assert after.get("token") == "aaaaaaaaaaaaaaaa"
    assert int(after.get("notify_count")) == 1
    # 错误 token 不盖戳（notify_count 不变）。
    assert k.stamp_pending_notified(task_id="taskA", token="ffffffffffffffff", root=root) is False
    assert int(k.read_pending_marker(task_id="taskA", root=root).get("notify_count")) == 1
    # 关键回归：stamp 之后用原 token 仍能清掉（token guard 未被破坏）。
    removed = k.clear_pending_marker(task_id="taskA", token="aaaaaaaaaaaaaaaa", root=root)
    assert removed == [str(pa)]
    assert k.read_pending_marker(task_id="taskA", root=root) is None
    # 缺失 marker → stamp 返回 False、不抛。
    assert k.stamp_pending_notified(task_id="taskA", token="aaaaaaaaaaaaaaaa", root=root) is False
    # iter 在空/不存在目录上返回 []（不抛）。
    assert k.iter_pending_markers(root=tmp_path / "nonexistent") == []


def test_awaiting_dispatched_agent_marker_arm_iter_clear_round_trip(tmp_path: Path) -> None:
    """wait-on 派发登记 marker：arm → iter 命中 → clear → 不再命中；一个 session 多条派发并存。

    这是 RVF 内外统一 shield 的 writer/reader 契约单测：dispatch 层 arm、Stop hook 的 reader
    （iter_active_awaiting_for_session）查「本 session 是否还有未完成 wait-on 派发」。
    """
    a = load_awaiting_dispatched_agent_marker_module()
    root = tmp_path / "lock-root"
    path = a.arm_awaiting_dispatched_agent(
        main_session_id="sess-X",
        dispatched_agent_id="bg-impl-1",
        dispatcher="delegate-to-cursor",
        description="后台实现 agent",
        repo="/repo",
        cwd="/repo",
        root=root,
    )
    assert path is not None and path.exists()
    # 物理隔离：落在独立子目录，不与 in-progress / pending marker 同名互相覆盖。
    assert path.parent.name == a.SUBDIR_NAME == "awaiting-dispatched-agent"
    active = a.iter_active_awaiting_for_session("sess-X", root=root)
    assert len(active) == 1
    marker = active[0]
    assert marker["state"] == a.AWAITING_STATE == "awaiting_dispatched_agent"
    assert marker["wait_on"] is True
    assert marker["dispatched_agent_id"] == "bg-impl-1"
    assert marker["dispatcher"] == "delegate-to-cursor"
    assert marker.get("expires_at")
    assert marker.get("_marker_path")
    # 一个 session 可有多条未完成 wait-on 派发：第二条并存。
    a.arm_awaiting_dispatched_agent(
        main_session_id="sess-X", dispatched_agent_id="bg-impl-2", root=root
    )
    ids = {m["dispatched_agent_id"] for m in a.iter_active_awaiting_for_session("sess-X", root=root)}
    assert ids == {"bg-impl-1", "bg-impl-2"}
    # 另一个 session 的 marker 不串台。
    a.arm_awaiting_dispatched_agent(
        main_session_id="sess-Y", dispatched_agent_id="other", root=root
    )
    assert {m["dispatched_agent_id"] for m in a.iter_active_awaiting_for_session("sess-X", root=root)} == {
        "bg-impl-1",
        "bg-impl-2",
    }
    # clear 一条，另一条仍在。
    removed = a.clear_awaiting_dispatched_agent(
        main_session_id="sess-X", dispatched_agent_id="bg-impl-1", root=root
    )
    assert removed == [str(path)]
    assert {m["dispatched_agent_id"] for m in a.iter_active_awaiting_for_session("sess-X", root=root)} == {
        "bg-impl-2"
    }
    # 缺 session/dispatch-id → no-op（不抛）。
    assert a.arm_awaiting_dispatched_agent(main_session_id=None, dispatched_agent_id="x", root=root) is None
    assert a.clear_awaiting_dispatched_agent(main_session_id="sess-X", dispatched_agent_id="absent", root=root) == []
    # iter 在不存在目录上返回 []（不抛）。
    assert a.iter_active_awaiting_for_session("sess-X", root=tmp_path / "nonexistent") == []


def test_awaiting_dispatched_agent_marker_stale_is_lazily_cleared(tmp_path: Path) -> None:
    """STALE（TTL 过期）的 wait-on marker：iter 不计入「还在等」(→ Stop 放行)，并惰性删除该文件。

    这把「writer 忘 clear / 后台 agent 静默死」的 masking 上界钉死在 TTL：过期即不再挡停。
    """
    a = load_awaiting_dispatched_agent_marker_module()
    root = tmp_path / "lock-root"
    path = a.arm_awaiting_dispatched_agent(
        main_session_id="sess-stale",
        dispatched_agent_id="bg-dead",
        ttl_seconds_override=0,  # expires_at = arm 时刻 → 检查时必已过期
        root=root,
    )
    assert path is not None and path.exists()
    # iter 不返回它（STALE 不计入 active），且顺手把过期文件惰性清掉。
    assert a.iter_active_awaiting_for_session("sess-stale", root=root) == []
    assert not Path(path).exists()


def test_awaiting_dispatched_agent_marker_cli_round_trip(tmp_path: Path) -> None:
    """CLI helper（dispatcher 接入点）：subprocess 跑 arm → list(1) → clear → list(0)。"""
    root = tmp_path / "cli-root"
    env = dict(os.environ)
    env["RVF_KANBAN_FOLLOWUP_LOCK_ROOT"] = str(root)

    def cli(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(AWAITING_DISPATCHED_AGENT_MARKER), *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(SCRIPT_DIR),
        )

    armed = cli(
        "arm", "--session", "cli-sess", "--dispatch-id", "cli-bg",
        "--dispatcher", "delegate-to-cursor", "--description", "后台 agent",
    )
    assert armed.returncode == 0, armed.stderr
    assert Path(armed.stdout.strip()).exists()
    listed = cli("list", "--session", "cli-sess")
    assert listed.returncode == 0, listed.stderr
    active = json.loads(listed.stdout)
    assert [m["dispatched_agent_id"] for m in active] == ["cli-bg"]
    cleared = cli("clear", "--session", "cli-sess", "--dispatch-id", "cli-bg")
    assert cleared.returncode == 0, cleared.stderr
    assert json.loads(cleared.stdout)  # 返回被删路径列表（非空）
    listed_after = cli("list", "--session", "cli-sess")
    assert json.loads(listed_after.stdout) == []


def test_notify_kanban_followup_stranded(tmp_path: Path) -> None:
    """S1a/S1b 共用 OS 通知：命令含 -open <taskUrl> + task 标题 + 按 task 合并 group；
    缺 notifier / 非 darwin 返回显式 reason 且不抛。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    mod = _load_rvf_handoff_module()
    notifier_log = tmp_path / "notify.log"
    notifier = _write_fake_notifier(tmp_path / "fake_notifier.py", notifier_log)
    saved_bin = os.environ.get(mod.TERMINAL_NOTIFIER_BIN_ENV)
    os.environ[mod.TERMINAL_NOTIFIER_BIN_ENV] = str(notifier)
    try:
        result = mod.notify_kanban_followup_stranded(
            task_id="4fc83",
            task_title="修复登录",
            task_url="http://127.0.0.1:3484/repo?task=4fc83",
            reason="stranded-escalated",
        )
        assert result["notified"] is True
        calls = [
            json.loads(line)
            for line in notifier_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(calls) == 1
        argv = calls[0]
        assert argv[argv.index("-open") + 1] == "http://127.0.0.1:3484/repo?task=4fc83"
        msg = argv[argv.index("-message") + 1]
        assert "4fc83" in msg and "修复登录" in msg
        group = argv[argv.index("-group") + 1]
        assert group.startswith("rvf-followup-")
    finally:
        if saved_bin is None:
            os.environ.pop(mod.TERMINAL_NOTIFIER_BIN_ENV, None)
        else:
            os.environ[mod.TERMINAL_NOTIFIER_BIN_ENV] = saved_bin
    # 缺 notifier：非 darwin → unsupported-platform；darwin → terminal-notifier-missing；均不抛。
    saved_which = mod.shutil.which
    saved_bin2 = os.environ.pop(mod.TERMINAL_NOTIFIER_BIN_ENV, None)
    try:
        mod.shutil.which = lambda name: None
        r2 = mod.notify_kanban_followup_stranded(
            task_id="t", task_title=None, task_url=None, reason="dispatched-unconfirmed"
        )
        assert r2["notified"] is False
        assert r2["reason"] in {"terminal-notifier-missing", "unsupported-platform"}
    finally:
        mod.shutil.which = saved_which
        if saved_bin2 is not None:
            os.environ[mod.TERMINAL_NOTIFIER_BIN_ENV] = saved_bin2


def test_parse_manual_scope_directive_variants() -> None:
    """`scope:` 指令解析：空白/逗号分隔、去引号、大小写不敏感、行内/跨行、无 scope。"""
    submit = load_rvf_user_prompt_submit_module()
    parse = submit.parse_manual_scope_directive
    assert parse("/review-validate-fix scope: src/a.py src/b.py") == ["src/a.py", "src/b.py"]
    assert parse("/review-validate-fix scope: src/a.py, src/b.py") == ["src/a.py", "src/b.py"]
    assert parse('/review-validate-fix scope: "src/a.py"') == ["src/a.py"]
    assert parse("/review-validate-fix SCOPE: src/a.py") == ["src/a.py"]
    assert parse("/review-validate-fix\nscope: src/a.py") == ["src/a.py"]
    assert parse("/review-validate-fix please review my work") == []
    # `telescope:` 之类的子串不得被当成指令（要求行首或空白前缀）。
    assert parse("just talking about telescope: lens here") == []
    assert parse("") == []
    assert parse(None) == []


def test_claude_plugin_hooks_declare_user_prompt_submit() -> None:
    """Option C: the Claude plugin's own hooks/hooks.json must declare both
    Stop and UserPromptSubmit (no installer / settings.json mutation needed),
    and the UserPromptSubmit shim must exist and compile."""
    import py_compile

    hooks_json = ROOT / "plugins" / "review-validate-fix" / "hooks" / "hooks.json"
    data = json.loads(hooks_json.read_text(encoding="utf-8"))
    hooks = data["hooks"]
    assert "Stop" in hooks, "Stop hook regression"

    ups_groups = hooks.get("UserPromptSubmit")
    assert isinstance(ups_groups, list) and ups_groups, "UserPromptSubmit not declared"
    commands = [
        entry.get("command")
        for group in ups_groups
        for entry in group.get("hooks", [])
    ]
    assert any(
        isinstance(cmd, str)
        and "${CLAUDE_PLUGIN_ROOT}" in cmd
        and "hooks/user_prompt_submit.py" in cmd
        for cmd in commands
    ), f"UserPromptSubmit command not wired to the shim: {commands}"

    stop_groups = hooks.get("Stop")
    assert isinstance(stop_groups, list) and stop_groups, "Stop not declared"
    stop_commands = [
        entry.get("command")
        for group in stop_groups
        for entry in group.get("hooks", [])
    ]
    assert any(
        isinstance(cmd, str)
        and "${CLAUDE_PLUGIN_ROOT}" in cmd
        and "hooks/stop.py" in cmd
        for cmd in stop_commands
    ), f"Stop command not wired to the shim: {stop_commands}"

    # 两入口 shim + S3 单一契约 sibling 均须存在且可编译。
    hooks_dir = ROOT / "plugins" / "review-validate-fix" / "hooks"
    for name in ("user_prompt_submit.py", "stop.py", "_claude_hook_entry.py"):
        shim = hooks_dir / name
        assert shim.is_file(), f"hooks/{name} missing"
        py_compile.compile(str(shim), doraise=True)


def test_claude_plugin_hooks_declare_post_tool_use_with_write_matcher() -> None:
    """park 的 race-free 落点：hooks.json 必须声明一个带写型工具 matcher 的 PostToolUse
    条目，wired 到 post_tool_use.py shim；且 shim + 核心脚本存在可编译。"""
    import py_compile

    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    groups = data["hooks"].get("PostToolUse")
    assert isinstance(groups, list) and groups, "PostToolUse not declared"
    matched = [
        group
        for group in groups
        if group.get("matcher") == "Edit|Write|MultiEdit|NotebookEdit"
        and any(
            isinstance(entry.get("command"), str)
            and "${CLAUDE_PLUGIN_ROOT}" in entry["command"]
            and "hooks/post_tool_use.py" in entry["command"]
            for entry in group.get("hooks", [])
        )
    ]
    assert matched, f"PostToolUse not wired with write-tool matcher: {groups}"

    hooks_dir = ROOT / "plugins" / "review-validate-fix" / "hooks"
    shim = hooks_dir / "post_tool_use.py"
    assert shim.is_file(), "hooks/post_tool_use.py missing"
    py_compile.compile(str(shim), doraise=True)
    py_compile.compile(str(RVF_POST_TOOL_USE), doraise=True)


def _write_fake_park_client(tmp_path: Path) -> tuple[Path, Path]:
    """假 cline_kanban_client：记录 argv 到 calls.jsonl、对任意 action 回 {ok:true}。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake = tmp_path / "fake_client.py"
    calls = tmp_path / "client_calls.jsonl"
    fake.write_text(
        "import json, sys\n"
        f"calls = {json.dumps(str(calls))}\n"
        "with open(calls, 'a', encoding='utf-8') as h:\n"
        "    h.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'ok': True, 'parked': True}))\n",
        encoding="utf-8",
    )
    return fake, calls


_PARK_ENV_KEYS = (
    "KANBAN_TASK_ID",
    "CLINE_KANBAN_TASK_ID",
    "KANBAN_HOOK_TASK_ID",
    "KANBAN_PROJECT_PATH",
    "CLINE_KANBAN_PROJECT_PATH",
    "CODEX_RVF_CLINE_KANBAN_CLIENT",
    "CODEX_RVF_CLINE_KANBAN_TASK_CMD",
    "CODEX_RVF_KANBAN_PARK_STATE_ROOT",
)


def _save_park_env() -> dict[str, str | None]:
    return {k: os.environ.pop(k, None) for k in _PARK_ENV_KEYS}


def _restore_park_env(saved: dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_cline_kanban_client_session_field_readers() -> None:
    """task_session_state / task_session_exit_code / task_payload_from_list：从 kanban task 的
    session 子对象稳健取值（搁浅判定真相源；缺失/非法类型 → None，exitCode str 数字 → int）。"""
    mod = load_cline_kanban_client_module()
    # state：取字符串；空白 / 缺失 / 非 dict session → None。
    assert mod.task_session_state({"session": {"state": "awaiting_review"}}) == "awaiting_review"
    assert mod.task_session_state({"session": {"state": "  "}}) is None
    assert mod.task_session_state({"session": {}}) is None
    assert mod.task_session_state({"session": None}) is None
    assert mod.task_session_state({}) is None
    # exitCode：int 原样；str 数字 → int；bool / 非数字 / 缺失 → None（bool 不当作退出码）。
    assert mod.task_session_exit_code({"session": {"exitCode": 0}}) == 0
    assert mod.task_session_exit_code({"session": {"exitCode": 137}}) == 137
    assert mod.task_session_exit_code({"session": {"exitCode": "137"}}) == 137
    assert mod.task_session_exit_code({"session": {"exitCode": None}}) is None
    assert mod.task_session_exit_code({"session": {"exitCode": True}}) is None
    assert mod.task_session_exit_code({"session": {"exitCode": "oops"}}) is None
    assert mod.task_session_exit_code({"session": {}}) is None
    assert mod.task_session_exit_code({}) is None
    # task_payload_from_list：按 id 命中；未命中 / tasks 非 list → None。
    payload = {"tasks": [{"id": "x", "session": {"state": "running"}}]}
    assert mod.task_payload_from_list(payload, "x")["id"] == "x"
    assert mod.task_payload_from_list(payload, "y") is None
    assert mod.task_payload_from_list({"tasks": "bad"}, "x") is None


def test_cline_kanban_client_park_unpark_is_parked(tmp_path: Path) -> None:
    """client 新增三 verb：park（带 --label）/ unpark / is-parked，argv 路由正确 + JSON 透传。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake_task = tmp_path / "fake_kanban_task.py"
    calls = tmp_path / "calls.jsonl"
    fake_task.write_text(
        "import json, os, sys\n"
        "with open(os.environ['KANBAN_CALLS'], 'a', encoding='utf-8') as h:\n"
        "    h.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "action = sys.argv[1]\n"
        "tid = sys.argv[sys.argv.index('--task-id') + 1]\n"
        "out = {'ok': True, 'taskId': tid}\n"
        "out['parked'] = action != 'unpark'\n"
        "print(json.dumps(out))\n",
        encoding="utf-8",
    )
    repo = init_repo(tmp_path / "repo")
    env = os.environ.copy()
    env["KANBAN_CALLS"] = str(calls)
    task_cmd = f"{sys.executable} {fake_task}"

    def _client(action: str, *extra: str) -> dict:
        completed = run(
            [
                sys.executable,
                str(CLINE_KANBAN_CLIENT),
                action,
                "--repo",
                str(repo),
                "--task-cmd",
                task_cmd,
                "--task-id",
                "task-1",
                *extra,
            ],
            env=env,
        )
        return json.loads(completed.stdout)

    park_payload = _client("park", "--label", "self-rising")
    assert park_payload["task_id"] == "task-1"
    assert park_payload["parked"] is True
    unpark_payload = _client("unpark")
    assert unpark_payload["parked"] is False
    is_parked_payload = _client("is-parked")
    assert is_parked_payload["task_id"] == "task-1"

    recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert recorded[0][0] == "park"
    assert "--label" in recorded[0] and recorded[0][recorded[0].index("--label") + 1] == "self-rising"
    assert recorded[1][0] == "unpark"
    assert recorded[2][0] == "is-parked"
    for call in recorded:
        assert call[call.index("--task-id") + 1] == "task-1"
        # client 对 --repo 做 .resolve()（macOS /var→/private/var），故比较解析后路径。
        assert call[call.index("--project-path") + 1] == str(Path(repo).resolve())


def test_post_tool_use_parks_eligible_edit_turn(tmp_path: Path) -> None:
    """eligible 回合 + 写型工具 → park 一次；同 nonce 第二次编辑 → 不重复 park。"""
    saved = _save_park_env()
    try:
        park = load_rvf_post_tool_use_module()
        root = tmp_path / "state"
        fake, calls = _write_fake_park_client(tmp_path / "fake")
        os.environ["CODEX_RVF_CLINE_KANBAN_CLIENT"] = str(fake)

        nonce = park.mark_park_eligibility("sess-1", eligible=True, root=root)
        assert nonce
        event = {
            "session_id": "sess-1",
            "tool_name": "Edit",
            "task_id": "task-1",
            "project_path": str(tmp_path),
            "cwd": str(tmp_path),
        }
        first = park.inspect_post_tool_use(event, root=root)
        assert first["status"] == "parked"
        assert park.read_self_park_state("sess-1", root=root)["nonce"] == nonce
        # 同 nonce 第二次编辑：不重复 park。
        second = park.inspect_post_tool_use(event, root=root)
        assert second["status"] == "noop"
        recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
        assert len(recorded) == 1
        assert recorded[0][0] == "park"
        assert recorded[0][recorded[0].index("--task-id") + 1] == "task-1"
    finally:
        _restore_park_env(saved)


def test_post_tool_use_skips_token_turn(tmp_path: Path) -> None:
    """eligible=false（Turn 2 token 回合 / 终态待审）→ 绝不 park。"""
    saved = _save_park_env()
    try:
        park = load_rvf_post_tool_use_module()
        root = tmp_path / "state"
        fake, calls = _write_fake_park_client(tmp_path / "fake")
        os.environ["CODEX_RVF_CLINE_KANBAN_CLIENT"] = str(fake)
        park.mark_park_eligibility("sess-1", eligible=False, root=root)
        event = {"session_id": "sess-1", "tool_name": "Write", "task_id": "task-1", "cwd": str(tmp_path)}
        result = park.inspect_post_tool_use(event, root=root)
        assert result["status"] == "skipped" and result["reason"] == "not_eligible"
        assert not calls.exists()
        assert park.read_self_park_state("sess-1", root=root) is None
    finally:
        _restore_park_env(saved)


def test_post_tool_use_skips_non_edit_or_non_kanban(tmp_path: Path) -> None:
    """非写型工具 / 不在 kanban task → noop（保热路径开销 + 不误 park）。"""
    saved = _save_park_env()
    try:
        park = load_rvf_post_tool_use_module()
        root = tmp_path / "state"
        fake, calls = _write_fake_park_client(tmp_path / "fake")
        os.environ["CODEX_RVF_CLINE_KANBAN_CLIENT"] = str(fake)
        park.mark_park_eligibility("sess-1", eligible=True, root=root)
        # 非 kanban task（无 task id，env 已清）→ 早退。
        non_kanban = park.inspect_post_tool_use(
            {"session_id": "sess-1", "tool_name": "Edit", "cwd": str(tmp_path)}, root=root
        )
        assert non_kanban["reason"] == "not_kanban_task"
        # 非写型工具 → 早退。
        non_edit = park.inspect_post_tool_use(
            {"session_id": "sess-1", "tool_name": "Read", "task_id": "task-1", "cwd": str(tmp_path)},
            root=root,
        )
        assert non_edit["reason"] == "not_write_tool"
        assert not calls.exists()
    finally:
        _restore_park_env(saved)


def test_user_prompt_submit_marks_park_eligibility(tmp_path: Path) -> None:
    """UPS 在回合开头标 park 资格：人类 kanban 回合 eligible=true；token 回合 eligible=false。"""
    saved = _save_park_env()
    try:
        submit = load_rvf_user_prompt_submit_module()
        park = load_rvf_post_tool_use_module()
        root = tmp_path / "state"
        os.environ["CODEX_RVF_KANBAN_PARK_STATE_ROOT"] = str(root)
        prep_root = tmp_path / "prep"

        # 人类 kanban 实现回合（无 token/marker/manual，且在 kanban task）→ eligible=true。
        submit.inspect_user_prompt_submit(
            {"session_id": "sess-human", "task_id": "task-1", "cwd": str(tmp_path), "prompt": "fix the bug"},
            prep_root=prep_root,
        )
        human = park.read_park_eligibility("sess-human")
        assert human is not None and human["eligible"] is True

        # token 回合（被注入 followup 唤回，终态待审）→ eligible=false。
        submit.inspect_user_prompt_submit(
            {
                "session_id": "sess-token",
                "task_id": "task-1",
                "cwd": str(tmp_path),
                "prompt": "RVF_DISPATCH=token=deadbeefdeadbeef resume",
            },
            prep_root=prep_root,
        )
        token_turn = park.read_park_eligibility("sess-token")
        assert token_turn is not None and token_turn["eligible"] is False
    finally:
        _restore_park_env(saved)


def _load_claude_hook_entry_module():
    """以模块方式加载 hooks/_claude_hook_entry.py（两 shim 共享的单一契约），
    对 ``is_foreign_invocation`` / ``run_claude_hook`` 做单元测试。该模块
    stdlib-only、无 sibling 依赖，故 ``spec_from_file_location`` 直接可加载。"""
    import importlib.util

    entry_path = (
        ROOT / "plugins" / "review-validate-fix" / "hooks" / "_claude_hook_entry.py"
    )
    spec = importlib.util.spec_from_file_location("rvf_claude_hook_entry", entry_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_claude_hook_entry_detects_foreign_invocation() -> None:
    """``is_foreign_invocation`` 守卫（S3 单源化：原 stop.py / user_prompt_submit.py
    两份逐字复制的 ``_is_codex_invocation`` 收敛为本契约一处）：Codex 转写路径
    返回 True；Claude 转写路径 / 缺路径 / 非 str 值时返回 False（保守，未知按
    Claude 跑）。"""
    entry = _load_claude_hook_entry_module()

    assert entry.is_foreign_invocation(
        {"transcript_path": "/Users/me/.codex/sessions/2026/05/21/rollout-XYZ.jsonl"}
    ) is True
    assert entry.is_foreign_invocation(
        {"conversation_path": "/Users/me/.codex/sessions/anywhere/file.jsonl"}
    ) is True
    assert entry.is_foreign_invocation(
        {"session_path": "/Users/me/.codex/sessions/2026/05/file.jsonl"}
    ) is True
    assert entry.is_foreign_invocation(
        {"session_file": "/Users/me/.codex/sessions/some/leaf.jsonl"}
    ) is True
    assert entry.is_foreign_invocation(
        {"transcript_path": "/Users/me/.claude/projects/-encoded/session.jsonl"}
    ) is False
    # 缺所有路径键 → False（保守，按 Claude 跑）
    assert entry.is_foreign_invocation({}) is False
    # 路径键值非 str → False
    assert entry.is_foreign_invocation({"transcript_path": None}) is False
    assert entry.is_foreign_invocation({"transcript_path": 12345}) is False


def test_claude_hook_entry_dispatches_claude_invocation() -> None:
    """G 正向：Claude 调用（无 Codex 证据）经 ``run_claude_hook`` 恰好调起核心
    一次并 normalize event；foreign（Codex）调用则**不**调起核心——「每 host
    恰好处理一次」的两半。用替身替换 entry 模块的 subprocess 拦截，不触真
    subprocess、不真起核心。"""
    import io

    entry = _load_claude_hook_entry_module()

    class _Completed:
        stdout = '{"continue": true}'
        stderr = ""
        returncode = 0

    def _run_once(event: dict) -> list[dict]:
        calls: list[dict] = []

        class _FakeSubprocess:
            @staticmethod
            def run(argv, **kwargs):
                calls.append({"argv": argv, "input": kwargs.get("input")})
                return _Completed()

        saved_stdin = sys.stdin
        saved_stdout = sys.stdout
        saved_subprocess = entry.subprocess
        sys.stdin = io.StringIO(json.dumps(event))
        sys.stdout = io.StringIO()
        entry.subprocess = _FakeSubprocess
        try:
            rc = entry.run_claude_hook(
                event_name="Stop",
                core_script=(
                    "skills",
                    "review-validate-fix",
                    "scripts",
                    "stop_review_validate_fix.py",
                ),
                timeout_env="CLAUDE_RVF_STOP_HOOK_TIMEOUT",
                default_timeout="115",
                silent_success=False,
            )
        finally:
            sys.stdin = saved_stdin
            sys.stdout = saved_stdout
            entry.subprocess = saved_subprocess
        assert rc == 0
        return calls

    # Claude 调用：核心被调起恰好一次，且 forwarded event 已 normalize。
    calls = _run_once({"session_id": "claude-abc"})
    assert len(calls) == 1, "Claude invocation must dispatch the core exactly once"
    assert calls[0]["argv"][0] == sys.executable
    assert calls[0]["argv"][1].endswith("stop_review_validate_fix.py")
    forwarded = json.loads(calls[0]["input"])
    assert forwarded["hook_event_name"] == "Stop"
    assert forwarded["source"] == {"provider": "claude-code", "plugin": "review-validate-fix"}

    # Foreign（Codex）调用：守卫 no-op，核心**不**被调起。
    foreign_calls = _run_once(
        {"transcript_path": "/Users/me/.codex/sessions/2026/05/22/rollout-fake.jsonl"}
    )
    assert foreign_calls == [], "foreign (Codex) invocation must not dispatch the core"


def test_claude_plugin_shim_codex_invocation_noop(tmp_path: Path) -> None:
    """端到端：用 Codex 转写路径的 event 调 shim，应静默退出（stdout 空，
    退出码 0，且不写任何 prep file）。"""
    shim_path = ROOT / "plugins" / "review-validate-fix" / "hooks" / "user_prompt_submit.py"
    prep_root = tmp_path / "prep-root"
    prep_root.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "CODEX_RVF_PREP_ROOT": str(prep_root),
        "CODEX_RVF_LOG_ROOT": str(tmp_path / "log-root"),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
    }
    event = {
        "prompt": "/review-validate-fix",
        "session_id": "codex-session-abc",
        "cwd": str(tmp_path),
        "transcript_path": "/Users/bominzhang/.codex/sessions/2026/05/21/rollout-fake.jsonl",
    }
    completed = subprocess.run(
        [sys.executable, str(shim_path)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "", f"expected silent no-op, got stdout={completed.stdout!r}"
    # 无 prep file 写入（如果 core 跑了就会写）
    assert not any(prep_root.iterdir()), "prep root should be empty after Codex no-op"


def test_claude_plugin_stop_shim_codex_invocation_noop(tmp_path: Path) -> None:
    """端到端：用 Codex 转写路径的 event 调 Stop shim，应静默退出（stdout
    空，退出码 0，且不调起 RVF 核心）。如果核心被调起，CODEX_RVF_LOG_ROOT
    指向的 log 目录会有 run dir 写入；空目录证明守卫生效。"""
    shim_path = ROOT / "plugins" / "review-validate-fix" / "hooks" / "stop.py"
    log_root = tmp_path / "log-root"
    log_root.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "CODEX_RVF_LOG_ROOT": str(log_root),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
    }
    event = {
        "session_id": "codex-session-stop-abc",
        "cwd": str(tmp_path),
        "transcript_path": "/Users/bominzhang/.codex/sessions/2026/05/22/rollout-fake.jsonl",
    }
    completed = subprocess.run(
        [sys.executable, str(shim_path)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "", f"expected silent no-op, got stdout={completed.stdout!r}"
    # 守卫生效则核心未被调起，log_root 下不会创建任何 run 子目录
    assert not any(log_root.iterdir()), "log root should be empty after Codex no-op"


def test_prepare_run_from_prep_file_timeout_returns_immediately(tmp_path: Path) -> None:
    """Verify TimeoutError unblocks the hook even when the worker is still running.

    Regression: previous implementation used ``with ThreadPoolExecutor(...)``,
    whose ``__exit__`` defaults to ``wait=True``, so a timeout exception still
    blocked until the worker finished. The fix manages the executor manually
    and calls ``shutdown(wait=False, cancel_futures=True)`` on timeout.
    """

    prep = load_rvf_prep_file_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "prep-root"
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    run(["git", "add", "README.md"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)

    now = prep.parse_timestamp("2026-05-07T00:00:00Z")
    record = prep.write_prep_file(
        {
            "origin_session_id": "session-a",
            "origin_repo": str(repo),
            "origin_cwd": str(repo),
            "target_worktree": str(repo),
            "target_flow": "flow-manual",
            "rvf_run": {
                "run_id": "rvf-timeout-test",
                "run_dir": str(tmp_path / "rvf-state" / "runs" / "rvf-timeout-test"),
            },
        },
        root=root,
        token="dddddddddddddddd",
        now=now,
        ttl_seconds=3600,
    )

    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import importlib

    prepare_module = importlib.import_module("prepare_review_run")
    original_prepare_run = prepare_module.prepare_run
    original_boundary_gate = prepare_module.require_review_start_boundary_for_prepare
    original_run_binding = prepare_module.bind_review_start_boundary_to_one_run
    # 本用例只测 worker 超时，不测宿主；前置绑定仍保持真实接口的返回类型。
    prepare_module.require_review_start_boundary_for_prepare = lambda **kw: {"test_scope": "prepare-timeout"}
    prepare_module.bind_review_start_boundary_to_one_run = lambda receipt, run_id, run_dir: (run_id, Path(run_dir))

    def slow_prepare_run(**_: object) -> dict[str, object]:
        # Sleep well past our 1-second timeout to prove that the wrapper does
        # not block on shutdown.
        time.sleep(5.0)
        return {"run_id": "should-never-finish"}

    prepare_module.prepare_run = slow_prepare_run
    try:
        start = time.monotonic()
        state = prepare_module.prepare_run_from_prep_file(
            record,
            timeout_seconds=1.0,
            base_dir=tmp_path / "base",
        )
        elapsed = time.monotonic() - start
    finally:
        prepare_module.prepare_run = original_prepare_run
        prepare_module.require_review_start_boundary_for_prepare = original_boundary_gate
        prepare_module.bind_review_start_boundary_to_one_run = original_run_binding

    # Wall-clock must reflect the 1s timeout, not the 5s worker sleep. Allow
    # generous slack for slow CI but still well below the worker's 5s sleep.
    assert elapsed < 3.0, f"prepare_run_from_prep_file blocked for {elapsed:.2f}s"
    assert state["status"] == "timeout", state
    assert "1s timeout" in state["error"]

    # Prep file on disk must reflect the timeout state.
    stored = json.loads((root / "dddddddddddddddd.json").read_text(encoding="utf-8"))
    assert stored["rvf_run"]["shared_workflow_state"]["status"] == "timeout"


def _load_rvf_handoff_module():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import rvf_handoff

    return rvf_handoff


def _write_fake_notifier(path: Path, log: Path, *, fail: bool = False) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(log)!r}).open('a', encoding='utf-8')."
        "write(json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')\n"
        + ("sys.exit(5)\n" if fail else ""),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_rvf_handoff_cli_notify(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    notifier_log = tmp_path / "notify.log"
    notifier = _write_fake_notifier(tmp_path / "fake_notifier.py", notifier_log)
    env = {**os.environ, "RVF_TERMINAL_NOTIFIER_BIN": str(notifier)}

    completed = run(
        [
            sys.executable,
            str(RVF_HANDOFF),
            "notify",
            str(handoff),
            "--task-url",
            "http://127.0.0.1:3484/repo?task=t1",
            "--summary",
            "两个 reviewer 通过",
        ],
        env=env,
    )
    payload = json.loads(completed.stdout)

    assert payload["valid"] is True
    assert payload["notified"] is True
    assert payload["handoff_path"] == str(handoff.resolve())
    calls = [
        json.loads(line)
        for line in notifier_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(calls) == 1
    assert calls[0][calls[0].index("-message") + 1] == "两个 reviewer 通过"
    assert calls[0][calls[0].index("-open") + 1] == "http://127.0.0.1:3484/repo?task=t1"


def test_rvf_handoff_marker_tail_and_summary() -> None:
    mod = _load_rvf_handoff_module()
    text = "前置废话\nRVF_HANDOFF_FILE: /tmp/h.md\n两个 reviewer 通过；1 项已修复。"
    tail = mod.handoff_marker_tail(text)
    assert tail == "RVF_HANDOFF_FILE: /tmp/h.md\n两个 reviewer 通过；1 项已修复。"
    assert mod._notification_summary(tail) == "两个 reviewer 通过；1 项已修复。"
    # 多个 marker → 取最后一个起的尾段。
    multi = "RVF_HANDOFF_FILE: /a.md\n旧\nRVF_HANDOFF_FILE: /b.md\n新结论"
    assert mod.handoff_marker_tail(multi) == "RVF_HANDOFF_FILE: /b.md\n新结论"
    assert mod.handoff_marker_tail("没有 marker") is None


def test_rvf_handoff_resolve_kanban_task_url(tmp_path: Path) -> None:
    mod = _load_rvf_handoff_module()
    home = tmp_path / "home"
    index = home / ".cline" / "kanban" / "workspaces" / "index.json"
    index.parent.mkdir(parents=True)
    repo = "/Users/x/Documents/GitHub/review-validate-fix"
    index.write_text(
        json.dumps({"repoPathToId": {repo: "review-validate-fix"}}),
        encoding="utf-8",
    )
    saved_home = os.environ.get("HOME")
    saved_port = os.environ.pop("KANBAN_RUNTIME_PORT", None)
    os.environ["HOME"] = str(home)
    try:
        # index 命中。
        assert mod.workspace_id_for_repo(repo) == "review-validate-fix"
        assert (
            mod.resolve_kanban_task_url(repo, "task-1")
            == "http://127.0.0.1:3484/review-validate-fix?task=task-1"
        )
        # index 缺失 → kebab basename 兜底。
        assert mod.workspace_id_for_repo("/Users/x/Some-Other Repo") == "some-other-repo"
        # 缺 project_path / task_id → None。
        assert mod.resolve_kanban_task_url(None, "t") is None
        assert mod.resolve_kanban_task_url(repo, None) is None
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        if saved_port is not None:
            os.environ["KANBAN_RUNTIME_PORT"] = saved_port


def test_rvf_handoff_notify_requires_terminal_notifier(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    mod = _load_rvf_handoff_module()
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    saved_which = mod.shutil.which
    saved_bin = os.environ.pop(mod.TERMINAL_NOTIFIER_BIN_ENV, None)
    try:
        mod.shutil.which = lambda name: None
        result = mod.notify_handoff_ready(
            handoff_path=handoff, summary_text=None, task_url=None, group_ref="r"
        )
        assert result["notified"] is False
        # darwin 上缺二进制 → 显式 missing；非 darwin 且无 override → unsupported-platform。
        if sys.platform == "darwin":
            assert result["reason"] == "terminal-notifier-missing"
        else:
            assert result["reason"] == "unsupported-platform"
    finally:
        mod.shutil.which = saved_which
        if saved_bin is not None:
            os.environ[mod.TERMINAL_NOTIFIER_BIN_ENV] = saved_bin


def test_rvf_handoff_maybe_trigger_kanban_notification(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    mod = _load_rvf_handoff_module()
    ctx = {"kanban_task_id": "task-9"}
    saved = os.environ.pop(mod.KANBAN_NOTIFY_CMD_ENV, None)
    try:
        # 未配置 → 守护性 no-op。
        assert mod.maybe_trigger_kanban_notification(
            task_url="u", copy_text="c", kanban_context=ctx, project_path=None
        ) == {"triggered": False, "reason": "kanban-notify-not-configured"}
        # 配置后 → 运行命令，把 context 经 stdin(JSON) + env 传入。
        out = tmp_path / "trigger.json"
        recorder = tmp_path / "recorder.py"
        recorder.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            f"pathlib.Path({str(out)!r}).write_text("
            "sys.stdin.read() + '|' + os.environ.get('RVF_KANBAN_TASK_ID', ''), encoding='utf-8')\n",
            encoding="utf-8",
        )
        recorder.chmod(0o755)
        os.environ[mod.KANBAN_NOTIFY_CMD_ENV] = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(recorder))}"
        )
        result = mod.maybe_trigger_kanban_notification(
            task_url="http://x/repo?task=task-9",
            copy_text="尾段",
            kanban_context=ctx,
            project_path=None,
        )
        assert result["triggered"] is True
        body, task_id = out.read_text(encoding="utf-8").split("|")
        parsed = json.loads(body)
        assert parsed["task_id"] == "task-9"
        assert parsed["copy_text"] == "尾段"
        assert task_id == "task-9"
    finally:
        if saved is None:
            os.environ.pop(mod.KANBAN_NOTIFY_CMD_ENV, None)
        else:
            os.environ[mod.KANBAN_NOTIFY_CMD_ENV] = saved


@functools.lru_cache(maxsize=1)
def _repo_template() -> Path:
    """Build the canonical repo once per process; callers copy it.

    A plain `git init` repo stores no absolute worktree path in
    `.git/config`, so a filesystem copy reproduces byte-identical repo
    state (history, baked identity, dirty `tracked.txt`/untracked
    `new.txt`) without re-spawning git per test.
    """
    base = Path(tempfile.mkdtemp(prefix="rvf-repo-template-"))
    atexit.register(shutil.rmtree, base, ignore_errors=True)
    template = base / "repo"
    template.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=template)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=template)
    run(["git", "config", "user.name", "RVF Test"], cwd=template)
    (template / "tracked.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "tracked.txt"], cwd=template)
    run(["git", "commit", "-q", "-m", "base"], cwd=template)
    (template / "tracked.txt").write_text("base\nchange\n", encoding="utf-8")
    (template / "new.txt").write_text("new\n", encoding="utf-8")
    return template


def init_repo(path: Path) -> Path:
    # `copytree` runs `os.makedirs(path)` (creates parents, errors if
    # `path` exists) — same contract as the former `path.mkdir(parents=True)`.
    shutil.copytree(_repo_template(), path, symlinks=True)
    return path


# 保险丝型 idle timeout 的统一取值：调用点的 stub 秒级退出，idle timeout 只为「stub 真挂死时
# 别把整套测试拖死」兜底，不构成被测语义。取值必须显著大于「高负载机器上 fork/exec + 解释器
# 启动到吐出第一个字节」的最坏耗时——watchdog 只在 stdout/stderr 长度增长时刷新 liveness
# （run_alternative_reviewer.py 的 `if stdout_len > last_stdout_len or ...` 分支），首字节之前
# 是零活动，历史上的 5.0s 会先于被测语义开火，把 125/0/1 覆盖成 124 假红。
#
# 注意：2026-07-25 那次 `/rvf-local-deploy` 翻红的**主因**不在这里，而是 prompt 根本没投递完
# （见 run_alternative_reviewer.py 的 deliver_reviewer_prompt_on_dedicated_stdin_writer_thread），
# 那个缺陷已单独修掉。本常量兜的是剩下那一层：纯进程启动延迟。
# 放大对正常路径零成本：stub 秒退时循环在 communicate() 返回处退出，根本不走超时分支。
HANG_PREVENTION_IDLE_TIMEOUT_SECONDS = 60.0

# activity probe 的**自身**超时。同样是保险丝而非被测语义：需要探针「失败」的测试一律让探针
# 自己非零退出（毫秒级），从不靠把这个超时调小来制造失败。调小它只会在高负载下把「探针本该
# 成功」的测试炸掉——探针是 `python3 -c ...`，解释器冷启动在 load 35+ 时轻易超过 0.5s，
# 连续两次即触发 `no_observable_activity_probe_failed`（2026-07-27 部署失败的 shard 2/4）。
HANG_PREVENTION_ACTIVITY_PROBE_TIMEOUT_SECONDS = 30.0

# command lock 争用测试的两个时长：等 holder 拿到锁、以及 holder 持锁多久。两者都只是保险丝
# ——被测语义是「contender 超时后把 holder 元数据写进日志」。持锁时长放大的**原意**是抗高负载：
# contender 若花 >3s 才启动，短 hold 会让 holder 提前释放，contender 就会拿到锁而不是超时。
#
# 「放大在墙钟上免费」这个前提**必须由 teardown 兑现**，它不是自动成立的：holder 是
# `command_lock.py -- python3 -c sleep(...)` 两层进程，只 terminate/kill 外层的话，内层仍继承
# 着 holder 的 stdout/stderr 管道，`communicate()` 会一直阻塞到内层自己睡完（实测本用例因此
# 耗时 30.8s，teardown 独占约 28s）。所以 holder 必须 `start_new_session=True` 启动，并由
# `_terminate_lock_holder_process_group_and_drain_pipes` 按**进程组**收——改动这两个常量前
# 请确认那条路径还在。
HANG_PREVENTION_LOCK_HOLDER_ACQUISITION_SECONDS = 30.0
HANG_PREVENTION_LOCK_HOLDER_HOLD_SECONDS = 30.0


def _write_alternative_reviewer_config_payload_unclassified(
    path: Path,
    command: list[str],
    *,
    idle_timeout_seconds: float,
    activity_check_interval_seconds: float,
    activity_probe_command: list[str] | None = None,
    activity_probe_timeout_seconds: float | None = None,
    activity_probe_failure_threshold: int | None = None,
    tool_failure_threshold: int | None = None,
    max_runtime_seconds: float | None = None,
    output_format: str | None = "text",
    health_command: list[str] | None = None,
    pre_run_health: bool | None = None,
) -> Path:
    """底层 payload 写盘，对 idle timeout 的语义不表态。

    不要直接调用：调用点必须经由下面两个 wrapper 之一显式声明 idle timeout 属于「防挂死保险丝」
    还是「被断言的超时行为」。`test_alternative_reviewer_config_helper_call_sites_declare_idle_timeout_classification`
    是这条约束的结构性守卫。
    """
    payload = {
        "enabled": True,
        "label": "alternative-reviewer:test",
        "command": command,
        "allow_repo_cwd": True,
        "idle_timeout_seconds": idle_timeout_seconds,
        "activity_check_interval_seconds": activity_check_interval_seconds,
        "env_unset": [],
    }
    if activity_probe_command is not None:
        payload["activity_probe_command"] = activity_probe_command
    if activity_probe_timeout_seconds is not None:
        payload["activity_probe_timeout_seconds"] = activity_probe_timeout_seconds
    if activity_probe_failure_threshold is not None:
        payload["activity_probe_failure_threshold"] = activity_probe_failure_threshold
    if tool_failure_threshold is not None:
        payload["tool_failure_threshold"] = tool_failure_threshold
    if max_runtime_seconds is not None:
        payload["max_runtime_seconds"] = max_runtime_seconds
    if output_format is not None:
        payload["output_format"] = output_format
    if health_command is not None:
        payload["health_command"] = health_command
        # 夹具的 "reviewer" 是 `python3 -c "<脚本>"`：两边 `-c` 的载荷本来就必须不同
        # （健康脚本验环境、评审脚本写工件），根本不存在可比对的 CLI 入口，因此显式声明豁免
        # health probe parity gate。这个字面量自证其为夹具，且 contract gate 会禁止它出现在
        # 任何出厂 config/alternative-reviewer.*.json 里。
        payload["health_probe_parity_exemption"] = (
            "inline_script_fixture_has_no_shared_cli_entrypoint"
        )
    if pre_run_health is not None:
        payload["pre_run_health"] = pre_run_health
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse(
    path: Path,
    command: list[str],
    *,
    activity_check_interval_seconds: float,
    activity_probe_command: list[str] | None = None,
    activity_probe_timeout_seconds: float | None = None,
    activity_probe_failure_threshold: int | None = None,
    tool_failure_threshold: int | None = None,
    max_runtime_seconds: float | None = None,
    output_format: str | None = "text",
    health_command: list[str] | None = None,
    pre_run_health: bool | None = None,
) -> Path:
    """给「idle timeout 不是被测语义」的调用点用：stub 秒退，超时纯粹是防挂死保险丝。

    刻意**不接受** `idle_timeout_seconds` 实参——物理上杜绝调用点重新传入负载敏感的紧值。
    需要断言超时时序的调用点改用
    `write_alternative_reviewer_config_with_idle_timeout_as_asserted_timeout_behavior`。
    """
    return _write_alternative_reviewer_config_payload_unclassified(
        path,
        command,
        idle_timeout_seconds=HANG_PREVENTION_IDLE_TIMEOUT_SECONDS,
        activity_check_interval_seconds=activity_check_interval_seconds,
        activity_probe_command=activity_probe_command,
        activity_probe_timeout_seconds=activity_probe_timeout_seconds,
        activity_probe_failure_threshold=activity_probe_failure_threshold,
        tool_failure_threshold=tool_failure_threshold,
        max_runtime_seconds=max_runtime_seconds,
        output_format=output_format,
        health_command=health_command,
        pre_run_health=pre_run_health,
    )


def write_alternative_reviewer_config_with_idle_timeout_as_asserted_timeout_behavior(
    path: Path,
    command: list[str],
    *,
    idle_timeout_seconds: float,
    activity_check_interval_seconds: float,
    activity_probe_command: list[str] | None = None,
    activity_probe_timeout_seconds: float | None = None,
    activity_probe_failure_threshold: int | None = None,
    tool_failure_threshold: int | None = None,
    max_runtime_seconds: float | None = None,
    output_format: str | None = "text",
    health_command: list[str] | None = None,
    pre_run_health: bool | None = None,
) -> Path:
    """给「idle timeout 值与 stub 时序的相对关系就是被测语义」的调用点用。

    这里的紧值不得放大：放大即把「超时确实开火 / 探针确实保活 / 快速失败确实没等满」这类断言
    做成恒真的假绿。这些调用点在高负载下有残余脆弱性，是被测语义本身的代价，只能靠降低机器
    负载（`scripts/check_skill_contracts.sh` 的并发推导）缓解，不能靠放宽断言。
    """
    return _write_alternative_reviewer_config_payload_unclassified(
        path,
        command,
        idle_timeout_seconds=idle_timeout_seconds,
        activity_check_interval_seconds=activity_check_interval_seconds,
        activity_probe_command=activity_probe_command,
        activity_probe_timeout_seconds=activity_probe_timeout_seconds,
        activity_probe_failure_threshold=activity_probe_failure_threshold,
        tool_failure_threshold=tool_failure_threshold,
        max_runtime_seconds=max_runtime_seconds,
        output_format=output_format,
        health_command=health_command,
        pre_run_health=pre_run_health,
    )


def test_alternative_reviewer_config_helper_call_sites_declare_idle_timeout_classification() -> None:
    """结构性守卫：禁止绕过「保险丝 / 被断言超时行为」的分类直接写 config payload。

    没有这条守卫，未来新增的测试会重新写出 `idle_timeout_seconds=5.0` 这类紧值保险丝——
    它们在低负载下永远绿，只在部署机负载 50+ 时集体翻红（2026-07-25 `/rvf-local-deploy`
    卡在 `plugin_contract_check` 就是这么表现的），而翻红时看起来像是被部署的改动引入的
    真回归。分类同时把「这个超时值到底是不是被测语义」写进函数名，避免下一个人靠猜。
    """
    private_payload_writer = "_write_alternative_reviewer_config_payload_unclassified"
    aggregator_source_path = Path(__file__).resolve()

    aggregator_lines = aggregator_source_path.read_text(encoding="utf-8").splitlines()
    aggregator_reference_lines = [
        (index + 1, line.strip())
        for index, line in enumerate(aggregator_lines)
        if private_payload_writer in line
    ]
    # 允许的三处：def 本身 + 两个 wrapper 各自的 return。docstring 里的提及不带调用括号。
    aggregator_call_or_def_lines = [
        (lineno, text)
        for lineno, text in aggregator_reference_lines
        if f"{private_payload_writer}(" in text
    ]
    assert len(aggregator_call_or_def_lines) == 3, aggregator_call_or_def_lines
    assert aggregator_call_or_def_lines[0][1].startswith(f"def {private_payload_writer}("), (
        aggregator_call_or_def_lines[0]
    )
    assert all(
        text.startswith(f"return {private_payload_writer}(")
        for _lineno, text in aggregator_call_or_def_lines[1:]
    ), aggregator_call_or_def_lines

    leaked_call_sites: list[str] = []
    for candidate in sorted((ROOT / "tests").rglob("*.py")):
        if candidate.resolve() == aggregator_source_path:
            continue
        source = candidate.read_text(encoding="utf-8")
        if private_payload_writer in source:
            leaked_call_sites.append(str(candidate.relative_to(ROOT)))
    assert leaked_call_sites == [], leaked_call_sites

    fuse_parameters = inspect.signature(
        write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse
    ).parameters
    assert "idle_timeout_seconds" not in fuse_parameters, sorted(fuse_parameters)
    asserted_parameters = inspect.signature(
        write_alternative_reviewer_config_with_idle_timeout_as_asserted_timeout_behavior
    ).parameters
    assert "idle_timeout_seconds" in asserted_parameters, sorted(asserted_parameters)
    assert HANG_PREVENTION_IDLE_TIMEOUT_SECONDS >= 30.0, HANG_PREVENTION_IDLE_TIMEOUT_SECONDS

    # activity probe 的自身超时同理：任何数值字面量都是负载炸弹。需要探针失败的测试让探针
    # 自己非零退出（毫秒级），不靠压缩这个超时——压缩它只会在高负载下打死本该成功的探针。
    probe_timeout_literal_sites: list[str] = []
    for candidate in sorted((ROOT / "tests").rglob("*.py")):
        for line_number, line in enumerate(
            candidate.read_text(encoding="utf-8").splitlines(), start=1
        ):
            assignment = line.split("activity_probe_timeout_seconds=", 1)
            if len(assignment) != 2:
                continue
            passed_value = assignment[1].strip().rstrip(",")
            if passed_value[:1].isdigit():
                probe_timeout_literal_sites.append(
                    f"{candidate.relative_to(ROOT)}:{line_number} -> {passed_value}"
                )
    assert probe_timeout_literal_sites == [], probe_timeout_literal_sites
    assert HANG_PREVENTION_ACTIVITY_PROBE_TIMEOUT_SECONDS >= 5.0, (
        HANG_PREVENTION_ACTIVITY_PROBE_TIMEOUT_SECONDS
    )


def clean_review_result_python(*, stdout: str = "artifact written") -> str:
    return (
        "import os, subprocess, sys; "
        "sys.stdin.read(); "
        "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
        "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary', 'audited diff; no correctness issues found'], check=True); "
        f"print({stdout!r})"
    )


def write_codex_transcript(path: Path, repo: Path) -> Path:
    apply_patch_input = (
        "*** Begin Patch\n"
        "*** Update File: tracked.txt\n"
        "@@\n"
        " base\n"
        "+change\n"
        "*** Add File: owned-new.txt\n"
        "+owned\n"
        "*** Delete File: removed.txt\n"
        "*** End Patch\n"
    )
    records = [
        {
            "timestamp": "2999-04-27T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "session-tracking-test", "cwd": str(repo)},
        },
        {
            "timestamp": "2999-04-27T00:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": apply_patch_input,
                "call_id": "call_patch",
            },
        },
        {
            "timestamp": "2999-04-27T00:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "printf generated > generated.txt", "workdir": str(repo)}),
                "call_id": "call_exec",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    return path


def test_check_review_output_lock_request() -> None:
    result = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="RVF_LOCK_REQUEST name=npm-test command=npm test reason=shared-cache\n",
    )
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["kind"] == "lock_request"
    assert payload["lock_request_count"] == 1

    invalid = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_LOCK_REQUEST name=n command=x reason=y\nNO_ISSUES\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0

    malformed_lock = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_LOCK_REQUEST please-lock-npm-test\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed_lock.returncode != 0

    empty_lock_field = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_LOCK_REQUEST name= command=npm test reason=shared-cache\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert empty_lock_field.returncode != 0


def test_check_review_output_protocol_extension_requests() -> None:
    standard = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text=(
            "RVF_STANDARD_REQUEST domain=security reason=auth-boundary scope=src/auth.py\n"
        ),
    )
    standard_payload = json.loads(standard.stdout)
    assert standard_payload["valid"] is True
    assert standard_payload["kind"] == "request"
    assert standard_payload["request_count"] == 1
    assert standard_payload["request_types"] == ["standard_request"]

    mixed_requests = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text=(
            "RVF_MEASUREMENT_REQUEST metric=p95 command=pytest reason=needs-baseline\n"
            "RVF_CONTEXT_REQUEST need=test-result reason=compare-existing-output\n"
            "RVF_SUBTASK_REQUEST type=security_check scope=src/auth.py reason=auth-change\n"
        ),
    )
    mixed_payload = json.loads(mixed_requests.stdout)
    assert mixed_payload["valid"] is True
    assert mixed_payload["kind"] == "request"
    assert mixed_payload["request_count"] == 3
    assert mixed_payload["request_types"] == [
        "context_request",
        "measurement_request",
        "subtask_request",
    ]

    malformed = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_MEASUREMENT_REQUEST metric=p95 reason=missing-command\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed.returncode != 0

    empty_field = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_STANDARD_REQUEST domain=security reason= scope=src/auth.py\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert empty_field.returncode != 0

    invalid_standard_domain = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_STANDARD_REQUEST domain=privacy reason=auth-boundary scope=src/auth.py\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid_standard_domain.returncode != 0

    invalid_subtask_type = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_SUBTASK_REQUEST type=delete_repo scope=src/auth.py reason=auth-change\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid_subtask_type.returncode != 0

    invalid_context_need = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_CONTEXT_REQUEST need=secret reason=compare-existing-output\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid_context_need.returncode != 0

    mixed_with_completion = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input=(
            "RVF_STANDARD_REQUEST domain=performance reason=needs-budget scope=src/app.ts\n"
            "NO_ISSUES\n"
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert mixed_with_completion.returncode != 0


def test_review_result_artifact_no_issues_and_issues(tmp_path: Path) -> None:
    clean = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")

    run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "no-issues",
            "--out",
            str(clean),
            "--audit-summary",
            "审了 src/foo.ts 与 Dockerfile 改动及其边界，未发现 correctness 问题。",
        ],
        env=env,
    )
    clean_check = run([sys.executable, str(CHECK_REVIEW_RESULT), str(clean), "--json"])
    clean_payload = json.loads(clean_check.stdout)
    assert clean_payload["valid"] is True
    assert clean_payload["kind"] == "no_issues"
    assert clean_payload["audit_summary"].strip()
    assert json.loads(clean.read_text(encoding="utf-8"))["audit_summary"].strip()

    issues = tmp_path / "run" / "artifacts" / "reviewers" / "b" / "review-result.json"
    run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(issues),
            "--path",
            "src/foo.ts",
            "--line",
            "42",
            "--message",
            "空输入时会跳过必要校验。",
            "--kind",
            "REAL",
            "--severity",
            "high",
        ],
        env=env,
    )
    run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(issues),
            "--path",
            "Dockerfile",
            "--line",
            "3",
            "--message",
            "构建参数没有传入默认值。",
            "--kind",
            "REAL",
            "--severity",
            "medium",
        ],
        env=env,
    )
    issue_check = run([sys.executable, str(CHECK_REVIEW_RESULT), str(issues), "--json"])
    issue_payload = json.loads(issue_check.stdout)
    assert issue_payload["valid"] is True
    assert issue_payload["kind"] == "issues"
    assert issue_payload["issue_count"] == 2
    assert issue_payload["issues"][0]["path"] == "src/foo.ts"
    assert issue_payload["issues"][0]["kind"] == "REAL"
    assert issue_payload["issues"][0]["severity"] == "high"
    assert issue_payload["issues"][1]["kind"] == "REAL"
    assert issue_payload["issues"][1]["severity"] == "medium"


def test_no_issues_requires_audit_summary(tmp_path: Path) -> None:
    """no_issues 必须带非空 audit_summary：写入层强制必填，校验层拒绝缺失/空白，正常路径会被 surface。"""
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")
    out = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"

    # 1) 写入层：缺 --audit-summary 必须失败
    missing = subprocess.run(
        [sys.executable, str(WRITE_REVIEW_RESULT), "no-issues", "--out", str(out)],
        env=env, capture_output=True, text=True, check=False,
    )
    assert missing.returncode != 0
    assert "audit-summary" in (missing.stderr + missing.stdout)

    # 2) 写入层：空白 audit_summary 必须失败
    blank = subprocess.run(
        [sys.executable, str(WRITE_REVIEW_RESULT), "no-issues", "--out", str(out),
         "--audit-summary", "   "],
        env=env, capture_output=True, text=True, check=False,
    )
    assert blank.returncode != 0

    # 3) 校验层：手写一个缺 audit_summary 的 no_issues artifact 必须被判违规
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({
            "schema_version": 1, "kind": "no_issues",
            "created_at": "2026-05-30T00:00:00+00:00",
            "issues": [], "requests": [],
        }) + "\n", encoding="utf-8",
    )
    rejected = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(out), "--json"],
        capture_output=True, text=True, check=False,
    )
    payload = json.loads(rejected.stdout)
    assert payload["valid"] is False
    assert any("audit_summary" in e for e in payload["errors"])

    # 4) 正常路径：带 audit_summary 写入并通过校验，且被 surface 出来
    run(
        [sys.executable, str(WRITE_REVIEW_RESULT), "no-issues", "--out", str(out),
         "--audit-summary", "逐函数核对 docstring 与边界，确认无 correctness 回归。"],
        env=env,
    )
    ok = json.loads(run([sys.executable, str(CHECK_REVIEW_RESULT), str(out), "--json"]).stdout)
    assert ok["valid"] is True
    assert ok["kind"] == "no_issues"
    assert "docstring" in ok["audit_summary"]


def test_review_result_artifact_requests_and_scope_exclusions(tmp_path: Path) -> None:
    result = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")
    run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "lock-request",
            "--out",
            str(result),
            "--name",
            "npm-test",
            "--command",
            "npm test",
            "--reason",
            "测试命令会争用共享缓存。",
        ],
        env=env,
    )
    payload = json.loads(run([sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"]).stdout)
    assert payload["valid"] is True
    assert payload["kind"] == "request"
    assert payload["request_types"] == ["lock_request"]

    excluded = tmp_path / "run" / "artifacts" / "reviewers" / "b" / "review-result.json"
    contract = tmp_path / "scope.contract.json"
    contract.write_text('{"canonical_scope":{"excluded_path_prefixes":["vendor"]}}\n', encoding="utf-8")
    run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(excluded),
            "--path",
            "vendor/generated.py",
            "--line",
            "1",
            "--message",
            "不应报告 excluded path。",
            "--kind",
            "REAL",
            "--severity",
            "medium",
        ],
        env=env,
    )
    # 护栏而非闸门：排除区里的 issue 不再让整条腿作废，只记 warning，合并时只能 dropped。
    warned = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(excluded), "--scope-contract", str(contract), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert warned.returncode == 0
    warned_payload = json.loads(warned.stdout)
    assert warned_payload["valid"] is True
    assert warned_payload["issue_indices_in_excluded_paths_one_based"] == [1]
    assert any("excluded" in warning for warning in warned_payload["warnings"])


def test_review_result_artifact_rejects_malformed_and_mixed_state(tmp_path: Path) -> None:
    result = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")

    missing = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode != 0
    assert "missing review result artifact" in missing.stdout

    bad_path = subprocess.run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(result),
            "--path",
            "../escape.py",
            "--line",
            "1",
            "--message",
            "bad",
            "--kind",
            "REAL",
            "--severity",
            "high",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_path.returncode != 0

    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "no_issues",
                "issues": [
                    {
                        "path": "src/a.py",
                        "line": 1,
                        "message": "mixed",
                        "kind": "REAL",
                        "severity": "high",
                    }
                ],
                "requests": [],
            }
        ),
        encoding="utf-8",
    )
    mixed = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert mixed.returncode != 0
    assert "no_issues result must not include issues" in mixed.stdout

    outside = subprocess.run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "no-issues",
            "--out",
            str(tmp_path / "outside.json"),
            "--audit-summary",
            "审了改动，无问题。",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert outside.returncode != 0
    assert "RVF_RUN_DIR" in outside.stderr


def test_issue_requires_kind(tmp_path: Path) -> None:
    out = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")
    result = subprocess.run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(out),
            "--path",
            "src/foo.py",
            "--line",
            "1",
            "--message",
            "missing kind argument",
            "--severity",
            "high",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "--kind" in result.stderr


def test_issue_requires_severity(tmp_path: Path) -> None:
    out = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")
    result = subprocess.run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(out),
            "--path",
            "src/foo.py",
            "--line",
            "1",
            "--message",
            "missing severity argument",
            "--kind",
            "REAL",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "--severity" in result.stderr


def test_check_rejects_issue_without_kind(tmp_path: Path) -> None:
    result = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "issues",
                "issues": [
                    {
                        "path": "src/foo.py",
                        "line": 1,
                        "message": "missing kind",
                        "severity": "high",
                    }
                ],
                "requests": [],
            }
        ),
        encoding="utf-8",
    )
    check = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode != 0
    assert "kind" in check.stdout


def test_check_rejects_invalid_severity(tmp_path: Path) -> None:
    result = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "issues",
                "issues": [
                    {
                        "path": "src/foo.py",
                        "line": 1,
                        "message": "bad severity value",
                        "kind": "REAL",
                        "severity": "wat",
                    }
                ],
                "requests": [],
            }
        ),
        encoding="utf-8",
    )
    check = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode != 0
    assert "severity" in check.stdout


def test_check_skill_contracts_requires_validate_fix_request_literals() -> None:
    script = CHECK_SKILL_CONTRACTS.read_text(encoding="utf-8")
    for literal in (
        "require_literal \"prompts/validate-fix.md\" 'RVF_STANDARD_REQUEST'",
        "require_literal \"prompts/validate-fix.md\" 'RVF_MEASUREMENT_REQUEST'",
        "require_literal \"prompts/validate-fix.md\" 'RVF_SUBTASK_REQUEST'",
        "require_literal \"prompts/validate-fix.md\" 'RVF_CONTEXT_REQUEST'",
    ):
        assert literal in script


def test_contract_check_entrypoints_default_quiet_with_verbose_flag() -> None:
    skill_script = CHECK_SKILL_CONTRACTS.read_text(encoding="utf-8")
    plugin_script = CHECK_PLUGIN_CONTRACTS.read_text(encoding="utf-8")
    for literal in (
        "verbose=0",
        "-v|--verbose)",
        "run_step()",
        "run_parallel_test_steps()",
        "RVF_CONTRACT_PARALLEL_TESTS",
        "RVF_CONTRACT_PARALLEL_JOBS",
        "RVF_CONTRACT_REVIEW_SUPPORT_SHARDS",
        "RVF_CONTRACT_STOP_HOOK_SHARDS",
        "RVF_CONTRACT_DISPATCHER_SHARDS",
        "command_status",
        "验证失败:",
        'return "$command_status"',
    ):
        assert literal in skill_script
    for literal in (
        'parser.add_argument("-v", "--verbose"',
        'command.append("--verbose")',
        "capture_output=True",
        "plugin 契约检查通过",
    ):
        assert literal in plugin_script

    function_start = skill_script.index("timestamp_ms() {")
    function_end = skill_script.index("\nhash_file() {")
    probe = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "verbose=0\n"
        f"{skill_script[function_start:function_end]}\n"
        f"run_step failing {shlex.quote(sys.executable)} -c "
        "'import sys; print(\"boom\"); sys.exit(7)'\n"
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        probe_path = Path(tmp_dir) / "probe.sh"
        timing_path = Path(tmp_dir) / "timing.jsonl"
        probe_path.write_text(probe, encoding="utf-8")
        env = os.environ.copy()
        env["RVF_CONTRACT_TIMING_JSONL"] = str(timing_path)
        env["RVF_CONTRACT_TIMING_SCRIPT"] = str(CHECK_SKILL_CONTRACTS)
        completed = subprocess.run(
            ["bash", str(probe_path)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert not timing_path.exists()
    assert completed.returncode == 7
    assert "验证失败: failing" in completed.stderr
    assert "boom" in completed.stderr


def test_contract_check_parallel_jobs_derive_from_host_cpu_capped_at_one_third() -> None:
    """并发度必须跟着机器走、不超过 cpu/3，且显式设置永远优先。

    写死 8 的旧行为在开发机上恰好合适，但契约检查经常与别的 agent / 构建同时跑；机器一忙，
    对时序敏感的那批测试就集体翻红，看起来像是被部署的改动引入的真回归。

    系数从 cpu-2 收到 cpu/3 的依据见 `contract_parallel_jobs` 的注释：并行组的滑动 FIFO
    窗口会因队首塌陷，把有效并发压在 3.82，再高的 jobs 只多占资源、不缩短墙钟。
    """
    skill_script = CHECK_SKILL_CONTRACTS.read_text(encoding="utf-8")
    helper_start = skill_script.index("contract_host_cpu_count() {")
    helper_end = skill_script.index("\ncontract_review_support_shards() {")
    probe_body = skill_script[helper_start:helper_end]
    assert "RVF_CONTRACT_PARALLEL_JOBS" in probe_body, "推导逻辑必须仍然读取显式覆盖"

    probe = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"{probe_body}\n"
        "printf 'derived=%s\\n' \"$(unset RVF_CONTRACT_PARALLEL_JOBS; contract_parallel_jobs)\"\n"
        "printf 'explicit=%s\\n' \"$(RVF_CONTRACT_PARALLEL_JOBS=3 contract_parallel_jobs)\"\n"
        "printf 'zero=%s\\n' \"$(RVF_CONTRACT_PARALLEL_JOBS=0 contract_parallel_jobs)\"\n"
        "printf 'nonnumeric=%s\\n' \"$(RVF_CONTRACT_PARALLEL_JOBS=abc contract_parallel_jobs)\"\n"
        "printf 'cpu=%s\\n' \"$(contract_host_cpu_count)\"\n"
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        probe_path = Path(tmp_dir) / "parallel-jobs-probe.sh"
        probe_path.write_text(probe, encoding="utf-8")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key != "RVF_CONTRACT_PARALLEL_JOBS"
        }
        completed = subprocess.run(
            ["bash", str(probe_path)],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
    assert completed.returncode == 0, completed.stderr
    observed = dict(
        line.split("=", 1) for line in completed.stdout.strip().splitlines() if "=" in line
    )
    host_cpu_count = int(observed["cpu"])
    assert host_cpu_count >= 1

    derived_jobs = int(observed["derived"])
    assert derived_jobs >= 2, observed
    # 上界 cpu/3：并行组是滑动 FIFO 窗口（窗口满时等的是队首，不是任意先完成者），实测
    # 名义 jobs=8 的有效并发只有 3.82，jobs=5/6/7/8 四档墙钟一模一样而峰值 CPU 从 3.39
    # 涨到 5.85 核 —— 高出去的并发买不到墙钟，只多占机器。这条上界就是防止它被调回去；
    # 高负载时还会在此基础上再折半，所以断言的是上界而非等值。
    assert derived_jobs <= max(2, host_cpu_count // 3), observed
    assert observed["explicit"] == "3", observed
    assert observed["zero"] == "1", observed
    assert observed["nonnumeric"] == observed["derived"], observed


def test_contract_check_parallel_test_steps_record_parallel_timing() -> None:
    skill_script = CHECK_SKILL_CONTRACTS.read_text(encoding="utf-8")
    function_start = skill_script.index("timestamp_ms() {")
    function_end = skill_script.index("\nhash_file() {")
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        tests_dir = root / "tests"
        tests_dir.mkdir()
        for name in (
            "test_install_to_codex.py",
            "test_rvf_handoff_intake.py",
            "test_review_reopen_marker.py",
            "test_review_support_scripts.py",
            "test_stop_hook_dispatcher.py",
            "test_stop_review_validate_fix.py",
        ):
            (tests_dir / name).write_text(
                "#!/usr/bin/env python3\n"
                "import time\n"
                "time.sleep(0.05)\n"
                "print('ok')\n",
                encoding="utf-8",
            )
        timing_path = root / "timing.jsonl"
        probe = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "verbose=0\n"
            f"tests_dir={shlex.quote(str(tests_dir))}\n"
            f"export RVF_CONTRACT_TIMING_JSONL={shlex.quote(str(timing_path))}\n"
            "export RVF_CONTRACT_TIMING_SCRIPT=\"$0\"\n"
            "export RVF_CONTRACT_PARALLEL_TESTS=1\n"
            "export RVF_CONTRACT_PARALLEL_JOBS=4\n"
            "export RVF_CONTRACT_REVIEW_SUPPORT_SHARDS=4\n"
            "export RVF_CONTRACT_STOP_HOOK_SHARDS=4\n"
            "export RVF_CONTRACT_DISPATCHER_SHARDS=2\n"
            f"{skill_script[function_start:function_end]}\n"
            "run_parallel_test_steps\n"
        )
        probe_path = root / "probe.sh"
        probe_path.write_text(probe, encoding="utf-8")
        completed = subprocess.run(
            ["bash", str(probe_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        records = [json.loads(line) for line in timing_path.read_text(encoding="utf-8").splitlines()]

    assert completed.returncode == 0
    assert [record["execution_mode"] for record in records] == ["parallel"] * 13
    assert {record["label"] for record in records} == {
        "tests: install_to_codex",
        "tests: rvf_handoff_intake",
        "tests: review_reopen_marker",
        "tests: review_support_scripts shard 1/4",
        "tests: review_support_scripts shard 2/4",
        "tests: review_support_scripts shard 3/4",
        "tests: review_support_scripts shard 4/4",
        "tests: stop_hook_dispatcher shard 1/2",
        "tests: stop_hook_dispatcher shard 2/2",
        "tests: stop_review_validate_fix shard 1/4",
        "tests: stop_review_validate_fix shard 2/4",
        "tests: stop_review_validate_fix shard 3/4",
        "tests: stop_review_validate_fix shard 4/4",
    }


def test_contract_check_timing_report_accounts_internal_steps() -> None:
    module = load_check_plugin_contracts_module()

    report = module.build_timing_report(
        started_at="2026-05-01T00:00:00Z",
        ended_at="2026-05-01T00:00:01Z",
        duration_ms=1000,
        returncode=0,
        command=["bash", "scripts/check_skill_contracts.sh"],
        top_level_steps=[
            {
                "label": "preflight",
                "source": "check_plugin_contracts.py",
                "status": "completed",
                "returncode": 0,
                "duration_ms": 50,
            },
            {
                "label": "contract shell script",
                "source": "check_plugin_contracts.py",
                "status": "completed",
                "returncode": 0,
                "duration_ms": 900,
            },
        ],
        shell_steps=[
            {
                "label": "python compile",
                "source": "check_skill_contracts.sh",
                "status": "completed",
                "returncode": 0,
                "duration_ms": 200,
            },
            {
                "label": "tests: stop_review_validate_fix",
                "source": "check_skill_contracts.sh",
                "status": "completed",
                "returncode": 0,
                "duration_ms": 600,
            },
        ],
    )

    assert report["slowest_step"]["label"] == "tests: stop_review_validate_fix"
    assert report["slowest_step"]["percentage_of_total"] == 60.0
    assert report["slowest_step"]["percentage_of_wall_time"] == 60.0
    assert report["slowest_step"]["percentage_of_measured_work"] == 63.16
    assert report["measured_work_duration_ms"] == 950
    assert any(step["label"] == "shell script overhead" for step in report["steps"])
    assert report["groups"][0]["name"] == "tests"
    assert report["groups"][0]["duration_ms"] == 600


def test_run_ledger_summary_preserves_contract_timing_fields(tmp_path: Path) -> None:
    module = load_run_ledger_module()
    ledger = module.RunLedger(
        component="dispatcher",
        run_id="rvf-contract-timing-preserve",
        run_dir=tmp_path / "run",
    )

    ledger.summary(
        status="synced",
        reason_code="synced",
        dev_sync_steps=[{"name": "contract-check"}],
        contract_check_timing_report_path="/tmp/contract-check.timing.json",
        contract_check_timing={
            "path": "/tmp/contract-check.timing.json",
            "slowest_step": {"label": "tests: stop_review_validate_fix"},
        },
    )
    later = ledger.summary(
        status="session-hook-control",
        reason_code="session_hook_gate_disabled",
    )

    assert later["dev_sync_steps"] == [{"name": "contract-check"}]
    assert later["contract_check_timing_report_path"] == "/tmp/contract-check.timing.json"
    assert later["contract_check_timing"]["slowest_step"]["label"] == (
        "tests: stop_review_validate_fix"
    )


def test_rvf_logging_all_skill_dirs_default_to_user_local_run_logs(
    tmp_path: Path,
) -> None:
    module = load_run_ledger_module()
    installed_skill = tmp_path / "home" / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"
    installed_skill.mkdir(parents=True)
    (installed_skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    cline_skill = (
        tmp_path
        / "home"
        / ".cline"
        / "worktrees"
        / "9336c"
        / "review-validate-fix"
        / "plugins"
        / "review-validate-fix"
        / "skills"
        / "review-validate-fix"
    )
    dev_skill = tmp_path / "dev" / "skills" / "review-validate-fix"

    original = os.environ.get("RVF_USER_LOCAL_DATA_ROOT")
    user_local_data_root = tmp_path / "rvf-user-local-data"
    os.environ["RVF_USER_LOCAL_DATA_ROOT"] = str(user_local_data_root)
    try:
        expected_log_root = user_local_data_root / "run-logs"
        assert module.default_log_root_for_skill_dir(cline_skill) == expected_log_root
        assert module.default_log_root_for_skill_dir(dev_skill) == expected_log_root
        assert (
            module.default_log_root_for_skill_dir(installed_skill) == expected_log_root
        )
    finally:
        if original is None:
            os.environ.pop("RVF_USER_LOCAL_DATA_ROOT", None)
        else:
            os.environ["RVF_USER_LOCAL_DATA_ROOT"] = original


def test_rvf_logging_does_not_fall_back_to_plugin_state_when_install_missing(
    tmp_path: Path,
) -> None:
    module = load_run_ledger_module()
    installed_skill = tmp_path / "home" / "plugins" / "missing" / "skills" / "review-validate-fix"
    dev_skill = tmp_path / "dev" / "skills" / "review-validate-fix"

    original_installed_skill = os.environ.get("CODEX_RVF_INSTALLED_SKILL_DIR")
    original_user_local_data_root = os.environ.get("RVF_USER_LOCAL_DATA_ROOT")
    user_local_data_root = tmp_path / "rvf-user-local-data"
    os.environ["CODEX_RVF_INSTALLED_SKILL_DIR"] = str(installed_skill)
    os.environ["RVF_USER_LOCAL_DATA_ROOT"] = str(user_local_data_root)
    try:
        assert module.default_log_root_for_skill_dir(dev_skill) == (
            user_local_data_root / "run-logs"
        )
    finally:
        if original_installed_skill is None:
            os.environ.pop("CODEX_RVF_INSTALLED_SKILL_DIR", None)
        else:
            os.environ["CODEX_RVF_INSTALLED_SKILL_DIR"] = original_installed_skill
        if original_user_local_data_root is None:
            os.environ.pop("RVF_USER_LOCAL_DATA_ROOT", None)
        else:
            os.environ["RVF_USER_LOCAL_DATA_ROOT"] = original_user_local_data_root


def test_check_review_output_accepts_wrapped_issue_continuation() -> None:
    result = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text=(
            "1. apps/theseus-mcp/src/tool_registry.ts:1306 task 级上下文先截断 reviewRuns。\n"
            "`query_checkpoint_context` 随后用截断后的 run 集合过滤 signals，可能漏掉同 task 的较早 run。\n"
        ),
    )
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["kind"] == "issues"
    assert payload["issue_count"] == 1
    assert payload["continuation_line_count"] == 1

    cro = load_check_review_output_module()

    accept_cases = [
        ("extensionless_numbered", "1. Dockerfile:3 合法 issue 可以引用没有扩展名的文件。\n"),
        ("spaced_path", "1. slide-versions/claude cowork 1/deck.txt:2 含空格路径仍是合法 path:line。\n"),
        ("spaced_root_component", "1. my dir/file.py:2 根目录组件含空格仍是合法 path:line。\n"),
        ("colon_path", "1. foo:bar.py:2 路径名含冒号时应使用最后的 :line 作为行号。\n"),
        ("unicode_root_path", "1. 设计 文档.md:3 非 ASCII 根路径也应支持。\n"),
        ("repeated_path_line", "1. a.py:1 causes b.py:2 to fail when both paths are involved.\n"),
    ]
    for case_id, text in accept_cases:
        out = cro.classify(text)
        assert out["valid"] is True, f"[{case_id}] expected valid, got {out}"
        assert out["issue_count"] == 1, f"[{case_id}] expected 1 issue, got {out}"

    reject_cases = [
        ("unnumbered_missing_line", "1. apps/foo.ts 这条缺少行号\n续行不能补足 path:line\n"),
        ("misplaced_path_line", "1. 这里先写说明，再引用 plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44\n"),
        ("english_misplaced_path_line", "1. explanation before plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44\n"),
        ("prose_see_path_line", "1. See plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44 misplaced path\n"),
        ("prose_in_path_line", "1. in plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44 misplaced path\n"),
        ("prose_because_path_line", "1. Because a.py:1 misplaced path\n"),
        ("chinese_because_path_line", "1. 因为 a.py:1 misplaced path\n"),
        ("chinese_file_path_line", "1. 文件 a.py:1 misplaced path\n"),
        ("prose_note_colon_path_line", "1. Note: a.py:1 misplaced path\n"),
        ("prose_warning_path_line", "1. warning a.py:1 misplaced path\n"),
        (
            "invalid_extensionless",
            "1. plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44 valid issue\n"
            "Dockerfile:2 missing numbered prefix\n"
            "Makefile:10 missing numbered prefix\n",
        ),
        ("unnumbered_issue", "1. a.py:1 第一条问题\nb.py:2 第二条问题但缺少编号\n"),
        ("unnumbered_no_extension_issue", "1. a.py:1 第一条问题\nMakefile:2 第二条问题但缺少编号\n"),
        ("malformed_numbered_issue", "1. a.py:1 第一条问题\n2) b.py:2 第二条编号格式错误\n"),
        ("malformed_numbered_continuation", "1. a.py:1 第一条问题\n2) 第二条编号格式错误\n"),
        ("chinese_no_issue_continuation", "1. a.py:1 第一条问题\n没有问题\n"),
        ("fix_summary_continuation", "1. a.py:1 第一条问题\n修复说明：已修改文件\n"),
        ("handoff_completion_continuation", "1. a.py:1 第一条问题\nRVF_HANDOFF_FILE: /tmp/rvf-handoff.md\n"),
        ("handoff_reviewers_summary_continuation", "1. a.py:1 第一条问题\nReviewers：NO_ISSUES\n"),
        ("handoff_validate_fixers_summary_continuation", "1. a.py:1 第一条问题\nValidate/fixers：REAL fixed\n"),
        ("unnumbered_spaced_issue", "1. a.py:1 第一条问题\nmy file.py:2 第二条问题但缺少编号\n"),
        ("unnumbered_spaced_dir_issue", "1. a.py:1 第一条问题\nmy dir/file.py:2 第二条问题但缺少编号\n"),
        ("unnumbered_colon_issue", "1. a.py:1 第一条问题\nfoo:bar.py:2 第二条问题但缺少编号\n"),
        ("unnumbered_unicode_issue", "1. a.py:1 第一条问题\n设计 文档.md:3 第二条问题但缺少编号\n"),
    ]
    for case_id, text in reject_cases:
        out = cro.classify(text)
        assert out["valid"] is False, f"[{case_id}] expected rejection, got {out}"


def test_complete_review_scope_waits_for_all_edit_claim_units(tmp_path: Path) -> None:
    module = load_diff_tracker_module()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    run(["git", "add", "a.txt", "b.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    (repo / "a.txt").write_text("a\nclaimed-a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\nclaimed-b\n", encoding="utf-8")
    log_root = tmp_path / "logs"
    registered = module.register_claims(
        repo=repo,
        session_id="S",
        run_id="run-1",
        worktree=repo,
        branch=None,
        owned_paths=["a.txt", "b.txt"],
        apply_patch_paths={"a.txt", "b.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert registered.status == "ok"
    unit_a = module.unit_ids_for_owned_unit(repo, module.OwnedUnit(path="a.txt", unit="path", hunk_anchor=None))[0]
    unit_b = module.unit_ids_for_owned_unit(repo, module.OwnedUnit(path="b.txt", unit="path", hunk_anchor=None))[0]
    edit_registered = module.register_edit_claims(
        repo=repo,
        session_id="S",
        run_id="run-1",
        edit_claims=[
            {
                "claim_id": "edit-two-units",
                "tool_name": "apply_patch",
                "call_id": "patch-1",
                "transcript_line_number": 10,
                "path": "a.txt",
                "hunk_index": 1,
                "operation": "update",
                "mapped_unit_ids": [unit_a, unit_b],
                "latest_user_line_number": 2,
                "latest_user_message": "change both files",
            }
        ],
        log_root_override=log_root,
    )
    assert edit_registered["status"] == "ok"

    first = module.complete_review_scope(
        repo=repo,
        lease_id="missing-lease-a",
        unit_ids=[unit_a],
        scope_hash="sha256:first",
        run_id="review-a",
        log_root_override=log_root,
    )
    assert first["status"] == "released"
    assert first["reviewed_edit_claim_count"] == 0
    tracker_dir = Path(first["tracker_dir"])
    with sqlite3.connect(tracker_dir / module.SQLITE_FILENAME) as conn:
        assert conn.execute("SELECT status FROM edit_claims WHERE claim_id='edit-two-units'").fetchone()[0] == "pending"

    second = module.complete_review_scope(
        repo=repo,
        lease_id="missing-lease-b",
        unit_ids=[unit_b],
        scope_hash="sha256:second",
        run_id="review-b",
        log_root_override=log_root,
    )
    assert second["status"] == "released"
    assert second["reviewed_edit_claim_count"] == 1
    with sqlite3.connect(tracker_dir / module.SQLITE_FILENAME) as conn:
        assert conn.execute("SELECT status FROM edit_claims WHERE claim_id='edit-two-units'").fetchone()[0] == "reviewed"


def test_diagnose_stop_hook_scope_reports_stale_runtime_and_claude_write_gap(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    transcript = tmp_path / "claude-session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-06T06:00:00.000Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": str(repo / "tracked.txt")},
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    run_dir = tmp_path / "run"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "stop-event.json").write_text(
        json.dumps({"cwd": str(repo), "transcript_path": str(transcript)}, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifacts / "session-manifest.json").write_text(
        json.dumps(
            {
                "owned_paths": [],
                "owned_dirty_paths": [],
                "unattributed_dirty_paths": ["tracked.txt"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    summary = run_dir / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-06T06:00:01Z",
                "status": "skipped",
                "reason_code": "no_session_owned_dirty",
                "repo": str(repo),
                "run_dir": str(run_dir),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    old_scripts = tmp_path / "old-scripts"
    old_scripts.mkdir()
    (old_scripts / "session_manifest.py").write_text(
        "def build_manifest():\n"
        "    return {'owned_dirty_paths': []}\n",
        encoding="utf-8",
    )
    (old_scripts / "stop_review_validate_fix.py").write_text(
        "def legacy_session_scope_gate_payload():\n"
        "    pass\n",
        encoding="utf-8",
    )

    completed = run(
        [
            sys.executable,
            str(DIAGNOSE_STOP_HOOK_SCOPE),
            "--summary",
            str(summary),
            "--runtime-scripts-dir",
            str(old_scripts),
            "--format",
            "json",
        ]
    )
    payload = json.loads(completed.stdout)
    codes = {item["code"] for item in payload["diagnoses"]}

    assert payload["transcript_probe"]["claude_dirty_write_paths"] == ["tracked.txt"]
    assert "stop_hook_skipped_no_session_owned_dirty" in codes
    assert "run_manifest_missing_tracker_field" in codes
    assert "claude_writes_not_attributed" in codes
    assert "runtime_session_manifest_differs_from_reference" in codes
    assert "runtime_session_manifest_lacks_tracker_field" in codes


def test_command_lock_writes_lifecycle_events(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    state = tmp_path / "state"
    run_id = "test-command-lock-lifecycle"
    env = os.environ.copy()
    env["CODEX_RVF_LOG_ROOT"] = str(state)
    env["RVF_RUN_ID"] = run_id
    env.pop("RVF_RUN_DIR", None)

    locked = run(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "lifecycle-test",
            "--",
            sys.executable,
            "-c",
            "print('locked')",
        ],
        env=env,
    )

    assert "locked" in locked.stdout
    events = read_jsonl(state / "runs" / run_id / "events.jsonl")
    event_names = [event["event"] for event in events]
    assert event_names == ["lock_wait_started", "lock_acquired", "lock_released"]
    assert {event["component"] for event in events} == {"command-lock"}
    assert all(event["phase"] == "review" for event in events)
    assert events[1]["lock_name"] == "lifecycle-test"
    assert events[2]["returncode"] == 0

    summary = json.loads((state / "runs" / run_id / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "completed"
    assert summary["reason_code"] == "lock_released"
    assert summary["lock_name"] == "lifecycle-test"


def test_command_lock_respects_env_run_dir(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    state = tmp_path / "state"
    run_dir = tmp_path / "custom-run-dir"
    env = os.environ.copy()
    env["CODEX_RVF_LOG_ROOT"] = str(state)
    env["RVF_RUN_ID"] = "test-command-lock-custom-dir"
    env["RVF_RUN_DIR"] = str(run_dir)

    run(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "custom-dir-test",
            "--",
            sys.executable,
            "-c",
            "print('locked')",
        ],
        env=env,
    )

    assert (run_dir / "events.jsonl").exists()
    assert not (state / "runs" / "test-command-lock-custom-dir" / "events.jsonl").exists()
    events = read_jsonl(run_dir / "events.jsonl")
    assert [event["event"] for event in events] == ["lock_wait_started", "lock_acquired", "lock_released"]


def _terminate_lock_holder_process_group_and_drain_pipes(
    holder: subprocess.Popen,
    holder_process_group_id: int,
) -> tuple[str, str]:
    """收掉 holder **整条进程链**并读干其 stdout/stderr。

    holder 是 `command_lock.py -- python3 -c sleep(...)`：外层 command_lock 收到 SIGTERM 会直接
    死掉（它没装 signal handler，也不转发给 subprocess.run 起的内层），但内层 python 仍继承着
    holder 的 stdout/stderr 管道并继续睡。因此只收外层是不够的——`communicate()` 要等到**所有**
    写端关闭，也就是一直等到内层自己睡完，把 hold 时长完全兑换成墙钟时间。

    holder 用 `start_new_session=True` 启动，故其 pgid 恒等于 `holder.pid`，可以直接 killpg 整组。
    这里不用 `os.getpgid(holder.pid)`：外层若已被 `poll()` 回收，getpgid 会 ProcessLookupError，
    而内层仍活着。POSIX 保证进程组只要还有成员就不会被复用，用启动时记下的 pgid 是安全的。
    """
    try:
        os.killpg(holder_process_group_id, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # 整组已经没了（或不可及）：退回到只收外层，communicate() 此时不会再被内层挂住。
        if holder.poll() is None:
            holder.kill()
    return holder.communicate()


def test_command_lock_logs_timeout_with_holder_metadata(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    state = tmp_path / "state"
    lock_dir = tmp_path / "locks"
    holder_env = os.environ.copy()
    holder_env["CODEX_RVF_LOG_ROOT"] = str(state)
    holder_env["RVF_RUN_ID"] = "test-command-lock-holder"
    holder_env.pop("RVF_RUN_DIR", None)
    contender_env = os.environ.copy()
    contender_env["CODEX_RVF_LOG_ROOT"] = str(state)
    contender_env["RVF_RUN_ID"] = "test-command-lock-contender"
    contender_env.pop("RVF_RUN_DIR", None)

    lock_path_result = run(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "contended-test",
            "--lock-dir",
            str(lock_dir),
            "--print-path",
        ],
    )
    metadata_path = Path(lock_path_result.stdout.strip()).with_suffix(".json")

    holder = subprocess.Popen(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "contended-test",
            "--lock-dir",
            str(lock_dir),
            "--",
            sys.executable,
            "-c",
            f"import time; time.sleep({HANG_PREVENTION_LOCK_HOLDER_HOLD_SECONDS})",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=holder_env,
        # 自成 session/进程组：teardown 才能一次收掉 command_lock + 内层 sleep 两层，
        # 否则内层会攥着管道让 communicate() 一直等到 hold 时长睡满（见常量处注释）。
        start_new_session=True,
    )
    # start_new_session=True 下 pgid == holder.pid；在外层被 poll() 回收前记下来。
    holder_process_group_id = holder.pid
    holder_pipes_already_drained = False
    try:
        # 被测语义是「contender 超时后把 holder 元数据写进日志」，不是「holder 多快拿到锁」。
        # 这个 deadline 纯粹是保险丝：holder 要冷启动一个 Python 解释器、import RVF 模块、
        # 拿锁并落 metadata，load 35+ 时超过原来的 5s 完全正常（2026-07-27 部署失败的
        # shard 4/4 即此）。放大它不弱化任何断言——holder 真挂死时仍会红，只是晚一点。
        deadline = time.monotonic() + HANG_PREVENTION_LOCK_HOLDER_ACQUISITION_SECONDS
        while not metadata_path.exists():
            if holder.poll() is not None:
                # 外层已退出，但内层可能还攥着管道；照样按进程组收，别让失败路径挂死。
                stdout, stderr = _terminate_lock_holder_process_group_and_drain_pipes(
                    holder, holder_process_group_id
                )
                holder_pipes_already_drained = True
                raise AssertionError(stderr.strip() or stdout.strip() or "holder exited before acquiring lock")
            if time.monotonic() >= deadline:
                raise AssertionError("holder did not acquire lock")
            time.sleep(0.01)

        contender = subprocess.run(
            [
                sys.executable,
                str(COMMAND_LOCK),
                "--repo",
                str(repo),
                "--name",
                "contended-test",
                "--lock-dir",
                str(lock_dir),
                "--timeout",
                "0.3",
                "--poll-interval",
                "0.05",
                "--",
                sys.executable,
                "-c",
                "print('should-not-run')",
            ],
            capture_output=True,
            text=True,
            env=contender_env,
            check=False,
        )
    finally:
        # holder 是 `command_lock.py -- python3 -c sleep(...)` 两层进程；hold 时长放大后就不能
        # 再指望「它自己睡完」兜底，必须按进程组一次收掉两层（否则 communicate() 会被内层挂住）。
        if not holder_pipes_already_drained:
            _terminate_lock_holder_process_group_and_drain_pipes(holder, holder_process_group_id)

    assert contender.returncode == 75
    assert "current holder metadata" in contender.stderr
    events = read_jsonl(state / "runs" / "test-command-lock-contender" / "events.jsonl")
    event_names = [event["event"] for event in events]
    assert event_names == ["lock_wait_started", "lock_timeout"]
    timeout_event = events[-1]
    assert timeout_event["reason_code"] == "lock_timeout"
    assert timeout_event["lock_name"] == "contended-test"
    assert "holder_metadata" in timeout_event
    assert "contended-test" in str(timeout_event["holder_metadata"])


def _cursor_tool_call_line(tool_key: str, result: dict) -> str:
    return json.dumps(
        {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {tool_key: {"result": result}},
        }
    )


def test_cursor_stream_tool_layer_health_classifier_and_summary() -> None:
    health = load_cursor_tool_layer_health_module()
    classify = health.classify_cursor_tool_call_outcome

    # spawnError（shell 执行器坏了）=> runtime_failure
    assert (
        classify({"shellToolCall": {"result": {"spawnError": {"error": "returned no exit status"}}}})
        == "runtime_failure"
    )
    # read/glob 的 Aborted（两种 error 嵌套形态）=> runtime_failure
    assert classify({"readToolCall": {"result": {"error": {"errorMessage": "Aborted"}}}}) == "runtime_failure"
    assert classify({"globToolCall": {"result": {"error": {"error": "Aborted"}}}}) == "runtime_failure"
    # 关键反例：普通工具错误（文件不存在）证明工具层在工作 => ok，绝不能误判成 runtime_failure
    assert (
        classify({"readToolCall": {"result": {"error": {"errorMessage": "ENOENT: no such file"}}}})
        == "ok"
    )
    # 成功 => ok；非工具结果 => None
    assert classify({"shellToolCall": {"result": {"success": "out", "isBackground": False}}}) == "ok"
    assert classify({"toolCallId": "x"}) is None
    assert classify("not-a-dict") is None

    broken_stream = "\n".join(
        _cursor_tool_call_line("shellToolCall", {"spawnError": {"error": "returned no exit status"}})
        for _ in range(4)
    )
    broken = health.summarize_cursor_stream_tool_layer(broken_stream)
    assert broken == {
        "tool_runtime_failures": 4,
        "tool_successes": 0,
        "completed_tool_calls": 4,
        "healthy": False,
    }

    healthy_stream = "\n".join(
        [
            _cursor_tool_call_line("shellToolCall", {"success": "ok", "isBackground": False}),
            _cursor_tool_call_line("readToolCall", {"success": {"content": "x"}}),
        ]
    )
    assert health.summarize_cursor_stream_tool_layer(healthy_stream)["healthy"] is True


def test_apply_worktree_bootstrap_replays_tracked_and_untracked(tmp_path: Path) -> None:
    source = init_repo(tmp_path / "source")
    clone = tmp_path / "clone"
    run(["git", "clone", "-q", str(source), str(clone)], cwd=tmp_path)
    (source / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (source / "owned.txt").write_text("owned\n", encoding="utf-8")
    base_ref = run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
    patch = tmp_path / "bootstrap.patch"
    patch.write_text(subprocess.run(["git", "diff", "--binary", "HEAD", "--", "tracked.txt"], cwd=source, check=True, capture_output=True, text=True).stdout, encoding="utf-8")
    files = tmp_path / "files"
    files.mkdir()
    stored = files / "owned.txt"
    stored.write_text("owned\n", encoding="utf-8")
    metadata = tmp_path / "bootstrap.json"
    metadata.write_text(json.dumps({"base_ref": base_ref, "patch_file": str(patch), "files_dir": str(files), "untracked_files": [{"path": "owned.txt", "stored_path": str(stored)}]}), encoding="utf-8")
    completed = run([sys.executable, str(APPLY_WORKTREE_BOOTSTRAP), "--metadata", str(metadata), "--repo", str(clone)])
    assert json.loads(completed.stdout)["ok"] is True
    assert (clone / "tracked.txt").read_text(encoding="utf-8") == "changed\n"
    assert (clone / "owned.txt").read_text(encoding="utf-8") == "owned\n"


def test_apply_worktree_bootstrap_rejects_mismatched_base_ref(tmp_path: Path) -> None:
    source = init_repo(tmp_path / "source")
    clone = tmp_path / "clone"
    run(["git", "clone", "-q", str(source), str(clone)], cwd=tmp_path)
    (source / "tracked.txt").write_text("changed\n", encoding="utf-8")
    base_ref = run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
    patch = tmp_path / "bootstrap.patch"
    patch.write_text(subprocess.run(["git", "diff", "--binary", "HEAD", "--", "tracked.txt"], cwd=source, check=True, capture_output=True, text=True).stdout, encoding="utf-8")
    metadata = tmp_path / "bootstrap.json"
    metadata.write_text(json.dumps({"base_ref": base_ref, "patch_file": str(patch), "untracked_files": []}), encoding="utf-8")

    (clone / "other.txt").write_text("other\n", encoding="utf-8")
    run(["git", "add", "other.txt"], cwd=clone)
    run(["git", "commit", "-q", "-m", "advance clone"], cwd=clone)
    completed = subprocess.run(
        [sys.executable, str(APPLY_WORKTREE_BOOTSTRAP), "--metadata", str(metadata), "--repo", str(clone)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "base_ref mismatch" in completed.stdout
    assert (clone / "tracked.txt").read_text(encoding="utf-8") == "base\n"


def test_run_ledger_summary_preserves_cline_kanban_fields(tmp_path: Path) -> None:
    module = load_run_ledger_module()
    run_dir = tmp_path / "run"
    ledger = module.RunLedger(component="stop-hook", repo=tmp_path, cwd=tmp_path, run_id="run-1", run_dir=run_dir)
    ledger.summary(
        status="cline-kanban-started",
        reason_code="cline_kanban_task_started",
        cline_kanban_task_id="task-1",
        cline_kanban_base_ref="HEAD",
        worktree_bootstrap_path="/tmp/bootstrap.json",
    )
    later = ledger.summary(status="completed", reason_code="prepare_completed", message="later phase")
    assert later["cline_kanban_task_id"] == "task-1"
    assert later["cline_kanban_base_ref"] == "HEAD"
    assert later["worktree_bootstrap_path"] == "/tmp/bootstrap.json"


def test_run_ledger_summary_preserves_rvf_state_fields(tmp_path: Path) -> None:
    module = load_run_ledger_module()
    run_dir = tmp_path / "run"
    ledger = module.RunLedger(component="stop-hook", repo=tmp_path, cwd=tmp_path, run_id="run-1", run_dir=run_dir)
    ledger.summary(
        status="cline-kanban-started",
        reason_code="cline_kanban_task_started",
        **module.rvf_state_fields(
            phase="prepare",
            backend="kanban-task",
            backend_raw="cline-kanban",
            scope_contract_path="/tmp/scope.contract.json",
            review_packet_path="/tmp/review-packet.md",
        ),
    )
    later = ledger.summary(status="completed", reason_code="prepare_completed", message="later phase")
    assert later["rvf_backend"] == "kanban-task"
    assert later["rvf_backend_raw"] == "cline-kanban"
    assert later["rvf_state_phase"] == "prepare"
    assert later["rvf_scope_contract_path"] == "/tmp/scope.contract.json"
    assert later["rvf_review_packet_path"] == "/tmp/review-packet.md"
    assert later["rvf_state"]["phases"] == list(module.RVF_STATE_PHASES)


def test_cancel_rvf_run_marks_cancelled_and_trashes_cline_task(tmp_path: Path) -> None:
    run_dir = tmp_path / "state" / "runs" / "rvf-user-cancel"
    run_dir.mkdir(parents=True)
    repo = init_repo(tmp_path / "repo")
    fake_task = tmp_path / "fake_task.py"
    calls = tmp_path / "trash-calls.jsonl"
    fake_task.write_text(
        "import json, os, sys\n"
        "with open(os.environ['KANBAN_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'task_id': sys.argv[-1], 'status': 'trashed'}))\n",
        encoding="utf-8",
    )
    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "run_id": "rvf-user-cancel",
                "status": "cline-kanban-started",
                "repo": str(repo),
                "cwd": str(repo),
                "run_dir": str(run_dir),
                "cline_kanban_task_id": "task-1",
                "runner_pid": 999999,
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["KANBAN_CALLS"] = str(calls)
    completed = run(
        [
            sys.executable,
            str(CANCEL_RVF_RUN),
            "--summary",
            str(summary_path),
            "--force-after",
            "0",
            "--task-cmd",
            f"{sys.executable} {fake_task}",
        ],
        env=env,
    )
    payload = json.loads(completed.stdout)
    assert payload["status"] == "cancelled"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "cline-kanban-rvf-cancelled"
    assert summary["reason_code"] == "user_cancelled"
    events = read_jsonl(run_dir / "events.jsonl")
    assert any(event["event"] == "run_cancel_requested" for event in events)
    assert any(event["event"] == "run_cancelled" for event in events)
    recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert recorded == [["trash", "--project-path", str(repo.resolve()), "--task-id", "task-1"]]


def test_cancel_rvf_run_ignores_stale_runner_pid_without_matching_command() -> None:
    module = load_cancel_rvf_run_module()
    original_ps = module.ps_processes
    try:
        module.ps_processes = lambda: [
            (4242, "/usr/bin/python unrelated_server.py"),
            (4343, "/usr/local/bin/codex exec --output-last-message /tmp/rvf-live/final.md -"),
        ]
        candidates = module.discover_run_processes(
            "rvf-live",
            {"runner_pid": 4242},
        )
    finally:
        module.ps_processes = original_ps

    assert 4242 not in candidates
    assert candidates == {4343: "/usr/local/bin/codex exec --output-last-message /tmp/rvf-live/final.md -"}


DIFF_TRACKER = ROOT / "core" / "session_scope_allocation" / "reviewable_unit_diff_tracker.py"


def load_diff_tracker_module():
    if "rvf_diff_tracker" in sys.modules:
        return sys.modules["rvf_diff_tracker"]
    spec = importlib.util.spec_from_file_location("rvf_diff_tracker", DIFF_TRACKER)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load diff_tracker module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["rvf_diff_tracker"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop("rvf_diff_tracker", None)
        raise
    return module


def _write_session_transcript(path: Path, repo: Path, *, session_id: str, target_path: str, line: str) -> Path:
    apply_patch_input = (
        "*** Begin Patch\n"
        f"*** Update File: {target_path}\n"
        "@@\n"
        "-base\n"
        f"+{line}\n"
        "*** End Patch\n"
    )
    records = [
        {
            "timestamp": "2026-04-27T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": str(repo)},
        },
        {
            "timestamp": "2026-04-27T00:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": apply_patch_input,
                "call_id": "call_patch",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    return path


def test_canonical_patch_hash_stable_across_reruns(tmp: Path) -> None:
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    first = module.register_claims(
        repo=repo,
        session_id="session-stable",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    second = module.register_claims(
        repo=repo,
        session_id="session-stable",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert first.claim_ids == second.claim_ids
    assert second.dropped_stale_claim_ids == []
    db_path = Path(first.tracker_dir) / "tracker.sqlite3"
    conn = _sqlite.connect(str(db_path))
    try:
        units = conn.execute("SELECT unit_id FROM units").fetchall()
    finally:
        conn.close()
    assert len(units) == 1


def test_canonical_patch_hash_stable_under_line_shift(tmp: Path) -> None:
    """Inserting blank lines above an unrelated hunk shifts only `@@ -A,B +C,D @@`
    line numbers; the hunk's content payload is untouched, so its
    canonical_patch_hash (== unit_id) must stay byte-identical."""
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    baseline = "".join(f"line-{i}\n" for i in range(1, 21))
    target = repo / "shift.txt"
    target.write_text(baseline, encoding="utf-8")
    run(["git", "add", "shift.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)

    # First edit: change line-15 only — produces a single hunk near EOF.
    first_lines = baseline.splitlines(keepends=True)
    first_lines[14] = "LINE-15\n"
    target.write_text("".join(first_lines), encoding="utf-8")
    log_root = tmp / "logs"
    first = module.register_claims(
        repo=repo,
        session_id="session-shift",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["shift.txt"],
        apply_patch_paths={"shift.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert first.status == "ok", first.to_dict()
    assert len(first.claim_ids) == 1
    first_unit_id = first.claim_ids[0]
    db_path = Path(first.tracker_dir) / "tracker.sqlite3"

    # Second edit: re-write file inserting 5 blank lines at top, keeping the
    # same line-15 → LINE-15 edit (which now sits at line 20). The hunk content
    # the diff emits is identical (same context lines, same +/- pair); only
    # the @@ header numbers change.
    shifted = ["\n"] * 5 + first_lines
    target.write_text("".join(shifted), encoding="utf-8")
    # Recommit baseline so HEAD also has the prefix blanks — otherwise the diff
    # would include the blank-line insertions and the hunk content would
    # legitimately differ.
    run(["git", "add", "shift.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "shift baseline"], cwd=repo)
    # Restore the same edit on the new baseline.
    new_baseline = "".join(["\n"] * 5 + baseline.splitlines(keepends=True))
    target.write_text(new_baseline, encoding="utf-8")
    run(["git", "add", "shift.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "restore base after shift"], cwd=repo)
    edited2 = new_baseline.splitlines(keepends=True)
    edited2[19] = "LINE-15\n"  # 14 (original index) + 5 (prefix blanks) = 19
    target.write_text("".join(edited2), encoding="utf-8")

    second = module.register_claims(
        repo=repo,
        session_id="session-shift",
        run_id="run-2",
        worktree=None,
        branch=None,
        owned_paths=["shift.txt"],
        apply_patch_paths={"shift.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert second.status == "ok", second.to_dict()
    assert second.claim_ids == [first_unit_id], (first_unit_id, second.claim_ids)
    assert second.dropped_stale_claim_ids == []
    conn = _sqlite.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT unit_id, observed_state FROM units WHERE path='shift.txt'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, rows
    assert rows[0][0] == first_unit_id
    assert rows[0][1] == "dirty"


def test_canonical_patch_hash_changes_on_content_edit(tmp: Path) -> None:
    """Editing the hunk content (not just shifting line numbers) must produce
    a new unit_id and demote the old unit to observed_state='superseded'."""
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    baseline = "alpha\nbeta\ngamma\n"
    target = repo / "edit.txt"
    target.write_text(baseline, encoding="utf-8")
    run(["git", "add", "edit.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    target.write_text("alpha\nBETA-v1\ngamma\n", encoding="utf-8")
    log_root = tmp / "logs"
    first = module.register_claims(
        repo=repo,
        session_id="session-edit",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["edit.txt"],
        apply_patch_paths={"edit.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert first.status == "ok"
    assert len(first.claim_ids) == 1
    old_unit_id = first.claim_ids[0]
    db_path = Path(first.tracker_dir) / "tracker.sqlite3"

    # Now genuinely change the hunk content — different replacement line.
    target.write_text("alpha\nBETA-v2\ngamma\n", encoding="utf-8")
    second = module.register_claims(
        repo=repo,
        session_id="session-edit",
        run_id="run-2",
        worktree=None,
        branch=None,
        owned_paths=["edit.txt"],
        apply_patch_paths={"edit.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert second.status == "ok"
    assert len(second.claim_ids) == 1
    new_unit_id = second.claim_ids[0]
    assert new_unit_id != old_unit_id
    # The old session_units row must be gone (replaced by the new unit_id),
    # and dropped_stale_claim_ids surfaces the old unit_id.
    assert old_unit_id in second.dropped_stale_claim_ids
    conn = _sqlite.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT unit_id, observed_state, review_state, is_tombstoned, tombstone_reason
              FROM units
             WHERE path='edit.txt'
             ORDER BY first_observed_at
            """
        ).fetchall()
        session_units = conn.execute(
            "SELECT unit_id FROM session_units WHERE session_id='session-edit'"
        ).fetchall()
        try:
            conn.execute("UPDATE units SET review_state='tombstoned' WHERE unit_id=?", (new_unit_id,))
        except _sqlite.IntegrityError:
            rejected_review_tombstone = True
        else:
            rejected_review_tombstone = False
    finally:
        conn.close()
    by_unit = {row[0]: row[1:] for row in rows}
    assert by_unit.get(old_unit_id) == (
        "superseded",
        "available",
        1,
        "session_no_longer_owns",
    ), by_unit
    assert by_unit.get(new_unit_id) == ("dirty", "available", 0, None), by_unit
    assert rejected_review_tombstone is True
    assert {row[0] for row in session_units} == {new_unit_id}


def test_migration_phase1_json_to_sqlite_idempotent(tmp: Path) -> None:
    """Hand-write a Phase 1 state.json + events.jsonl + meta.json under the
    legacy `<log_root>/tracker/<key>/` path. First register_claims call must
    create sqlite, archive legacy files into `_legacy/`, and stamp meta.json
    with `migrated_from`. Second call must not re-import or re-archive."""
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    common_dir = module.git_common_dir(repo.resolve())
    assert common_dir is not None
    repo_key = module.repo_key(common_dir)
    legacy_dir = log_root / "tracker" / repo_key
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_state = {
        "schema_version": 1,
        "claims": [
            {
                "claim_id": "clm-legacy-001",
                "session_id": "session-legacy",
                "run_id": "run-legacy",
                "worktree": str(repo.resolve()),
                "branch": "main",
                "path": "tracked.txt",
                "unit": "hunk",
                "hunk_anchor": {
                    "header": "@@ -1 +1,2 @@",
                    "context_hash": "deadbeefdeadbeef",
                    "old_range": [1, 1],
                    "new_range": [1, 2],
                },
                "evidence": "apply_patch",
                "claimed_at": "2026-04-01T00:00:00Z",
                "last_seen_at": "2026-04-01T00:00:00Z",
                "lease": None,
            },
        ],
        "tombstones": [
            {
                "claim_id": "clm-tombstoned",
                "session_id": "session-legacy",
                "path": "removed.txt",
                "unit": "path",
                "dropped_at": "2026-04-01T00:00:00Z",
                "reason": "session_no_longer_owns",
            },
        ],
    }
    (legacy_dir / "state.json").write_text(
        json.dumps(legacy_state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (legacy_dir / "events.jsonl").write_text(
        json.dumps({"timestamp": "2026-04-01T00:00:00Z", "event": "claim_added"}) + "\n",
        encoding="utf-8",
    )
    (legacy_dir / "meta.json").write_text(
        json.dumps({"schema_version": 1, "repo_key": repo_key}, ensure_ascii=False),
        encoding="utf-8",
    )

    first = module.register_claims(
        repo=repo,
        session_id="session-after-migration",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert first.status == "ok", first.to_dict()
    new_dir = log_root / "diff-tracker" / "repos" / repo_key
    db_path = new_dir / "tracker.sqlite3"
    assert db_path.is_file()
    archive = legacy_dir / "_legacy"
    assert (archive / "state.json").is_file()
    assert (archive / "events.jsonl").is_file()
    assert not (legacy_dir / "state.json").exists()
    meta_payload = json.loads((new_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta_payload.get("migrated_from") == "json-v1"
    assert meta_payload.get("schema_version") == module.SCHEMA_VERSION
    archive_state = json.loads((archive / "state.json").read_text(encoding="utf-8"))
    assert archive_state["claims"][0]["claim_id"] == "clm-legacy-001"

    conn = _sqlite.connect(str(db_path))
    try:
        units_before = conn.execute("SELECT unit_id, path FROM units ORDER BY unit_id").fetchall()
        sessions_before = conn.execute("SELECT session_id FROM sessions").fetchall()
        tombstones_before = conn.execute("SELECT ref_id FROM tombstones").fetchall()
    finally:
        conn.close()
    assert {row[0] for row in sessions_before} == {"session-legacy", "session-after-migration"}
    assert any(row[0] == "clm-tombstoned" for row in tombstones_before)
    archive_state_mtime = (archive / "state.json").stat().st_mtime

    events_first = read_jsonl(new_dir / "events.jsonl")
    migration_started_first = sum(1 for e in events_first if e.get("event") == "migration_started")
    assert migration_started_first == 1, events_first

    second = module.register_claims(
        repo=repo,
        session_id="session-after-migration",
        run_id="run-2",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert second.status == "ok"
    assert (archive / "state.json").stat().st_mtime == archive_state_mtime
    conn = _sqlite.connect(str(db_path))
    try:
        units_after = conn.execute("SELECT unit_id, path FROM units ORDER BY unit_id").fetchall()
    finally:
        conn.close()
    assert units_after == units_before
    events_second = read_jsonl(new_dir / "events.jsonl")
    migration_started_second = sum(1 for e in events_second if e.get("event") == "migration_started")
    assert migration_started_second == 1, events_second


def test_migration_phase1_recovers_when_archive_predates_db_marker(tmp: Path) -> None:
    """Simulate the historical crash window: legacy state.json was already moved
    into `_legacy/` but the SQLite transaction that recorded `migrated_from`
    was rolled back (process death between archive and COMMIT). The next
    register_claims call MUST re-import claims from the archived state.json
    rather than treat the missing live state.json as "nothing to migrate"
    and silently lose every Phase-1 claim."""
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    common_dir = module.git_common_dir(repo.resolve())
    assert common_dir is not None
    repo_key = module.repo_key(common_dir)

    # Stage the post-crash on-disk shape directly: archive dir holds the
    # legacy state.json, but live legacy_dir/state.json is gone and the
    # SQLite db has no `migrated_from` row.
    legacy_dir = log_root / "tracker" / repo_key
    archive = legacy_dir / "_legacy"
    archive.mkdir(parents=True, exist_ok=True)
    legacy_state = {
        "schema_version": 1,
        "claims": [
            {
                "claim_id": "clm-recover-001",
                "session_id": "session-recover",
                "run_id": "run-recover",
                "worktree": str(repo.resolve()),
                "branch": "main",
                "path": "tracked.txt",
                "unit": "path",
                "evidence": "apply_patch",
                "claimed_at": "2026-04-02T00:00:00Z",
                "last_seen_at": "2026-04-02T00:00:00Z",
                "lease": None,
            },
        ],
        "tombstones": [],
    }
    (archive / "state.json").write_text(
        json.dumps(legacy_state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # The archived events.jsonl is the only surviving copy after the crash
    # window: a prior _post_commit moved the live copy into _legacy/ before
    # the SQLite COMMIT rolled back. Recovery must replay these into the new
    # events.jsonl, matching the normal-migration path's side effects.
    (archive / "events.jsonl").write_text(
        json.dumps(
            {"timestamp": "2026-04-02T00:00:00Z", "event": "claim_added", "claim_id": "clm-recover-001"}
        )
        + "\n",
        encoding="utf-8",
    )
    # No live state.json — represents the "archived but not committed" gap.

    result = module.register_claims(
        repo=repo,
        session_id="session-after-crash",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert result.status == "ok", result.to_dict()

    new_dir = log_root / "diff-tracker" / "repos" / repo_key
    db_path = new_dir / "tracker.sqlite3"
    assert db_path.is_file()
    conn = _sqlite.connect(str(db_path))
    try:
        sessions = {row[0] for row in conn.execute("SELECT session_id FROM sessions").fetchall()}
        migrated_from = conn.execute(
            "SELECT value FROM meta WHERE key='migrated_from'"
        ).fetchone()
    finally:
        conn.close()
    # Both the recovered legacy session AND the new one must be present.
    assert sessions == {"session-recover", "session-after-crash"}, sessions
    assert migrated_from is not None and migrated_from[0] == "json-v1"
    # Archive remains intact; live legacy state.json must NOT have been
    # re-created (would re-trigger migration on every call).
    assert (archive / "state.json").is_file()
    assert not (legacy_dir / "state.json").exists()
    # Recovery must replay the archived phase1 events into the new events.jsonl
    # so the recovery branch is observationally aligned with the normal
    # migration path's events.jsonl side effects.
    new_events = read_jsonl(new_dir / "events.jsonl")
    replayed = [e for e in new_events if e.get("imported_from") == "phase1"]
    assert any(
        e.get("event") == "claim_added" and e.get("claim_id") == "clm-recover-001"
        for e in replayed
    ), new_events


def _slice_2b_repo_with_two_dirty(tmp: Path) -> Path:
    """init_repo + a second tracked file so two paths are dirty."""
    repo = init_repo(tmp / "repo")
    (repo / "tracked2.txt").write_text("base2\n", encoding="utf-8")
    run(["git", "add", "tracked2.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "add tracked2"], cwd=repo)
    (repo / "tracked2.txt").write_text("base2\nchange2\n", encoding="utf-8")
    return repo


def _slice_2b_tracker_scope_payload(
    *,
    paths: list[str] | None = None,
    unit_ids: list[str] | None = None,
    lease_id: str = "lse-2b-test",
    scope_hash: str = "sha256:" + "b" * 64,
    hunks: object = "default",
    source_session_id: str | None = "sess-2b",
    takeover_from_session_id: str | None = None,
    extras: dict[str, object] | None = None,
) -> dict[str, object]:
    if unit_ids is None:
        unit_ids = ["a" * 64, "c" * 64]
    if paths is None:
        paths = ["tracked.txt"]
    if hunks == "default":
        hunks = [
            {"unit_id": unit_ids[0], "path": paths[0] if paths else "tracked.txt", "hunk_header": "@@ -1 +1,2 @@"}
        ]
    payload: dict[str, object] = {
        "unit_ids": unit_ids,
        "lease_id": lease_id,
        "scope_hash": scope_hash,
        "paths": paths,
        "hunks": hunks,
        "source_session_id": source_session_id,
        "takeover_from_session_id": takeover_from_session_id,
    }
    if extras:
        payload.update(extras)
    return payload


def _slice_2b_write_scope_file(tmp: Path, payload: dict[str, object]) -> Path:
    path = tmp / "tracker-scope.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _slice_2b_prepare(
    *,
    tmp: Path,
    repo: Path,
    tracker_scope_path: Path | None,
    log_root: Path,
    extra_args: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path | None]:
    """Run prepare_review_run.py with a transcript, optionally with --tracker-scope.

    Returns (completed_process, run_dir_or_None). When tracker_scope rejection
    happens (non-zero exit), run_dir is None.
    """
    transcript = write_codex_transcript(tmp / "session.jsonl", repo)
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n- intent: 2-B test\n- work: edit tracked.txt\n",
        encoding="utf-8",
    )
    output_json = tmp / "run.json"
    base_dir = tmp / "runs"
    base_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(PREPARE_REVIEW_RUN),
        "--repo",
        str(repo),
        "--session-context",
        str(context),
        "--transcript",
        str(transcript),
        "--output-json",
        str(output_json),
        "--base-dir",
        str(base_dir),
    ]
    if tracker_scope_path is not None:
        cmd.extend(["--tracker-scope", str(tracker_scope_path)])
    if extra_args:
        cmd.extend(extra_args)
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)}
    completed = run_subprocess_with_independent_review_start_boundary(cmd, capture_output=True, text=True, env=env, check=False)
    if completed.returncode != 0:
        return completed, None
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    return completed, Path(payload["artifacts_dir"])


def test_tracker_scope_payload_splices_into_manifest(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    artifact_manifest = json.loads((artifacts_dir / "session-manifest.json").read_text(encoding="utf-8"))
    input_manifest = json.loads((artifacts_dir / "inputs" / "session-manifest.json").read_text(encoding="utf-8"))
    for manifest_payload in (artifact_manifest, input_manifest):
        tracker_block = manifest_payload.get("tracker")
        assert isinstance(tracker_block, dict)
        scope = tracker_block.get("tracker_scope")
        assert isinstance(scope, dict)
        assert scope["unit_ids"] == payload["unit_ids"]
        assert scope["lease_id"] == payload["lease_id"]
        assert scope["scope_hash"] == payload["scope_hash"]
        assert scope["paths"] == payload["paths"]
        assert scope["hunks"] == payload["hunks"]
        assert scope["source_session_id"] == payload["source_session_id"]
        assert scope["takeover_from_session_id"] == payload["takeover_from_session_id"]
    # tracker_scope_file (artifact-root entry) must point to artifact_dir copy,
    # not the inputs/ duplicate; otherwise downstream consumers cannot retrieve
    # the artifact-root tracker-scope.json.
    run_payload = json.loads((tmp / "run.json").read_text(encoding="utf-8"))
    assert run_payload["tracker_scope_file"] is not None
    assert run_payload["input_tracker_scope_file"] is not None
    assert run_payload["tracker_scope_file"] != run_payload["input_tracker_scope_file"]
    assert run_payload["tracker_scope_file"] == str((artifacts_dir / "tracker-scope.json").resolve())
    assert run_payload["input_tracker_scope_file"] == str(
        (artifacts_dir / "inputs" / "tracker-scope.json").resolve()
    )


def test_tracker_scope_unlocks_scope_contract_v2_fields(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload(unit_ids=["e" * 64, "d" * 64])
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    contract = json.loads((artifacts_dir / "inputs" / "scope.contract.json").read_text(encoding="utf-8"))
    assert contract["version"] == 3
    assert contract["primary_units"] == sorted({"e" * 64, "d" * 64})
    assert contract["tracker_lease_id"] == payload["lease_id"]
    assert contract["tracker_scope_hash"] == payload["scope_hash"]
    canonical = contract["canonical_scope"]
    assert "primary_units" not in canonical
    assert "tracker_lease_id" not in canonical
    assert "tracker_scope_hash" not in canonical


def test_scope_contract_v2_emitted_without_tracker_scope(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=None, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    contract = json.loads((artifacts_dir / "inputs" / "scope.contract.json").read_text(encoding="utf-8"))
    assert contract["version"] == 3
    assert contract["primary_units"] is None
    assert contract["tracker_lease_id"] is None
    assert contract["tracker_scope_hash"] is None
    canonical = contract["canonical_scope"]
    assert canonical["version"] == 3
    assert set(canonical.keys()) == {
        "version",
        "review_start_boundary_receipt",
        "repo",
        "scope_mode",
        "primary_files",
        "background_files",
        "protected_files",
        "canonical_issues",
        "fix_allowlist",
        "excluded_path_prefixes",
        "review_kind",
        "base_ref",
        "remote_name",
        "comparison_base_tip",
        "reviewed_base_tip",
        "reviewed_task_tip",
        "candidate_task_tip",
        "base_delta_paths",
        "task_change_paths",
        "interaction_primary_paths",
        "base_delta_protected_paths",
        "bootstrap_receipt_path",
        "bootstrap_receipt_digest",
        "candidate_evidence_path",
        "candidate_evidence_digest",
        "integration_candidate_receipt_path",
        "integration_candidate_receipt_digest",
    }


def test_packet_emits_tracker_scope_section_when_present(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    packet_text = (artifacts_dir / "review-packet.md").read_text(encoding="utf-8")
    assert "## Tracker Scope" in packet_text
    assert "## Allocated Git Diff" in packet_text
    assert "## Full Git Diff HEAD (Evidence Only)" in packet_text
    assert "## Session-Owned Git Diff" not in packet_text
    assert payload["lease_id"] in packet_text
    assert payload["scope_hash"] in packet_text


def test_packet_omits_tracker_scope_section_when_absent(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=None, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    packet_text = (artifacts_dir / "review-packet.md").read_text(encoding="utf-8")
    assert "## Tracker Scope" not in packet_text
    assert "## Allocated Git Diff" not in packet_text
    assert "## Session-Owned Git Diff" in packet_text


def test_packet_metadata_carries_tracker_scope_keys(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    metadata = json.loads((artifacts_dir / "review-packet.metadata.json").read_text(encoding="utf-8"))
    assert metadata["tracker_scope_present"] is True
    assert metadata["tracker_scope_unit_count"] == len(payload["unit_ids"])
    assert metadata["tracker_scope_lease_id"] == payload["lease_id"]
    assert metadata["tracker_scope_hash"] == payload["scope_hash"]
    assert metadata["tracker_scope_paths"] == payload["paths"]
    assert metadata["tracker_scope_source_session_id"] == payload["source_session_id"]
    assert metadata["tracker_scope_takeover_from_session_id"] == payload["takeover_from_session_id"]

    completed_b, artifacts_b = _slice_2b_prepare(
        tmp=tmp / "no-scope",
        repo=init_repo(tmp / "no-scope" / "repo"),
        tracker_scope_path=None,
        log_root=tmp / "no-scope" / "logs",
    )
    assert completed_b.returncode == 0, completed_b.stderr
    assert artifacts_b is not None
    metadata_b = json.loads((artifacts_b / "review-packet.metadata.json").read_text(encoding="utf-8"))
    assert metadata_b["tracker_scope_present"] is False
    assert metadata_b["tracker_scope_unit_count"] == 0
    assert metadata_b["tracker_scope_lease_id"] is None
    assert metadata_b["tracker_scope_hash"] is None
    assert metadata_b["tracker_scope_paths"] == []
    assert metadata_b["tracker_scope_source_session_id"] is None
    assert metadata_b["tracker_scope_takeover_from_session_id"] is None


def test_tracker_scope_payload_rejects_invalid_payloads(tmp: Path) -> None:
    cases = [
        ("missing_unit_ids", {"unit_ids": None}, "tracker_scope.unit_ids"),
        ("empty_unit_ids", {"unit_ids": []}, "tracker_scope.unit_ids"),
        ("non_string_unit_id", {"unit_ids": [123, "a" * 64]}, "tracker_scope.unit_ids"),
        ("non_hex_unit_id", {"unit_ids": ["zzzz" * 16]}, "tracker_scope.unit_ids"),
        ("missing_lease", {"lease_id": ""}, "tracker_scope.lease_id"),
        ("missing_scope_hash", {"scope_hash": ""}, "tracker_scope.scope_hash"),
        ("malformed_scope_hash", {"scope_hash": "sha256:short"}, "tracker_scope.scope_hash"),
        ("non_list_paths", {"paths": "tracked.txt"}, "tracker_scope.paths"),
        ("non_string_path", {"paths": [123]}, "tracker_scope.paths"),
        ("non_list_hunks", {"hunks": "x"}, "tracker_scope.hunks"),
    ]
    log_root = tmp / "logs"
    for index, (label, override, expected_substr) in enumerate(cases):
        sub_tmp = tmp / f"case-{index}-{label}"
        sub_tmp.mkdir(parents=True, exist_ok=True)
        repo = init_repo(sub_tmp / "repo")
        payload = _slice_2b_tracker_scope_payload()
        for key, value in override.items():
            if value is None:
                payload.pop(key, None)
            else:
                payload[key] = value
        scope_path = _slice_2b_write_scope_file(sub_tmp, payload)
        completed, artifacts_dir = _slice_2b_prepare(
            tmp=sub_tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
        )
        assert completed.returncode != 0, f"case {label} should have failed, got: {completed.stdout}"
        assert artifacts_dir is None
        assert expected_substr in completed.stderr, (
            f"case {label}: stderr did not mention {expected_substr!r}: {completed.stderr!r}"
        )


def test_tracker_scope_requires_session_manifest_or_transcript(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    context = tmp / "context.md"
    context.write_text("## Session context\n- intent: D4 test\n", encoding="utf-8")
    base_dir = tmp / "runs"
    base_dir.mkdir(parents=True, exist_ok=True)
    log_root = tmp / "logs"
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)}
    completed = run_subprocess_with_independent_review_start_boundary(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--tracker-scope",
            str(scope_path),
            "--base-dir",
            str(base_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode != 0
    assert "--tracker-scope requires --session-manifest or --transcript" in completed.stderr


def test_tracker_scope_tolerates_unknown_keys(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload(extras={"future_field": "preserved"})
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    manifest = json.loads((artifacts_dir / "session-manifest.json").read_text(encoding="utf-8"))
    spliced = manifest["tracker"]["tracker_scope"]
    assert spliced.get("future_field") == "preserved"


def test_review_env_exports_rvf_tracker_scope_path(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    env_text = (artifacts_dir / "review-env.sh").read_text(encoding="utf-8")
    assert "RVF_TRACKER_SCOPE" in env_text
    assert "tracker-scope.json" in env_text
    assert (artifacts_dir / "inputs" / "tracker-scope.json").exists()

    repo_b = init_repo(tmp / "no-scope" / "repo")
    log_root_b = tmp / "no-scope" / "logs"
    completed_b, artifacts_b = _slice_2b_prepare(
        tmp=tmp / "no-scope", repo=repo_b, tracker_scope_path=None, log_root=log_root_b
    )
    assert completed_b.returncode == 0, completed_b.stderr
    assert artifacts_b is not None
    env_text_b = (artifacts_b / "review-env.sh").read_text(encoding="utf-8")
    assert "RVF_TRACKER_SCOPE" not in env_text_b
    assert not (artifacts_b / "inputs" / "tracker-scope.json").exists()


def test_allocated_git_diff_uses_tracker_scope_paths(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload(paths=["tracked.txt"])
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    packet_text = (artifacts_dir / "review-packet.md").read_text(encoding="utf-8")
    allocated_idx = packet_text.index("## Allocated Git Diff")
    full_idx = packet_text.index("## Full Git Diff HEAD (Evidence Only)")
    assert allocated_idx < full_idx
    allocated_block = packet_text[allocated_idx:full_idx]
    full_block = packet_text[full_idx:]
    assert "diff --git a/tracked.txt b/tracked.txt" in allocated_block
    assert "diff --git a/tracked2.txt b/tracked2.txt" not in allocated_block
    assert "diff --git a/tracked.txt b/tracked.txt" in full_block
    assert "diff --git a/tracked2.txt b/tracked2.txt" in full_block


def test_existing_cross_session_conflicts_path_unchanged_with_tracker_scope(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    module.register_claims(
        repo=repo,
        session_id="other-session",
        run_id="run-other",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    packet_text = (artifacts_dir / "review-packet.md").read_text(encoding="utf-8")
    metadata = json.loads((artifacts_dir / "review-packet.metadata.json").read_text(encoding="utf-8"))
    assert "## Tracker Scope" in packet_text
    assert "## Cross-Session Conflicts" in packet_text
    assert "other-session" in packet_text
    assert metadata["cross_session_conflicts"]
    assert metadata["tracker_scope_present"] is True


# ---------------------------------------------------------------------------
# Committed-round detection (RVF Stop hook auto-includes work the agent
# committed mid-round). Exercises diff_tracker committed observation/dedup,
# session_manifest committed attribution, and the round-baseline marker.
# ---------------------------------------------------------------------------

def _round_baseline_committed_modules():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import core.session_scope_allocation.reviewable_unit_diff_tracker as _dt  # noqa: PLC0415
    import core.session_scope_allocation.session_change_manifest as _sm  # noqa: PLC0415
    from core.session_scope_allocation import round_baseline_marker as _rbm  # noqa: PLC0415

    return _dt, _sm, _rbm


def _committed_round_repo(tmp: Path) -> tuple[Path, str]:
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    baseline = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    return repo, baseline


def _committed_round_transcript(tmp: Path, repo: Path, *, session_id: str, path: str, old: str, new: str) -> Path:
    transcript = tmp / "session.jsonl"
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {path}\n"
        "@@\n"
        f"-{old}\n"
        f"+{new}\n"
        "*** End Patch\n"
    )
    records = [
        {"timestamp": "2026-04-27T00:00:00.000Z", "type": "session_meta", "payload": {"id": session_id, "cwd": str(repo)}},
        {
            "timestamp": "2026-04-27T00:00:01.000Z",
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "c1"},
        },
    ]
    transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return transcript


def _units_observed_state_by_path(tracker_dir: str, path: str) -> dict[str, str]:
    import sqlite3 as _sqlite  # noqa: PLC0415

    conn = _sqlite.connect(str(Path(tracker_dir) / "tracker.sqlite3"))
    try:
        rows = conn.execute(
            "SELECT unit_id, observed_state FROM units WHERE path=?", (path,)
        ).fetchall()
    finally:
        conn.close()
    return {row[0]: row[1] for row in rows}


def _units_full_by_path(tracker_dir: str, path: str) -> dict[str, tuple[str, str]]:
    import sqlite3 as _sqlite  # noqa: PLC0415

    conn = _sqlite.connect(str(Path(tracker_dir) / "tracker.sqlite3"))
    try:
        rows = conn.execute(
            "SELECT unit_id, observed_state, review_state FROM units WHERE path=?", (path,)
        ).fetchall()
    finally:
        conn.close()
    return {row[0]: (row[1], row[2]) for row in rows}


def _session_units_evidence_by_path(tracker_dir: str, path: str) -> set[str]:
    import sqlite3 as _sqlite  # noqa: PLC0415

    conn = _sqlite.connect(str(Path(tracker_dir) / "tracker.sqlite3"))
    try:
        rows = conn.execute(
            "SELECT su.evidence FROM session_units su "
            "JOIN units u ON u.unit_id = su.unit_id WHERE u.path = ?",
            (path,),
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def _build_packet_with_manifest(
    tmp: Path, repo: Path, manifest: dict, *, base_ref: str | None = None
) -> tuple[str, dict]:
    """Run build_review_packet.py against a hand-written manifest; return (packet_text, metadata).

    Side-effect-free wrt the tracker (build_review_packet only reads the manifest), so it is
    the right level to assert the committed-round diff-base behavior in isolation.
    """
    tmp.mkdir(parents=True, exist_ok=True)
    manifest_path = tmp / "session-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    sow = tmp / "sow.md"
    sow.write_text("Review the committed slice for the owned files.\n", encoding="utf-8")
    out = tmp / "packet.md"
    meta = tmp / "packet.metadata.json"
    cmd = [
        sys.executable,
        str(BUILD_PACKET),
        "--repo",
        str(repo),
        "--session-context",
        str(sow),
        "--session-manifest",
        str(manifest_path),
        "--output",
        str(out),
        "--metadata-output",
        str(meta),
    ]
    if base_ref is not None:
        cmd.extend(["--base-ref", base_ref])
    run(cmd, cwd=repo)
    return out.read_text(encoding="utf-8"), json.loads(meta.read_text(encoding="utf-8"))


def test_committed_round_packet_diff_uses_baseline(tmp: Path) -> None:
    """(a) committed_round_baseline set on a clean worktree -> owned-diff renders the
    already-committed change via `git diff <baseline>` instead of an empty `git diff HEAD`."""
    repo, baseline = _committed_round_repo(tmp)
    (repo / "f.txt").write_text("base\nCOMMITTED\n", encoding="utf-8")
    run(["git", "commit", "-qam", "slice"], cwd=repo)
    assert run(["git", "status", "--porcelain"], cwd=repo).stdout.strip() == ""  # clean worktree
    manifest = {
        "version": 1,
        "repo": str(repo),
        "session_id": "s1",
        "owned_paths": ["f.txt"],
        "owned_dirty_paths": [],
        "owned_committed_round_paths": ["f.txt"],
        "committed_round_baseline": baseline,
    }
    packet, meta = _build_packet_with_manifest(tmp, repo, manifest)
    assert meta["owned_diff_base_ref"] == baseline
    assert meta["session_owned_diff_paths"] == ["f.txt"]
    owned = packet.split("## Session-Owned Git Diff", 1)[1]
    assert "diff --git a/f.txt" in owned and "COMMITTED" in owned


def test_committed_round_packet_baseline_absent_is_head_equivalent(tmp: Path) -> None:
    """(b) No committed_round_baseline -> base_ref resolves to HEAD and the owned-diff is
    byte-identical to today's `git diff HEAD` (zero regression for the pure-dirty case)."""
    repo, _ = _committed_round_repo(tmp)
    (repo / "f.txt").write_text("base\nDIRTY\n", encoding="utf-8")  # uncommitted edit
    manifest = {
        "version": 1,
        "repo": str(repo),
        "session_id": "s1",
        "owned_paths": ["f.txt"],
        "owned_dirty_paths": ["f.txt"],
    }
    packet, meta = _build_packet_with_manifest(tmp, repo, manifest)
    assert meta["owned_diff_base_ref"] == "HEAD"
    direct = run(["git", "diff", "--find-renames", "HEAD", "--", "f.txt"], cwd=repo).stdout.rstrip()
    assert direct and direct in packet  # exact HEAD-based diff body embedded verbatim


def test_committed_round_packet_captures_committed_and_dirty(tmp: Path) -> None:
    """(c) Mixed: one hunk committed, a second left dirty. `git diff <baseline>` (vs worktree)
    must capture BOTH — this is why we diff against the baseline, not <baseline>..HEAD."""
    repo, baseline = _committed_round_repo(tmp)
    (repo / "f.txt").write_text("base\nCOMMITTED\n", encoding="utf-8")
    run(["git", "commit", "-qam", "slice"], cwd=repo)
    (repo / "f.txt").write_text("base\nCOMMITTED\nDIRTY\n", encoding="utf-8")  # extra dirty hunk
    manifest = {
        "version": 1,
        "repo": str(repo),
        "session_id": "s1",
        "owned_paths": ["f.txt"],
        "owned_dirty_paths": ["f.txt"],
        "owned_committed_round_paths": ["f.txt"],
        "committed_round_baseline": baseline,
    }
    packet, meta = _build_packet_with_manifest(tmp, repo, manifest)
    assert meta["owned_diff_base_ref"] == baseline
    owned = packet.split("## Session-Owned Git Diff", 1)[1]
    assert "COMMITTED" in owned and "DIRTY" in owned


def test_committed_round_packet_base_ref_falls_back_when_not_ancestor(tmp: Path) -> None:
    """(d) Ancestor guard: a committed_round_baseline that is NOT an ancestor of HEAD
    (divergent branch) falls back to HEAD instead of producing a reverse/divergent diff."""
    repo, _ = _committed_round_repo(tmp)
    run(["git", "checkout", "-q", "-b", "other"], cwd=repo)
    (repo / "g.txt").write_text("x\n", encoding="utf-8")
    run(["git", "add", "g.txt"], cwd=repo)
    run(["git", "commit", "-qm", "divergent"], cwd=repo)
    non_ancestor = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    run(["git", "checkout", "-q", "main"], cwd=repo)
    (repo / "f.txt").write_text("base\nDIRTY\n", encoding="utf-8")
    manifest = {
        "version": 1,
        "repo": str(repo),
        "session_id": "s1",
        "owned_paths": ["f.txt"],
        "owned_dirty_paths": ["f.txt"],
        "committed_round_baseline": non_ancestor,
    }
    packet, meta = _build_packet_with_manifest(tmp, repo, manifest)
    assert meta["owned_diff_base_ref"] == "HEAD"  # guard rejected the non-ancestor baseline
    direct = run(["git", "diff", "--find-renames", "HEAD", "--", "f.txt"], cwd=repo).stdout.rstrip()
    assert direct and direct in packet


def test_committed_round_manifest_to_packet_reviews_committed_work(tmp: Path) -> None:
    """(e) B1 chain end-to-end: build_manifest(committed_baseline=) attributes the clean-worktree
    committed paths into owned_committed_round_paths + committed_round_baseline, and feeding that
    manifest to the packet renders the committed diff — the manual path's git-reset replacement."""
    _dt, sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    (repo / "f.txt").write_text("base\nCOMMITTED\n", encoding="utf-8")
    run(["git", "commit", "-qam", "slice"], cwd=repo)
    transcript = tmp / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "s1", "cwd": str(repo)}}) + "\n",
        encoding="utf-8",
    )
    manifest = sm.build_manifest(
        repo, transcript, tracker_enabled=False, committed_baseline=baseline, committed_base_ref=None
    )
    assert manifest["committed_round_baseline"] == baseline
    assert "f.txt" in manifest["owned_committed_round_paths"]
    packet, meta = _build_packet_with_manifest(tmp, repo, manifest)
    assert meta["owned_diff_base_ref"] == baseline
    owned = packet.split("## Session-Owned Git Diff", 1)[1]
    assert "diff --git a/f.txt" in owned and "COMMITTED" in owned


def test_committed_round_tracker_scope_renders_path_level_when_baseline(tmp: Path) -> None:
    """(F1 regression) With a committed-round baseline, the tracker-scope Allocated diff must NOT be
    silently emptied by hunk-header filtering when a file has both a committed and a residual-dirty
    hunk (the recorded headers were computed against a different base than baseline-vs-worktree).
    base_ref != HEAD falls back to path-level diff of the allocated paths."""
    repo, _ = _committed_round_repo(tmp)
    (repo / "f.txt").write_text("L1\nL2\nL3\nL4\nL5\n", encoding="utf-8")
    run(["git", "commit", "-qam", "grow"], cwd=repo)
    grow_baseline = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    (repo / "f.txt").write_text("L1\nL2-COMMITTED\nL3\nL4\nL5\n", encoding="utf-8")
    run(["git", "commit", "-qam", "slice"], cwd=repo)
    (repo / "f.txt").write_text("L1\nL2-COMMITTED\nL3\nL4\nL5-DIRTY\n", encoding="utf-8")  # residual dirty hunk
    dirty_header = ""
    for ln in run(["git", "diff", "--find-renames", "HEAD", "--", "f.txt"], cwd=repo).stdout.splitlines():
        if ln.startswith("@@"):
            dirty_header = ln.split(" @@")[0] + " @@"
            break
    assert dirty_header
    manifest = {
        "version": 1, "repo": str(repo), "session_id": "s1",
        "owned_paths": ["f.txt"], "owned_dirty_paths": ["f.txt"], "owned_committed_round_paths": ["f.txt"],
        "committed_round_baseline": grow_baseline,
        "tracker": {"status": "ok", "session_id": "s1", "tracker_scope": {
            "unit_ids": ["u1"], "lease_id": "lse-1", "scope_hash": "h1",
            "paths": ["f.txt"], "hunks": [{"path": "f.txt", "hunk_header": dirty_header}]}},
    }
    packet, meta = _build_packet_with_manifest(tmp, repo, manifest)
    assert meta["owned_diff_base_ref"] == grow_baseline
    parts = packet.split("## Allocated Git Diff", 1)
    assert len(parts) > 1, "expected an Allocated Git Diff section when tracker scope is present"
    section = parts[1]
    assert "L2-COMMITTED" in section and "L5-DIRTY" in section  # both visible, not filtered to empty


def test_committed_round_base_ref_drives_manifest_attribution(tmp: Path) -> None:
    """(F2 regression) An explicit --base-ref on a clean already-committed worktree must drive the
    manifest's committed-round attribution (not only the packet diff-base), so prepare builds a
    session-owned packet instead of aborting with 'session manifest has no owned paths'."""
    repo, _ = _committed_round_repo(tmp)
    (repo / "f.txt").write_text("base\nCOMMITTED\n", encoding="utf-8")
    run(["git", "commit", "-qam", "slice"], cwd=repo)
    baseline = run(["git", "rev-parse", "HEAD~1"], cwd=repo).stdout.strip()
    assert run(["git", "status", "--porcelain"], cwd=repo).stdout.strip() == ""  # clean worktree
    transcript = tmp / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "s1", "cwd": str(repo)}}) + "\n",
        encoding="utf-8",
    )
    context = tmp / "context.md"
    context.write_text("Review the committed slice for f.txt.\n", encoding="utf-8")
    output_json = tmp / "run.json"
    base_dir = tmp / "runs"
    base_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(PREPARE_REVIEW_RUN), "--repo", str(repo),
        "--session-context", str(context), "--transcript", str(transcript),
        "--output-json", str(output_json), "--base-dir", str(base_dir),
        "--base-ref", baseline, "--rvf-backend", "manual",
    ]
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(tmp / "logs")}
    completed = run_subprocess_with_independent_review_start_boundary(cmd, capture_output=True, text=True, env=env, check=False)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    art = Path(payload["artifacts_dir"])
    contract = json.loads((art / "inputs" / "scope.contract.json").read_text(encoding="utf-8"))
    meta = json.loads((art / "inputs" / "review-packet.metadata.json").read_text(encoding="utf-8"))
    assert contract["scope_mode"] == "session-owned"
    assert meta["owned_diff_base_ref"] == baseline
    assert "f.txt" in contract["primary_files"]


def test_committed_unit_dedup_reviewed_not_resurrected(tmp: Path) -> None:
    """A reviewed dirty unit, once committed, must NOT re-enter the candidate
    pool: same unit_id, review_state stays 'reviewed' (§4)."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("base\nadded\n", encoding="utf-8")
    owned = dt.OwnedUnit(path="f.txt", unit="path", hunk_anchor=None)
    dt.register_claims(
        repo=repo, session_id="s1", run_id="r1", worktree=repo, branch=None,
        owned_paths=["f.txt"], apply_patch_paths={"f.txt"}, exec_only_paths=set(),
        owned_units_override=[(owned, "apply_patch")], log_root_override=logs,
    )
    first = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False,
    )
    assert first["status"] == "allocated", first
    assert first["candidate_unit_count"] >= 1, first
    dt.complete_review_scope(
        repo=repo, lease_id=first["lease_id"], scope_hash=first["scope_hash"], run_id="r1",
        log_root_override=logs,
    )
    reviewed_before = _units_full_by_path(first["tracker_dir"], "f.txt")
    reviewed_ids = [uid for uid, (_obs, rev) in reviewed_before.items() if rev == "reviewed"]
    assert reviewed_ids, reviewed_before
    # Commit the reviewed work; re-register + re-allocate with the baseline.
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "commit reviewed"], cwd=repo)
    dt.register_claims(
        repo=repo, session_id="s1", run_id="r2", worktree=repo, branch=None,
        owned_paths=["f.txt"], apply_patch_paths=set(), exec_only_paths=set(),
        owned_units_override=[(owned, "apply_patch")], log_root_override=logs,
        committed_paths={"f.txt"}, committed_baseline=baseline,
    )
    second = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r2", reviewer_id="rev2",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
    )
    assert second["status"] == "empty", second
    # The reviewed units are now observed 'committed' yet stay 'reviewed'.
    after = _units_full_by_path(first["tracker_dir"], "f.txt")
    for uid in reviewed_ids:
        assert after[uid][0] == "committed", (uid, after[uid])
        assert after[uid][1] == "reviewed", (uid, after[uid])


def test_added_file_unit_identity_stable_dirty_staged_committed(tmp: Path) -> None:
    """A NEW file must mint ONE content-keyed unit_id across all three observed
    forms — untracked (dirty), staged, and committed — so a new file reviewed
    while dirty stays deduped once it becomes tracked. Before the add-identity
    unification the tracked (staged/committed) forms minted a fresh hunk-body
    unit_id and diverged from the untracked id, which is what re-dispatched
    reviewed-then-committed new files (the rename add-half over-dispatch)."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    (repo / "newfile.txt").write_text("hello\nworld\n", encoding="utf-8")

    dirty_obs = dt._classify_path(repo, "newfile.txt")
    assert dirty_obs is not None and dirty_obs.kind == "untracked_file", dirty_obs
    dirty_ids = sorted(s.unit_id for s in dt._specs_from_observation(dirty_obs, "newfile.txt"))
    assert len(dirty_ids) == 1, dirty_ids

    run(["git", "add", "newfile.txt"], cwd=repo)
    staged_obs = dt._classify_path(repo, "newfile.txt")
    assert staged_obs is not None and staged_obs.kind == "tracked_hunk", staged_obs
    assert staged_obs.change_type == "add", staged_obs
    staged_ids = sorted(s.unit_id for s in dt._specs_from_observation(staged_obs, "newfile.txt"))

    run(["git", "commit", "-q", "-m", "add newfile"], cwd=repo)
    assert run(["git", "status", "--porcelain"], cwd=repo).stdout.strip() == ""
    committed_obs = dt._classify_committed_path(repo, "newfile.txt", baseline)
    assert committed_obs is not None and committed_obs.change_type == "add", committed_obs
    committed_ids = sorted(s.unit_id for s in dt._specs_from_observation(committed_obs, "newfile.txt"))

    assert dirty_ids == staged_ids == committed_ids, (dirty_ids, staged_ids, committed_ids)


def test_empty_added_file_unit_identity_stable(tmp: Path) -> None:
    """Edge of the add-identity unification (RVF reviewer cursor-cli finding):
    an EMPTY new file produces no diff hunk, so without the hunkless-add handling
    the committed/staged observation falls back to path_only and mints a unit_id
    that diverges from the untracked observation — re-dispatching a reviewed
    empty new file after commit. Assert all three forms share one unit_id."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    (repo / "empty.txt").write_text("", encoding="utf-8")

    dirty_obs = dt._classify_path(repo, "empty.txt")
    assert dirty_obs is not None and dirty_obs.kind == "untracked_file", dirty_obs
    dirty_ids = sorted(s.unit_id for s in dt._specs_from_observation(dirty_obs, "empty.txt"))

    run(["git", "add", "empty.txt"], cwd=repo)
    staged_obs = dt._classify_path(repo, "empty.txt")
    assert staged_obs is not None and staged_obs.change_type == "add", staged_obs
    staged_ids = sorted(s.unit_id for s in dt._specs_from_observation(staged_obs, "empty.txt"))

    run(["git", "commit", "-q", "-m", "add empty file"], cwd=repo)
    committed_obs = dt._classify_committed_path(repo, "empty.txt", baseline)
    assert committed_obs is not None and committed_obs.change_type == "add", committed_obs
    committed_ids = sorted(s.unit_id for s in dt._specs_from_observation(committed_obs, "empty.txt"))

    assert dirty_ids == staged_ids == committed_ids, (dirty_ids, staged_ids, committed_ids)
    assert len(dirty_ids) == 1, dirty_ids


def test_committed_added_file_dedup_reviewed_not_redispatched(tmp: Path) -> None:
    """The reported over-dispatch dual, at the allocate level: a NEW file (models
    a rename's add half / a new file authored + reviewed while dirty) is reviewed
    clean, then committed; the follow-up allocate with the round baseline must
    find NO candidate (status 'empty') — not re-dispatch it. Pre-fix the committed
    add minted a fresh unit_id, so it re-entered the candidate pool as available
    and the follow-up over-dispatched a no-op review round."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "newfile.txt").write_text("brand new content\n", encoding="utf-8")
    owned = dt.OwnedUnit(path="newfile.txt", unit="path", hunk_anchor=None)
    dt.register_claims(
        repo=repo, session_id="s1", run_id="r1", worktree=repo, branch=None,
        owned_paths=["newfile.txt"], apply_patch_paths={"newfile.txt"}, exec_only_paths=set(),
        owned_units_override=[(owned, "apply_patch")], log_root_override=logs,
    )
    first = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False,
    )
    assert first["status"] == "allocated", first
    assert first["candidate_unit_count"] >= 1, first
    dt.complete_review_scope(
        repo=repo, lease_id=first["lease_id"], scope_hash=first["scope_hash"], run_id="r1",
        log_root_override=logs,
    )
    reviewed_before = _units_full_by_path(first["tracker_dir"], "newfile.txt")
    reviewed_ids = [uid for uid, (_obs, rev) in reviewed_before.items() if rev == "reviewed"]
    assert reviewed_ids, reviewed_before

    run(["git", "add", "newfile.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "commit reviewed new file"], cwd=repo)
    dt.register_claims(
        repo=repo, session_id="s1", run_id="r2", worktree=repo, branch=None,
        owned_paths=["newfile.txt"], apply_patch_paths=set(), exec_only_paths=set(),
        owned_units_override=[(owned, "apply_patch")], log_root_override=logs,
        committed_paths={"newfile.txt"}, committed_baseline=baseline,
    )
    second = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r2", reviewer_id="rev2",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
    )
    assert second["status"] == "empty", second
    after = _units_full_by_path(first["tracker_dir"], "newfile.txt")
    for uid in reviewed_ids:
        assert after[uid][0] == "committed", (uid, after[uid])
        assert after[uid][1] == "reviewed", (uid, after[uid])


def test_complete_review_scope_mark_reviewed_gate(tmp: Path) -> None:
    """`complete_review_scope(mark_reviewed=False)` must release the lease but NOT
    flip any unit to 'reviewed' (the no-op-completion guard); the default
    (mark_reviewed=True) still flips. Without the gate, a no-op completion
    silently marks unreviewed work reviewed → reverse missed-review."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("base\nadded\n", encoding="utf-8")
    owned = dt.OwnedUnit(path="f.txt", unit="path", hunk_anchor=None)
    dt.register_claims(
        repo=repo, session_id="s1", run_id="r1", worktree=repo, branch=None,
        owned_paths=["f.txt"], apply_patch_paths={"f.txt"}, exec_only_paths=set(),
        owned_units_override=[(owned, "apply_patch")], log_root_override=logs,
    )
    alloc = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False,
    )
    assert alloc["status"] == "allocated", alloc
    released = dt.complete_review_scope(
        repo=repo, lease_id=alloc["lease_id"], scope_hash=alloc["scope_hash"], run_id="r1",
        mark_reviewed=False, log_root_override=logs,
    )
    assert released["released"] is True, released
    assert released["released_unit_count"] == 0, released
    after_noop = _units_full_by_path(alloc["tracker_dir"], "f.txt")
    assert after_noop, after_noop
    # No-op completion reverts assigned units to 'available' (re-reviewable), not 'reviewed'.
    assert all(rev == "available" for (_obs, rev) in after_noop.values()), after_noop

    # Nothing was marked reviewed, so the same work is still allocatable.
    re_alloc = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1b", reviewer_id="rev2",
        log_root_override=logs, auto_claim_observed=False,
    )
    assert re_alloc["status"] == "allocated", re_alloc
    dt.complete_review_scope(
        repo=repo, lease_id=re_alloc["lease_id"], scope_hash=re_alloc["scope_hash"], run_id="r1b",
        log_root_override=logs,
    )
    after_real = _units_full_by_path(alloc["tracker_dir"], "f.txt")
    assert any(rev == "reviewed" for (_obs, rev) in after_real.values()), after_real


def test_release_tracker_lease_noop_completion_does_not_mark_reviewed(tmp: Path) -> None:
    """finalize-level wiring of the no-op guard: `_release_tracker_lease` derives
    `mark_reviewed` from reviewer-artifact presence. A run_dir with NO
    reviewers/*/review-result.json (a no-op follow-up) must release the lease with
    did_review=False and leave units unreviewed; a run_dir WITH a reviewer
    artifact marks them reviewed."""
    import importlib  # noqa: PLC0415

    dt, _sm, _rbm = _round_baseline_committed_modules()
    if "rvf_run_finalize" in sys.modules:
        fin = sys.modules["rvf_run_finalize"]
    else:
        fin = importlib.import_module("rvf_run_finalize")
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("base\nadded\n", encoding="utf-8")
    owned = dt.OwnedUnit(path="f.txt", unit="path", hunk_anchor=None)

    def _write_contract(run_dir: Path, lease_id: str, scope_hash: str, run_id: str, units: list[str]) -> None:
        inputs = run_dir / "artifacts" / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        (inputs / "scope.contract.json").write_text(
            json.dumps({
                "tracker_lease_id": lease_id,
                "tracker_scope_hash": scope_hash,
                "run_id": run_id,
                "primary_units": units,
            }),
            encoding="utf-8",
        )

    original_log_root = os.environ.get("CODEX_RVF_LOG_ROOT")
    os.environ["CODEX_RVF_LOG_ROOT"] = str(logs)
    try:
        dt.register_claims(
            repo=repo, session_id="s1", run_id="r1", worktree=repo, branch=None,
            owned_paths=["f.txt"], apply_patch_paths={"f.txt"}, exec_only_paths=set(),
            owned_units_override=[(owned, "apply_patch")], log_root_override=logs,
        )
        alloc1 = dt.allocate_review_scope(
            repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
            log_root_override=logs, auto_claim_observed=False,
        )
        assert alloc1["status"] == "allocated", alloc1
        unit_ids = list(_units_full_by_path(alloc1["tracker_dir"], "f.txt").keys())

        # No-op follow-up: run_dir without any reviewer artifact.
        noop_run = tmp / "run-noop"
        _write_contract(noop_run, alloc1["lease_id"], alloc1["scope_hash"], "r1", unit_ids)
        noop_result = fin._release_tracker_lease(noop_run, repo, decision_kind="handoff")
        assert noop_result is not None, "expected a release result"
        assert noop_result["did_review"] is False, noop_result
        assert noop_result["released"] is True, noop_result
        after_noop = _units_full_by_path(alloc1["tracker_dir"], "f.txt")
        # No-op finalize reverts assigned units to 'available', never 'reviewed'.
        assert all(rev == "available" for (_obs, rev) in after_noop.values()), after_noop

        # Genuine review: run_dir with a reviewer artifact present.
        alloc2 = dt.allocate_review_scope(
            repo=repo, session_id="s1", run_id="r2", reviewer_id="rev2",
            log_root_override=logs, auto_claim_observed=False,
        )
        assert alloc2["status"] == "allocated", alloc2
        real_run = tmp / "run-real"
        reviewer_dir = real_run / "artifacts" / "reviewers" / "rev2"
        reviewer_dir.mkdir(parents=True, exist_ok=True)
        (reviewer_dir / "review-result.json").write_text("{}", encoding="utf-8")
        _write_contract(real_run, alloc2["lease_id"], alloc2["scope_hash"], "r2", unit_ids)
        real_result = fin._release_tracker_lease(real_run, repo, decision_kind="handoff")
        assert real_result is not None and real_result["did_review"] is True, real_result
        after_real = _units_full_by_path(alloc1["tracker_dir"], "f.txt")
        assert any(rev == "reviewed" for (_obs, rev) in after_real.values()), after_real
    finally:
        if original_log_root is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = original_log_root


def test_seal_round_baseline_to_head_advances_marker(tmp: Path) -> None:
    """rvf-land 封窗: `seal_round_baseline_to_head` advances the round-baseline
    marker from an older baseline to the current HEAD, so the next Stop's
    committed-round window (baseline..HEAD) is empty for the just-landed commit."""
    dt, _sm, rbm = _round_baseline_committed_modules()
    import seal_round_baseline_to_head as seal  # noqa: PLC0415
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    original_log_root = os.environ.get("CODEX_RVF_LOG_ROOT")
    original_task = os.environ.get("KANBAN_TASK_ID")
    os.environ["CODEX_RVF_LOG_ROOT"] = str(logs)
    os.environ["KANBAN_TASK_ID"] = "seal-task-1"
    try:
        # Marker initially pinned at the pre-work baseline.
        rbm.write_round_baseline_marker(
            task_id="seal-task-1", session_id=None, baseline_head=baseline, repo=str(repo),
        )
        (repo / "f.txt").write_text("base\nlanded\n", encoding="utf-8")
        run(["git", "add", "f.txt"], cwd=repo)
        run(["git", "commit", "-q", "-m", "land reviewed work"], cwd=repo)
        new_head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
        assert new_head != baseline

        result = seal.seal_round_baseline_to_head(str(repo))
        assert result["sealed"] is True, result
        assert result["baseline_head"] == new_head, result
        sealed_head = rbm.resolve_round_baseline_head(
            task_id="seal-task-1", session_id=None, repo=str(repo)
        )
        assert sealed_head == new_head, (sealed_head, new_head)
        # Window is now empty for the landed commit.
        assert dt._list_committed_round_changed_paths(repo, sealed_head) == []
    finally:
        for name, val in (("CODEX_RVF_LOG_ROOT", original_log_root), ("KANBAN_TASK_ID", original_task)):
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val


def test_review_highwater_marker_round_trip(tmp: Path) -> None:
    """review_highwater_marker: task-first/session-fallback keying, overwrite
    advances, missing/empty/no-key → None, clear removes. Mirrors
    round_baseline_marker's IO contract so finalize/seal writes land exactly where
    committed-round reads (same (task,session) under the same log_root())."""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from core.session_scope_allocation import review_highwater_marker as hwm  # noqa: PLC0415
    repo, _baseline = _committed_round_repo(tmp)
    root = tmp / "state"
    head_a = "a" * 40
    head_b = "b" * 40
    hwm.write_review_highwater(task_id="t1", session_id="s1", reviewed_head=head_a, repo=str(repo), root=root)
    assert hwm.resolve_review_highwater_head(task_id="t1", session_id=None, repo=str(repo), root=root) == head_a
    # Overwrite advances (high-water only moves forward by caller contract).
    hwm.write_review_highwater(task_id="t1", session_id="s1", reviewed_head=head_b, repo=str(repo), root=root)
    assert hwm.resolve_review_highwater_head(task_id="t1", session_id=None, repo=str(repo), root=root) == head_b
    # Session-only keying when no task_id.
    hwm.write_review_highwater(task_id=None, session_id="s9", reviewed_head=head_a, repo=str(repo), root=root)
    assert hwm.resolve_review_highwater_head(task_id=None, session_id="s9", repo=str(repo), root=root) == head_a
    # Missing / empty head / no key → None (no write).
    assert hwm.resolve_review_highwater_head(task_id="nope", session_id=None, repo=str(repo), root=root) is None
    assert hwm.write_review_highwater(task_id=None, session_id=None, reviewed_head=head_a, repo=str(repo), root=root) is None
    assert hwm.write_review_highwater(task_id="t2", session_id=None, reviewed_head="  ", repo=str(repo), root=root) is None
    # 仓身份是 key 的一部分（scope_marker_identity_key）：解析不出来一律 fail-closed 不写，
    # 而不是退到某个共享占位桶——那等于把「不同仓撞同一个 task id」原样搬个地方。
    assert hwm.write_review_highwater(
        task_id="t1", session_id=None, reviewed_head=head_a, repo=None, root=root
    ) is None
    assert hwm.write_review_highwater(
        task_id="t1", session_id=None, reviewed_head=head_a, repo=str(tmp / "not-a-git-repo"), root=root
    ) is None
    # 另一个仓的同名 task 读不到本仓的水位（撞 key 的实测反例：task-a877b 在两根分别指
    # cline-kanban 与 my-ai-setup）。
    other_repo, _ = _committed_round_repo(tmp / "other")
    assert hwm.resolve_review_highwater_head(
        task_id="t1", session_id=None, repo=str(other_repo), root=root
    ) is None
    removed = hwm.clear_review_highwater(task_id="t1", session_id="s1", repo=str(repo), root=root)
    assert removed, removed
    assert hwm.resolve_review_highwater_head(task_id="t1", session_id=None, repo=str(repo), root=root) is None


def test_seal_advances_review_highwater(tmp: Path) -> None:
    """rvf-land 封窗 must advance BOTH round-baseline AND the last-reviewed high-water
    to HEAD. committed-round prefers the high-water; if seal advanced only
    round-baseline, the next Stop would re-window the just-landed commit
    (over-dispatch) because the high-water still pointed at the prior review's HEAD."""
    _dt, _sm, _rbm = _round_baseline_committed_modules()
    import seal_round_baseline_to_head as seal  # noqa: PLC0415
    from core.session_scope_allocation import review_highwater_marker as hwm  # noqa: PLC0415
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    original_log_root = os.environ.get("CODEX_RVF_LOG_ROOT")
    original_task = os.environ.get("KANBAN_TASK_ID")
    os.environ["CODEX_RVF_LOG_ROOT"] = str(logs)
    os.environ["KANBAN_TASK_ID"] = "seal-hw-task"
    try:
        (repo / "f.txt").write_text("base\nlanded\n", encoding="utf-8")
        run(["git", "add", "f.txt"], cwd=repo)
        run(["git", "commit", "-q", "-m", "land reviewed work"], cwd=repo)
        new_head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
        result = seal.seal_round_baseline_to_head(str(repo))
        assert result["sealed"] is True, result
        assert result.get("highwater_marker_path"), result
        assert hwm.resolve_review_highwater_head(
            task_id="seal-hw-task", session_id=None, repo=str(repo)
        ) == new_head
    finally:
        for name, val in (("CODEX_RVF_LOG_ROOT", original_log_root), ("KANBAN_TASK_ID", original_task)):
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val


def test_advance_review_highwater_only_on_did_review(tmp: Path) -> None:
    """finalize 推进高水位的语义：``did_review=True`` → 写高水位到被审 HEAD；
    ``did_review=False``（纯 no-op 完成）→ 不写。偏向「重审」安全方向，杜绝把未审已提交
    工作静默标已审。键走 task（cline-kanban 主路径）。"""
    import importlib  # noqa: PLC0415

    if "rvf_run_finalize" in sys.modules:
        fin = sys.modules["rvf_run_finalize"]
    else:
        fin = importlib.import_module("rvf_run_finalize")
    from core.session_scope_allocation import review_highwater_marker as hwm  # noqa: PLC0415
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    original_log_root = os.environ.get("CODEX_RVF_LOG_ROOT")
    original_task = os.environ.get("KANBAN_TASK_ID")
    os.environ["CODEX_RVF_LOG_ROOT"] = str(logs)
    os.environ["KANBAN_TASK_ID"] = "hw-fin-task"
    try:
        run_dir = tmp / "run"
        # No-op completion (no reviewer artifacts) → high-water NOT advanced.
        rec_noop = {
            "tracker_lease_release": {"did_review": False},
            "workspace_diff": {"head_after": head},
        }
        out_noop = fin._advance_review_highwater(run_dir, repo, {}, {}, rec_noop)
        assert out_noop["status"] == "skipped", out_noop
        assert hwm.resolve_review_highwater_head(
            task_id="hw-fin-task", session_id=None, repo=str(repo)
        ) is None

        # Genuine review completion → high-water advanced to reviewed HEAD.
        rec_real = {
            "tracker_lease_release": {"did_review": True},
            "workspace_diff": {"head_after": head},
        }
        out_real = fin._advance_review_highwater(run_dir, repo, {}, {}, rec_real)
        assert out_real["status"] == "advanced", out_real
        assert out_real["reviewed_head"] == head, out_real
        assert hwm.resolve_review_highwater_head(
            task_id="hw-fin-task", session_id=None, repo=str(repo)
        ) == head
    finally:
        for name, val in (("CODEX_RVF_LOG_ROOT", original_log_root), ("KANBAN_TASK_ID", original_task)):
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val


def test_review_highwater_write_is_monotonic_with_repo(tmp: Path) -> None:
    """高水位只前移不回退（实际强制，非仅注释）：传入 repo 时，把高水位写成现值的严格祖先
    会被拒绝（保留更高水位）；前移正常推进；repo=None 退化盲写。修 task-keyed 跨 worktree
    落后上下文后完成 review 盲覆盖回退高水位 → committed-round 窗口偏宽重派已审工作 的缝
    （对抗式 review 发现，over-dispatch-regression lens）。"""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from core.session_scope_allocation import review_highwater_marker as hwm  # noqa: PLC0415
    repo, _baseline = _committed_round_repo(tmp)
    root = tmp / "state"
    (repo / "f.txt").write_text("base\nc1\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo); run(["git", "commit", "-q", "-m", "c1"], cwd=repo)
    c1 = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    (repo / "f.txt").write_text("base\nc1\nc2\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo); run(["git", "commit", "-q", "-m", "c2"], cwd=repo)
    c2 = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()

    # Advance to c2 (ahead).
    hwm.write_review_highwater(task_id="mono", session_id=None, reviewed_head=c2, repo=str(repo), root=root)
    assert hwm.resolve_review_highwater_head(task_id="mono", session_id=None, repo=str(repo), root=root) == c2
    # Regress to c1 (strict ancestor of c2) WITH repo → refused, stays c2.
    hwm.write_review_highwater(task_id="mono", session_id=None, reviewed_head=c1, repo=str(repo), root=root)
    assert hwm.resolve_review_highwater_head(task_id="mono", session_id=None, repo=str(repo), root=root) == c2, "regress must be refused"
    # Forward to c3 (descendant) → advances.
    (repo / "f.txt").write_text("base\nc1\nc2\nc3\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo); run(["git", "commit", "-q", "-m", "c3"], cwd=repo)
    c3 = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    hwm.write_review_highwater(task_id="mono", session_id=None, reviewed_head=c3, repo=str(repo), root=root)
    assert hwm.resolve_review_highwater_head(task_id="mono", session_id=None, repo=str(repo), root=root) == c3
    # repo=None → key 里的仓身份解析不出来 ⇒ 整个 marker 无归属，**不写**（fail-closed）。
    # 这条以前断言的是「退化成盲写、允许回退」；仓身份进 key 之后那条路径不再可达——
    # 能拿到 repo 才有 key，拿得到 key 就同样拿得到 ancestry 判据。守卫里 repo 为空的
    # 回退分支仍在，但只服务「git 调用本身失败」那一种，不再服务 repo=None。
    assert hwm.write_review_highwater(
        task_id="mono", session_id=None, reviewed_head=c1, repo=None, root=root
    ) is None
    assert hwm.resolve_review_highwater_head(task_id="mono", session_id=None, repo=str(repo), root=root) == c3


def test_parent_harness_identity_normalizes_every_signal_layer_to_one_token(tmp: Path) -> None:
    """同一个 harness 无论命中哪一层信号，都必须归一到**同一个 token**。

    这条是承重的，不是风格问题：round-baseline 的三个读写点各自只看得见不同的层——
    UPS / Stop 是 hook 子进程（shim 注入的 agent id）、``seal_round_baseline_to_head``
    跑在 agent shell（只有 harness 原生环境标记 / kanban 的 ``AI_AGENT`` 描述串）。
    三者写 / 读的是同一个 marker；任何一层归一得不一样，key 就静默裂开：封窗写进一个
    Stop 读不到的文件（封窗失效 ⇒ 多派），或者更糟，UPS 与 Stop 不一致（marker 消失
    ⇒ committed 窗口没了 ⇒ 漏审）。
    """
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from core.host_adapter import parent_harness_identity as phi  # noqa: PLC0415

    claude_layers = [
        phi.resolve_parent_harness_identity(explicit_host_kind="claude_code"),
        phi.resolve_parent_harness_identity(environ={phi.CLINE_KANBAN_AGENT_ID_ENV: "claude"}),
        phi.resolve_parent_harness_identity(environ={"CLAUDECODE": "1"}),
        phi.resolve_parent_harness_identity(environ={"CLAUDE_CODE_SESSION_ID": "x"}),
        phi.resolve_parent_harness_identity(
            environ={phi.KANBAN_RUNTIME_AGENT_DESCRIPTOR_ENV: "claude-code_2-1-231_agent"}
        ),
    ]
    assert set(claude_layers) == {phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE}, claude_layers

    codex_layers = [
        phi.resolve_parent_harness_identity(explicit_host_kind="codex"),
        phi.resolve_parent_harness_identity(environ={phi.CLINE_KANBAN_AGENT_ID_ENV: "codex"}),
        phi.resolve_parent_harness_identity(environ={"CODEX_SESSION_ID": "x"}),
        phi.resolve_parent_harness_identity(
            environ={phi.KANBAN_RUNTIME_AGENT_DESCRIPTOR_ENV: "codex_0-9-1_agent"}
        ),
    ]
    assert set(codex_layers) == {phi.PARENT_HARNESS_IDENTITY_CODEX_CLI}, codex_layers

    # ``CODEX_HOME`` 不是身份信号，是**常驻安装路径 override**（本仓 adapters/codex/
    # subagent.py 正是拿它解析 $CODEX_HOME/sessions）。它一旦被当成「当前进程是 Codex」
    # 的证据，就会在 harness 原生标记层抢答，而该层整体排在 ``AI_AGENT`` 之前——于是它
    # 压过下游明确的非 Codex 信号：设过 CODEX_HOME 的用户在 Cursor 会话、或在只有
    # AI_AGENT 可用的 kanban / seal 路径下，都会被误判成 codex-cli，seal 与 hook 两侧
    # 归一随即裂开（封窗写进 Stop 读不到的 key ⇒ 多派；UPS/Stop 不一致 ⇒ 漏审）。
    codex_home_environment = {"CODEX_HOME": "/Users/someone/.codex"}
    assert phi.resolve_parent_harness_identity(
        environ={**codex_home_environment, "CURSOR_AGENT": "1"}
    ) == phi.PARENT_HARNESS_IDENTITY_CURSOR_CLI, "CODEX_HOME 压过了明确的 CURSOR_AGENT"
    assert phi.resolve_parent_harness_identity(
        environ={
            **codex_home_environment,
            phi.KANBAN_RUNTIME_AGENT_DESCRIPTOR_ENV: "claude-code_2-1-231_agent",
        }
    ) == phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE, "CODEX_HOME 压过了明确的 AI_AGENT"
    assert phi.resolve_parent_harness_identity(
        environ={**codex_home_environment, "CLAUDECODE": "1"}
    ) == phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE, "CODEX_HOME 压过了同层的 CLAUDECODE"
    # 连一个别的信号都没有时也不许拿 CODEX_HOME 顶包：猜错一个**具体** token 会让写者与
    # 读者落到不同的 key（marker 消失 ⇒ 漏审），而 unspecified 在读侧有专门的回退候选，
    # 至多让窗口偏宽（多审）。判不出就老实判不出，严格更安全。
    assert (
        phi.resolve_parent_harness_identity(environ=dict(codex_home_environment))
        == phi.PARENT_HARNESS_IDENTITY_UNSPECIFIED
    ), "CODEX_HOME 被单独当成了 Codex 会话证据"
    # 会话级的 CODEX_SESSION_ID 才是真信号，必须继续生效（上面 codex_layers 已覆盖），
    # 且它与常驻的 CODEX_HOME 同时存在时结论不变。
    assert phi.resolve_parent_harness_identity(
        environ={**codex_home_environment, "CODEX_SESSION_ID": "x"}
    ) == phi.PARENT_HARNESS_IDENTITY_CODEX_CLI

    # 两个 harness 必须互不相等，否则整个分键是空操作。
    assert (
        phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE != phi.PARENT_HARNESS_IDENTITY_CODEX_CLI
    )
    # transcript schema 探测层（③）：实际读文件，不走字符串。
    claude_transcript = tmp / "claude.jsonl"
    claude_transcript.parent.mkdir(parents=True, exist_ok=True)
    claude_transcript.write_text('{"type": "user"}\n', encoding="utf-8")
    codex_transcript = tmp / "codex.jsonl"
    codex_transcript.write_text('{"type": "session_meta"}\n', encoding="utf-8")
    assert phi.resolve_parent_harness_identity(
        parent_transcript_path=claude_transcript, environ={}
    ) == phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE
    assert phi.resolve_parent_harness_identity(
        parent_transcript_path=codex_transcript, environ={}
    ) == phi.PARENT_HARNESS_IDENTITY_CODEX_CLI

    # 一层都没有 ⇒ unspecified（词表里的正常一员，读侧对它有专门的回退候选）。
    assert (
        phi.resolve_parent_harness_identity(environ={})
        == phi.PARENT_HARNESS_IDENTITY_UNSPECIFIED
    )
    # 未知 harness 拿自己的 bucket，而不是一律折进 unspecified——折进去等于让两个互不相干
    # 的未知 harness 重新开始互相覆盖，正是本次改 key 要消除的东西。
    unknown = phi.resolve_parent_harness_identity(
        environ={phi.CLINE_KANBAN_AGENT_ID_ENV: "some-new-harness"}
    )
    assert unknown not in (
        phi.PARENT_HARNESS_IDENTITY_UNSPECIFIED,
        phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE,
        phi.PARENT_HARNESS_IDENTITY_CODEX_CLI,
    ), unknown


def test_round_baseline_marker_is_not_shared_across_parent_harnesses(tmp: Path) -> None:
    """同一个 worktree / 同一个 task 被两个 harness 打开时，round-baseline 不得互相覆盖。

    这正是双状态根一直在偶然掩盖的分裂（Claude hook 落根 A、shell 进程落根 B）：统一根
    之后两个 harness 的 UserPromptSubmit 会写进同一个文件，A 的 Stop 于是读到 B 上一条
    prompt 的 HEAD，窗口下界被抬高 ⇒ 漏审。
    """
    _dt, _sm, rbm = _round_baseline_committed_modules()
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from core.host_adapter import parent_harness_identity as phi  # noqa: PLC0415

    repo, _baseline = _committed_round_repo(tmp)
    root = tmp / "state"
    claude_head = "a" * 40
    codex_head = "b" * 40

    for identity, head in (
        (phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE, claude_head),
        (phi.PARENT_HARNESS_IDENTITY_CODEX_CLI, codex_head),
    ):
        rbm.write_round_baseline_marker(
            task_id="shared-task", session_id=None, baseline_head=head,
            repo=str(repo), parent_harness_identity=identity, root=root,
        )

    assert rbm.resolve_round_baseline_head(
        task_id="shared-task", session_id=None, repo=str(repo),
        parent_harness_identity=phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE, root=root,
    ) == claude_head, "Codex 的 UPS 覆盖掉了 Claude 的 round-baseline"
    assert rbm.resolve_round_baseline_head(
        task_id="shared-task", session_id=None, repo=str(repo),
        parent_harness_identity=phi.PARENT_HARNESS_IDENTITY_CODEX_CLI, root=root,
    ) == codex_head

    # 判不出 harness 的写入方留下的那份，任何 harness 都读得到（读侧回退候选）——命中它
    # 至多让窗口偏宽（多审），不会让 marker 消失（漏审）。迁移过来的历史 marker 走同一条路。
    fallback_head = "c" * 40
    rbm.write_round_baseline_marker(
        task_id="fallback-task", session_id=None, baseline_head=fallback_head,
        repo=str(repo), parent_harness_identity=phi.PARENT_HARNESS_IDENTITY_UNSPECIFIED,
        root=root,
    )
    assert rbm.resolve_round_baseline_head(
        task_id="fallback-task", session_id=None, repo=str(repo),
        parent_harness_identity=phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE, root=root,
    ) == fallback_head


def test_exact_harness_scope_marker_candidates_outrank_unspecified_fallback(tmp: Path) -> None:
    """``unspecified`` 回退不得越过更精确的候选去抢先命中。

    ``unspecified`` 的设计意图是「**同一 key 维度内**判不出 harness 时的回退」。若候选顺序
    以 key 维度为外层、harness 为内层（``task-<exact>`` → ``task-unspecified`` →
    ``sess-<exact>`` → ``sess-unspecified``），那么本会话精确写入的 ``sess-<exact>`` 会被
    「迁移工具或另一个判不出身份的会话留下的 ``task-unspecified``」抢先命中——读到别人的
    baseline，窗口下界被**抬高** ⇒ 漏审。回退「至多让窗口偏宽（多审）」这条安全性论证，
    只在本会话确实没有更精确 marker 时才成立。

    故顺序必须是两层：外层 harness 精度、内层 key 维度。本用例同时钉死两条不变量——
    精度分层（新）与同层内 task 优先于 session（既有语义，不得被本次分层改掉）。
    """
    _dt, _sm, rbm = _round_baseline_committed_modules()
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from core.host_adapter import parent_harness_identity as phi  # noqa: PLC0415
    from core.session_scope_allocation import review_highwater_marker as hwm  # noqa: PLC0415
    from core.session_scope_allocation import scope_marker_identity_key as smk  # noqa: PLC0415

    # ---- ① 候选顺序本身：精确 harness 的两条全部排在任何 unspecified 之前 ----
    candidate_names = [
        path.name
        for path in smk.scope_marker_candidate_paths(
            marker_directory=tmp / "candidates",
            task_id_token="T",
            session_id_token="S",
            repository_identity="repoid",
            parent_harness_identity=phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE,
        )
    ]
    assert candidate_names == [
        f"task-T--repo-repoid--harness-{phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE}.json",
        f"sess-S--repo-repoid--harness-{phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE}.json",
        f"task-T--repo-repoid--harness-{phi.PARENT_HARNESS_IDENTITY_UNSPECIFIED}.json",
        f"sess-S--repo-repoid--harness-{phi.PARENT_HARNESS_IDENTITY_UNSPECIFIED}.json",
    ], candidate_names
    # 写者仍取 [0]：分层不得改变写入目标（最精确 harness + 最优先 key）。
    assert candidate_names[0].startswith("task-T--")
    # 不按 harness 分键的 marker（highwater）完全不受分层影响，仍是 task → sess。
    assert [
        path.name
        for path in smk.scope_marker_candidate_paths(
            marker_directory=tmp / "candidates",
            task_id_token="T",
            session_id_token="S",
            repository_identity="repoid",
            parent_harness_identity=None,
        )
    ] == ["task-T--repo-repoid.json", "sess-S--repo-repoid.json"]

    repo, _baseline = _committed_round_repo(tmp)
    root = tmp / "state"
    own_session_head = "a" * 40
    foreign_unspecified_head = "b" * 40
    own_task_head = "d" * 40

    # ---- ② 端到端：sess-<exact> 与 task-unspecified 并存时，读到本会话精确写入的那份 ----
    # 本会话 UPS 时拿不到 task_id，故精确写入 sess-<claude-code>。
    rbm.write_round_baseline_marker(
        task_id=None, session_id="own-session", baseline_head=own_session_head,
        repo=str(repo), parent_harness_identity=phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE,
        root=root,
    )
    # 迁移工具 / 另一个判不出身份的会话在同一个 task 下留下的那份。
    rbm.write_round_baseline_marker(
        task_id="shared-task", session_id=None, baseline_head=foreign_unspecified_head,
        repo=str(repo), parent_harness_identity=phi.PARENT_HARNESS_IDENTITY_UNSPECIFIED,
        root=root,
    )
    assert rbm.resolve_round_baseline_head(
        task_id="shared-task", session_id="own-session", repo=str(repo),
        parent_harness_identity=phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE, root=root,
    ) == own_session_head, "别的会话的 task-unspecified 抢在本会话 sess-<exact> 之前命中 ⇒ 漏审"

    # ---- ③ 既有语义：同一精度层内 task 仍优先于 session ----
    rbm.write_round_baseline_marker(
        task_id="shared-task", session_id=None, baseline_head=own_task_head,
        repo=str(repo), parent_harness_identity=phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE,
        root=root,
    )
    assert rbm.resolve_round_baseline_head(
        task_id="shared-task", session_id="own-session", repo=str(repo),
        parent_harness_identity=phi.PARENT_HARNESS_IDENTITY_CLAUDE_CODE, root=root,
    ) == own_task_head, "同一精度层内 task 优先于 session 的既有语义被改掉了"

    # ---- ④ 既有语义：本会话没有任何精确 marker 时，unspecified 回退仍接得住迁移产物 ----
    migrated_head = "e" * 40
    rbm.write_round_baseline_marker(
        task_id="migrated-only-task", session_id=None, baseline_head=migrated_head,
        repo=str(repo), parent_harness_identity=phi.PARENT_HARNESS_IDENTITY_UNSPECIFIED,
        root=root,
    )
    assert rbm.resolve_round_baseline_head(
        task_id="migrated-only-task", session_id="own-session", repo=str(repo),
        parent_harness_identity=phi.PARENT_HARNESS_IDENTITY_CODEX_CLI, root=root,
    ) == migrated_head, "unspecified 回退没接住迁移产物 ⇒ marker 消失"

    # ---- ⑤ highwater 不按 harness 分键，读写路径不受本次分层影响 ----
    highwater_head = "f" * 40
    hwm.write_review_highwater(
        task_id="hw-task", session_id="own-session", reviewed_head=highwater_head,
        repo=str(repo), root=root,
    )
    assert hwm.resolve_review_highwater_head(
        task_id="hw-task", session_id="own-session", repo=str(repo), root=root,
    ) == highwater_head


def test_scope_markers_do_not_collide_across_repositories_sharing_a_task_id(tmp: Path) -> None:
    """不同仓的同名 task id 不得共用 marker。

    实测反例：``round-baseline-pending/task-a877b.json`` 在一个状态根里指
    ``.cline/worktrees/a877b/cline-kanban``、在另一个根里指 ``Documents/GitHub/my-ai-setup``。
    cline-kanban 的 task id 是短哈希前缀，跨 project 撞车是必然而非意外。
    """
    _dt, _sm, rbm = _round_baseline_committed_modules()
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from core.session_scope_allocation import review_highwater_marker as hwm  # noqa: PLC0415

    first_repo, _ = _committed_round_repo(tmp / "first")
    second_repo, _ = _committed_round_repo(tmp / "second")
    root = tmp / "state"
    first_head = "1" * 40
    second_head = "2" * 40
    colliding_task_id = "a877b"

    for repo, head in ((first_repo, first_head), (second_repo, second_head)):
        hwm.write_review_highwater(
            task_id=colliding_task_id, session_id=None, reviewed_head=head,
            repo=str(repo), root=root,
        )
        rbm.write_round_baseline_marker(
            task_id=colliding_task_id, session_id=None, baseline_head=head,
            repo=str(repo), root=root,
        )

    for repo, head in ((first_repo, first_head), (second_repo, second_head)):
        assert hwm.resolve_review_highwater_head(
            task_id=colliding_task_id, session_id=None, repo=str(repo), root=root,
        ) == head, "另一个仓的水位覆盖了本仓的"
        assert rbm.resolve_round_baseline_head(
            task_id=colliding_task_id, session_id=None, repo=str(repo), root=root,
        ) == head

    # 同一个仓的不同 worktree **必须**共享身份（比的是 git_common_dir 而不是工作树路径）：
    # 要区分的是不同的仓，不是同一个仓的不同 attempt。
    linked_worktree = tmp / "linked-worktree"
    run(["git", "worktree", "add", "-q", "--detach", str(linked_worktree)], cwd=first_repo)
    assert hwm.resolve_review_highwater_head(
        task_id=colliding_task_id, session_id=None, repo=str(linked_worktree), root=root,
    ) == first_head


def test_committed_observation_excludes_base_branch_sync_merge(tmp: Path) -> None:
    """Files brought in only via a base-branch-sync merge (second parent) are
    excluded from committed-round paths; first-parent agent work is kept (§3)."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    run(["git", "checkout", "-q", "-b", "feature"], cwd=repo)
    # main advances with a base-only file.
    run(["git", "checkout", "-q", "main"], cwd=repo)
    (repo / "base_only.txt").write_text("from base\n", encoding="utf-8")
    run(["git", "add", "base_only.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base advance"], cwd=repo)
    # feature: agent's own first-parent commit, then merge main in.
    run(["git", "checkout", "-q", "feature"], cwd=repo)
    (repo / "feature.txt").write_text("agent work\n", encoding="utf-8")
    run(["git", "add", "feature.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "feature work"], cwd=repo)
    run(["git", "merge", "-q", "--no-edit", "main"], cwd=repo)
    paths = dt._list_committed_round_changed_paths(repo, baseline)
    assert "feature.txt" in paths, paths
    assert "base_only.txt" not in paths, paths


def test_committed_round_changed_paths_excludes_rebased_base_commits(tmp: Path) -> None:
    """The dual of the merge test for the path a *rebase* takes: when base work is
    REPLAYED onto the round's first-parent line (`git rebase` / `pull --rebase` /
    base-branch-sync diverged handback), `--first-parent --no-merges` no longer
    drops it (only merge SECOND parents are dropped). Supplying the live base ref
    (`main`) subtracts the base-reachable commits; own first-parent work stays.

    Reproduces the reported committed-round over-trigger: 34 base 'monthly-deck'
    files surfaced as this session's committed-but-unreviewed work after a rebase.
    `base_ref=None` keeps today's (buggy) behaviour — proving the fallback is safe
    and that the exclusion is the load-bearing change."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, _c0 = _committed_round_repo(tmp)
    run(["git", "checkout", "-q", "-b", "feature"], cwd=repo)
    # Own work done THIS round, then the round baseline is pinned at its tip
    # (mirrors the marker UserPromptSubmit writes at the task's HEAD).
    (repo / "feature.txt").write_text("agent work\n", encoding="utf-8")
    run(["git", "add", "feature.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "feature work"], cwd=repo)
    baseline = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    # main advances with a base-only file (the 'monthly-deck' analogue)...
    run(["git", "checkout", "-q", "main"], cwd=repo)
    (repo / "base_only.txt").write_text("from base\n", encoding="utf-8")
    run(["git", "add", "base_only.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base advance"], cwd=repo)
    # ...and the diverged feature branch absorbs it via REBASE (base commit lands
    # on the first-parent line as an ordinary non-merge commit).
    run(["git", "checkout", "-q", "feature"], cwd=repo)
    run(["git", "rebase", "-q", "main"], cwd=repo)
    # New own work committed after the rebase, so we can prove own work is kept.
    (repo / "own2.txt").write_text("more agent work\n", encoding="utf-8")
    run(["git", "add", "own2.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "more feature work"], cwd=repo)

    excluded = dt._list_committed_round_changed_paths(repo, baseline, base_ref="main")
    assert excluded == ["own2.txt"], excluded  # base_only.txt subtracted, own kept

    # Safe fallback: without a base ref the base file leaks back in (today's bug).
    leaked = dt._list_committed_round_changed_paths(repo, baseline, base_ref=None)
    assert "base_only.txt" in leaked and "own2.txt" in leaked, leaked
    # An unresolvable base ref degrades to the same no-exclusion behaviour.
    assert dt._base_ref_exclude_revs(repo, "no/such/ref") == []


def test_committed_round_base_ref_self_disables_when_head_on_base(tmp: Path) -> None:
    """Regression guard for the plain-repo flow: when the agent commits directly
    ON the base branch (HEAD == `main`), `^main` would subtract the session's OWN
    commits and silently disable committed-round entirely. `_base_ref_exclude_revs`
    must self-disable (return []) whenever HEAD is an ancestor-or-equal of base, so
    own committed work is still observed."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)  # HEAD is on `main`
    (repo / "own.txt").write_text("on-main work\n", encoding="utf-8")
    run(["git", "add", "own.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "work on main"], cwd=repo)
    # HEAD == main tip → no safe base to subtract → exclusion disabled.
    assert dt._base_ref_exclude_revs(repo, "main") == []
    kept = dt._list_committed_round_changed_paths(repo, baseline, base_ref="main")
    assert kept == ["own.txt"], kept  # NOT empty — own work survives


def test_committed_round_base_ref_excludes_ff_to_base(tmp: Path) -> None:
    """The sibling of the rebase test, for the path a *fast-forward* takes: when a
    task worktree (detached HEAD, or a non-base feature branch with no own commits)
    is quick-forwarded onto the base tip (``git merge --ff-only main`` /
    base-branch-sync non-divergent handback), ``HEAD == base tip`` but the checkout
    is NOT on the base branch ref. The previous ``merge-base --is-ancestor HEAD
    base`` guard saw ``HEAD == base tip`` and self-disabled → ``^base`` never fired →
    the base commits in ``baseline..HEAD`` leaked back as this session's committed-
    but-unreviewed work (the reported committed-round over-trigger).

    ``_base_ref_exclude_revs`` must NOT self-disable here (ff implies the branch has
    no own commits, so ``baseline..HEAD`` is pure base work): it returns ``^<tip>``
    and the committed-round window holds no base file. The on-base self-disable
    regression (committing directly on ``main``) is covered by the sibling test."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)  # HEAD on `main`, baseline at base
    # main advances with a base-only file the session does NOT own.
    (repo / "base_only.txt").write_text("from base\n", encoding="utf-8")
    run(["git", "add", "base_only.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base advance"], cwd=repo)
    base_tip = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()

    # Detached HEAD fast-forwarded to the base tip (worktree quick-forward).
    run(["git", "checkout", "-q", "--detach", base_tip], cwd=repo)
    assert dt._base_ref_exclude_revs(repo, "main") == [f"^{base_tip}"]
    detached_paths = dt._list_committed_round_changed_paths(repo, baseline, base_ref="main")
    assert "base_only.txt" not in detached_paths, detached_paths  # base work subtracted
    assert detached_paths == [], detached_paths  # pure base ff → empty window

    # Same for a NON-base feature branch fast-forwarded onto the base tip: ff only
    # succeeds because `feat` has no own commits, so the window is still pure base.
    run(["git", "checkout", "-q", "-b", "feat", baseline], cwd=repo)
    run(["git", "merge", "-q", "--ff-only", "main"], cwd=repo)
    assert dt._base_ref_exclude_revs(repo, "main") == [f"^{base_tip}"]
    feat_paths = dt._list_committed_round_changed_paths(repo, baseline, base_ref="main")
    assert "base_only.txt" not in feat_paths, feat_paths
    assert feat_paths == [], feat_paths


def test_build_manifest_includes_committed_round_owned_paths(tmp: Path) -> None:
    """An apply_patch-attributed file committed clean within the round still
    lands in owned_committed_round_paths and registers tracker ownership (§5)."""
    dt, sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    transcript = _committed_round_transcript(tmp, repo, session_id="s1", path="f.txt", old="base", new="changed")
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "agent commit"], cwd=repo)
    manifest = sm.build_manifest(repo, transcript, tracker_run_id="r1", tracker_log_root=logs, committed_baseline=baseline)
    assert manifest["owned_committed_round_paths"] == ["f.txt"], manifest["owned_committed_round_paths"]
    assert manifest["owned_dirty_paths"] == []
    assert manifest["tracker"]["status"] == "ok"
    # Zero-diff guarantee: no baseline => no committed scope.
    manifest_none = sm.build_manifest(repo, transcript, tracker_run_id="r2", tracker_log_root=tmp / "logs2", committed_baseline=None)
    assert manifest_none["owned_committed_round_paths"] == []


def test_build_manifest_attributes_committed_round_without_transcript_evidence(tmp: Path) -> None:
    """Second-gate fix: a path committed in-round with NO parent-session
    transcript tool evidence (sub-agent / headless runner / Kanban commit) is
    still attributed (evidence='committed_round_git') and registers session_units,
    so `_collect_candidate_unit_ids_in_txn` (EXISTS session_units) can see it.
    Reproduces the `no_session_owned_dirty` leak that survived 218c5ed."""
    dt, sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    # Transcript with session_meta only — no apply_patch / write / exec record,
    # i.e. the parent session never touched f.txt via a tool.
    transcript = tmp / "session.jsonl"
    records = [
        {"timestamp": "2026-06-24T00:00:00.000Z", "type": "session_meta", "payload": {"id": "s1", "cwd": str(repo)}},
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    # A "sub-agent" commits work to f.txt directly; HEAD is clean.
    (repo / "f.txt").write_text("base\nsubagent\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "subagent work"], cwd=repo)
    manifest = sm.build_manifest(repo, transcript, tracker_run_id="r1", tracker_log_root=logs, committed_baseline=baseline)
    assert manifest["owned_committed_round_paths"] == ["f.txt"], manifest["owned_committed_round_paths"]
    assert manifest["owned_dirty_paths"] == []
    assert manifest["tracker"]["status"] == "ok", manifest["tracker"]
    evidence = _session_units_evidence_by_path(manifest["tracker"]["tracker_dir"], "f.txt")
    assert evidence == {"committed_round_git"}, evidence


def _new_codex_rollout(
    tmp: Path,
    repo: Path,
    *,
    session_id: str,
    abs_path: str,
    old: str,
    new: str,
    include_patch_apply_end: bool = True,
    include_exec_embedded_apply_patch: bool = True,
) -> Path:
    """gpt-5.6-sol 世代 Codex rollout：编辑工具统一成 custom_tool_call name="exec"、
    apply_patch 以 JS 文本内嵌 input，旧 apply_patch 工具调用与 exec_command_end 消失，
    结构化编辑真相源是 event_msg/patch_apply_end。"""
    records: list[dict] = [
        {"timestamp": "2999-07-12T00:00:00.000Z", "type": "session_meta", "payload": {"id": session_id, "cwd": str(repo)}},
    ]
    if include_exec_embedded_apply_patch:
        records.append(
            {
                "timestamp": "2999-07-12T00:00:01.000Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "input": f'const r = await tools.apply_patch("*** Begin Patch\\n*** Update File: {abs_path}\\n...");',
                    "call_id": "call_exec",
                },
            }
        )
    if include_patch_apply_end:
        records.append(
            {
                "timestamp": "2999-07-12T00:00:02.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "patch_apply_end",
                    "call_id": "exec-abc",
                    "turn_id": "turn-1",
                    "success": True,
                    "changes": {abs_path: {"type": "update", "unified_diff": f"@@ -1 +1 @@\n-{old}\n+{new}\n"}},
                    "status": "completed",
                },
            }
        )
    transcript = tmp / "session.jsonl"
    transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return transcript


def test_build_manifest_recovers_ownership_from_new_codex_patch_apply_end(tmp: Path) -> None:
    """新 Codex（gpt-5.6-sol）把编辑走 exec custom_tool_call + patch_apply_end，旧解析器零
    识别 → owned_paths 空 → no_codebase_changes → Stop hook 误 skip 待审工作（本次报告的 bug）。
    修复后由 patch_apply_end 合成补丁文本恢复归属。"""
    _dt, sm, _rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")  # dirty
    transcript = _new_codex_rollout(tmp, repo, session_id="s1", abs_path=str(repo / "f.txt"), old="base", new="changed")
    manifest = sm.build_manifest(repo, transcript, tracker_run_id="r1", tracker_log_root=logs)
    assert manifest["owned_dirty_paths"] == ["f.txt"], manifest["owned_dirty_paths"]
    assert manifest["owned_paths"] == ["f.txt"], manifest["owned_paths"]
    assert manifest["schema_drift_suspected"] is False
    from core.session_scope_allocation.session_scope_gate_pure_helpers import (  # noqa: PLC0415
        session_change_type_from_manifest,
    )
    assert session_change_type_from_manifest(manifest) == "dirty_codebase_changes"


def test_build_manifest_new_codex_guard_flags_schema_drift(tmp: Path) -> None:
    """Fail-loud 守卫：有编辑证据（exec input 内嵌 apply_patch）却零归属 + 工作树 dirty
    → schema_drift_suspected=True（防未来 transcript schema 再漂移时静默盲）。"""
    _dt, sm, _rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")  # dirty
    # 有编辑证据（exec 内嵌 apply_patch）但无 patch_apply_end → 归属无法建立 → 守卫应触发。
    transcript = _new_codex_rollout(
        tmp, repo, session_id="s1", abs_path=str(repo / "f.txt"), old="base", new="changed",
        include_patch_apply_end=False, include_exec_embedded_apply_patch=True,
    )
    manifest = sm.build_manifest(repo, transcript, tracker_run_id="r1", tracker_log_root=logs)
    assert manifest["owned_paths"] == []
    assert manifest["schema_drift_suspected"] is True
    assert any("schema_drift_suspected" in w for w in manifest.get("warnings", []))


def test_build_manifest_no_schema_drift_flag_without_edit_evidence(tmp: Path) -> None:
    """守卫不误伤：纯读/纯命令（无编辑证据）+ dirty 工作树 → drift=False，避免把「本会话没
    编辑、dirty 来自他处」的合法情形误报为漂移。"""
    _dt, sm, _rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")  # dirty（非本会话所致）
    records = [
        {"timestamp": "2999-07-12T00:00:00.000Z", "type": "session_meta", "payload": {"id": "s1", "cwd": str(repo)}},
        {
            "timestamp": "2999-07-12T00:00:01.000Z",
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec", "input": 'await tools.exec_command({cmd:"ls -la"});', "call_id": "c1"},
        },
    ]
    transcript = tmp / "session.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    manifest = sm.build_manifest(repo, transcript, tracker_run_id="r1", tracker_log_root=logs)
    assert manifest["owned_paths"] == []
    assert manifest["schema_drift_suspected"] is False


def test_build_manifest_old_codex_patch_apply_end_not_double_counted(tmp: Path) -> None:
    """旧 Codex 对同一编辑同时发 apply_patch 工具调用与 patch_apply_end；两者都并入会重复
    登记 edit_claims。apply_patch 工具调用存在即旧格式，patch_apply_end 不再重复归属。"""
    _dt, sm, _rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")  # dirty
    patch = "*** Begin Patch\n*** Update File: f.txt\n@@\n-base\n+changed\n*** End Patch\n"
    records = [
        {"timestamp": "2999-07-02T00:00:00.000Z", "type": "session_meta", "payload": {"id": "s1", "cwd": str(repo)}},
        {
            "timestamp": "2999-07-02T00:00:01.000Z",
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "c1"},
        },
        {
            "timestamp": "2999-07-02T00:00:02.000Z",
            "type": "event_msg",
            "payload": {
                "type": "patch_apply_end",
                "call_id": "exec-xyz",
                "success": True,
                "changes": {str(repo / "f.txt"): {"type": "update", "unified_diff": "@@ -1 +1 @@\n-base\n+changed\n"}},
                "status": "completed",
            },
        },
    ]
    transcript = tmp / "session.jsonl"
    transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    manifest = sm.build_manifest(repo, transcript, tracker_run_id="r1", tracker_log_root=logs)
    assert manifest["owned_dirty_paths"] == ["f.txt"], manifest["owned_dirty_paths"]
    # patch_apply_end 被抑制 → apply_patch_operations 只来自单一 apply_patch 工具调用。
    assert len(manifest["apply_patch_operations"]) == 1, manifest["apply_patch_operations"]
    assert manifest["schema_drift_suspected"] is False


def test_build_manifest_resume_mixed_apply_patch_and_patch_apply_end(tmp: Path) -> None:
    """RVF-001 F1：resume / 切换模型的混合 session——前段旧式 apply_patch 工具调用编辑 f.txt、
    后段新式 patch_apply_end 编辑 g.txt。旧的全局布尔 gate（存在任意旧式调用即跳过所有
    patch_apply_end）会丢弃后段 g.txt 的归属；路径级去重后 g.txt 必须照常摄入。"""
    _dt, sm, _rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)  # f.txt = "base"
    logs = tmp / "logs"
    (repo / "g.txt").write_text("gbase\n", encoding="utf-8")
    run(["git", "add", "g.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "add g"], cwd=repo)
    # 两个文件都被本会话编辑（工作树 dirty）。
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")
    (repo / "g.txt").write_text("gchanged\n", encoding="utf-8")
    legacy_patch = "*** Begin Patch\n*** Update File: f.txt\n@@\n-base\n+changed\n*** End Patch\n"
    records = [
        {"timestamp": "2999-07-13T00:00:00.000Z", "type": "session_meta", "payload": {"id": "s1", "cwd": str(repo)}},
        # 前段旧式（CLI 升级前）：apply_patch 工具调用编辑 f.txt。
        {
            "timestamp": "2999-07-13T00:00:01.000Z",
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": legacy_patch, "call_id": "call_legacy"},
        },
        # 后段新式（CLI 升级后）：patch_apply_end 编辑 g.txt，无对应旧式工具调用。
        {
            "timestamp": "2999-07-13T00:00:02.000Z",
            "type": "event_msg",
            "payload": {
                "type": "patch_apply_end",
                "call_id": "exec-g1",
                "success": True,
                "changes": {str(repo / "g.txt"): {"type": "update", "unified_diff": "@@ -1 +1 @@\n-gbase\n+gchanged\n"}},
                "status": "completed",
            },
        },
    ]
    transcript = tmp / "session.jsonl"
    transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    manifest = sm.build_manifest(repo, transcript, tracker_run_id="r1", tracker_log_root=logs)
    assert manifest["owned_dirty_paths"] == ["f.txt", "g.txt"], manifest["owned_dirty_paths"]
    assert manifest["schema_drift_suspected"] is False
    ops_paths = sorted({op.get("path") for op in manifest["apply_patch_operations"]})
    assert ops_paths == ["f.txt", "g.txt"], ops_paths


def test_build_manifest_new_codex_rename_edit_attributes_target(tmp: Path) -> None:
    """RVF-001 F2：新 Codex patch_apply_end 的 change 同时带 move_path 与 unified_diff（未暂存
    的 rename+edit）。合成补丁文本必须把 `*** Move to:` 紧挨 `*** Update File:`、写在 hunk 之前，
    否则 mutation hunk 会被归到已被 rename 走的源路径 → target 落入 unattributed_dirty、Stop
    hook 误跳过。修复后 target 路径进入 owned_dirty。"""
    _dt, sm, _rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)  # src 用 f.txt
    logs = tmp / "logs"
    (repo / "src.txt").write_text("l1\nl2\n", encoding="utf-8")
    run(["git", "add", "src.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "add src"], cwd=repo)
    # 未暂存的 rename+edit：src.txt 删除、dst.txt 为改后新文件。
    os.remove(repo / "src.txt")
    (repo / "dst.txt").write_text("l1\nl2edited\n", encoding="utf-8")
    records = [
        {"timestamp": "2999-07-13T00:00:00.000Z", "type": "session_meta", "payload": {"id": "s1", "cwd": str(repo)}},
        {
            "timestamp": "2999-07-13T00:00:02.000Z",
            "type": "event_msg",
            "payload": {
                "type": "patch_apply_end",
                "call_id": "exec-r1",
                "success": True,
                "changes": {
                    str(repo / "src.txt"): {
                        "type": "update",
                        "move_path": str(repo / "dst.txt"),
                        "unified_diff": "@@ -1,2 +1,2 @@\n l1\n-l2\n+l2edited\n",
                    }
                },
                "status": "completed",
            },
        },
    ]
    transcript = tmp / "session.jsonl"
    transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    manifest = sm.build_manifest(repo, transcript, tracker_run_id="r1", tracker_log_root=logs)
    assert "dst.txt" in manifest["owned_dirty_paths"], manifest["owned_dirty_paths"]
    assert "dst.txt" not in manifest["unattributed_dirty_paths"], manifest["unattributed_dirty_paths"]
    assert manifest["schema_drift_suspected"] is False


def test_build_manifest_readonly_command_with_apply_patch_substring_is_not_edit_evidence(tmp: Path) -> None:
    """RVF-001 F4：新式 exec input 内的只读命令（``rg -n apply_patch`` / ``grep apply_patch``）
    含子串 ``apply_patch`` 但不是真编辑。裸子串判定会把它误计为编辑证据 → 叠加背景 dirty 文件
    → schema_drift_suspected 误报（F3 生效后会造成高频误跳过）。精确匹配 ``tools.apply_patch(``
    调用形态后，只读命令不再计入编辑证据。"""
    _dt, sm, _rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "background.txt").write_text("bg\n", encoding="utf-8")  # 背景 dirty（非本会话编辑）
    records = [
        {"timestamp": "2999-07-13T00:00:00.000Z", "type": "session_meta", "payload": {"id": "s1", "cwd": str(repo)}},
        {
            "timestamp": "2999-07-13T00:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": 'const r = await tools.exec_command({cmd: "rg -n apply_patch core/"});',
                "call_id": "c_ro",
            },
        },
    ]
    transcript = tmp / "session.jsonl"
    transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    manifest = sm.build_manifest(repo, transcript, tracker_run_id="r1", tracker_log_root=logs)
    assert manifest["owned_paths"] == []
    assert manifest["schema_drift_suspected"] is False


def _load_stop_review_validate_fix_module():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "stop_review_validate_fix_for_rvf001", SCRIPT_DIR / "stop_review_validate_fix.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load stop_review_validate_fix module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_schema_drift_suspected_triggers_loud_skip_via_stop_gate(tmp: Path) -> None:
    """RVF-001 F3：manifest 报 schema_drift_suspected（有编辑证据但零归属 + dirty）时，
    `refresh_global_diff_tracker` 必须返回带 `error` 的 sentinel、走与 session_manifest_failed
    同形的 loud-skip 通路（session_scope_gate_payload 与 tracker-disabled 的 legacy 路径均覆盖），
    产出显式可见的 skip 原因，而非静默降级为 no_session_owned_dirty。修复前守卫只 emit warning
    后仍 observed=True 继续 → allocator 空归属 → 静默跳过，名不副实的 fail-loud。"""
    _dt, _sm, _rbm = _round_baseline_committed_modules()
    stop = _load_stop_review_validate_fix_module()
    repo, _baseline = _committed_round_repo(tmp)  # f.txt 已提交 "base"
    (repo / "f.txt").write_text("dirty\n", encoding="utf-8")  # 工作树 dirty
    transcript = tmp / "session.jsonl"
    records = [
        {"timestamp": "2999-07-13T00:00:00.000Z", "type": "session_meta", "payload": {"id": "sess-drift", "cwd": str(repo)}},
        {"timestamp": "2999-07-13T00:00:01.000Z", "type": "event_msg", "payload": {"type": "user_message", "message": "改一下 f.txt。"}},
        # 编辑证据：exec input 内嵌 tools.apply_patch(，但无 patch_apply_end → 归属无法建立。
        {
            "timestamp": "2999-07-13T00:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": 'text(await tools.apply_patch("*** Begin Patch\\n*** Update File: f.txt\\n@@\\n-base\\n+dirty\\n*** End Patch"));',
                "call_id": "c1",
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    event = {"cwd": str(repo), "transcript_path": str(transcript)}
    old_log = os.environ.get("CODEX_RVF_LOG_ROOT")
    old_disable = os.environ.get("RVF_TRACKER_DISABLE")
    try:
        os.environ.pop("RVF_TRACKER_DISABLE", None)
        # 1) tracker-enabled：refresh 返回带 error 的 sentinel；gate 转为 loud skip。
        os.environ["CODEX_RVF_LOG_ROOT"] = str(tmp / "state-enabled")
        (tmp / "state-enabled").mkdir(parents=True, exist_ok=True)
        ledger = stop.start_run("rvf001-f3-enabled", repo=str(repo), cwd=str(repo))
        refresh = stop.refresh_global_diff_tracker(stop.resolve_stop_context(event, str(repo), ledger), ledger)
        assert refresh.get("error"), refresh
        assert "schema_drift" in refresh["error"], refresh["error"]

        ledger2 = stop.start_run("rvf001-f3-enabled-2", repo=str(repo), cwd=str(repo))
        result = stop.session_scope_gate_payload(event, str(repo), ledger2)
        assert result is not None
        system_message = result.get("systemMessage", "")
        assert "reason=session_manifest_failed" in system_message, system_message
        assert "no_session_owned_dirty" not in system_message
        raw2 = ledger2.events_path.read_text(encoding="utf-8") if ledger2.events_path.exists() else ""
        assert "session_transcript_schema_drift_suspected" in raw2

        # 2) tracker-disabled 的 legacy 路径：同样 loud skip（不落回 no_session_owned_dirty）。
        ledger3 = stop.start_run("rvf001-f3-legacy", repo=str(repo), cwd=str(repo))
        legacy_result = stop.legacy_session_scope_gate_payload(event, str(repo), ledger3)
        assert legacy_result is not None
        legacy_message = legacy_result.get("systemMessage", "")
        assert "reason=session_manifest_failed" in legacy_message, legacy_message
        assert "no_session_owned_dirty" not in legacy_message
        raw3 = ledger3.events_path.read_text(encoding="utf-8") if ledger3.events_path.exists() else ""
        assert "session_transcript_schema_drift_suspected" in raw3
    finally:
        if old_log is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = old_log
        if old_disable is None:
            os.environ.pop("RVF_TRACKER_DISABLE", None)
        else:
            os.environ["RVF_TRACKER_DISABLE"] = old_disable


def test_committed_round_changed_paths_excludes_skip_review_trailer(tmp: Path) -> None:
    """Per-commit opt-out: a round commit carrying the `RVF-Skip-Review` trailer
    drops its EXCLUSIVE paths from the committed-round set, while a path also
    touched by a normal round commit stays (it remains in that commit's
    name-only block), and normal-commit paths are kept."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    # Normal round commit: normal.txt (exclusive) + shared.txt.
    (repo / "normal.txt").write_text("normal\n", encoding="utf-8")
    (repo / "shared.txt").write_text("v1\n", encoding="utf-8")
    run(["git", "add", "normal.txt", "shared.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "normal round work"], cwd=repo)
    # Opt-out round commit: skip_only.txt (exclusive) + shared.txt again.
    (repo / "skip_only.txt").write_text("skip\n", encoding="utf-8")
    (repo / "shared.txt").write_text("v2\n", encoding="utf-8")
    run(["git", "add", "skip_only.txt", "shared.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "headless cleanup", "-m", "RVF-Skip-Review: noise commit"], cwd=repo)
    skip_shas = dt._list_round_skip_review_commit_shas(repo, baseline)
    assert len(skip_shas) == 1, skip_shas
    paths = dt._list_committed_round_changed_paths(repo, baseline)
    assert "normal.txt" in paths, paths
    assert "shared.txt" in paths, paths  # kept: also in the non-skip commit's block
    assert "skip_only.txt" not in paths, paths  # excluded: only the skip commit touched it
    # Bare-token form (no colon / value) also opts a commit out.
    (repo / "bare_skip.txt").write_text("bare\n", encoding="utf-8")
    run(["git", "add", "bare_skip.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "bare opt out", "-m", "RVF-Skip-Review"], cwd=repo)
    assert len(dt._list_round_skip_review_commit_shas(repo, baseline)) == 2
    assert "bare_skip.txt" not in dt._list_committed_round_changed_paths(repo, baseline)


def test_committed_then_dirty_same_path_resolves_to_dirty(tmp: Path) -> None:
    """A path both committed in-round and further dirtied: the live worktree
    change is observed 'dirty' (committed walk runs first, dirty walk wins; §5)."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("base\nX\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "committed X"], cwd=repo)
    (repo / "f.txt").write_text("base\nX\nY\n", encoding="utf-8")  # further dirty edit
    owned = dt.OwnedUnit(path="f.txt", unit="path", hunk_anchor=None)
    dt.register_claims(
        repo=repo, session_id="s1", run_id="r1", worktree=repo, branch=None,
        owned_paths=["f.txt"], apply_patch_paths={"f.txt"}, exec_only_paths=set(),
        owned_units_override=[(owned, "apply_patch")], log_root_override=logs,
    )
    result = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
    )
    states = set(_units_observed_state_by_path(result["tracker_dir"], "f.txt").values())
    assert "dirty" in states, states  # the live worktree edit is captured


def test_committed_unit_gone_after_reset_is_superseded(tmp: Path) -> None:
    """A committed unit that drops out of baseline..HEAD (commit reset away) is
    swept to 'superseded' by the unified supersession chokepoint."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("base\nadded\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "work"], cwd=repo)
    dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
    )
    tracker_dir = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1b", reviewer_id="rev1b",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
        dry_run=True,
    )["tracker_dir"]
    states_before = _units_observed_state_by_path(tracker_dir, "f.txt")
    committed_ids = [uid for uid, st in states_before.items() if st == "committed"]
    assert committed_ids, states_before
    # Drop the commit; the committed unit leaves baseline..HEAD.
    run(["git", "reset", "-q", "--hard", baseline], cwd=repo)
    dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r2", reviewer_id="rev2",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
    )
    states_after = _units_observed_state_by_path(tracker_dir, "f.txt")
    assert all(states_after.get(uid) == "superseded" for uid in committed_ids), states_after


def test_round_baseline_marker_round_trip(tmp: Path) -> None:
    """write/read/dual-key/overwrite/status for the round-baseline marker."""
    _dt, _sm, rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    root = tmp / "state"
    head_a = "a" * 40
    head_b = "b" * 40
    # task-keyed write, then overwrite (multi-prompt semantics: last wins).
    rbm.write_round_baseline_marker(task_id="t1", session_id="s1", baseline_head=head_a, repo=str(repo), root=root)
    assert rbm.read_round_baseline_marker(task_id="t1", session_id=None, repo=str(repo), root=root)["baseline_head"] == head_a
    rbm.write_round_baseline_marker(task_id="t1", session_id="s1", baseline_head=head_b, repo=str(repo), root=root)
    marker = rbm.read_round_baseline_marker(task_id="t1", session_id=None, repo=str(repo), root=root)
    assert marker["baseline_head"] == head_b
    # session fallback when no task id.
    rbm.write_round_baseline_marker(task_id=None, session_id="s9", baseline_head=head_a, repo=str(repo), root=root)
    assert rbm.read_round_baseline_marker(task_id=None, session_id="s9", repo=str(repo), root=root)["baseline_head"] == head_a
    # status + resolve helper.
    assert rbm.round_baseline_status(marker) == rbm.STATUS_ACTIVE
    assert rbm.resolve_round_baseline_head(task_id="t1", session_id=None, repo=str(repo), root=root) == head_b
    assert rbm.round_baseline_status(marker, now_ts=4102444800.0) == rbm.STATUS_STALE  # year 2100
    assert rbm.round_baseline_status({"baseline_head": ""}) == rbm.STATUS_INVALID
    assert rbm.round_baseline_status(None) == rbm.STATUS_INVALID
    # No keys / empty head / 仓身份解析不出 => no write（后者 fail-closed，见 scope_marker_identity_key）。
    assert rbm.write_round_baseline_marker(task_id=None, session_id=None, baseline_head=head_a, repo=str(repo), root=root) is None
    assert rbm.write_round_baseline_marker(task_id="t2", session_id=None, baseline_head="", repo=str(repo), root=root) is None
    assert rbm.write_round_baseline_marker(task_id="t2", session_id=None, baseline_head=head_a, repo=None, root=root) is None


def test_round_baseline_marker_expiry_extension_does_not_overwrite_concurrent_developer_prompt_baseline(
    tmp: Path,
) -> None:
    """续期（host 合成回合）与真实 prompt 的 baseline 写入对同一份 marker 必须串行化。

    交错时序：续期读到旧 marker（head_a）→ 真实 prompt 写入新 baseline（head_b）→ 续期把它**之前读到**的
    head_a / captured_at 整份写回 ⇒ 真实 prompt 的 round 边界被静默抹掉。同一把按 marker 文件的排他锁
    把两者串行化：续期要么先完成（随后被新 baseline 覆盖），要么读到的就已经是新 baseline。
    """
    import threading  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    _dt, _sm, rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    root = tmp / "state"
    head_before_developer_prompt = "a" * 40
    head_at_concurrent_developer_prompt = "b" * 40
    rbm.write_round_baseline_marker(
        task_id="t1", session_id="s1", baseline_head=head_before_developer_prompt, repo=str(repo), root=root
    )

    extension_side_has_read_marker = threading.Event()
    developer_prompt_side_started = threading.Event()
    original_round_baseline_status = rbm.round_baseline_status

    def round_baseline_status_paused_after_extension_side_read(marker, **kwargs):
        extension_side_has_read_marker.set()
        developer_prompt_side_started.wait(timeout=5)
        _time.sleep(0.4)  # 无锁时真实 prompt 在这段窗口里早已写完新 baseline
        return original_round_baseline_status(marker, **kwargs)

    extension_results: list[object] = []

    def extension_side():
        extension_results.append(
            rbm.extend_round_baseline_marker_expiry_without_moving_baseline_head(
                task_id="t1", session_id="s1", repo=str(repo), root=root
            )
        )

    def developer_prompt_side():
        extension_side_has_read_marker.wait(timeout=5)
        developer_prompt_side_started.set()
        rbm.write_round_baseline_marker(
            task_id="t1", session_id="s1", baseline_head=head_at_concurrent_developer_prompt,
            repo=str(repo), root=root,
        )

    rbm.round_baseline_status = round_baseline_status_paused_after_extension_side_read
    try:
        threads = [threading.Thread(target=extension_side), threading.Thread(target=developer_prompt_side)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive(), "锁不得造成死锁"
    finally:
        rbm.round_baseline_status = original_round_baseline_status

    assert extension_results and extension_results[0] is not None, (
        "续期本身应照常发生（它先拿到了锁、读到的是活跃 marker）", extension_results
    )
    final = rbm.read_round_baseline_marker(task_id="t1", session_id="s1", repo=str(repo), root=root)
    assert final["baseline_head"] == head_at_concurrent_developer_prompt, (
        "续期把旧 baseline_head 整份写回，覆盖了并发真实 prompt 写入的新 round 边界", final
    )
    assert rbm.round_baseline_status(final) == rbm.STATUS_ACTIVE, final


def test_allocate_review_scope_includes_committed_round(tmp: Path) -> None:
    """End-to-end: round-baseline marker present + in-round commit ⇒ the
    committed unit reaches allocator scope through resolve→build_manifest→allocate."""
    dt, sm, rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    state_root = tmp / "state"
    transcript = _committed_round_transcript(tmp, repo, session_id="s1", path="f.txt", old="base", new="changed")
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "agent commit"], cwd=repo)
    # Marker written at the prior prompt (baseline = pre-commit HEAD).
    rbm.write_round_baseline_marker(task_id=None, session_id="s1", baseline_head=baseline, repo=str(repo), root=state_root)
    resolved = rbm.resolve_round_baseline_head(
        task_id=None, session_id="s1", repo=str(repo), root=state_root
    )
    assert resolved == baseline
    sm.build_manifest(repo, transcript, tracker_run_id="r1", tracker_log_root=logs, committed_baseline=resolved)
    result = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=resolved,
    )
    assert result["status"] == "allocated", result
    assert result["candidate_unit_count"] >= 1, result


def load_dispatch_reviewers_module():
    # dispatch_reviewers imports core.run_ledger.run_ledger / run_alternative_reviewer / trajectory_distill
    # from SCRIPT_DIR, so SCRIPT_DIR must be importable.
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "dispatch_reviewers", SCRIPT_DIR / "dispatch_reviewers.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load dispatch_reviewers module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # 本测试簇只验证既有状态机；真实宿主/Git 分界由独立集成测试覆盖。
    module.require_prepared_run_review_start_boundary = lambda **kwargs: {"test_scope": "dispatch-unit"}
    return module


def load_agentic_initiation_guardian_dispatch_module():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "agentic_initiation_guardian_dispatch",
        SCRIPT_DIR / "agentic_initiation_guardian_dispatch.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load agentic_initiation_guardian_dispatch module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_guardian_analyze_completion_milestones_and_retirement(tmp_path: Path) -> None:
    """S8·R23 守护节点计数:analyze 完成累计 1/2/4/8/16 首达各派一次守护分析;
    16 后守护下线(guardian_retired)并给出 R9 删除确认提示(含 reviewed_head 前置);
    下线后不再派发;计数器落盘幂等。全程 RVF_HOME 隔离。"""
    guardian = load_agentic_initiation_guardian_dispatch_module()
    saved_rvf_home = os.environ.get("RVF_HOME")
    os.environ["RVF_HOME"] = str(tmp_path / "rvf-home")
    try:
        root = tmp_path / "guardian-root"
        dispatched: list[int] = []

        def fake_dispatcher(*, milestone: int, guardian_root: Path, metrics_snapshot: dict) -> dict:
            dispatched.append(milestone)
            assert guardian_root == root
            assert "snapshot_at" in metrics_snapshot
            return {"milestone": milestone, "launch_status": "launched"}

        retirement_prompts: list[str] = []
        for i in range(1, 18):
            result = guardian.record_analyze_completion(
                run_dir=f"/tmp/run-{i}",
                root=root,
                guardian_analysis_dispatcher=fake_dispatcher,
            )
            assert result["status"] == "recorded", result
            assert result["analyze_completion_count"] == i
            if result.get("retirement_prompt"):
                retirement_prompts.append(result["retirement_prompt"])

        assert dispatched == [1, 2, 4, 8, 16], dispatched
        # 恰在 16 那次给出一次 R9 删除确认提示,含 reviewed_head 前置依赖。
        assert len(retirement_prompts) == 1, retirement_prompts
        assert "R9" in retirement_prompts[0]
        assert "reviewed_head" in retirement_prompts[0]
        counter = json.loads(
            (root / guardian.ANALYZE_COMPLETION_COUNTER_FILENAME).read_text(encoding="utf-8")
        )
        assert counter["analyze_completion_count"] == 17
        assert counter["dispatched_milestones"] == [1, 2, 4, 8, 16]
        assert counter["guardian_retired"] is True
        # 派发器抛异常不阻断计数(best-effort)。
        def broken_dispatcher(**_kwargs: object) -> dict:
            raise RuntimeError("boom")

        result = guardian.record_analyze_completion(
            root=tmp_path / "guardian-root-2", guardian_analysis_dispatcher=broken_dispatcher
        )
        assert result["status"] == "recorded" and result["analyze_completion_count"] == 1
        assert "dispatch_error" in result
    finally:
        if saved_rvf_home is None:
            os.environ.pop("RVF_HOME", None)
        else:
            os.environ["RVF_HOME"] = saved_rvf_home


def test_guardian_handoff_doc_resolution_works_in_repo_and_degrades_when_absent(
    tmp_path: Path,
) -> None:
    """默认派发器不依赖固定 parents 层级：repo 树能解析，vendored 无 docs 树返回 None。"""
    guardian = load_agentic_initiation_guardian_dispatch_module()
    resolved = guardian._resolve_guardian_handoff_doc()
    assert resolved == ROOT / "docs" / "agentic-initiation-overhaul" / "guardian-agent-handoff.md"
    assert resolved.is_file()

    original_file = guardian.__file__
    guardian.__file__ = str(tmp_path / "vendored" / "skills" / "scripts" / "guardian.py")
    try:
        assert guardian._resolve_guardian_handoff_doc() is None
    finally:
        guardian.__file__ = original_file


def test_guardian_failed_milestones_retry_and_retire_only_after_success(
    tmp_path: Path,
) -> None:
    """节点失败不越过；更高 count 重试最高 due 并吸收低节点，16 成功前不得退休。"""
    guardian = load_agentic_initiation_guardian_dispatch_module()

    def broken_dispatcher(**_kwargs: object) -> dict:
        raise RuntimeError("boom")

    root = tmp_path / "retry"
    first = guardian.record_analyze_completion(
        root=root, guardian_analysis_dispatcher=broken_dispatcher
    )
    assert first["analyze_completion_count"] == 1
    assert first["milestone_dispatched"] is None and "dispatch_error" in first
    second = guardian.record_analyze_completion(
        root=root,
        guardian_analysis_dispatcher=lambda **kw: {
            "launch_status": "launched",
            "milestone": kw["milestone"],
        },
    )
    assert second["analyze_completion_count"] == 2
    assert second["milestone_dispatched"] == 2
    counter = json.loads(
        (root / guardian.ANALYZE_COMPLETION_COUNTER_FILENAME).read_text(encoding="utf-8")
    )
    assert counter["dispatched_milestones"] == [1, 2]

    # 返回 launch_failed（非抛异常）同样不能误记成功。
    returned_failure = guardian.record_analyze_completion(
        root=tmp_path / "returned-launch-failure",
        guardian_analysis_dispatcher=lambda **_kw: {
            "launch_status": "launch_failed",
            "error": "tmux unavailable",
        },
    )
    assert returned_failure["milestone_dispatched"] is None
    assert "dispatch_error" in returned_failure

    retirement_root = tmp_path / "retirement-retry"
    retirement_root.mkdir(parents=True)
    (retirement_root / guardian.ANALYZE_COMPLETION_COUNTER_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "analyze_completion_count": 15,
                "dispatched_milestones": [1, 2, 4, 8],
                "guardian_retired": False,
            }
        ),
        encoding="utf-8",
    )
    failed_16 = guardian.record_analyze_completion(
        root=retirement_root, guardian_analysis_dispatcher=broken_dispatcher
    )
    assert failed_16["analyze_completion_count"] == 16
    assert failed_16["guardian_retired"] is False
    assert failed_16["retirement_prompt"] is None

    retried_16 = guardian.record_analyze_completion(
        root=retirement_root,
        guardian_analysis_dispatcher=lambda **_kw: {"launch_status": "already_running"},
    )
    assert retried_16["analyze_completion_count"] == 17
    assert retried_16["milestone_dispatched"] == 16
    assert retried_16["guardian_retired"] is True
    assert "R9" in retried_16["retirement_prompt"]


def test_guardian_counter_read_modify_write_is_serialized(tmp_path: Path) -> None:
    """并发心跳/analyze 完成不丢增量；两个 JSON 计数文件各自以旁路 flock 串行化。"""
    guardian = load_agentic_initiation_guardian_dispatch_module()
    now = datetime.datetime(2026, 7, 12, 12, 0, tzinfo=datetime.timezone.utc)
    total = 48

    daily_root = tmp_path / "daily"
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        list(
            pool.map(
                lambda _i: guardian.record_stop_event_heartbeat(now=now, root=daily_root),
                range(total),
            )
        )
    daily = json.loads(
        (daily_root / guardian.DAILY_EVENT_COUNTS_FILENAME).read_text(encoding="utf-8")
    )
    assert daily["stop_event_counts_by_utc_day"]["2026-07-12"] == total, daily

    analyze_root = tmp_path / "analyze"
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(
            pool.map(
                lambda _i: guardian.record_analyze_completion(
                    root=analyze_root,
                    guardian_analysis_dispatcher=lambda **_kw: {"launch_status": "launched"},
                ),
                range(total),
            )
        )
    assert all(result["status"] == "recorded" for result in results), results
    counter = json.loads(
        (analyze_root / guardian.ANALYZE_COMPLETION_COUNTER_FILENAME).read_text(encoding="utf-8")
    )
    assert counter["analyze_completion_count"] == total, counter
    assert counter["dispatched_milestones"] == [1, 2, 4, 8, 16], counter


def test_guardian_initiation_liveness_alarm_paths(tmp_path: Path) -> None:
    """S8·R23 wall-clock 心跳判定矩阵:
    stop==0 → None;stop>0 且注入/review 双零 → 告警;注入非零或 review 非零 → None;
    窗口外旧心跳不计入。全程 RVF_HOME 隔离(注入 ledger 与守护计数同根)。"""
    guardian = load_agentic_initiation_guardian_dispatch_module()
    saved_rvf_home = os.environ.get("RVF_HOME")
    os.environ["RVF_HOME"] = str(tmp_path / "rvf-home")
    try:
        import datetime as _dt

        now = _dt.datetime(2026, 7, 12, 12, 0, tzinfo=_dt.timezone.utc)
        noop_dispatcher = lambda **_kw: {"launch_status": "faked"}  # noqa: E731
        # 零状态:无告警。
        assert guardian.evaluate_initiation_layer_liveness(now=now) is None
        # 窗口外旧心跳(5 天前)不计入 → 仍无告警。
        guardian.record_stop_event_heartbeat(now=now - _dt.timedelta(days=5))
        assert guardian.evaluate_initiation_layer_liveness(now=now) is None
        # 窗口内心跳但发起层从未活跃(冷启动护栏)→ 仍无告警。
        guardian.record_stop_event_heartbeat(now=now)
        assert guardian.evaluate_initiation_layer_liveness(now=now) is None
        # seed 历史活跃证据(analyze 完成计数)→ stop>0 且注入/review 双零 → 告警。
        guardian.record_analyze_completion(guardian_analysis_dispatcher=noop_dispatcher)
        alarm = guardian.evaluate_initiation_layer_liveness(now=now)
        assert alarm is not None, alarm
        assert alarm["alarm"] == "initiation_layer_suspected_silent_death"
        assert alarm["stop_event_count"] == 1 and alarm["injection_attempt_count"] == 0
        assert "疑似静默死亡" in alarm["message"]
        # review 完成非零 → 解除(独立分支,先于注入分支验证,回避真实时钟)。
        guardian.record_review_completion(now=now)
        assert guardian.evaluate_initiation_layer_liveness(now=now) is None
        # 注入尝试非零 → 解除:换一个干净 guardian 根重建「stop>0、review==0」态,
        # 写一条注入 ledger 记录(recorded_at=真实当下,恒 >= 锚点窗口下界——真实时钟
        # 只会晚于 2026-07-12 锚点,字典序比较对 ISO 日期单调安全)。
        import agentic_initiation_injection_attempt_ledger as injection_ledger  # noqa: PLC0415

        clean_root = tmp_path / "guardian-root-injection-branch"
        guardian.record_stop_event_heartbeat(now=now, root=clean_root)
        guardian.record_analyze_completion(root=clean_root, guardian_analysis_dispatcher=noop_dispatcher)
        assert guardian.evaluate_initiation_layer_liveness(now=now, root=clean_root) is not None
        injection_ledger.record_injection_attempt(
            worktree=str(tmp_path / "repo"),
            session_id="sess-guardian",
            candidate_fingerprint="sha256:deadbeef",
            candidate_path_count=1,
            has_committed_but_unreviewed=False,
            mint_outcome=injection_ledger.INJECTION_OUTCOME_INJECTED,
            injected=True,
        )
        assert guardian.evaluate_initiation_layer_liveness(now=now, root=clean_root) is None
    finally:
        if saved_rvf_home is None:
            os.environ.pop("RVF_HOME", None)
        else:
            os.environ["RVF_HOME"] = saved_rvf_home


def test_guardian_liveness_utc_day_bucket_boundary(tmp_path: Path) -> None:
    """window_days=3 含下界日+当日共 4 个 UTC 桶：恰 3 天前抑制，4 天前不抑制。"""
    guardian = load_agentic_initiation_guardian_dispatch_module()
    saved_rvf_home = os.environ.get("RVF_HOME")
    os.environ["RVF_HOME"] = str(tmp_path / "rvf-home")
    try:
        import agentic_initiation_injection_attempt_ledger as injection_ledger  # noqa: PLC0415

        now = datetime.datetime(2026, 7, 12, 12, 0, tzinfo=datetime.timezone.utc)
        root = tmp_path / "guardian"
        guardian.record_stop_event_heartbeat(now=now, root=root)
        guardian.record_analyze_completion(
            now=now,
            root=root,
            guardian_analysis_dispatcher=lambda **_kw: {"launch_status": "launched"},
        )

        def write_attempt_at(day_offset: int) -> Path:
            path = injection_ledger.record_injection_attempt(
                worktree=str(tmp_path / "repo"),
                session_id="bucket-boundary",
                candidate_fingerprint=f"sha256:{day_offset}",
                candidate_path_count=1,
                has_committed_but_unreviewed=False,
                mint_outcome=injection_ledger.INJECTION_OUTCOME_INJECTED,
                injected=True,
            )
            assert path is not None
            record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
            record["recorded_at"] = (
                now - datetime.timedelta(days=day_offset)
            ).replace(hour=0, minute=0).isoformat().replace("+00:00", "Z")
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            return path

        ledger_path = write_attempt_at(3)
        assert guardian.evaluate_initiation_layer_liveness(now=now, root=root) is None
        ledger_path.unlink()
        write_attempt_at(4)
        alarm = guardian.evaluate_initiation_layer_liveness(now=now, root=root)
        assert alarm is not None, alarm
        assert "含当日共 4 个桶" in alarm["message"]
        assert "完整 72 小时" in alarm["message"]
    finally:
        if saved_rvf_home is None:
            os.environ.pop("RVF_HOME", None)
        else:
            os.environ["RVF_HOME"] = saved_rvf_home


def test_detached_wrapper_post_exit_argv_appended_with_failsafe(tmp_path: Path) -> None:
    """S8:detached wrapper 的可选 post-exit 回调——缺省时输出与旧版逐字节相同
    (不含 ``|| true``);给定时追加在 finalize-status 之后、以 ``|| true`` 兜底。"""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import rvf_detached_thread as detached  # noqa: PLC0415

    kwargs = dict(
        argv=["echo", "hi"],
        log_path=tmp_path / "t.log",
        status_path=tmp_path / "t.status.json",
        exports={"K": "v"},
        stdin_path=None,
    )
    baseline = detached._build_wrapper_shell(**kwargs)
    assert "|| true" not in baseline
    default_none = detached._build_wrapper_shell(**kwargs, post_exit_argv=None)
    assert default_none == baseline
    with_post = detached._build_wrapper_shell(
        **kwargs, post_exit_argv=["python3", "guardian.py", "record-analyze-completion"]
    )
    assert with_post.startswith(baseline), with_post
    assert with_post.endswith("|| true"), with_post
    assert "guardian.py record-analyze-completion" in with_post


def test_detached_wrapper_executes_real_guardian_cli_post_exit(tmp_path: Path) -> None:
    """真实 sh wrapper → guardian CLI record-analyze-completion 接缝会把计数 +1。"""
    guardian = load_agentic_initiation_guardian_dispatch_module()
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import rvf_detached_thread as detached  # noqa: PLC0415

    rvf_home = tmp_path / "rvf-home"
    guardian_root = rvf_home / guardian.GUARDIAN_DIRNAME
    guardian_root.mkdir(parents=True)
    # 预记 milestone 1，避免本接缝测试真实拉起 guardian detached agent。
    (guardian_root / guardian.ANALYZE_COMPLETION_COUNTER_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "analyze_completion_count": 0,
                "dispatched_milestones": [1],
                "guardian_retired": False,
            }
        ),
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    wrapper = detached._build_wrapper_shell(
        argv=["/usr/bin/true"],
        log_path=tmp_path / "wrapper.log",
        status_path=tmp_path / "wrapper.status.json",
        exports={"RVF_HOME": str(rvf_home)},
        post_exit_argv=[
            sys.executable,
            str(SCRIPT_DIR / "agentic_initiation_guardian_dispatch.py"),
            "record-analyze-completion",
            "--run-dir",
            str(run_dir),
        ],
    )
    subprocess.run(["/bin/sh", "-c", wrapper], check=True)
    counter = json.loads(
        (guardian_root / guardian.ANALYZE_COMPLETION_COUNTER_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert counter["analyze_completion_count"] == 1
    assert counter["last_run_dir"] == str(run_dir)


def test_rvf_analyze_thread_wires_guardian_post_exit_cli_signature(tmp_path: Path) -> None:
    """analyze launcher 生成的 tmux wrapper 含真实 guardian CLI 子命令与 run-dir 参数。"""
    module = load_rvf_analyze_thread_module()
    run_dir = tmp_path / "runs" / "rvf-post-exit-wire"
    analysis = _seed_analysis_payload(run_dir)
    fake_tmux = write_fake_tmux_script(tmp_path / "fake_tmux.py")
    tmux_calls = tmp_path / "tmux-calls.jsonl"
    saved = {
        key: os.environ.get(key)
        for key in ("RVF_TMUX_BIN", "FAKE_TMUX_CALLS", "FAKE_TMUX_RETURNCODE")
    }
    os.environ["RVF_TMUX_BIN"] = str(fake_tmux)
    os.environ["FAKE_TMUX_CALLS"] = str(tmux_calls)
    os.environ["FAKE_TMUX_RETURNCODE"] = "0"
    try:
        result = module.launch_detached_analyze_thread(
            event={}, ledger=_AnalyzeLedgerStub(), analysis=analysis, finalize_record=None
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert result["launch_status"] == "launched"
    tmux_argv = json.loads(tmux_calls.read_text(encoding="utf-8").splitlines()[0])["argv"]
    wrapper = tmux_argv[-1]
    assert str(SCRIPT_DIR / "agentic_initiation_guardian_dispatch.py") in wrapper
    assert "record-analyze-completion" in wrapper
    assert "--run-dir" in wrapper and str(run_dir) in wrapper


def load_harness_limit_cooldown_module():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "harness_limit_cooldown", SCRIPT_DIR / "harness_limit_cooldown.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load harness_limit_cooldown module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _failing_text_reviewer_config_without_review_result(path: Path, *, stderr_text: str) -> Path:
    """文本格式 reviewer 配置：shim 往 stderr 吐给定错误并非零退出、不写 review-result。

    stderr 文本是否带额度签名决定 dispatch 的分支：带签名走「记跨 run cooldown 的额度路径」，
    不带则走「只在本轮排除该 harness 的通用失败路径」（auth 失效/崩溃等）。
    """
    return write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse(
        path,
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import sys; sys.stdin.read(); "
                f"sys.stderr.write({stderr_text!r}); sys.stderr.flush(); sys.exit(1)"
            ),
        ],
        activity_check_interval_seconds=0.05,
        output_format="text",
    )


def _usage_limit_text_reviewer_config(path: Path, *, stderr_text: str) -> Path:
    """文本格式 reviewer 配置：shim 往 stderr 吐额度签名并非零退出、不写 review-result。"""
    return _failing_text_reviewer_config_without_review_result(path, stderr_text=stderr_text)


def _rc_zero_but_no_review_result_reviewer_config(path: Path, *, label: str) -> Path:
    """文本格式 reviewer 配置：shim 干净退出(rc0)却**不**写 review-result。

    专测「腿有效」判据不能只看 returncode：进程怎么退出与有没有产出评审结论是两回事。
    """
    cfg = write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse(
        path,
        [
            sys.executable,
            "-u",
            "-c",
            "import sys; sys.stdin.read(); sys.stdout.write('done, but wrote nothing\\n')",
        ],
        activity_check_interval_seconds=0.05,
        output_format="text",
    )
    payload = json.loads(cfg.read_text(encoding="utf-8"))
    payload["label"] = label
    cfg.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return cfg


def _review_request_pending_text_reviewer_config(path: Path, *, label: str) -> Path:
    """文本格式 reviewer 配置：shim 写合法 ``kind: request`` review-result 并 rc0。

    这是 review 协议的合法待处理终态（reviewer 缺上下文/要标准子集），**不是**失败腿：
    dispatch 不得把它当 ``no_review_result`` 换腿，否则替换腿一成功 plan 就变 completed，
    request 被静默丢弃、主会话再也不会按 Contract Gate 处理它。
    """
    cfg = write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse(
        path,
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import os, subprocess, sys; "
                "sys.stdin.read(); "
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'context-request', '--out', os.environ['RVF_REVIEW_RESULT'], "
                "'--need', 'file', '--reason', "
                "'review packet 缺少被改函数的调用方文件，无法判断影响面'], check=True); "
                "print('request written')"
            ),
        ],
        activity_check_interval_seconds=0.05,
        output_format="text",
    )
    payload = json.loads(cfg.read_text(encoding="utf-8"))
    payload["label"] = label
    cfg.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return cfg


def _clean_text_reviewer_config(path: Path, *, label: str) -> Path:
    """文本格式 reviewer 配置：shim 写合法 no-issues review-result 并 rc0（reroute 备援腿用）。"""
    cfg = write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse(
        path,
        [
            sys.executable,
            "-u",
            "-c",
            clean_review_result_python(stdout="ok"),
        ],
        activity_check_interval_seconds=0.05,
        output_format="text",
    )
    payload = json.loads(cfg.read_text(encoding="utf-8"))
    payload["label"] = label
    cfg.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return cfg


def _dispatch_registry() -> dict:
    """路由算法的冻结 fixture：三条真实 harness id，【刻意不随出厂 registry 增长】。

    这里测的是 route() 这个纯函数的选路行为，不是出厂 registry 装了几条腿。把 fixture
    绑到出厂那份，等于每加一条评审腿就要重写一批与选路无关的断言，而且 available 集
    的大小本身就是 routing_rule / selection_mode 的判据（len(available)>=3、
    len(selection_pool)==2）——跟着涨会让这些断言测的东西悄悄换掉。
    出厂 registry 与各腿 config 是否一一对应，由
    tests/test_reviewer_registry_additive_merge.py 单独钉住。
    """
    return {
        "schema_version": 1,
        "harnesses": {
            "cursor": {
                "harness_id": "cursor",
                "label_prefix": "alternative-reviewer:cursor-cli",
                "config_path": "config/alternative-reviewer.cursor.json",
                "dispatch_mode": "external_cli",
                "enabled": True,
                "priority_default": 100,
            },
            "claude_code": {
                "harness_id": "claude_code",
                "label_prefix": "alternative-reviewer:claude-code",
                "config_path": "config/alternative-reviewer.claude.json",
                "dispatch_mode": "external_cli",
                "enabled": True,
                "priority_default": 50,
            },
            "codex": {
                "harness_id": "codex",
                "label_prefix": "alternative-reviewer:codex-cli",
                "config_path": "config/alternative-reviewer.codex.json",
                "dispatch_mode": "external_cli",
                "enabled": True,
                "priority_default": 50,
            },
        },
    }


def load_rvf_detached_thread_module():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "rvf_detached_thread", SCRIPT_DIR / "rvf_detached_thread.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_detached_thread module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_realexec_tmux_script(path: Path) -> Path:
    """fake tmux：同步执行 wrapper shell（区别于 write_fake_tmux_script 只记录调用）。

    真实 ``tmux new-session -d`` detach 所有 fd 后台运行；测试为确定性改为同步跑完
    wrapper（被包命令 + ``--finalize-status`` 回调），故 launch_detached 返回时
    status.json 已落终态，便于断言两阶段写入。
    """
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys\n"
        "shell = sys.argv[-1]\n"  # tmux new-session -d -s <name> <shell>
        "raise SystemExit(subprocess.run(['/bin/sh', '-c', shell]).returncode)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _detached_status_payload() -> dict:
    return {
        "schema_version": 1,
        "started_at": "t0",
        "returncode": None,
        "finished_at": None,
        "launch_status": "launched",
        "error": None,
    }


def _seed_detached_stale_lock(root: Path, *, returncode: object) -> tuple[Path, Path]:
    """预置一个「持锁线程已退出」的现场：写锁文件 + 带指定 returncode 的 status.json。

    ``returncode=None`` 表示未干净完成（launched 后没回写）；``returncode=0`` 表示干净完成。
    """
    lock_path = root / "s.lock"
    status_path = root / "s.status.json"
    lock_path.write_text("rvf-detached-unit\nt0\n", encoding="utf-8")
    payload: dict[str, object] = {"launch_status": "launched", "finished_at": None}
    if returncode is not None:
        payload["returncode"] = returncode
        payload["finished_at"] = "t1"
    else:
        payload["returncode"] = None
    status_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return lock_path, status_path


def _launch_detached_with_staleness_env(
    module,
    root: Path,
    *,
    lock_path: Path,
    status_path: Path,
    has_session_rc: str,
    new_session_rc: str = "0",
) -> tuple[dict, list[list]]:
    """用 subcommand-aware fake tmux 跑一次 launch_detached，返回 (result, 记录的 tmux argv)。"""
    fake_tmux = write_subcommand_aware_tmux_script(root / "tmux.py")
    calls = root / "calls.jsonl"
    saved = {
        k: os.environ.get(k)
        for k in (
            "RVF_TMUX_BIN",
            "FAKE_TMUX_CALLS",
            "FAKE_TMUX_RETURNCODE",
            "FAKE_TMUX_HAS_SESSION_RETURNCODE",
        )
    }
    os.environ["RVF_TMUX_BIN"] = str(fake_tmux)
    os.environ["FAKE_TMUX_CALLS"] = str(calls)
    os.environ["FAKE_TMUX_RETURNCODE"] = new_session_rc
    os.environ["FAKE_TMUX_HAS_SESSION_RETURNCODE"] = has_session_rc
    try:
        result = module.launch_detached(
            session_name="rvf-detached-unit",
            argv=["echo", "hi"],
            log_path=root / "s.log",
            status_path=status_path,
            lock_path=lock_path,
            status_payload=_detached_status_payload(),
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    recorded = (
        [json.loads(line)["argv"] for line in calls.read_text(encoding="utf-8").splitlines()]
        if calls.exists()
        else []
    )
    return result, recorded


def _run_alternative_reviewer_summary(run_dir: Path, reviewer_id: str = "test") -> dict:
    reviewer_dir = run_dir / "artifacts" / "reviewers" / reviewer_id
    return json.loads(
        next(reviewer_dir.glob("reviewer.summary*.json")).read_text(encoding="utf-8")
    )


def test_run_alternative_reviewer_usage_limit_codex_json_error(tmp_path: Path) -> None:
    """codex_json 吐 turn.failed + 真实额度文案 → 退出码 125 + summary usage_limit_exhausted。"""
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import json, sys; sys.stdin.read(); "
                "print(json.dumps({'type':'turn.failed','error':{'message':"
                "'You have hit your usage limit. Please try again in 4h.'}}), flush=True)"
            ),
        ],
        activity_check_interval_seconds=0.05,
        output_format="codex_json",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 125, (completed.returncode, completed.stderr)
    assert "RVF_EXTERNAL_REVIEWER_USAGE_LIMIT" in completed.stderr, completed.stderr
    summary = _run_alternative_reviewer_summary(run_dir)
    assert summary["output_error_reason"] == "usage_limit_exhausted", summary
    assert "usage limit" in (summary.get("output_error_message") or "").lower(), summary


def test_run_alternative_reviewer_usage_limit_stderr_text(tmp_path: Path) -> None:
    """text reviewer：stderr 吐额度签名 + 非零退出 + 无 review-result → 退出码 125。"""
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = _usage_limit_text_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        stderr_text="Error: rate limit exceeded — HTTP 429 Too Many Requests\n",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 125, (completed.returncode, completed.stderr)
    assert "RVF_EXTERNAL_REVIEWER_USAGE_LIMIT" in completed.stderr, completed.stderr
    summary = _run_alternative_reviewer_summary(run_dir)
    assert summary["output_error_reason"] == "usage_limit_exhausted", summary


def test_run_alternative_reviewer_usage_limit_no_false_positive_success(tmp_path: Path) -> None:
    """成功评审正文含 'rate limit' → 退出码 0、非额度（rc0 路径不扫正文）。"""
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            clean_review_result_python(
                stdout="review complete: consider adding rate limit handling for HTTP 429"
            ),
        ],
        activity_check_interval_seconds=0.05,
        output_format="text",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "RVF_EXTERNAL_REVIEWER_USAGE_LIMIT" not in (completed.stderr or ""), completed.stderr
    summary = _run_alternative_reviewer_summary(run_dir)
    assert summary.get("output_error_reason") is None, summary


def test_run_alternative_reviewer_usage_limit_no_false_positive_invalid_result(tmp_path: Path) -> None:
    """rc0 子进程 + 无合法 review-result + 正文含 429/rate limit → reviewer_result_invalid，非 usage_limit、非 125（D2 回归）。"""
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import os, sys; sys.stdin.read(); "
                "open(os.environ['RVF_REVIEW_RESULT'], 'w', encoding='utf-8')"
                ".write('not-valid-json — discussing http 429 rate limit'); "
                "print('model body mentions rate limit and 429')"
            ),
        ],
        activity_check_interval_seconds=0.05,
        output_format="text",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1, (completed.returncode, completed.stderr)
    assert completed.returncode != 125
    assert "RVF_EXTERNAL_REVIEWER_USAGE_LIMIT" not in (completed.stderr or ""), completed.stderr
    summary = _run_alternative_reviewer_summary(run_dir)
    assert summary.get("output_error_reason") is None, summary


def test_harness_limit_cooldown_record_active_sweep(tmp_path: Path) -> None:
    """record→active True；过期→lazy sweep 后 False；默认 TTL 3600；parse_reset_hint '4h'→14400。"""
    from datetime import datetime, timedelta, timezone

    cd = load_harness_limit_cooldown_module()
    root = tmp_path / "cooldown"
    assert cd.default_ttl_seconds() == 3600.0
    assert cd.parse_reset_hint("please try again in 4h") == 14400.0
    assert cd.parse_reset_hint("retry in 30 minutes") == 1800.0
    assert cd.parse_reset_hint("Retry-After: 120") == 120.0
    assert cd.parse_reset_hint("no reset hint here") is None

    cd.record("codex", reason="usage_limit_exhausted", error_message="hit usage limit", root=root)
    assert cd.active("codex", root=root) is True
    assert cd.active("claude_code", root=root) is False
    assert set(cd.active_harnesses(root=root)) == {"codex"}

    # 写一条过期 marker → active() 的 lazy sweep 应清掉它。
    past = (
        (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    )
    marker_path = cd._marker_path("codex", root)
    marker_path.write_text(
        json.dumps(
            {"marker_version": 1, "harness_id": "codex", "recorded_at": past, "expires_at": past}
        ),
        encoding="utf-8",
    )
    assert cd.active("codex", root=root) is False
    assert not marker_path.exists()


def _make_usage_or_clean_registry(tmp_path: Path, specs: dict[str, tuple[str, int]]) -> dict:
    """specs: {harness_id: (kind, priority)}，返回内存 registry。

    kind：
      - ``'usage'``      额度耗尽（rc125 + stderr 额度签名）→ 走记 cooldown 的额度路径
      - ``'auth'``       认证失败（rc1、无额度签名、不写 review-result）→ 走通用失败路径
      - ``'rc0_no_result'`` 干净退出但不写 review-result → 验「腿有效」不能只看 returncode
      - ``'review_request'`` 干净退出 + 合法 ``kind: request`` → 合法待处理终态，不是失败腿
      - 其它（``'clean'``）合法 no-issues 结论 + rc0
    """
    reg: dict = {"schema_version": 1, "harnesses": {}}
    for hid, (kind, prio) in specs.items():
        cfg_path = tmp_path / f"alt-{hid}.json"
        if kind == "usage":
            _usage_limit_text_reviewer_config(
                cfg_path, stderr_text=f"fatal: usage limit reached for {hid} plan\n"
            )
        elif kind == "auth":
            _failing_text_reviewer_config_without_review_result(
                cfg_path,
                stderr_text=(
                    "Error: Authentication required. Please run 'agent login' first, "
                    f"or set {hid.upper()}_API_KEY environment variable.\n"
                ),
            )
        elif kind == "rc0_no_result":
            _rc_zero_but_no_review_result_reviewer_config(
                cfg_path, label=f"alternative-reviewer:{hid}-cli"
            )
        elif kind == "review_request":
            _review_request_pending_text_reviewer_config(
                cfg_path, label=f"alternative-reviewer:{hid}-cli"
            )
        else:
            _clean_text_reviewer_config(cfg_path, label=f"alternative-reviewer:{hid}-cli")
        reg["harnesses"][hid] = {
            "harness_id": hid,
            "label_prefix": f"alternative-reviewer:{hid}-cli",
            "config_path": str(cfg_path),
            "dispatch_mode": "external_cli",
            "enabled": True,
            "priority_default": prio,
        }
    return reg


def _with_cooldown_env(cooldown_root: Path):
    """上下文：把 RVF_HARNESS_LIMIT_COOLDOWN_ROOT 指到 tmp，退出时还原（防污染真实 ~/.rvf）。"""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        key = "RVF_HARNESS_LIMIT_COOLDOWN_ROOT"
        prev = os.environ.get(key)
        os.environ[key] = str(cooldown_root)
        try:
            yield
        finally:
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev

    return _ctx()


# ---------------------------------------------------------------------------
# rvf_decision_token_store（RVF overhaul S1：agentic-initiation 决策 token 基建）
# 全部落盘根目录经显式参数注入 tmp 隔离，绝不触碰真实 ~/.rvf。
# ---------------------------------------------------------------------------

def _decision_token_store_roots(tmp: Path) -> dict[str, Path]:
    """给 mint_decision_token 一组 tmp 隔离的三根目录（token / 冷却表 / 熔断 ledger）。"""
    return {
        "root": tmp / "decision-tokens",
        "cooldown_root": tmp / "declined-fingerprints",
        "circuit_breaker_root_dir": tmp / "circuit-breaker",
    }


def _fixed_utc(seconds_from_zero: int = 0):
    import datetime as _dt  # noqa: PLC0415

    return _dt.datetime(2026, 7, 11, 12, 0, 0, tzinfo=_dt.timezone.utc) + _dt.timedelta(
        seconds=seconds_from_zero
    )


def test_decision_token_mint_and_legal_state_machine(tmp: Path) -> None:
    """合法状态链 minted→injected→acknowledged→dispatched：每步打对应时间戳、追加 history，
    mark_injected 幂等、acknowledged 记 start_attempt_count、dispatched 为终态且携带严格度。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    tok_root = roots["root"]
    t0 = _fixed_utc(0)

    minted = dts.mint_decision_token(
        worktree="/wt", session_id="s1", candidate_fingerprint="fp1",
        candidate_summary={"paths": ["a.py"]}, now=t0, **roots,
    )
    assert minted.outcome == dts.MINT_OUTCOME_MINTED, minted.outcome
    assert minted.record.state == dts.STATE_MINTED
    assert minted.record.payload["start_attempt_count"] == 0
    assert minted.record.payload["candidate_summary"] == {"paths": ["a.py"]}
    assert minted.record.payload["history"][0] == {"from": None, "to": "minted", "at": dts.format_timestamp(t0)}
    token = minted.token

    injected = dts.mark_injected(token, root=tok_root, now=t0)
    assert injected.state == dts.STATE_INJECTED
    assert "injected_at" in injected.payload
    # mark_injected 幂等：再调不报错、状态不变、不重复追加 history。
    history_len = len(injected.payload["history"])
    injected_again = dts.mark_injected(token, root=tok_root, now=t0)
    assert injected_again.state == dts.STATE_INJECTED
    assert len(injected_again.payload["history"]) == history_len

    acked = dts.acknowledge_decision_token(token, root=tok_root, now=t0)
    assert acked.state == dts.STATE_ACKNOWLEDGED
    assert acked.payload["start_attempt_count"] == 1
    assert "acknowledged_at" in acked.payload

    dispatched = dts.mark_dispatched(token, strictness="strict", root=tok_root, now=t0)
    assert dispatched.state == dts.STATE_DISPATCHED
    assert dispatched.payload["dispatched_strictness"] == "strict"
    assert "dispatched_at" in dispatched.payload

    lookup = dts.read_decision_token(token, root=tok_root, now=t0)
    assert lookup.status == "valid" and lookup.state == dts.STATE_DISPATCHED


def test_decision_token_direct_minted_paths_to_ack_and_decline(tmp: Path) -> None:
    """两条不经 injected 的合法直迁：minted→acknowledged（manual 路径）与 minted→declined。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    tok_root, cd_root = roots["root"], roots["cooldown_root"]
    t0 = _fixed_utc(0)

    m1 = dts.mint_decision_token(worktree="/wt", session_id="sa", candidate_fingerprint="fpA", now=t0, **roots)
    acked = dts.acknowledge_decision_token(m1.token, root=tok_root, now=t0)
    assert acked.state == dts.STATE_ACKNOWLEDGED

    m2 = dts.mint_decision_token(worktree="/wt", session_id="sb", candidate_fingerprint="fpB", now=t0, **roots)
    declined = dts.mark_declined(m2.token, decline_kind="structural_not_needed", reason="not this round", root=tok_root, now=t0, cooldown_root=cd_root)
    assert declined.state == dts.STATE_DECLINED
    assert declined.payload["decline_reason"] == "not this round"
    # decline 顺带把指纹写入冷却表。
    assert dts.is_fingerprint_in_declined_cooldown(worktree="/wt", candidate_fingerprint="fpB", cooldown_root=cd_root)


def test_decision_token_illegal_transitions_rejected(tmp: Path) -> None:
    """非法迁移一律显式抛 DecisionTokenStateError：跳步、逆向、以及从终态再迁移。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    tok_root, cd_root = roots["root"], roots["cooldown_root"]
    t0 = _fixed_utc(0)

    def _mint(session, fp):
        return dts.mint_decision_token(worktree="/wt", session_id=session, candidate_fingerprint=fp, now=t0, **roots).token

    # minted 直接 dispatch = 跳过 acknowledge（start）→ 非法。
    tk = _mint("s1", "f1")
    try:
        dts.mark_dispatched(tk, root=tok_root, now=t0)
        raise AssertionError("minted→dispatched 应被拒绝")
    except dts.DecisionTokenStateError:
        pass

    # injected 直接 dispatch（仍未 start）→ 非法。
    tk2 = _mint("s2", "f2")
    dts.mark_injected(tk2, root=tok_root, now=t0)
    try:
        dts.mark_dispatched(tk2, root=tok_root, now=t0)
        raise AssertionError("injected→dispatched 应被拒绝")
    except dts.DecisionTokenStateError:
        pass

    # acknowledged 逆向回 injected → 非法。
    tk3 = _mint("s3", "f3")
    dts.mark_injected(tk3, root=tok_root, now=t0)
    dts.acknowledge_decision_token(tk3, root=tok_root, now=t0)
    try:
        dts.mark_injected(tk3, root=tok_root, now=t0)
        raise AssertionError("acknowledged→injected 逆向应被拒绝")
    except dts.DecisionTokenStateError:
        pass

    # 从终态 dispatched 再任何迁移 → 非法。
    tk4 = _mint("s4", "f4")
    dts.mark_injected(tk4, root=tok_root, now=t0)
    dts.acknowledge_decision_token(tk4, root=tok_root, now=t0)
    dts.mark_dispatched(tk4, root=tok_root, now=t0)
    for op in (
        lambda: dts.mark_injected(tk4, root=tok_root, now=t0),
        lambda: dts.acknowledge_decision_token(tk4, root=tok_root, now=t0),
        lambda: dts.mark_declined(tk4, decline_kind="structural_not_needed", reason="x", root=tok_root, now=t0, cooldown_root=cd_root),
        lambda: dts.mark_dispatched(tk4, root=tok_root, now=t0),
    ):
        try:
            op()
            raise AssertionError("从终态 dispatched 再迁移应被拒绝")
        except dts.DecisionTokenStateError:
            pass

    # 从终态 declined 再迁移 → 非法。
    tk5 = _mint("s5", "f5")
    dts.mark_declined(tk5, decline_kind="structural_not_needed", reason="done", root=tok_root, now=t0, cooldown_root=cd_root)
    try:
        dts.acknowledge_decision_token(tk5, root=tok_root, now=t0)
        raise AssertionError("从终态 declined 再 acknowledge 应被拒绝")
    except dts.DecisionTokenStateError:
        pass


def test_decision_token_acknowledged_retry_and_rollback(tmp: Path) -> None:
    """acknowledged 非终态：start 失败经 record_start_failure 回退保持 acknowledged，
    start --retry 再 acknowledge 递增计数，最终 dispatch；rollback 仅在 acknowledged 合法。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    tok_root, cd_root = roots["root"], roots["cooldown_root"]
    t0 = _fixed_utc(0)

    tk = dts.mint_decision_token(worktree="/wt", session_id="s1", candidate_fingerprint="f1", now=t0, **roots).token

    # 尚未 acknowledge（minted）时记录 start 失败 → 非法。
    try:
        dts.record_start_failure(tk, reason="too early", root=tok_root, now=t0)
        raise AssertionError("minted 状态 rollback 应被拒绝")
    except dts.DecisionTokenStateError:
        pass

    first = dts.acknowledge_decision_token(tk, root=tok_root, now=t0)
    assert first.payload["start_attempt_count"] == 1

    # 重量级准备失败 → 回退保持 acknowledged，落盘失败诊断。
    rolled = dts.record_start_failure(tk, reason="provider health gate failed", root=tok_root, now=t0)
    assert rolled.state == dts.STATE_ACKNOWLEDGED
    assert rolled.payload["last_start_failure"]["reason"] == "provider health gate failed"

    # start --retry：再 acknowledge，计数前进。
    second = dts.acknowledge_decision_token(tk, root=tok_root, now=t0)
    assert second.state == dts.STATE_ACKNOWLEDGED
    assert second.payload["start_attempt_count"] == 2

    dispatched = dts.mark_dispatched(tk, root=tok_root, now=t0)
    assert dispatched.state == dts.STATE_DISPATCHED

    # dispatched（终态）后再 rollback → 非法（坐实「dispatched 前才可回退」）。
    try:
        dts.record_start_failure(tk, reason="late", root=tok_root, now=t0)
        raise AssertionError("dispatched 后 rollback 应被拒绝")
    except dts.DecisionTokenStateError:
        pass


def test_decision_token_dedup_same_key_returns_existing(tmp: Path) -> None:
    """去重键 (worktree, session_id, candidate_fingerprint)：同键活跃 token 幂等返回既有 token，
    且不计入熔断；任一维度不同则新铸；同键既有 token 达终态后允许新铸（陈旧索引被覆盖）。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    tok_root, cb_root = roots["root"], roots["circuit_breaker_root_dir"]
    # 本测试专注去重：把熔断阈值抬高到不干扰，避免同 session 多次新铸误触频率熔断。
    mint_kwargs = {**roots, "circuit_breaker_max_requests": 1000}
    t0 = _fixed_utc(0)

    a = dts.mint_decision_token(worktree="/wt", session_id="s1", candidate_fingerprint="fp1", now=t0, **mint_kwargs)
    assert a.outcome == dts.MINT_OUTCOME_MINTED
    assert dts.count_recent_mint_requests("s1", now=t0, root=cb_root) == 1

    # 同键重铸 → 幂等返回同 token，且不再记一次铸造请求。
    a2 = dts.mint_decision_token(worktree="/wt", session_id="s1", candidate_fingerprint="fp1", now=t0, **mint_kwargs)
    assert a2.outcome == dts.MINT_OUTCOME_DEDUPLICATED
    assert a2.token == a.token
    assert dts.count_recent_mint_requests("s1", now=t0, root=cb_root) == 1, "去重命中不应计入熔断"

    # 指纹不同 → 新铸。
    b = dts.mint_decision_token(worktree="/wt", session_id="s1", candidate_fingerprint="fp2", now=t0, **mint_kwargs)
    assert b.outcome == dts.MINT_OUTCOME_MINTED and b.token != a.token
    # session 不同 → 新铸。
    c = dts.mint_decision_token(worktree="/wt", session_id="s2", candidate_fingerprint="fp1", now=t0, **mint_kwargs)
    assert c.outcome == dts.MINT_OUTCOME_MINTED and c.token != a.token
    # worktree 不同 → 新铸。
    d = dts.mint_decision_token(worktree="/wt-other", session_id="s1", candidate_fingerprint="fp1", now=t0, **mint_kwargs)
    assert d.outcome == dts.MINT_OUTCOME_MINTED and d.token != a.token

    # 把 token a 推到终态 dispatched，同键再 mint → 新铸（陈旧索引覆盖）。
    dts.mark_injected(a.token, root=tok_root, now=t0)
    dts.acknowledge_decision_token(a.token, root=tok_root, now=t0)
    dts.mark_dispatched(a.token, root=tok_root, now=t0)
    e = dts.mint_decision_token(worktree="/wt", session_id="s1", candidate_fingerprint="fp1", now=t0, **mint_kwargs)
    assert e.outcome == dts.MINT_OUTCOME_MINTED and e.token != a.token


def test_decision_token_declined_fingerprint_cooldown_write_query(tmp: Path) -> None:
    """declined 指纹冷却表读写：record/is/read/clear 按 (worktree, fingerprint) 键；
    冷却命中时 mint 返回 declined_cooldown 且不计入熔断。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    cd_root, cb_root = roots["cooldown_root"], roots["circuit_breaker_root_dir"]
    t0 = _fixed_utc(0)

    path = dts.record_declined_fingerprint(
        worktree="/wt", candidate_fingerprint="fpX", reason="too small", token="0123456789abcdef",
        now=t0, cooldown_root=cd_root,
    )
    assert path.is_file()
    assert dts.is_fingerprint_in_declined_cooldown(worktree="/wt", candidate_fingerprint="fpX", cooldown_root=cd_root)
    # 键隔离：不同 worktree / 不同指纹都不命中。
    assert not dts.is_fingerprint_in_declined_cooldown(worktree="/wt2", candidate_fingerprint="fpX", cooldown_root=cd_root)
    assert not dts.is_fingerprint_in_declined_cooldown(worktree="/wt", candidate_fingerprint="fpY", cooldown_root=cd_root)

    payload = dts.read_declined_fingerprint(worktree="/wt", candidate_fingerprint="fpX", cooldown_root=cd_root)
    assert payload is not None and payload["reason"] == "too small"

    # 冷却命中 → mint 拒铸为 declined_cooldown，且不记铸造请求。
    result = dts.mint_decision_token(worktree="/wt", session_id="sc", candidate_fingerprint="fpX", now=t0, **roots)
    assert result.outcome == dts.MINT_OUTCOME_DECLINED_COOLDOWN
    assert result.record is None and result.message
    assert dts.count_recent_mint_requests("sc", now=t0, root=cb_root) == 0, "冷却拒铸不应计入熔断"

    # clear 幂等：首删 True、再删 False，删后不再命中。
    assert dts.clear_declined_fingerprint(worktree="/wt", candidate_fingerprint="fpX", cooldown_root=cd_root)
    assert not dts.clear_declined_fingerprint(worktree="/wt", candidate_fingerprint="fpX", cooldown_root=cd_root)
    assert not dts.is_fingerprint_in_declined_cooldown(worktree="/wt", candidate_fingerprint="fpX", cooldown_root=cd_root)


def test_decision_token_mint_explicit_user_request_clears_declined_cooldown(tmp: Path) -> None:
    """``initiated_by_explicit_user_request=True``（用户手动触发）对闸二不适用：命中 declined 冷却
    时不再拒铸，而是**清掉**冷却记录后照常铸，并如实回报
    explicit_user_request_overrode_agent_declined_cooldown（命中即真）与
    declined_cooldown_record_still_blocks_automatic_path（清除成功 ⇒ False）；
    清除而非仅本次绕过，故随后的自动路径（默认 False）也不再被这条陈旧 decline 挡住。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    cd_root = roots["cooldown_root"]
    t0 = _fixed_utc(0)

    dts.record_declined_fingerprint(
        worktree="/wt", candidate_fingerprint="fpU", reason="agent declined earlier",
        now=t0, cooldown_root=cd_root,
    )
    # 对照：默认（自动路径）仍被冷却挡住。
    auto = dts.mint_decision_token(worktree="/wt", session_id="sa", candidate_fingerprint="fpU", now=t0, **roots)
    assert auto.outcome == dts.MINT_OUTCOME_DECLINED_COOLDOWN
    assert auto.explicit_user_request_overrode_agent_declined_cooldown is False

    manual = dts.mint_decision_token(
        worktree="/wt", session_id="su", candidate_fingerprint="fpU", now=t0,
        initiated_by_explicit_user_request=True, explicit_user_request_provenance="developer_manual_trigger_via_user_prompt_submit", **roots,
    )
    assert manual.outcome == dts.MINT_OUTCOME_MINTED
    assert manual.token and manual.record is not None
    assert manual.explicit_user_request_overrode_agent_declined_cooldown is True
    assert manual.declined_cooldown_record_still_blocks_automatic_path is False
    # 冷却记录已被物理清除 ⇒ 自动路径也解锁。
    assert not dts.is_fingerprint_in_declined_cooldown(
        worktree="/wt", candidate_fingerprint="fpU", cooldown_root=cd_root
    )
    auto_after = dts.mint_decision_token(worktree="/wt", session_id="sa", candidate_fingerprint="fpU", now=t0, **roots)
    assert auto_after.outcome == dts.MINT_OUTCOME_MINTED

    # 未命中冷却时两个标志都保持 False（不谎报推翻、也不谎报仍被挡）。
    fresh = dts.mint_decision_token(
        worktree="/wt", session_id="su", candidate_fingerprint="fpV", now=t0,
        initiated_by_explicit_user_request=True, explicit_user_request_provenance="developer_manual_trigger_via_user_prompt_submit", **roots,
    )
    assert fresh.outcome == dts.MINT_OUTCOME_MINTED
    assert fresh.explicit_user_request_overrode_agent_declined_cooldown is False
    assert fresh.declined_cooldown_record_still_blocks_automatic_path is False


def test_decision_token_mint_explicit_user_request_reports_override_even_when_cooldown_clear_fails(
    tmp: Path,
) -> None:
    """闸二豁免下的两个事实必须解耦（RVF-001 回归）：冷却记录 unlink 因 I/O 失败（被
    ``clear_declined_fingerprint`` 吞成 False）时，「用户显式推翻了 agent 早前的谢绝」这一**告知类**
    事实仍由命中证成、必须为 True；unlink 失败只体现在
    ``declined_cooldown_record_still_blocks_automatic_path=True``——即后续自动路径仍被这条陈旧
    decline 挡着。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    cd_root = roots["cooldown_root"]
    t0 = _fixed_utc(0)

    dts.record_declined_fingerprint(
        worktree="/wt", candidate_fingerprint="fpW", reason="agent declined earlier",
        now=t0, cooldown_root=cd_root,
    )

    # 只让「冷却表目录下的文件」删除失败，其余 unlink（token 落盘等）照常，避免误伤别处。
    blocked_dir = dts.declined_fingerprint_root(cd_root).resolve()
    original_unlink = Path.unlink

    def unlink_failing_only_inside_declined_cooldown_dir(self, *args, **kwargs):
        if self.resolve().parent == blocked_dir:
            raise PermissionError(13, "Permission denied", str(self))
        return original_unlink(self, *args, **kwargs)

    Path.unlink = unlink_failing_only_inside_declined_cooldown_dir
    try:
        manual = dts.mint_decision_token(
            worktree="/wt", session_id="sw", candidate_fingerprint="fpW", now=t0,
            initiated_by_explicit_user_request=True, explicit_user_request_provenance="developer_manual_trigger_via_user_prompt_submit", **roots,
        )
    finally:
        Path.unlink = original_unlink

    assert manual.outcome == dts.MINT_OUTCOME_MINTED
    # 告知类事实：命中即真，绝不因 unlink 失败被静默吞掉。
    assert manual.explicit_user_request_overrode_agent_declined_cooldown is True
    # 磁盘事实：记录仍在，自动路径依旧被挡。
    assert manual.declined_cooldown_record_still_blocks_automatic_path is True
    assert dts.is_fingerprint_in_declined_cooldown(
        worktree="/wt", candidate_fingerprint="fpW", cooldown_root=cd_root
    )
    auto_after = dts.mint_decision_token(
        worktree="/wt", session_id="sa", candidate_fingerprint="fpW", now=t0, **roots
    )
    assert auto_after.outcome == dts.MINT_OUTCOME_DECLINED_COOLDOWN


def test_decision_token_mint_explicit_user_request_is_exempt_from_circuit_breaker(tmp: Path) -> None:
    """``initiated_by_explicit_user_request=True`` 对闸三整段跳过：既不注册铸造请求、也不判定，
    远超阈值地连铸仍次次 minted、mint-request ledger 零新增、token payload 不带 record id；
    且不污染同 session 后续自动路径的计数（自动路径仍从 0 起算、到阈值照常熔断）。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    cb_root = roots["circuit_breaker_root_dir"]
    t0 = _fixed_utc(0)

    for index in range(5):  # 远超默认阈值 3
        result = dts.mint_decision_token(
            worktree="/wt", session_id="sm", candidate_fingerprint=f"fpM{index}", now=t0,
            initiated_by_explicit_user_request=True, explicit_user_request_provenance="developer_manual_trigger_via_user_prompt_submit", **roots,
        )
        assert result.outcome == dts.MINT_OUTCOME_MINTED, (index, result)
        assert result.circuit_breaker is None
        payload = json.loads(result.record.path.read_text(encoding="utf-8"))
        assert payload[dts.MINT_REQUEST_LEDGER_RECORD_ID_FIELD] is None

    assert dts.count_recent_mint_requests("sm", now=t0, root=cb_root) == 0, "手动铸造不该占熔断额度"

    # 同 session 的自动路径不受污染：从 0 起算，第 3 次才熔断。
    a1 = dts.mint_decision_token(worktree="/wt", session_id="sm", candidate_fingerprint="fpA1", now=t0, **roots)
    a2 = dts.mint_decision_token(worktree="/wt", session_id="sm", candidate_fingerprint="fpA2", now=t0, **roots)
    a3 = dts.mint_decision_token(worktree="/wt", session_id="sm", candidate_fingerprint="fpA3", now=t0, **roots)
    assert a1.outcome == dts.MINT_OUTCOME_MINTED and a2.outcome == dts.MINT_OUTCOME_MINTED
    assert a3.outcome == dts.MINT_OUTCOME_CIRCUIT_BREAKER_TRIPPED
    assert a3.circuit_breaker.request_count == 3


# ---------------------------------------------------------------------------
# 闸 2b：declined 会话级 snooze（内容变了但工作范围没变 → 静默）
# ---------------------------------------------------------------------------

def _candidate_summary_carrying_scope_identity(
    *, paths: list[str], head: str | None, unreviewed_lines: int, baseline: str | None = "base0"
) -> dict:
    """用**生产者自己的**打包函数造工作范围身份（不手抄 schema，防 fixture 与生产漂移）。"""
    summary_mod, _calc, _hw, _dt = _load_candidate_summary_modules()
    return {
        "candidate_scope_identity_for_declined_session_snooze": (
            summary_mod._candidate_scope_identity_for_declined_session_snooze(
                included_candidate_paths=paths,
                committed_round_baseline=baseline,
                worktree_head_commit_sha=head,
                unreviewed_nonblank_changed_line_estimate=unreviewed_lines,
            )
        )
    }


def _decline_with_scope_snapshot(dts, roots, *, session_id: str, fingerprint: str, summary: dict, now):
    minted = dts.mint_decision_token(
        worktree="/wt", session_id=session_id, candidate_fingerprint=fingerprint,
        candidate_summary=summary, now=now, **roots,
    )
    assert minted.outcome == dts.MINT_OUTCOME_MINTED, minted
    dts.mark_declined(
        minted.token, decline_kind="structural_not_needed", reason="work in progress", root=roots["root"], now=now,
        cooldown_root=roots["cooldown_root"],
    )
    return minted.token


def _snooze_lifecycle_events(dts, cooldown_root: Path) -> list[dict]:
    ledger = (
        dts.declined_session_scope_snooze_root(cooldown_root)
        / dts.DECLINED_SESSION_SCOPE_SNOOZE_LIFECYCLE_EVENT_LEDGER_FILENAME
    )
    if not ledger.is_file():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


def _snooze_release_events(dts, cooldown_root: Path) -> list[dict]:
    return [
        e for e in _snooze_lifecycle_events(dts, cooldown_root)
        if e["snooze_lifecycle_event"] == dts.DECLINED_SESSION_SCOPE_SNOOZE_LIFECYCLE_EVENT_RELEASED
    ]


def test_decision_token_declined_session_scope_snooze_silences_content_change_within_same_scope(
    tmp: Path,
) -> None:
    """闸 2b 主用例：agent 谢绝后，同会话同 worktree 内**内容变了但工作范围没变**（同路径集、同 HEAD、
    未审行数小幅增长）→ 自动铸造静默（``declined_session_scope_snooze``），不落 token、不占熔断额度；
    snooze 只限定到 ``(worktree, session_id)``——别的会话 / 别的 worktree 照常铸造。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    t0 = _fixed_utc(0)
    declined_summary = _candidate_summary_carrying_scope_identity(
        paths=["a.py"], head="h1", unreviewed_lines=10
    )
    _decline_with_scope_snapshot(dts, roots, session_id="s1", fingerprint="fp-content-1", summary=declined_summary, now=t0)
    assert dts.read_declined_session_scope_snooze(
        worktree="/wt", session_id="s1", cooldown_root=roots["cooldown_root"]
    ) is not None
    breaker_charge_before = dts.count_recent_mint_requests(
        "s1", now=t0, root=roots["circuit_breaker_root_dir"]
    )
    token_files_before = sorted(Path(roots["root"]).glob("*.json"))

    content_changed_same_scope = _candidate_summary_carrying_scope_identity(
        paths=["a.py"], head="h1", unreviewed_lines=14
    )
    silenced = dts.mint_decision_token(
        worktree="/wt", session_id="s1", candidate_fingerprint="fp-content-2",
        candidate_summary=content_changed_same_scope, now=_fixed_utc(60), **roots,
    )
    assert silenced.outcome == dts.MINT_OUTCOME_DECLINED_SESSION_SCOPE_SNOOZE, silenced
    assert silenced.token is None and silenced.record is None and silenced.message
    assert sorted(Path(roots["root"]).glob("*.json")) == token_files_before, "snooze 拒铸不得落 token"
    assert dts.count_recent_mint_requests(
        "s1", now=_fixed_utc(60), root=roots["circuit_breaker_root_dir"]
    ) == breaker_charge_before, "被闸 2b 拦下的请求不得占熔断额度"

    # 限定到会话：另一会话、同 worktree、同内容 → 照常铸造。
    other_session = dts.mint_decision_token(
        worktree="/wt", session_id="s2", candidate_fingerprint="fp-content-2",
        candidate_summary=content_changed_same_scope, now=_fixed_utc(60), **roots,
    )
    assert other_session.outcome == dts.MINT_OUTCOME_MINTED, other_session
    # 限定到 worktree：同会话、另一 worktree → 照常铸造。
    other_worktree = dts.mint_decision_token(
        worktree="/other-wt", session_id="s1", candidate_fingerprint="fp-content-2",
        candidate_summary=content_changed_same_scope, now=_fixed_utc(60), **roots,
    )
    assert other_worktree.outcome == dts.MINT_OUTCOME_MINTED, other_worktree
    # 摘要缺工作范围身份（无从比较）→ 放行（退回今天的行为），快照保留。挪到熔断窗口之外，
    # 免得与上面同会话的两次铸造一起凑满 3 次/小时。
    no_identity = dts.mint_decision_token(
        worktree="/wt", session_id="s1", candidate_fingerprint="fp-content-3",
        candidate_summary={"paths": ["a.py"]}, now=_fixed_utc(3700), **roots,
    )
    assert no_identity.outcome == dts.MINT_OUTCOME_MINTED, no_identity
    assert dts.read_declined_session_scope_snooze(
        worktree="/wt", session_id="s1", cooldown_root=roots["cooldown_root"]
    ) is not None


def test_decision_token_declined_session_scope_snooze_release_conditions(tmp: Path) -> None:
    """闸 2b 的解除矩阵：HEAD 变化 / 新路径且未审行数增长 > 50% / 12h 硬 TTL 解除并记解除事件；
    「只有新路径」或「只有行数暴涨」都**不**解除（两者须同时成立）。"""
    dts = load_rvf_decision_token_store_module()
    t0 = _fixed_utc(0)

    def fresh_case(name: str):
        roots = _decision_token_store_roots(tmp / name)
        _decline_with_scope_snapshot(
            dts, roots, session_id="s", fingerprint="fp-declined",
            summary=_candidate_summary_carrying_scope_identity(paths=["a.py"], head="h1", unreviewed_lines=10),
            now=t0,
        )
        return roots

    def mint(roots, *, paths, head, lines, now, fingerprint="fp-next"):
        return dts.mint_decision_token(
            worktree="/wt", session_id="s", candidate_fingerprint=fingerprint,
            candidate_summary=_candidate_summary_carrying_scope_identity(
                paths=paths, head=head, unreviewed_lines=lines
            ),
            now=now, **roots,
        )

    # 只有新路径（行数 10→12，+20%）→ 仍静默。
    roots = fresh_case("new-path-only")
    assert mint(roots, paths=["a.py", "b.py"], head="h1", lines=12, now=_fixed_utc(60)).outcome == (
        dts.MINT_OUTCOME_DECLINED_SESSION_SCOPE_SNOOZE
    )
    # 只有行数暴涨（同路径集，10→100）→ 仍静默。
    assert mint(roots, paths=["a.py"], head="h1", lines=100, now=_fixed_utc(60)).outcome == (
        dts.MINT_OUTCOME_DECLINED_SESSION_SCOPE_SNOOZE
    )
    # 恰好 +50%（10→15）不算「超过」→ 仍静默。
    assert mint(roots, paths=["a.py", "b.py"], head="h1", lines=15, now=_fixed_utc(60)).outcome == (
        dts.MINT_OUTCOME_DECLINED_SESSION_SCOPE_SNOOZE
    )
    # 新路径 **且** 增长 > 50%（10→16）→ 解除。
    released = mint(roots, paths=["a.py", "b.py"], head="h1", lines=16, now=_fixed_utc(60))
    assert released.outcome == dts.MINT_OUTCOME_MINTED, released
    assert dts.read_declined_session_scope_snooze(
        worktree="/wt", session_id="s", cooldown_root=roots["cooldown_root"]
    ) is None, "解除后快照应被清掉"
    assert [e["release_reason"] for e in _snooze_release_events(dts, roots["cooldown_root"])] == [
        dts.DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_SCOPE_EXPANSION
    ]

    # HEAD 变化（提交是工作边界）→ 解除。
    roots = fresh_case("head-changed")
    head_changed = mint(roots, paths=["a.py"], head="h2", lines=10, now=_fixed_utc(60))
    assert head_changed.outcome == dts.MINT_OUTCOME_MINTED, head_changed
    assert [e["release_reason"] for e in _snooze_release_events(dts, roots["cooldown_root"])] == [
        dts.DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_WORKTREE_HEAD_CHANGE
    ]

    # 12h 硬 TTL：差 1 秒仍静默，到点解除。
    roots = fresh_case("hard-ttl")
    ttl = dts.DECLINED_SESSION_SCOPE_SNOOZE_HARD_TTL_SECONDS
    assert ttl == 12 * 3600
    assert mint(roots, paths=["a.py"], head="h1", lines=10, now=_fixed_utc(ttl - 1)).outcome == (
        dts.MINT_OUTCOME_DECLINED_SESSION_SCOPE_SNOOZE
    )
    expired = mint(roots, paths=["a.py"], head="h1", lines=10, now=_fixed_utc(ttl))
    assert expired.outcome == dts.MINT_OUTCOME_MINTED, expired
    assert [e["release_reason"] for e in _snooze_release_events(dts, roots["cooldown_root"])] == [
        dts.DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_HARD_TTL
    ]

    # 损坏快照按「已过期」处理：宁可多问一次，不让坏快照永久静音。
    roots = fresh_case("corrupt-snapshot")
    snooze_files = sorted(dts.declined_session_scope_snooze_root(roots["cooldown_root"]).glob("*/*.json"))
    assert len(snooze_files) == 1, snooze_files
    corrupt = json.loads(snooze_files[0].read_text(encoding="utf-8"))
    corrupt["declined_at"] = "not-a-timestamp"
    snooze_files[0].write_text(json.dumps(corrupt), encoding="utf-8")
    assert mint(roots, paths=["a.py"], head="h1", lines=10, now=_fixed_utc(60)).outcome == (
        dts.MINT_OUTCOME_MINTED
    )


def test_decision_token_explicit_user_request_clears_declined_session_scope_snooze(tmp: Path) -> None:
    """用户显式触发与闸二同理：闸 2b 对它不适用，且**清除**快照（后续自动路径也重回可审）。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    t0 = _fixed_utc(0)
    same_scope = _candidate_summary_carrying_scope_identity(paths=["a.py"], head="h1", unreviewed_lines=10)
    _decline_with_scope_snapshot(dts, roots, session_id="s1", fingerprint="fp-1", summary=same_scope, now=t0)

    manual = dts.mint_decision_token(
        worktree="/wt", session_id="s1", candidate_fingerprint="fp-2", candidate_summary=same_scope,
        now=_fixed_utc(60), initiated_by_explicit_user_request=True, explicit_user_request_provenance="developer_manual_trigger_via_user_prompt_submit", **roots,
    )
    assert manual.outcome == dts.MINT_OUTCOME_MINTED, manual
    assert dts.read_declined_session_scope_snooze(
        worktree="/wt", session_id="s1", cooldown_root=roots["cooldown_root"]
    ) is None
    assert [e["release_reason"] for e in _snooze_release_events(dts, roots["cooldown_root"])] == [
        dts.DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_EXPLICIT_USER_REQUEST
    ]
    automatic_after = dts.mint_decision_token(
        worktree="/wt", session_id="s1", candidate_fingerprint="fp-3", candidate_summary=same_scope,
        now=_fixed_utc(120), **roots,
    )
    assert automatic_after.outcome == dts.MINT_OUTCOME_MINTED, automatic_after

    # 无 session_id 时绝不写 snooze（宁可不 snooze，也不跨会话误伤）。
    assert dts.record_declined_session_scope_snooze(
        worktree="/wt", session_id="", candidate_summary=same_scope, cooldown_root=roots["cooldown_root"]
    ) is None


def test_decision_token_declined_session_scope_snooze_releases_on_unreviewed_line_growth_alone(
    tmp: Path,
) -> None:
    """M11：同一批文件上的工作量翻几倍，本身就是新工作，不必等出现新路径。

    改动前「范围扩大」要求新路径 **且** 行数增长 > 50%：审计探针里同一批文件行数涨 100 倍，到第
    11 小时仍然静默（本仓默认不提交、改动常集中在几个巨型文件上）。新增一条只看行数的解除条件：
    未审行数 ≥ 对比基线的 3 倍 **且** 绝对增量 ≥ 150 非空行（两者都卡边界）。
    """
    dts = load_rvf_decision_token_store_module()
    t0 = _fixed_utc(0)

    def case(name: str, baseline_lines: int):
        roots = _decision_token_store_roots(tmp / name)
        _decline_with_scope_snapshot(
            dts, roots, session_id="s", fingerprint="fp-declined",
            summary=_candidate_summary_carrying_scope_identity(
                paths=["huge.py"], head="h1", unreviewed_lines=baseline_lines
            ),
            now=t0,
        )
        return roots

    def mint_same_paths(roots, lines: int, *, seconds: int = 60):
        return dts.mint_decision_token(
            worktree="/wt", session_id="s", candidate_fingerprint=f"fp-{lines}-{seconds}",
            candidate_summary=_candidate_summary_carrying_scope_identity(
                paths=["huge.py"], head="h1", unreviewed_lines=lines
            ),
            now=_fixed_utc(seconds), **roots,
        )

    # 审计探针：同一批文件、行数 ×100、第 11 小时 ⇒ 必须解除。
    roots = case("probe-100x-at-hour-11", 10)
    released = mint_same_paths(roots, 1000, seconds=11 * 3600)
    assert released.outcome == dts.MINT_OUTCOME_MINTED, released
    assert [e["release_reason"] for e in _snooze_release_events(dts, roots["cooldown_root"])] == [
        dts.DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_UNREVIEWED_LINE_GROWTH_ALONE
    ]

    # 倍数边界：100 → 299（2.99 倍，增量 199）仍静默；100 → 300（恰 3 倍，增量 200）解除。
    roots = case("ratio-boundary", 100)
    assert mint_same_paths(roots, 299).outcome == dts.MINT_OUTCOME_DECLINED_SESSION_SCOPE_SNOOZE
    assert mint_same_paths(roots, 300).outcome == dts.MINT_OUTCOME_MINTED

    # 绝对增量边界：10 → 159（增量 149，远超 3 倍）仍静默；10 → 160（增量 150）解除——小基线不许
    # 靠倍数被几行改动顶开。
    roots = case("absolute-growth-boundary", 10)
    assert mint_same_paths(roots, 159).outcome == dts.MINT_OUTCOME_DECLINED_SESSION_SCOPE_SNOOZE
    assert mint_same_paths(roots, 160).outcome == dts.MINT_OUTCOME_MINTED
    assert dts.DECLINED_SESSION_SCOPE_SNOOZE_UNREVIEWED_LINE_GROWTH_ALONE_MINIMUM_MULTIPLE_OF_BASELINE == 3
    assert dts.DECLINED_SESSION_SCOPE_SNOOZE_UNREVIEWED_LINE_GROWTH_ALONE_MINIMUM_ABSOLUTE_NONBLANK_LINES == 150


def test_decision_token_developer_prompt_rebase_rewrites_nothing_when_baseline_is_unchanged(
    tmp: Path,
) -> None:
    """L-F3b：开发者发话时基线**没变**（snooze 期间尚无观测，或观测与基线相同）⇒ 快照不改写，
    ``scope_expansion_baseline_source`` 如实停在 ``declined``；lifecycle 事件照记但标明「基线未变」。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    cooldown_root = roots["cooldown_root"]
    summary = _candidate_summary_carrying_scope_identity(paths=["a.py"], head="h1", unreviewed_lines=10)
    _decline_with_scope_snapshot(dts, roots, session_id="s1", fingerprint="fp-0", summary=summary, now=_fixed_utc(0))
    before = dts.read_declined_session_scope_snooze(worktree="/wt", session_id="s1", cooldown_root=cooldown_root)

    assert dts.rebase_declined_session_scope_snooze_expansion_baselines_on_developer_prompt(
        session_id="s1", now=_fixed_utc(60), cooldown_root=cooldown_root
    ) == 1
    after = dts.read_declined_session_scope_snooze(worktree="/wt", session_id="s1", cooldown_root=cooldown_root)
    assert after == before, "基线没变时快照不得被改写"
    assert after["scope_expansion_baseline_source"] == "declined"
    assert after["scope_expansion_baseline_rebased_at"] is None

    # 观测与基线相同：同样不改写。
    dts.consult_declined_session_scope_snooze(
        worktree="/wt", session_id="s1", candidate_summary=summary, now=_fixed_utc(90),
        cooldown_root=cooldown_root,
    )
    observed_same = dts.read_declined_session_scope_snooze(worktree="/wt", session_id="s1", cooldown_root=cooldown_root)
    dts.rebase_declined_session_scope_snooze_expansion_baselines_on_developer_prompt(
        session_id="s1", now=_fixed_utc(120), cooldown_root=cooldown_root
    )
    assert dts.read_declined_session_scope_snooze(
        worktree="/wt", session_id="s1", cooldown_root=cooldown_root
    ) == observed_same
    lifecycle = _snooze_lifecycle_events(dts, cooldown_root)
    assert [e["scope_expansion_baseline_changed"] for e in lifecycle] == [False, False], lifecycle


def test_decision_token_declined_session_scope_snooze_read_modify_write_is_serialized_between_stop_and_user_prompt_submit(
    tmp: Path,
) -> None:
    """待证项（已取成本低的修法）：Stop 侧 consult 与 UPS 侧 rebase 都是对同一份快照的读改写。

    交错时序：Stop 读到快照 → UPS 重置基线并写回 → Stop 把它**之前读到**的旧快照（附上本次观测）
    写回 ⇒ UPS 的重置被静默覆盖。同一把按会话的排他锁把两边（以及 decline 写入 / 解除清除）串行化。
    """
    import threading  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    cooldown_root = roots["cooldown_root"]

    def summary(lines: int):
        return _candidate_summary_carrying_scope_identity(paths=["a.py"], head="h1", unreviewed_lines=lines)

    _decline_with_scope_snapshot(dts, roots, session_id="s1", fingerprint="fp-0", summary=summary(10), now=_fixed_utc(0))
    dts.consult_declined_session_scope_snooze(
        worktree="/wt", session_id="s1", candidate_summary=summary(40), now=_fixed_utc(30),
        cooldown_root=cooldown_root,
    )

    stop_side_has_read_snapshot = threading.Event()
    user_prompt_submit_side_started = threading.Event()
    original_evaluate = dts.evaluate_declined_session_scope_snooze_release_reason

    def evaluate_paused_after_read(**kwargs):
        stop_side_has_read_snapshot.set()
        user_prompt_submit_side_started.wait(timeout=5)
        _time.sleep(0.4)  # 无锁时 UPS 在这段窗口里早已写完
        return original_evaluate(**kwargs)

    def stop_side():
        dts.consult_declined_session_scope_snooze(
            worktree="/wt", session_id="s1", candidate_summary=summary(50), now=_fixed_utc(60),
            cooldown_root=cooldown_root,
        )

    def user_prompt_submit_side():
        stop_side_has_read_snapshot.wait(timeout=5)
        user_prompt_submit_side_started.set()
        dts.rebase_declined_session_scope_snooze_expansion_baselines_on_developer_prompt(
            session_id="s1", now=_fixed_utc(61), cooldown_root=cooldown_root
        )

    dts.evaluate_declined_session_scope_snooze_release_reason = evaluate_paused_after_read
    try:
        threads = [threading.Thread(target=stop_side), threading.Thread(target=user_prompt_submit_side)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive(), "锁不得造成死锁"
    finally:
        dts.evaluate_declined_session_scope_snooze_release_reason = original_evaluate

    final = dts.read_declined_session_scope_snooze(worktree="/wt", session_id="s1", cooldown_root=cooldown_root)
    assert final["scope_expansion_baseline_source"] == "developer_prompt", (
        "UPS 的基线重置被 Stop 的旧快照写回覆盖了", final
    )
    assert final["scope_expansion_baseline_identity"]["unreviewed_nonblank_changed_line_estimate"] == 50, final


def test_decision_point_delivery_intent_read_merge_write_is_serialized_between_stop_receipt_and_user_prompt_submit_observation(
    tmp: Path,
) -> None:
    """Stop 侧写投递回执与 UPS 侧写首达观测，都是对同一份 task 投递意图的 read-merge-write。

    交错时序：Stop 读到意图 → UPS 把 ``user_prompt_submit_first_arrival_observation`` 合并写回 →
    Stop 把它**之前读到**的旧副本（附上回执字段）整份写回 ⇒ 观测被静默抹掉，而两次更新都返回 True；
    搁浅对账随后会把「UPS 收到但没注入正文」误判成「UPS 从未处理」。同一把按意图文件的排他锁把两边串行化，
    两次合并都必须保留。
    """
    import threading  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    kfl = load_kanban_followup_lock_module()
    root = tmp / "lock-root"
    task_id = "task-intent-rmw"
    decision_token = "decision-token-intent-rmw"
    assert kfl.write_decision_point_delivery_intent(
        task_id=task_id, root=root, session_id="s1", worktree="/wt", cwd="/wt",
        decision_token=decision_token, candidate_fingerprint="fp-1", kanban_project_path="/proj",
    ) is not None

    stop_side_thread_name = "stop-side-delivery-receipt-update"
    stop_side_has_read_intent = threading.Event()
    user_prompt_submit_side_started = threading.Event()
    original_iso_now = kfl._iso_now

    def iso_now_paused_after_stop_side_read():
        # 只暂停 Stop 线程：它在 read 之后、写回之前取 ``updated_at``。
        if threading.current_thread().name == stop_side_thread_name:
            stop_side_has_read_intent.set()
            user_prompt_submit_side_started.wait(timeout=5)
            _time.sleep(0.4)  # 无锁时 UPS 在这段窗口里早已写完观测
        return original_iso_now()

    update_results: dict[str, object] = {}

    def stop_side():
        update_results["stop_receipt"] = kfl.update_decision_point_delivery_intent(
            task_id=task_id,
            decision_token=decision_token,
            updates={
                "state": kfl.DECISION_POINT_DELIVERY_INTENT_STATE_SENT_AWAITING_AGENT_RECEIPT,
                "out_of_band_delivery_status": "accepted_pending_submit_confirmation",
            },
            root=root,
        )

    def user_prompt_submit_side():
        stop_side_has_read_intent.wait(timeout=5)
        user_prompt_submit_side_started.set()
        update_results["user_prompt_submit_observation"] = (
            kfl.record_user_prompt_submit_first_arrival_observation_on_decision_point_delivery_intent(
                task_id=task_id,
                decision_token=decision_token,
                decision_point_banner_body_text_injected=True,
                outcome="decision_point_banner_body_text_injected",
                root=root,
            )
        )

    kfl._iso_now = iso_now_paused_after_stop_side_read
    try:
        threads = [
            threading.Thread(target=stop_side, name=stop_side_thread_name),
            threading.Thread(target=user_prompt_submit_side),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive(), "锁不得造成死锁"
    finally:
        kfl._iso_now = original_iso_now

    assert update_results == {"stop_receipt": True, "user_prompt_submit_observation": True}, update_results
    intent = kfl.read_decision_point_delivery_intent(task_id=task_id, root=root)
    assert intent is not None
    observation = intent.get(
        kfl.DECISION_POINT_DELIVERY_INTENT_USER_PROMPT_SUBMIT_FIRST_ARRIVAL_OBSERVATION_KEY
    )
    assert isinstance(observation, dict) and observation.get(
        "decision_point_banner_body_text_injected"
    ) is True, ("UPS 的首达观测被 Stop 的旧副本写回静默抹掉了", intent)
    assert intent["out_of_band_delivery_status"] == "accepted_pending_submit_confirmation", intent
    assert intent["state"] == kfl.DECISION_POINT_DELIVERY_INTENT_STATE_SENT_AWAITING_AGENT_RECEIPT, intent


def test_decision_token_explicit_user_request_reports_it_overrode_agent_declined_session_scope_snooze(
    tmp: Path,
) -> None:
    """M7：显式触发清掉闸 2b snooze 时，``MintResult`` 必须如实回报「用户推翻了 agent 的谢绝」。

    T3 之后「内容变了、冷却没命中、只有 snooze 挡着」恰是最常见的被推翻形态；只回报闸二那个字段，
    UPS 就不会告诉 agent，agent 很可能照上次的判断再谢绝一次。与闸二同理拆成两个事实：**命中**即
    证成推翻（与删文件成败无关），删不掉只说明后续自动路径仍被这份快照挡着。
    """
    dts = load_rvf_decision_token_store_module()
    t0 = _fixed_utc(0)
    same_scope = _candidate_summary_carrying_scope_identity(paths=["a.py"], head="h1", unreviewed_lines=10)

    # 有 snooze（且冷却没命中：指纹不同）→ 推翻 snooze 为真、推翻冷却为假。
    roots = _decision_token_store_roots(tmp / "snooze-present")
    _decline_with_scope_snapshot(dts, roots, session_id="s1", fingerprint="fp-declined", summary=same_scope, now=t0)
    manual = dts.mint_decision_token(
        worktree="/wt", session_id="s1", candidate_fingerprint="fp-content-changed",
        candidate_summary=same_scope, now=_fixed_utc(60), initiated_by_explicit_user_request=True, explicit_user_request_provenance="developer_manual_trigger_via_user_prompt_submit", **roots,
    )
    assert manual.outcome == dts.MINT_OUTCOME_MINTED, manual
    assert manual.explicit_user_request_overrode_agent_declined_session_scope_snooze is True
    assert manual.declined_session_scope_snooze_record_still_blocks_automatic_path is False
    assert manual.explicit_user_request_overrode_agent_declined_cooldown is False

    # 没有 snooze → 两个事实都为假（不许凭空告知一次不存在的推翻）。
    roots = _decision_token_store_roots(tmp / "snooze-absent")
    manual_without_snooze = dts.mint_decision_token(
        worktree="/wt", session_id="s1", candidate_fingerprint="fp-x",
        candidate_summary=same_scope, now=t0, initiated_by_explicit_user_request=True, explicit_user_request_provenance="developer_manual_trigger_via_user_prompt_submit", **roots,
    )
    assert manual_without_snooze.explicit_user_request_overrode_agent_declined_session_scope_snooze is False
    assert manual_without_snooze.declined_session_scope_snooze_record_still_blocks_automatic_path is False

    # 快照删不掉（I/O 失败）→ 推翻照报，另报「自动路径仍被挡」。
    roots = _decision_token_store_roots(tmp / "snooze-clear-fails")
    _decline_with_scope_snapshot(dts, roots, session_id="s1", fingerprint="fp-declined", summary=same_scope, now=t0)
    snooze_root = dts.declined_session_scope_snooze_root(roots["cooldown_root"]).resolve()
    original_unlink = Path.unlink

    def unlink_failing_only_inside_snooze_root(self, *args, **kwargs):
        if snooze_root in self.resolve().parents:
            raise PermissionError(13, "Permission denied", str(self))
        return original_unlink(self, *args, **kwargs)

    Path.unlink = unlink_failing_only_inside_snooze_root
    try:
        manual_clear_fails = dts.mint_decision_token(
            worktree="/wt", session_id="s1", candidate_fingerprint="fp-content-changed",
            candidate_summary=same_scope, now=_fixed_utc(60), initiated_by_explicit_user_request=True, explicit_user_request_provenance="developer_manual_trigger_via_user_prompt_submit", **roots,
        )
    finally:
        Path.unlink = original_unlink
    assert manual_clear_fails.outcome == dts.MINT_OUTCOME_MINTED, manual_clear_fails
    assert manual_clear_fails.explicit_user_request_overrode_agent_declined_session_scope_snooze is True
    assert manual_clear_fails.declined_session_scope_snooze_record_still_blocks_automatic_path is True
    assert dts.read_declined_session_scope_snooze(
        worktree="/wt", session_id="s1", cooldown_root=roots["cooldown_root"]
    ) is not None


def test_decision_token_developer_prompt_rebases_snooze_scope_expansion_baseline_without_releasing(
    tmp: Path,
) -> None:
    """开发者发话（UPS）**不解除** snooze，只把「范围扩大」基线重置为发话那一刻（=发话前最后一次 Stop
    的观测）。HEAD 判据仍对照 decline 那一刻；按会话整目录处理，别的会话不受影响；snooze 期间尚无
    观测时基线保持 decline 那一刻。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    cooldown_root = roots["cooldown_root"]
    t0 = _fixed_utc(0)

    def summary(paths, lines, head="h1"):
        return _candidate_summary_carrying_scope_identity(paths=paths, head=head, unreviewed_lines=lines)

    def mint(paths, lines, *, seconds, head="h1", session_id="s1", fingerprint=None):
        return dts.mint_decision_token(
            worktree="/wt", session_id=session_id,
            candidate_fingerprint=fingerprint or f"fp-{session_id}-{seconds}",
            candidate_summary=summary(paths, lines, head), now=_fixed_utc(seconds), **roots,
        )

    _decline_with_scope_snapshot(dts, roots, session_id="s1", fingerprint="fp-0", summary=summary(["a.py"], 10), now=t0)
    # snooze 期间继续打磨 a.py（行数 10→40，无新路径）→ 仍静默，并记下这次观测。
    assert mint(["a.py"], 40, seconds=60).outcome == dts.MINT_OUTCOME_DECLINED_SESSION_SCOPE_SNOOZE
    observed = dts.read_declined_session_scope_snooze(worktree="/wt", session_id="s1", cooldown_root=cooldown_root)
    assert observed["latest_observed_scope_identity_while_snoozed"]["unreviewed_nonblank_changed_line_estimate"] == 40
    assert observed["scope_expansion_baseline_identity"]["unreviewed_nonblank_changed_line_estimate"] == 10

    # 另一会话也有 snooze（尚无观测）。
    _decline_with_scope_snapshot(dts, roots, session_id="s2", fingerprint="fp-s2", summary=summary(["a.py"], 10), now=t0)

    rebased = dts.rebase_declined_session_scope_snooze_expansion_baselines_on_developer_prompt(
        session_id="s1", now=_fixed_utc(120), cooldown_root=cooldown_root
    )
    assert rebased == 1, rebased
    after_prompt = dts.read_declined_session_scope_snooze(worktree="/wt", session_id="s1", cooldown_root=cooldown_root)
    assert after_prompt is not None, "开发者发话不得解除 snooze"
    assert after_prompt["scope_expansion_baseline_source"] == "developer_prompt"
    assert after_prompt["scope_expansion_baseline_identity"]["unreviewed_nonblank_changed_line_estimate"] == 40
    # HEAD 判据仍对照 decline 那一刻。
    assert after_prompt["candidate_scope_identity_for_declined_session_snooze"]["unreviewed_nonblank_changed_line_estimate"] == 10
    lifecycle = _snooze_lifecycle_events(dts, cooldown_root)
    assert [e["snooze_lifecycle_event"] for e in lifecycle] == [
        dts.DECLINED_SESSION_SCOPE_SNOOZE_LIFECYCLE_EVENT_SCOPE_EXPANSION_BASELINE_REBASED_ON_DEVELOPER_PROMPT
    ]
    assert lifecycle[0]["scope_expansion_baseline_changed"] is True
    # 别的会话不受影响。
    other = dts.read_declined_session_scope_snooze(worktree="/wt", session_id="s2", cooldown_root=cooldown_root)
    assert other["scope_expansion_baseline_source"] == "declined"

    # 相对 decline 那一刻本会解除（新路径 + 10→55），但相对新基线 40 只涨到 55（未超 60）→ 仍静默。
    assert mint(["a.py", "b.py"], 55, seconds=180).outcome == dts.MINT_OUTCOME_DECLINED_SESSION_SCOPE_SNOOZE
    # 转去做新工作：新路径 + 超过新基线的 50%（40→61）→ 解除并重新征询。
    released = mint(["a.py", "b.py"], 61, seconds=240)
    assert released.outcome == dts.MINT_OUTCOME_MINTED, released
    assert [e["release_reason"] for e in _snooze_release_events(dts, cooldown_root)] == [
        dts.DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_SCOPE_EXPANSION
    ]

    # 尚无观测时发话：基线保持 decline 那一刻（s2：10），新路径 + 16 行即解除。
    assert dts.rebase_declined_session_scope_snooze_expansion_baselines_on_developer_prompt(
        session_id="s2", now=_fixed_utc(300), cooldown_root=cooldown_root
    ) == 1
    s2_after = dts.read_declined_session_scope_snooze(worktree="/wt", session_id="s2", cooldown_root=cooldown_root)
    assert s2_after["scope_expansion_baseline_identity"]["unreviewed_nonblank_changed_line_estimate"] == 10
    assert mint(["a.py", "b.py"], 16, seconds=360, session_id="s2").outcome == dts.MINT_OUTCOME_MINTED
    # 无会话身份 → 什么都不做。
    assert dts.rebase_declined_session_scope_snooze_expansion_baselines_on_developer_prompt(
        session_id="", cooldown_root=cooldown_root
    ) == 0


def test_decision_token_mint_circuit_breaker_window(tmp: Path) -> None:
    """铸造频率熔断（R24 裁决 b）：同 session 3600s 窗口内第 ≥3 次铸造请求拒铸，写 tripped ledger；
    纯计数、零语义；请求滑出窗口后计数重置。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    cb_root = roots["circuit_breaker_root_dir"]
    t0 = _fixed_utc(0)

    r1 = dts.mint_decision_token(worktree="/wt", session_id="s1", candidate_fingerprint="g1", now=t0, **roots)
    r2 = dts.mint_decision_token(worktree="/wt", session_id="s1", candidate_fingerprint="g2", now=t0, **roots)
    assert r1.outcome == dts.MINT_OUTCOME_MINTED and r2.outcome == dts.MINT_OUTCOME_MINTED

    r3 = dts.mint_decision_token(worktree="/wt", session_id="s1", candidate_fingerprint="g3", now=t0, **roots)
    assert r3.outcome == dts.MINT_OUTCOME_CIRCUIT_BREAKER_TRIPPED
    assert r3.record is None and r3.message
    assert r3.circuit_breaker.tripped and r3.circuit_breaker.request_count == 3
    assert r3.circuit_breaker.ledger_path.is_file()

    r4 = dts.mint_decision_token(worktree="/wt", session_id="s1", candidate_fingerprint="g4", now=t0, **roots)
    assert r4.outcome == dts.MINT_OUTCOME_CIRCUIT_BREAKER_TRIPPED
    # 被拒的第 3 次请求当场冲销了自己那一份（没铸出 token），所以第 4 次仍只算到 3。
    assert r4.circuit_breaker.request_count == 3

    # 窗口内仍占额度的只有真正铸出 token 的那 2 次。
    assert dts.count_recent_mint_requests("s1", now=t0, root=cb_root) == 2
    # tripped ledger 记了两条（第 3、4 次）。
    tripped_lines = [ln for ln in r3.circuit_breaker.ledger_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(tripped_lines) == 2, tripped_lines

    # 其它 session 不受影响。
    other = dts.mint_decision_token(worktree="/wt", session_id="s2", candidate_fingerprint="g1", now=t0, **roots)
    assert other.outcome == dts.MINT_OUTCOME_MINTED

    # 窗口滑动：3601s 后原有请求全部滑出，计数重置，可再铸。
    later = _fixed_utc(3601)
    assert dts.count_recent_mint_requests("s1", now=later, root=cb_root) == 0
    r5 = dts.mint_decision_token(worktree="/wt", session_id="s1", candidate_fingerprint="g5", now=later, **roots)
    assert r5.outcome == dts.MINT_OUTCOME_MINTED


def test_decision_token_ttl_expiry_and_run_dir_revival(tmp: Path) -> None:
    """TTL 过期 + run_dir 复活（FU-1 方案 B）：无 run 锚点的过期 token 判 expired 不复活；
    挂载在途 run_dir 后过期 token 可复活为原状态、递增 revived_count；复活拒绝 valid/终态。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    tok_root = roots["root"]
    t0 = _fixed_utc(0)

    # 短 TTL 铸造。
    tk = dts.mint_decision_token(
        worktree="/wt", session_id="s1", candidate_fingerprint="fpR", ttl_seconds=60, now=t0, **roots,
    ).token
    dts.acknowledge_decision_token(tk, root=tok_root, now=t0)

    # 尚未挂 run 锚点：过 TTL → expired 且不可复活；此时 acknowledge 直接抛 expired。
    late = _fixed_utc(120)
    lk = dts.read_decision_token(tk, root=tok_root, now=late)
    assert lk.status == "expired" and lk.revivable is False
    try:
        dts.acknowledge_decision_token(tk, root=tok_root, now=late)
        raise AssertionError("无 run 锚点的过期 token 应抛 DecisionTokenExpiredError")
    except dts.DecisionTokenExpiredError:
        pass

    # 挂载在途 run_dir（run 在磁盘）后，过期 token 判 revivable。
    run_dir = tmp / "run-in-flight"
    run_dir.mkdir(parents=True)
    dts.attach_run_anchor(tk, run_dir=str(run_dir), root=tok_root, now=t0)
    lk2 = dts.read_decision_token(tk, root=tok_root, now=late)
    assert lk2.status == "expired" and lk2.revivable is True

    # 显式复活：续期、保持 acknowledged 状态、revived_count 前进。
    revived = dts.revive_decision_token(tk, root=tok_root, now=late, ttl_seconds=60)
    assert revived.state == dts.STATE_ACKNOWLEDGED
    assert revived.payload["revived_count"] == 1
    assert dts.read_decision_token(tk, root=tok_root, now=late).status == "valid"

    # acknowledge 遇过期可复活 token 会自动复活再推进（_load_active_record 内联复活）；
    # 显式传短 ttl_seconds=60，令复活续期同样短命，便于后续 run_dir 消失后判 expired。
    later2 = _fixed_utc(240)
    acked = dts.acknowledge_decision_token(tk, root=tok_root, now=later2, ttl_seconds=60)
    assert acked.state == dts.STATE_ACKNOWLEDGED and acked.payload["revived_count"] == 2

    # 复活拒绝 valid（未过期）token。
    fresh = dts.mint_decision_token(worktree="/wt", session_id="s2", candidate_fingerprint="fresh", now=t0, **roots).token
    try:
        dts.revive_decision_token(fresh, root=tok_root, now=t0)
        raise AssertionError("复活 valid token 应被拒绝")
    except dts.DecisionTokenError:
        pass

    # run_dir 消失后不再可复活。
    import shutil as _shutil  # noqa: PLC0415
    _shutil.rmtree(run_dir)
    gone = _fixed_utc(400)
    lk3 = dts.read_decision_token(tk, root=tok_root, now=gone)
    assert lk3.status == "expired" and lk3.revivable is False


def test_decision_token_bypass_lost_expired_states(tmp: Path) -> None:
    """旁路终态 lost/expired：可作用于已 TTL 过期的活跃 token（S4 投递回执丢失场景），
    但从 dispatched/declined 终态标注旁路终态为非法。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    tok_root, cd_root = roots["root"], roots["cooldown_root"]
    t0 = _fixed_utc(0)

    # injected 后 TTL 过期 → mark_lost（投递回执超时无信号）。
    tk = dts.mint_decision_token(
        worktree="/wt", session_id="s1", candidate_fingerprint="f1", ttl_seconds=60, now=t0, **roots,
    ).token
    dts.mark_injected(tk, root=tok_root, now=t0)
    late = _fixed_utc(120)
    lost = dts.mark_lost(tk, reason="no delivery signal within ttl", root=tok_root, now=late)
    assert lost.state == dts.STATE_LOST and lost.payload["lost_reason"].startswith("no delivery")

    # 另一枚：minted 过期 → mark_expired 显式终局化。
    tk2 = dts.mint_decision_token(
        worktree="/wt", session_id="s2", candidate_fingerprint="f2", ttl_seconds=60, now=t0, **roots,
    ).token
    expired = dts.mark_expired(tk2, reason="ttl dead", root=tok_root, now=late)
    assert expired.state == dts.STATE_EXPIRED

    # 从 dispatched 终态标注 lost → 非法。
    tk3 = dts.mint_decision_token(worktree="/wt", session_id="s3", candidate_fingerprint="f3", now=t0, **roots).token
    dts.mark_injected(tk3, root=tok_root, now=t0)
    dts.acknowledge_decision_token(tk3, root=tok_root, now=t0)
    dts.mark_dispatched(tk3, root=tok_root, now=t0)
    try:
        dts.mark_lost(tk3, root=tok_root, now=t0)
        raise AssertionError("dispatched→lost 应被拒绝")
    except dts.DecisionTokenStateError:
        pass


def test_decision_token_read_missing_and_corrupt(tmp: Path) -> None:
    """读取健壮性：缺失/损坏 JSON/非法 token 各归对应 status，不抛异常。"""
    dts = load_rvf_decision_token_store_module()
    roots = _decision_token_store_roots(tmp)
    tok_root = roots["root"]
    t0 = _fixed_utc(0)

    # 不存在 → missing。
    assert dts.read_decision_token("0011223344556677", root=tok_root, now=t0).status == "missing"
    # 非法 token 值 → invalid_token。
    assert dts.read_decision_token("not-a-token", root=tok_root, now=t0).status == "invalid_token"

    # 损坏 JSON → invalid_json。
    real = dts.mint_decision_token(worktree="/wt", session_id="s1", candidate_fingerprint="f1", now=t0, **roots)
    real.record.path.write_text("{ this is not json", encoding="utf-8")
    assert dts.read_decision_token(real.token, root=tok_root, now=t0).status == "invalid_json"


# ---------------------------------------------------------------------------
# rvf_agent_decision_cli（RVF overhaul S3：主 agent 发起审查的单一入口）
# 全部落盘根经显式 AgentDecisionRoots 注入 tmp 隔离；prepare / dispatch 一律 stub，
# 绝不真拉起 reviewer、绝不触碰真实 ~/.rvf / ~/.codex。
# ---------------------------------------------------------------------------

RVF_AGENT_DECISION_CLI = SCRIPT_DIR / "rvf_agent_decision_cli.py"


def load_rvf_agent_decision_cli_module():
    """加载 rvf_agent_decision_cli 模块。它 top-level import 兄弟脚本
    （rvf_decision_token_store / rvf_prep_file）与 core.*（经 _rvf_pyroot），故先保证 ROOT +
    SCRIPT_DIR 在 sys.path 上；spec 载入 + sys.modules 注册（与 dataclass 家族一致）。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("rvf_agent_decision_cli", RVF_AGENT_DECISION_CLI)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_agent_decision_cli module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # 本测试簇只验证既有状态机；真实宿主/Git 分界由独立集成测试覆盖。
    module.require_review_start_boundary = lambda **kwargs: {"test_scope": "decision-state-unit"}
    module.require_prepared_run_review_start_boundary = lambda **kwargs: {"test_scope": "decision-state-unit"}
    # 专用状态机夹具维持绑定器的 Path 返回契约；真实双向绑定由隔离 Git 集成套件验证。
    module.bind_review_start_boundary_to_one_run = lambda receipt, run_id, run_dir: (run_id, Path(run_dir) if run_dir is not None else None)
    return module


def _load_sibling_script_module(name: str):
    """按模块名加载 skill ``scripts/`` 下的兄弟脚本（与 load_* 家族同一套 path/spec 约定）。"""
    if name in sys.modules:
        return sys.modules[name]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise AssertionError(f"failed to load {name} module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _agent_cli_roots(cli, tmp: Path):
    """一组 tmp 隔离的 AgentDecisionRoots（token / 冷却 / scope 产出 / 遥测 / issue / 谢绝理由）。"""
    return cli.AgentDecisionRoots(
        token_root=tmp / "decision-tokens",
        cooldown_root=tmp / "declined-fingerprints",
        scope_adjustment_root=tmp / "scope-adjustments",
        telemetry_root=tmp / "decision-telemetry",
        scope_issue_ledger_root=tmp / "scope-issue-ledger",
        declined_reason_ledger_root=tmp / "declined-reasons",
    )


def _mint_agent_decision_token(
    dts, roots, *, worktree: str, fingerprint: str = "fp1", session_id: str = "s1",
    candidate_summary=None, now=None,
):
    """经 token store 铸一枚决策 token（roots 与 CLI 共用同一 tmp token 根）。"""
    return dts.mint_decision_token(
        worktree=worktree, session_id=session_id, candidate_fingerprint=fingerprint,
        candidate_summary=candidate_summary, now=now,
        root=roots.token_root, cooldown_root=roots.cooldown_root,
        circuit_breaker_root_dir=roots.token_root.parent / "circuit-breaker",
    ).token


def _write_stub_review_packet(packet: Path, scope_of_work_text: str) -> None:
    """按 ``build_review_packet`` 的真实排版把 scope-of-work 内联进 packet。

    渲染器写的是 ``## Session Context`` + 空行 + ``.strip()`` 后的正文；gate 就锚在这个排版上，
    所以这里必须照抄，而不是随手拼一段像 packet 的文本。
    """
    packet.parent.mkdir(parents=True, exist_ok=True)
    packet.write_text(
        "# Review Packet\n\nstub packet\n\n## Session Context\n\n"
        + scope_of_work_text.strip()
        + "\n\n## Git status\n\n(stub)\n",
        encoding="utf-8",
    )


def _agent_cli_stub_prepare(tmp: Path, *, run_name: str = "run-1", primary_files=None, fail_first_n: int = 0):
    """构造一个 stub prepare pipeline：写出假的 run_dir + scope.contract.json，返回归一化 dict。

    ``fail_first_n``：头 N 次返回 status=failed（供 start 失败回退 + --retry 测试）。
    """
    state = {"calls": 0}

    def _stub(
        *, token_record, repo, session_id, transcript_path, now, timeout_seconds=60.0,
        resume_run_id=None, resume_run_dir=None,
        parent_attempt_run_id=None, parent_attempt_run_dir=None,
    ):
        state["calls"] += 1
        state["last_resume_run_id"] = resume_run_id
        state["last_resume_run_dir"] = resume_run_dir
        state["last_parent_attempt_run_id"] = parent_attempt_run_id
        if state["calls"] <= fail_first_n:
            return {"status": "failed", "error": "stub prepare 故意失败", "run_id": None, "run_dir": None, "artifacts": {}}
        run_dir = tmp / "runs" / run_name
        inputs = run_dir / "artifacts" / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        scope_contract = inputs / "scope.contract.json"
        scope_contract.write_text(
            json.dumps({
                "version": 2, "run_id": run_name, "repo": repo, "scope_mode": "manual-all-uncommitted",
                "primary_files": list(primary_files or []), "background_files": [], "protected_files": [],
                # 与真实 prepare 同口径：fix_allowlist = primary（正式审查名单）。
                "canonical_issues": [], "fix_allowlist": list(primary_files or []), "excluded_path_prefixes": [],
                "scope_hash": "stub-old-hash",
            }),
            encoding="utf-8",
        )
        sow = run_dir / "artifacts" / "scope-of-work.md"; sow.write_text("stub scope of work", encoding="utf-8")
        # packet 必须**内联冻结**当时的 scope-of-work：真实 prepare 就是这么干的，而 dispatch 的
        # contract gate 校验的正是「reviewer 会读到的那一份」。写一个不含 Session Context 的假
        # packet 会让这条 gate 在所有用例里恒真，等于把它测没了。
        packet = run_dir / "artifacts" / "review-packet.md"
        _write_stub_review_packet(packet, sow.read_text(encoding="utf-8"))
        prep_file = tmp / "prep" / f"{run_name}.json"
        prep_file.parent.mkdir(parents=True, exist_ok=True)
        prep_file.write_text("{}", encoding="utf-8")
        review_env = run_dir / "artifacts" / "review-env.sh"
        review_env.write_text("# stub review env\n", encoding="utf-8")
        return {
            "status": "completed", "run_id": run_name, "run_dir": str(run_dir),
            "artifacts": {
                "scope_contract": str(scope_contract), "review_packet": str(packet),
                "scope_of_work": str(sow), "review_env": str(review_env),
            },
            # execution_handoff 要把 prep 文件落点交给 agent（真实 pipeline 也返回该键）。
            "prep_file": str(prep_file),
            "error": None,
        }

    # 让调用方能断言 CLI 透传下来的 resume / parent-attempt 血缘（bootstrap 恢复路径）。
    _stub.state = state
    return _stub


def _agent_cli_worktree(tmp: Path) -> str:
    """建一个「像 git 仓」的假 worktree（default_launch_preflight 只查 .git 是否存在）。"""
    wt = tmp / "worktree"
    (wt / ".git").mkdir(parents=True, exist_ok=True)
    return str(wt)


def test_agent_cli_start_success_chain_and_auto_scope(tmp: Path) -> None:
    """start 成功链：token minted→acknowledged、挂 run 锚点、自动 scope = 候选摘要纳入集、
    scope 产出落盘、遥测记 start_acknowledged + start_prepared。"""
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)
    wt = _agent_cli_worktree(tmp)
    candidate_summary = {
        "committed_round_baseline": "abc123", "committed_round_base_ref": "^main",
        "candidates": [
            {"path": "a.py", "included_in_candidate_set": True},
            {"path": "b.py", "included_in_candidate_set": True},
            {"path": "other-session.py", "included_in_candidate_set": False},
        ],
    }
    tok = _mint_agent_decision_token(dts, roots, worktree=wt, candidate_summary=candidate_summary, now=t0)

    result = cli.run_start(
        token=tok, reason="小改动，开审", roots=roots, now=t0,
        prepare_pipeline=_agent_cli_stub_prepare(tmp),
    )
    assert result.status == "ok" and result.exit_code == cli.EXIT_OK, result.message
    assert result.token_state == dts.STATE_ACKNOWLEDGED
    # 自动 scope = 纳入候选集的路径（排除 other_session_owned）。
    assert result.detail["auto_scope_paths"] == ["a.py", "b.py"], result.detail["auto_scope_paths"]

    # token 推进到 acknowledged 且挂了 run 锚点。
    lookup = dts.read_decision_token(tok, root=roots.token_root, now=t0)
    assert lookup.state == dts.STATE_ACKNOWLEDGED
    assert lookup.payload.get("run_anchor_dir")
    assert lookup.payload.get("start_attempt_count") == 1

    # scope 产出落盘。
    overlay = cli.read_scope_adjustment(tok, root=roots.scope_adjustment_root)
    assert overlay is not None and overlay["auto_scope_paths"] == ["a.py", "b.py"]
    assert overlay["exclusions"] == [] and overlay["inclusions"] == []

    # 遥测轨迹含 acknowledged + prepared。
    tele = read_jsonl(roots.telemetry_root / f"{tok}.jsonl")
    events = [r["event"] for r in tele]
    assert "start_acknowledged" in events and "start_prepared" in events, events


def test_agent_cli_start_failure_rollback_and_retry(tmp: Path) -> None:
    """start prepare 失败 → record_start_failure 回退保持 acknowledged（不吞决策）；--retry 再跑成功。"""
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)
    wt = _agent_cli_worktree(tmp)
    tok = _mint_agent_decision_token(dts, roots, worktree=wt, now=t0)

    flaky = _agent_cli_stub_prepare(tmp, fail_first_n=1)
    first = cli.run_start(token=tok, reason="开审", roots=roots, now=t0, prepare_pipeline=flaky)
    assert first.status == "start_failed" and first.exit_code == cli.EXIT_START_FAILED
    assert first.detail.get("phase") == "prepare"
    # token 仍 acknowledged、记了 last_start_failure。
    lk = dts.read_decision_token(tok, root=roots.token_root, now=t0)
    assert lk.state == dts.STATE_ACKNOWLEDGED
    assert lk.payload.get("last_start_failure", {}).get("reason")

    second = cli.run_start(token=tok, reason="重试", retry=True, roots=roots, now=t0, prepare_pipeline=flaky)
    assert second.status == "ok" and second.exit_code == cli.EXIT_OK
    lk2 = dts.read_decision_token(tok, root=roots.token_root, now=t0)
    assert lk2.state == dts.STATE_ACKNOWLEDGED
    # 两次 acknowledge（首次 + retry）。
    assert lk2.payload.get("start_attempt_count") == 2


def test_agent_cli_start_preflight_and_provider_health_gates(tmp: Path) -> None:
    """start 前两道门：backend/launch 前置校验（非 git 仓）与 provider-health 门（注入失败）都各自
    回退保持 acknowledged、可 --retry；且都记对应 phase。"""
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)

    # ① launch 前置校验失败：repo 不是 git 仓。
    tok1 = _mint_agent_decision_token(dts, roots, worktree=str(tmp / "nogit"), fingerprint="fpA", session_id="sA", now=t0)
    r1 = cli.run_start(
        token=tok1, reason="x", repo=str(tmp / "nogit"), roots=roots, now=t0,
        prepare_pipeline=_agent_cli_stub_prepare(tmp),
    )
    assert r1.status == "start_failed" and r1.detail.get("phase") == "launch_preflight"
    assert dts.read_decision_token(tok1, root=roots.token_root, now=t0).state == dts.STATE_ACKNOWLEDGED

    # ② provider-health 门失败（注入一个 requirement + 失败 runner）。
    wt = _agent_cli_worktree(tmp)
    tok2 = _mint_agent_decision_token(dts, roots, worktree=wt, fingerprint="fpB", session_id="sB", now=t0)
    r2 = cli.run_start(
        token=tok2, reason="x", roots=roots, now=t0, prepare_pipeline=_agent_cli_stub_prepare(tmp),
        provider_health_requirements_resolver=lambda backend: [object()],
        provider_health_runner=lambda req, timeout: {"status": "failed", "provider": "codex"},
    )
    assert r2.status == "start_failed" and r2.detail.get("phase") == "provider_health"
    assert dts.read_decision_token(tok2, root=roots.token_root, now=t0).state == dts.STATE_ACKNOWLEDGED


def test_agent_cli_scope_operations_and_state_guard(tmp: Path) -> None:
    """scope annotate/exclude/include 改动自动 scope + 落遥测；净效果 = auto − exclude ∪ include；
    非 acknowledged 态（minted）拒绝。"""
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)
    wt = _agent_cli_worktree(tmp)
    candidate_summary = {"candidates": [
        {"path": "a.py", "included_in_candidate_set": True},
        {"path": "b.py", "included_in_candidate_set": True},
    ]}
    tok = _mint_agent_decision_token(dts, roots, worktree=wt, candidate_summary=candidate_summary, now=t0)
    cli.run_start(token=tok, reason="开审", roots=roots, now=t0, prepare_pipeline=_agent_cli_stub_prepare(tmp))

    ann = cli.run_scope_annotate(token=tok, entries=[("a.py", "核心改动: 注意边界")], roots=roots, now=t0)
    assert ann.status == "ok"
    exc = cli.run_scope_exclude(token=tok, paths=["b.py"], reason="纯 reformat", roots=roots, now=t0)
    assert exc.status == "ok" and exc.detail["effective_primary_files"] == ["a.py"]
    inc = cli.run_scope_include(token=tok, paths=["runtime-artifact.json"], reason="R10 运行时产物追加", roots=roots, now=t0)
    assert inc.status == "ok"
    assert inc.detail["effective_primary_files"] == ["a.py", "runtime-artifact.json"]

    # overlay 落盘反映三操作。
    overlay = cli.read_scope_adjustment(tok, root=roots.scope_adjustment_root)
    assert overlay["annotations"] == {"a.py": "核心改动: 注意边界"}
    assert {e["path"] for e in overlay["exclusions"]} == {"b.py"}
    assert {i["path"] for i in overlay["inclusions"]} == {"runtime-artifact.json"}

    # 遥测记了 scope_annotate / scope_exclude / scope_include。
    events = [r["event"] for r in read_jsonl(roots.telemetry_root / f"{tok}.jsonl")]
    assert "scope_annotate" in events and "scope_exclude" in events and "scope_include" in events

    # 非 acknowledged 态守卫：新铸的 minted token 上 scope 操作被拒。
    tok_minted = _mint_agent_decision_token(dts, roots, worktree=wt, fingerprint="fp-minted", session_id="s-minted", now=t0)
    guard = cli.run_scope_exclude(token=tok_minted, paths=["x"], reason="r", roots=roots, now=t0)
    assert guard.status == "error" and guard.exit_code == cli.EXIT_TOKEN_STATE


def test_agent_cli_scope_issue_ledger_threshold_idempotent(tmp: Path) -> None:
    """R17：同源+同类型 scope 例外去重路径数 ≥3 自动立本地 issue；第 3 次首次立项、其后不重复立、
    重复同路径不灌水计数。"""
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)
    wt = _agent_cli_worktree(tmp)
    tok = _mint_agent_decision_token(dts, roots, worktree=wt, now=t0)
    cli.run_start(token=tok, reason="开审", roots=roots, now=t0, prepare_pipeline=_agent_cli_stub_prepare(tmp))

    r1 = cli.run_scope_exclude(token=tok, paths=["p1.py"], reason="r", roots=roots, now=t0)
    r2 = cli.run_scope_exclude(token=tok, paths=["p2.py"], reason="r", roots=roots, now=t0)
    r3 = cli.run_scope_exclude(token=tok, paths=["p3.py"], reason="r", roots=roots, now=t0)
    assert r1.detail["scope_issue"]["issue_filed"] is False
    assert r2.detail["scope_issue"]["issue_filed"] is False
    # 第 3 个不同路径跨阈值：首次立项。
    assert r3.detail["scope_issue"]["issue_filed"] is True
    assert r3.detail["scope_issue"]["newly_filed"] is True
    assert r3.detail["scope_issue"]["distinct_path_count"] == 3
    issue_path = Path(r3.detail["scope_issue"]["issue_path"])
    assert issue_path.is_file()
    issue = json.loads(issue_path.read_text(encoding="utf-8"))
    assert issue["operation_type"] == "exclude" and sorted(issue["paths"]) == ["p1.py", "p2.py", "p3.py"]

    # 第 4 个不同路径：issue 更新但非首次立项（幂等，一键一 issue）。
    r4 = cli.run_scope_exclude(token=tok, paths=["p4.py"], reason="r", roots=roots, now=t0)
    assert r4.detail["scope_issue"]["issue_filed"] is True and r4.detail["scope_issue"]["newly_filed"] is False
    assert r4.detail["scope_issue"]["distinct_path_count"] == 4

    # 重复排除已有路径：去重路径数不变。
    r5 = cli.run_scope_exclude(token=tok, paths=["p1.py"], reason="r", roots=roots, now=t0)
    assert r5.detail["scope_issue"]["distinct_path_count"] == 4

    # include 是不同「类型」→ 独立幂等键，独立计数（不与 exclude 混）。
    inc = cli.run_scope_include(token=tok, paths=["q1.py"], reason="r", roots=roots, now=t0)
    assert inc.detail["scope_issue"]["operation_type"] == "include"
    assert inc.detail["scope_issue"]["distinct_path_count"] == 1
    assert inc.detail["scope_issue"]["issue_filed"] is False


def test_agent_cli_dispatch_finalizes_scope_and_records_strictness(tmp: Path) -> None:
    """dispatch：scope 定稿写回 scope.contract.json（primary_files/excluded/重算 hash）+ 严格度终选
    读 preset + 派发（stub）+ token 终态 dispatched；dispatcher 失败 → dispatch_failed，token 仍
    acknowledged 可重试。

    定稿后的 primary_files = prepare 定的正式名单 − 排除 ∪ include，**不再**改写成候选全集（08ac4：
    候选里没进正式审查的路径原先同时出现在 primary 与 protected 两边，回执也因此高估覆盖）。"""
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)
    wt = _agent_cli_worktree(tmp)
    candidate_summary = {"candidates": [
        {"path": "a.py", "included_in_candidate_set": True},
        {"path": "b.py", "included_in_candidate_set": True},
    ]}
    tok = _mint_agent_decision_token(dts, roots, worktree=wt, candidate_summary=candidate_summary, now=t0)
    cli.run_start(
        token=tok, reason="开审", roots=roots, now=t0,
        prepare_pipeline=_agent_cli_stub_prepare(tmp, primary_files=["a.py", "b.py"]),
    )
    cli.run_scope_exclude(token=tok, paths=["b.py"], reason="reformat", roots=roots, now=t0)
    cli.run_scope_include(token=tok, paths=["c.py"], reason="遗漏", roots=roots, now=t0)
    # 模拟 prepare 把 c.py 归进 background（Bash mv 产生、transcript 里无 Write/Edit），
    # 并把 b.py 放进 allowlist：定稿后 include 必须出 background（但不进 allowlist），exclude 必须出 allowlist。
    stub_contract_path = tmp / "runs" / "run-1" / "artifacts" / "inputs" / "scope.contract.json"
    stub_contract = json.loads(stub_contract_path.read_text(encoding="utf-8"))
    stub_contract.update({"fix_allowlist": ["a.py", "b.py"], "background_files": ["c.py", "d.py"], "protected_files": ["c.py", "d.py"]})
    stub_contract_path.write_text(json.dumps(stub_contract), encoding="utf-8")

    # ① dispatcher 失败：dispatch_failed、token 仍 acknowledged。
    def _failing_dispatcher(**kw):
        return {"status": "failed", "error": "no external harness available"}
    fail = cli.run_dispatch(token=tok, strictness="standard", roots=roots, now=t0, reviewer_dispatcher=_failing_dispatcher)
    assert fail.status == "dispatch_failed" and fail.exit_code == cli.EXIT_DISPATCH_FAILED
    assert dts.read_decision_token(tok, root=roots.token_root, now=t0).state == dts.STATE_ACKNOWLEDGED

    # ② dispatcher 成功。
    seen = {}

    def _ok_dispatcher(*, repo, review_packet, scope_contract, session_context, run_id, run_dir, strictness, preset, dry_run):
        seen.update({"scope_contract": scope_contract, "strictness": strictness,
                     "external_target": preset.get("external_reviewer_count_target"), "dry_run": dry_run})
        return {"status": "ok", "plan_path": str(tmp / "reviewer-plan.json")}

    ok = cli.run_dispatch(token=tok, strictness="strict", roots=roots, now=t0, reviewer_dispatcher=_ok_dispatcher)
    assert ok.status == "ok" and ok.exit_code == cli.EXIT_OK
    assert ok.detail["effective_primary_files"] == ["a.py", "c.py"]
    # 严格度终选读 preset：strict = external target 3。
    assert seen["strictness"] == "strict" and seen["external_target"] == 3
    assert ok.detail["preset"]["reviewer_reasoning_effort_level"] == "high"

    # token 终态 dispatched，记严格度。
    lk = dts.read_decision_token(tok, root=roots.token_root, now=t0)
    assert lk.state == dts.STATE_DISPATCHED
    assert lk.payload.get("dispatched_strictness") == "strict"

    # scope.contract.json 已定稿：primary_files 反映净效果、excluded 含 b.py、hash 变了。
    contract = json.loads(Path(seen["scope_contract"]).read_text(encoding="utf-8"))
    assert contract["primary_files"] == ["a.py", "c.py"]
    assert "b.py" in contract["excluded_path_prefixes"]
    assert contract["scope_hash"] != "stub-old-hash"
    # include 的 c.py 移出 background/protected 但不进 fix_allowlist（正式审查只经 scope claim）；
    # exclude 的 b.py 移出 fix_allowlist（否则 fixer 改它不算外扩）。primary ∩ protected = ∅。
    assert contract["fix_allowlist"] == ["a.py"]
    assert contract["background_files"] == ["d.py"] and contract["protected_files"] == ["d.py"]
    assert contract["canonical_scope"]["fix_allowlist"] == ["a.py"]
    assert contract["canonical_scope"]["background_files"] == ["d.py"]
    assert not set(contract["primary_files"]) & set(contract["protected_files"])
    # 回执报「正式审查覆盖 X/Y」，不再报 primary_files N。
    assert "正式审查覆盖 1/1 个候选路径" in ok.message and "primary_files" not in ok.message


def test_agent_cli_dispatch_blocks_on_uncovered_candidates_until_claim_exclude_or_accept(tmp: Path) -> None:
    """覆盖闸（08ac4）：session-owned 口径下，候选里有路径既没进正式审查、也没被认领 / 排除 / include，
    dispatch 拦下（token 仍 acknowledged、contract 不被改写）；排除或 ``--accept-uncovered`` 之后放行，
    接受理由落进 overlay 与回执。"""
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)
    wt = _agent_cli_worktree(tmp)
    candidate_summary = {"candidates": [
        {"path": "a.py", "included_in_candidate_set": True},
        {"path": "bash_written.py", "included_in_candidate_set": True},
        {"path": "other.py", "included_in_candidate_set": True},
    ]}
    tok = _mint_agent_decision_token(dts, roots, worktree=wt, candidate_summary=candidate_summary, now=t0)
    start = cli.run_start(
        token=tok, reason="开审", roots=roots, now=t0,
        prepare_pipeline=_agent_cli_stub_prepare(tmp, primary_files=["a.py"]),
    )
    contract_path = Path(start.detail["prepare_artifacts"]["scope_contract"])
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["scope_mode"] = "session-owned"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    def _ok_dispatcher(**kw):
        return {"status": "ok"}

    blocked = cli.run_dispatch(token=tok, strictness="light", roots=roots, now=t0, reviewer_dispatcher=_ok_dispatcher)
    assert blocked.exit_code == cli.EXIT_UNCOVERED_CANDIDATE_PATHS_NEED_AGENT_DECISION, blocked.message
    assert "正式审查覆盖 1/3 个候选路径" in blocked.message
    assert dts.read_decision_token(tok, root=roots.token_root, now=t0).state == dts.STATE_ACKNOWLEDGED
    assert json.loads(contract_path.read_text(encoding="utf-8"))["scope_hash"] == "stub-old-hash"
    assert any("scope claim" in step for step in blocked.next_steps)

    cli.run_scope_exclude(token=tok, paths=["other.py"], reason="别人的", roots=roots, now=t0)
    still_blocked = cli.run_dispatch(token=tok, strictness="light", roots=roots, now=t0, reviewer_dispatcher=_ok_dispatcher)
    assert still_blocked.exit_code == cli.EXIT_UNCOVERED_CANDIDATE_PATHS_NEED_AGENT_DECISION

    ok = cli.run_dispatch(
        token=tok, strictness="light", roots=roots, now=t0, reviewer_dispatcher=_ok_dispatcher,
        accept_uncovered_reason="生成物，确认不审",
    )
    assert ok.exit_code == cli.EXIT_OK, ok.message
    assert ok.detail["accepted_uncovered_candidate_paths"] == {"reason": "生成物，确认不审", "paths": ["bash_written.py"]}
    assert "已显式接受 1 个候选路径本轮不审" in ok.message


def test_agent_cli_start_arms_in_flight_receipt_and_returns_execution_handoff(tmp: Path) -> None:
    """P2/P3：``start`` 成功后 ① arm 一枚在途凭证（INV-5 的载体），② 回执带全套
    ``execution_handoff``（INV-3：执行信息改从返回值读，不再经任何注入正文携带）。

    凭证刻意只写 **session** 维度的 key，且 key 含仓身份 —— 故这里必须用**真实** git 仓做
    worktree（假 .git 目录解析不出仓身份，凭证会被安全跳过而不是落盘）。
    """
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    lock_module = load_kanban_followup_lock_module()
    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)
    repo = init_repo(tmp / "worktree")
    lock_root = tmp / "locks"
    tok = _mint_agent_decision_token(
        dts, roots, worktree=str(repo), session_id="sess-in-flight", now=t0
    )

    result = cli.run_start(
        token=tok, reason="开审", roots=roots, now=t0,
        prepare_pipeline=_agent_cli_stub_prepare(tmp),
        lock_root=lock_root,
    )
    assert result.status == "ok", result.message

    # ① 在途凭证已 arm，且是 session-keyed（读侧 task 维度解析不出时也命中得到）。
    receipt = result.detail["in_flight_receipt"]
    assert receipt["status"] == cli.ACCEPTED_REVIEW_RUN_RECEIPT_ARMED, receipt
    marker = lock_module.read_marker(
        task_id=None, session_id="sess-in-flight", repo=str(repo), root=lock_root
    )
    assert marker is not None, "session-keyed 凭证应可被同一仓身份读回"
    assert marker["decision_token"] == tok
    assert marker["state"] == lock_module.STATE_IN_PROGRESS
    assert marker["delivered_session_id"] == "sess-in-flight"
    # rewind 对账的 needle 从 marker 现推：凭证走 RVF_DECISION_TOKEN= 而不是 RVF_DISPATCH=。
    presence = _load_sibling_script_module("kanban_followup_trigger_transcript_presence")
    needle_token, needle = presence.trigger_needle_for_lock_marker(marker)
    assert needle_token == tok and needle == f"RVF_DECISION_TOKEN={tok}", (needle_token, needle)

    # ② execution_handoff 字段齐全，且人类可读行也看得到（不必解析 JSON）。
    handoff = result.detail["execution_handoff"]
    for key in (
        "target_repo", "run_id", "run_dir", "prep_file", "review_env", "review_packet",
        "scope_contract", "scope_of_work", "origin_metadata", "handoff_path",
        "handoff_contract", "execution_instruction",
    ):
        assert handoff.get(key), f"execution_handoff 缺 {key}: {handoff}"
    assert handoff["origin_metadata"].endswith("artifacts/origin.json")
    assert handoff["handoff_path"].endswith("artifacts/handoff.md")
    rendered = result.render()
    assert "execution_handoff:" in rendered
    assert "review_packet:" in rendered and "scope_contract:" in rendered
    # INV-2：回执正文里那条 `$review-validate-fix` 只能作为「由你自己调用」的指引出现，
    # 绝不是某条注入正文的第一行。
    assert not rendered.strip().startswith("$review-validate-fix")


def test_agent_cli_start_consumes_review_reopen_marker_before_allocate(tmp: Path) -> None:
    """P1 落地后的必然连带：``$rvf-reopen`` 的消费点必须也在 ``start`` 这条**默认**发起入口上。

    历史上只有 Stop hook 的派发路径消费 rescope marker；默认发起改成「横幅 → agent 显式 start」
    之后不补这一刀，``$rvf-reopen`` 在默认路径上就整个失效（marker 永不消费、范围永不重开）。
    这里只验「marker 被消费」这一可观测事实（真实 tracker 重开在 stop hook 套件里覆盖）。
    """
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    rrm = _load_sibling_script_module("review_reopen_marker")
    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)
    repo = init_repo(tmp / "worktree")
    reopen_root = tmp / "reopen-root"
    prev_log_root = os.environ.get("CODEX_RVF_LOG_ROOT")
    os.environ["CODEX_RVF_LOG_ROOT"] = str(reopen_root)
    try:
        marker_path = rrm.write_review_reopen_marker(
            task_id=None,
            session_id="sess-reopen-start",
            target_run_id="rvf-earlier-impl",
            repo=str(repo),
        )
        assert marker_path is not None and Path(marker_path).exists()

        tok = _mint_agent_decision_token(
            dts, roots, worktree=str(repo), session_id="sess-reopen-start", now=t0
        )
        result = cli.run_start(
            token=tok, reason="开审", roots=roots, now=t0,
            prepare_pipeline=_agent_cli_stub_prepare(tmp),
            lock_root=tmp / "locks",
        )
        assert result.status == "ok", result.message
        assert not Path(marker_path).exists(), "start 必须在 allocate 之前消费掉 rescope marker"
        tele = read_jsonl(roots.telemetry_root / f"{tok}.jsonl")
        assert "start_review_reopen_consumed" in [r["event"] for r in tele], tele
    finally:
        if prev_log_root is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = prev_log_root


def test_agent_cli_dispatch_refuses_while_scope_of_work_is_still_the_hook_written_stub(
    tmp: Path,
) -> None:
    """contract gate：scope-of-work 仍是 stub 就拒派，且**没有**真的调 reviewer dispatcher。

    派了也白派——reviewer 侧的 contract gate 会当场把这一轮退回成 ``kind: request``，而 request
    在同一 run 内转不回 issues（2026-08-15 run 实测：整轮 codex 评审白跑约 13 分钟）。在派发
    **之前**拦住比派完再退回便宜一个数量级，所以这条 gate 排在 reviewer_dispatcher 之前。
    """
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    import rvf_dispatch_prompts  # noqa: PLC0415

    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)
    repo = init_repo(tmp / "worktree")
    tok = _mint_agent_decision_token(dts, roots, worktree=str(repo), now=t0)
    cli.run_start(
        token=tok, reason="开审", roots=roots, now=t0,
        prepare_pipeline=_agent_cli_stub_prepare(tmp), lock_root=tmp / "locks",
    )
    scope_of_work = tmp / "runs" / "run-1" / "artifacts" / "scope-of-work.md"
    stub_text = rvf_dispatch_prompts.dispatch_scope_of_work_text(
        target_flow=rvf_dispatch_prompts.AGENTIC_INITIATION_FLOW,
        cwd=str(repo), parent_session_id="s1", parent_thread_path=None, prompt_path=None,
        run_id="run-1", run_dir=tmp / "runs" / "run-1", candidate_paths=["a.py"],
    )
    scope_of_work.write_text(stub_text, encoding="utf-8")

    dispatcher_calls: list[dict] = []

    def _recording_dispatcher(**kw):
        dispatcher_calls.append(kw)
        return {"status": cli.DISPATCH_OUTCOME_OK, "returncode": 0}

    blocked = cli.run_dispatch(
        token=tok, strictness="standard", roots=roots, now=t0,
        reviewer_dispatcher=_recording_dispatcher,
    )
    assert blocked.status == "scope_of_work_still_stub", blocked
    assert blocked.exit_code == cli.EXIT_SCOPE_OF_WORK_STILL_STUB
    assert blocked.exit_code != cli.EXIT_DISPATCH_FAILED, "不是故障，是「还差一步没做」"
    assert dispatcher_calls == [], "gate 必须排在 reviewer 派发之前，否则白跑一轮"
    assert any("rerender-scope-of-work" in step for step in blocked.next_steps), blocked.next_steps
    # token 不进终态：agent 补完 scope-of-work 后要能经同一入口重派。
    assert dts.read_decision_token(tok, root=roots.token_root, now=t0).state == dts.STATE_ACKNOWLEDGED

    # agent 写了真实内容并删掉标记，但**没重渲** → 仍须拒派。
    # 这是这条 gate 本来要拦的那件事：packet 在 start 时就把 stub 内联冻结了，只改盘上的文件
    # 不会进 packet。只看盘上文件的 gate 会在这里放行，而 reviewer 读到的仍是 stub ⇒ 整轮空审。
    written = "# Scope of Work\n\n本轮改了 a.py，审查请盯错误处理。\n"
    scope_of_work.write_text(written, encoding="utf-8")
    stale = cli.run_dispatch(
        token=tok, strictness="standard", roots=roots, now=t0,
        reviewer_dispatcher=_recording_dispatcher,
    )
    assert stale.status == "scope_of_work_still_stub", stale
    assert stale.detail["reason"] == cli.SCOPE_OF_WORK_GATE_REASON_PACKET_STALE, stale.detail
    assert dispatcher_calls == [], "packet 里冻的还是 stub，这一轮派出去就是空审"

    # 声明过的 scope-of-work 读不到 ⇒ 拒派，不能跟「压根没声明」共用放行分支：
    # 否则「把文件删了」就是绕过这条 gate 最省事的办法。
    scope_of_work.unlink()
    missing = cli.run_dispatch(
        token=tok, strictness="standard", roots=roots, now=t0,
        reviewer_dispatcher=_recording_dispatcher,
    )
    assert missing.status == "scope_of_work_still_stub", missing
    assert missing.detail["reason"] == cli.SCOPE_OF_WORK_GATE_REASON_UNREADABLE, missing.detail
    assert dispatcher_calls == []
    scope_of_work.write_text(written, encoding="utf-8")

    # 经重渲把新正文冻进 packet → 放行。
    _write_stub_review_packet(tmp / "runs" / "run-1" / "artifacts" / "review-packet.md", written)
    ok = cli.run_dispatch(
        token=tok, strictness="standard", roots=roots, now=t0,
        reviewer_dispatcher=_recording_dispatcher,
    )
    assert ok.status == "ok", ok
    assert len(dispatcher_calls) == 1


def test_agent_cli_dispatch_separates_blocked_on_reviewer_request_from_failure(tmp: Path) -> None:
    """P3b：``blocked_on_reviewer_request`` 是**待处理终态**，不得报成 dispatch_failed。

    混同的后果不是「文案不好看」：agent 会把它当可修复故障去盲目重跑 dispatch，而正确处置是
    先走 Contract Gate 处理那条 ``kind: request``、再带同一 selection-seed 用同一对 reviewer 重派。
    """
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)
    repo = init_repo(tmp / "worktree")
    tok = _mint_agent_decision_token(dts, roots, worktree=str(repo), now=t0)
    cli.run_start(
        token=tok, reason="开审", roots=roots, now=t0,
        prepare_pipeline=_agent_cli_stub_prepare(tmp), lock_root=tmp / "locks",
    )

    def _request_dispatcher(**kw):
        return {
            "status": cli.DISPATCH_OUTCOME_BLOCKED_ON_REVIEWER_REQUEST,
            "returncode": 3,
            "dispatch_status_path": str(tmp / "status.json"),
            "launch_status": "launched",
        }

    blocked = cli.run_dispatch(
        token=tok, strictness="standard", roots=roots, now=t0,
        reviewer_dispatcher=_request_dispatcher,
    )
    assert blocked.status == cli.DISPATCH_OUTCOME_BLOCKED_ON_REVIEWER_REQUEST, blocked
    assert blocked.exit_code == cli.EXIT_DISPATCH_BLOCKED_ON_REVIEWER_REQUEST
    assert blocked.exit_code != cli.EXIT_DISPATCH_FAILED
    assert "Contract Gate" in blocked.message
    # 指引必须给出**只重派 request 腿**的形态：整盘重跑会把健康且已完成的那条腿一并覆写，
    # 这条修复只在 flag 上生效、在标准操作流程上不生效（RVF-004）。
    assert any(
        cli.REVIEW_REQUEST_REDISPATCH_FLAG in step for step in blocked.next_steps
    ), blocked.next_steps
    # token 不进终态：处置完 request 之后要能经同一入口重派。
    assert dts.read_decision_token(tok, root=roots.token_root, now=t0).state == dts.STATE_ACKNOWLEDGED

    # 成功路径把 detached 的 status.json 落点与轮询指引冒到 next_steps。
    def _detached_ok_dispatcher(**kw):
        return {
            "status": cli.DISPATCH_OUTCOME_OK,
            "returncode": 0,
            "dispatch_status_path": str(tmp / "reviewers" / ".dispatch-thread.status.json"),
            "launch_status": "launched",
        }

    ok = cli.run_dispatch(
        token=tok, strictness="standard", roots=roots, now=t0,
        reviewer_dispatcher=_detached_ok_dispatcher,
    )
    assert ok.status == "ok", ok
    assert ok.detail["dispatch_status_path"].endswith(".dispatch-thread.status.json")
    # 等待改走 `rvf wait`（按 host 分流），不再指引 agent 手动 --wait-status 轮询。
    # 解析不出会话 id 时回执会直接带上 `--run-dir <run>`，两种形态都算。
    assert any(
        " wait`" in step or " wait --run-dir " in step for step in ok.next_steps
    ), ok.next_steps
    assert not any("--wait-status" in step for step in ok.next_steps), ok.next_steps
    assert any("plan_status" in step for step in ok.next_steps), ok.next_steps


def test_default_reviewer_dispatcher_speaks_detached_execute_contract(tmp: Path) -> None:
    """P3b：``--execute`` 必须带 ``--detached``，且 plan 路径按 ``RVF_DISPATCH_STATUS=`` 前缀解析。

    不带 detached 的同步调用会被前台 Bash 工具的 600s 上限砍断（一次彻底 review 常常 >10min），
    而「取 stdout 最后一行」在 detached 的两行回执下会把 ``RVF_DISPATCH_LAUNCH=launched`` 当成
    plan 路径。两条都靠替换 subprocess.run 的真实 argv / stdout 钉死。
    """
    cli = load_rvf_agent_decision_cli_module()
    seen = {}

    class _Completed:
        returncode = 0
        stdout = (
            "RVF_DISPATCH_STATUS=/tmp/run/artifacts/reviewers/.dispatch-thread.status.json\n"
            "RVF_DISPATCH_LAUNCH=launched\n"
        )
        stderr = ""

    def _fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        return _Completed()

    original_run = cli.subprocess.run
    cli.subprocess.run = _fake_run
    try:
        result = cli.default_reviewer_dispatcher(
            repo=str(tmp), review_packet=None, scope_contract=None, session_context=None,
            run_id="rvf-x", run_dir=str(tmp / "run"), strictness="standard",
            preset={"external_reviewer_count_target": 2}, dry_run=False,
        )
    finally:
        cli.subprocess.run = original_run

    assert "--execute" in seen["argv"] and "--detached" in seen["argv"], seen["argv"]
    assert result["status"] == cli.DISPATCH_OUTCOME_OK
    assert result["dispatch_status_path"].endswith(".dispatch-thread.status.json")
    assert result["launch_status"] == "launched"
    # 明确钉死「不是取最后一行」：最后一行是 LAUNCH 那行。
    assert "RVF_DISPATCH_LAUNCH" not in str(result["dispatch_status_path"])


def test_kanban_followup_lock_key_separates_colliding_task_ids_across_repositories(tmp: Path) -> None:
    """P2a：cline-kanban 的 task id 是短哈希前缀，不同 project 会撞。

    这把锁自 P2 起是「任何已接受 RVF run 的在途凭证」，撞键 = A 仓的在途 run 静默压掉 B 仓的
    发起决策点（漏审）。key 编入仓身份之后，同一个 task id 在两个仓里必须是两把互不相干的锁。
    """
    lock_module = load_kanban_followup_lock_module()
    repo_a = init_repo(tmp / "repo-a")
    repo_b = init_repo(tmp / "repo-b")
    root = tmp / "locks"

    path_a = lock_module.write_marker(
        task_id="a877b", session_id=None, run_id="rvf-a", run_dir=str(tmp / "run-a"),
        repo=str(repo_a), cwd=str(repo_a), root=root,
    )
    path_b = lock_module.write_marker(
        task_id="a877b", session_id=None, run_id="rvf-b", run_dir=str(tmp / "run-b"),
        repo=str(repo_b), cwd=str(repo_b), root=root,
    )
    assert path_a is not None and path_b is not None
    assert path_a != path_b, "同一 task id 在两个仓里必须落到不同的 marker 文件"

    assert lock_module.read_marker(task_id="a877b", session_id=None, repo=str(repo_a), root=root)["run_id"] == "rvf-a"
    assert lock_module.read_marker(task_id="a877b", session_id=None, repo=str(repo_b), root=root)["run_id"] == "rvf-b"

    # 清 A 仓那把不得波及 B 仓。
    removed = lock_module.clear_marker(task_id="a877b", session_id=None, repo=str(repo_a), root=root)
    assert removed == [str(path_a)]
    assert lock_module.read_marker(task_id="a877b", session_id=None, repo=str(repo_a), root=root) is None
    assert lock_module.read_marker(task_id="a877b", session_id=None, repo=str(repo_b), root=root) is not None

    # 仓身份解析不出（不是 git 仓）→ 候选为空：写者 no-op、读者读不到。fail 方向必须是
    # 「至多多征询一次」，绝不是「静默读到别人的凭证」。
    not_a_repo = tmp / "plain-dir"
    not_a_repo.mkdir()
    assert lock_module.marker_paths(task_id="a877b", session_id=None, repo=str(not_a_repo)) == []
    assert lock_module.read_marker(task_id="a877b", session_id=None, repo=str(not_a_repo), root=root) is None


def test_agent_cli_decline_persists_reason_and_cooldown(tmp: Path) -> None:
    """decline：token 终态 declined、理由落 append-only 磁盘 ledger（rewind 不可抹）、指纹入冷却表；
    再次 decline 已终态 → 错误。"""
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)
    wt = _agent_cli_worktree(tmp)
    tok = _mint_agent_decision_token(dts, roots, worktree=wt, fingerprint="fpDecline", now=t0)

    result = cli.run_decline(token=tok, decline_kind="structural_not_needed", reason="fork 噪声，无真实改动，不再自审", roots=roots, now=t0)
    assert result.status == "ok" and result.exit_code == cli.EXIT_OK
    assert result.token_state == dts.STATE_DECLINED

    # 理由落 append-only ledger。
    ledger_path = Path(result.detail["declined_reason_ledger"])
    assert ledger_path.is_file()
    rows = read_jsonl(ledger_path)
    assert any(r.get("token") == tok and "fork 噪声" in str(r.get("reason")) for r in rows)

    # 指纹入 declined 冷却表（指纹未变不再注入）。
    assert dts.is_fingerprint_in_declined_cooldown(
        worktree=wt, candidate_fingerprint="fpDecline", cooldown_root=roots.cooldown_root
    )
    # 遥测记 declined。
    assert "declined" in [r["event"] for r in read_jsonl(roots.telemetry_root / f"{tok}.jsonl")]

    # 已终态再次 decline → 错误。
    again = cli.run_decline(token=tok, decline_kind="structural_not_needed", reason="再次", roots=roots, now=t0)
    assert again.status == "error" and again.exit_code == cli.EXIT_TOKEN_STATE


def test_agent_cli_decline_dispatched_review_clears_lock_and_optionally_waives(tmp: Path) -> None:
    """decline-dispatched-review：给「Kanban 内自动 RVF」补上历史上根本不存在的谢绝出口。

    收的是 **dispatch token**（注入 prompt 里的 RVF_DISPATCH=token=…），不是 S3 决策 token
    ——Kanban task 内的自动 RVF 走延续后端，结构上不经过 S3 决策点。断言四件事：
    ① 默认（不带豁免）= 清锁 + 清 pending + 理由落盘，scope 退回可审池；
    ② 带 --mark-declined-scope-units-review-not-required = 额外把本轮 scope 声明为不需审核；
    ③ **锁身份对账**：磁盘上的锁/pending 若属于**另一轮** dispatch（token 对不上），一律原样
       保留、不清——否则一条重试的旧命令会把新一轮的互斥与续跑状态删掉（RVF-003）；
    ④ **不谎报成功**：请求了豁免却一个 unit 都没声明成时，status 不得为 ok、message 不得
       宣称已豁免（RVF-004）。
    """
    cli = load_rvf_agent_decision_cli_module()
    prep = load_rvf_prep_file_module()
    tracker = load_diff_tracker_module()
    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)

    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    lock_root = tmp / "locks"
    allocated = tracker.allocate_review_scope(
        repo=repo, session_id="sess-decline", run_id="run-decline",
        reviewer_id="reviewer-decline", log_root_override=log_root,
    )
    assert allocated["status"] == "allocated", allocated
    scoped_unit_ids = list(allocated["scope"]["unit_ids"])

    prep_root = tmp / "prep"
    record = prep.write_prep_file(
        {
            "origin_session_id": "sess-decline",
            "origin_repo": str(repo),
            "origin_cwd": str(repo),
            "target_flow": "flow-1-self-rising",
            "target_worktree": str(repo),
            "target_kanban_task_id": "task-decline",
            "rvf_run": {
                "run_id": "run-decline",
                "run_dir": str(tmp / "run"),
                "tracker_lease_id": allocated["lease_id"],
            },
        },
        root=prep_root,
        token="dddddddddddddddd",
    )
    lock_module = load_kanban_followup_lock_module()
    lock_module.write_marker(
        task_id="task-decline", session_id="sess-decline",
        run_id="run-decline", run_dir=str(tmp / "run"), repo=str(repo), cwd=str(repo),
        dispatch_token=record.token,
        state=lock_module.STATE_DELIVERED_AWAITING_AGENT_DECISION,
        root=lock_root,
    )
    # pending（dispatched_unconfirmed）也一并摆上：正常投递时 UPS 会按 token 清掉它，但谢绝
    # 发生在「投递已落地、UPS 尚未清 / 已清」两种时序都可能，故这条路径必须自带 token guard。
    lock_module.write_pending_marker(
        task_id="task-decline", session_id="sess-decline",
        run_id="run-decline", run_dir=str(tmp / "run"), repo=str(repo), cwd=str(repo),
        token=record.token, delivery_channel="test", root=lock_root,
    )
    lock_marker_path = lock_root / "kanban-followup-in-progress" / in_progress_lock_marker_name(
        lock_module, repo=repo, task_id="task-decline"
    )
    pending_marker_path = lock_root / "kanban-followup-dispatched" / "task-task-decline.json"
    prev_log_root = os.environ.get("CODEX_RVF_LOG_ROOT")
    os.environ["CODEX_RVF_LOG_ROOT"] = str(log_root)
    try:
        # ① 默认谢绝：清锁 + 释放 lease，unit 退回 available（后续 Stop 仍可重新提议）。
        result = cli.run_decline_dispatched_review(
            token=record.token, reason="用户已让我改做别的工作",
            prep_root=prep_root, lock_root=lock_root, roots=roots, now=t0,
        )
        assert result.status == "ok" and result.exit_code == cli.EXIT_OK, result
        assert result.detail["failed_steps"] == [], result.detail
        assert result.detail["removed_lock_paths"], result.detail
        assert result.detail["lock_outcome"] == cli.KANBAN_FOLLOWUP_MARKER_CLEARED, result.detail
        assert result.detail["pending_outcome"] == cli.KANBAN_FOLLOWUP_MARKER_CLEARED, result.detail
        assert not lock_marker_path.exists()
        assert not pending_marker_path.exists()  # token 匹配 → pending 也确实被清掉
        assert result.detail["lease_release"]["released"] is True
        assert result.detail["lease_release_outcome"] == "released", result.detail
        rows = read_jsonl(Path(result.detail["declined_reason_ledger"]))
        assert any(
            r.get("token") == record.token
            and r.get("token_kind") == "dispatch"
            and r.get("review_not_required_waiver_requested") is False
            and r.get("review_not_required_waiver_declared") is False
            and r.get("failed_steps") == []
            for r in rows
        ), rows
        conn = sqlite3.connect(
            str(log_root / "diff-tracker" / "repos" / allocated["repo_key"] / "tracker.sqlite3")
        )
        try:
            states = {
                row[0]
                for row in conn.execute(
                    "SELECT review_state FROM units WHERE unit_id IN ({})".format(
                        ",".join("?" for _ in scoped_unit_ids)
                    ),
                    tuple(scoped_unit_ids),
                )
            }
        finally:
            conn.close()
        assert states == {"available"}, states  # 未豁免 → 退回可审池

        # ② 带豁免：本轮 scope 被声明为不需审核（diff/hunk 级）。
        second = tracker.allocate_review_scope(
            repo=repo, session_id="sess-decline", run_id="run-decline-2",
            reviewer_id="reviewer-decline-2", log_root_override=log_root,
        )
        record2 = prep.write_prep_file(
            {
                "origin_session_id": "sess-decline",
                "origin_repo": str(repo),
                "origin_cwd": str(repo),
                "target_flow": "flow-1-self-rising",
                "target_worktree": str(repo),
                "target_kanban_task_id": "task-decline",
                "rvf_run": {
                    "run_id": "run-decline-2",
                    "run_dir": str(tmp / "run"),
                    "tracker_lease_id": second["lease_id"],
                },
            },
            root=prep_root,
            token="eeeeeeeeeeeeeeee",
        )
        waived = cli.run_decline_dispatched_review(
            token=record2.token, reason="这批 diff 是生成物，无需审核",
            mark_declined_scope_units_review_not_required=True,
            prep_root=prep_root, lock_root=lock_root, roots=roots, now=t0,
        )
        assert waived.status == "ok", waived
        assert waived.detail["review_not_required"]["status"] == "declared", waived.detail
        assert waived.detail["review_not_required_declared"] is True, waived.detail
        assert waived.detail["failed_steps"] == [], waived.detail
        waived_rows = read_jsonl(Path(waived.detail["declined_reason_ledger"]))
        assert any(
            r.get("token") == record2.token
            and r.get("review_not_required_waiver_requested") is True
            and r.get("review_not_required_waiver_declared") is True
            for r in waived_rows
        ), waived_rows
        conn = sqlite3.connect(
            str(log_root / "diff-tracker" / "repos" / allocated["repo_key"] / "tracker.sqlite3")
        )
        try:
            rows2 = list(
                conn.execute(
                    "SELECT review_state, review_not_required_declared_reason FROM units "
                    "WHERE unit_id IN ({})".format(",".join("?" for _ in scoped_unit_ids)),
                    tuple(scoped_unit_ids),
                )
            )
        finally:
            conn.close()
        assert {r[0] for r in rows2} == {"reviewed"}, rows2
        assert all("这批 diff 是生成物" in (r[1] or "") for r in rows2), rows2
        # 豁免生效：再分配一次已无可审 scope。
        third = tracker.allocate_review_scope(
            repo=repo, session_id="sess-decline", run_id="run-decline-3",
            reviewer_id=None, dry_run=True, log_root_override=log_root,
        )
        assert third["status"] == "empty", third

        # ③ RVF-003：不带豁免的谢绝之后同一 scope 可能立刻派发下一轮。此时重放**旧** token 的
        #    谢绝命令，绝不能把新一轮的锁与 pending 删掉（否则当前 RVF 失去互斥与续跑状态）。
        lock_module.write_marker(
            task_id="task-decline", session_id="sess-decline",
            run_id="run-decline-next", run_dir=str(tmp / "run"), repo=str(repo), cwd=str(repo),
            dispatch_token="ffffffffffffffff",
            state=lock_module.STATE_DELIVERED_AWAITING_AGENT_DECISION,
            root=lock_root,
        )
        lock_module.write_pending_marker(
            task_id="task-decline", session_id="sess-decline",
            run_id="run-decline-next", run_dir=str(tmp / "run"), repo=str(repo), cwd=str(repo),
            token="ffffffffffffffff", delivery_channel="test", root=lock_root,
        )
        replayed = cli.run_decline_dispatched_review(
            token=record.token, reason="重试一条旧命令（或旧 token 的调用迟到）",
            prep_root=prep_root, lock_root=lock_root, roots=roots, now=t0,
        )
        assert replayed.status == "decline_incomplete", replayed
        assert replayed.exit_code == cli.EXIT_DECLINE_DISPATCHED_REVIEW_INCOMPLETE, replayed
        assert (
            replayed.detail["lock_outcome"]
            == cli.KANBAN_FOLLOWUP_MARKER_PRESERVED_FOR_ANOTHER_DISPATCH
        ), replayed.detail
        assert (
            replayed.detail["pending_outcome"]
            == cli.KANBAN_FOLLOWUP_MARKER_PRESERVED_FOR_ANOTHER_DISPATCH
        ), replayed.detail
        assert replayed.detail["removed_lock_paths"] == [], replayed.detail
        assert replayed.detail["preserved_lock_paths"], replayed.detail
        # 新一轮的锁与 pending 都还在，且内容未被动过。
        assert lock_marker_path.exists() and pending_marker_path.exists()
        assert json.loads(lock_marker_path.read_text(encoding="utf-8"))["dispatch_token"] == (
            "ffffffffffffffff"
        )
        assert json.loads(pending_marker_path.read_text(encoding="utf-8"))["token"] == (
            "ffffffffffffffff"
        )
        assert any(
            step["step"] == "kanban_followup_lock_clear" for step in replayed.detail["failed_steps"]
        ), replayed.detail
        # 部署滞后期间由老版 UPS arm 的锁没有 dispatch_token：必须回退按 run_id 对账，否则
        # 部署完成前每一次谢绝都会退化成空操作；两者皆无时才允许「无从对账、照清」。
        classify = cli.classify_kanban_followup_lock_identity_against_dispatch
        assert classify(
            {"run_id": "run-decline"}, dispatch_token=record.token, run_id="run-decline"
        ) == cli.LOCK_IDENTITY_MATCHES_THIS_DISPATCH
        assert classify(
            {"run_id": "run-other"}, dispatch_token=record.token, run_id="run-decline"
        ) == cli.LOCK_IDENTITY_BELONGS_TO_ANOTHER_DISPATCH
        assert classify(
            {}, dispatch_token=record.token, run_id="run-decline"
        ) == cli.LOCK_IDENTITY_UNVERIFIABLE_LEGACY_MARKER

        # ④ RVF-004：请求了豁免但一个 unit 都没声明成（这里用一把不存在的 lease 触发 noop，
        #    与 DB 写失败对用户是同一件事：**没有豁免**）。不得报 ok，更不得宣称已豁免。
        record_noop = prep.write_prep_file(
            {
                "origin_session_id": "sess-decline",
                "origin_repo": str(repo),
                "origin_cwd": str(repo),
                "target_flow": "flow-1-self-rising",
                "target_worktree": str(repo),
                "target_kanban_task_id": "task-decline-noop",
                "rvf_run": {
                    "run_id": "run-decline-noop",
                    "run_dir": str(tmp / "run"),
                    "tracker_lease_id": "lse-does-not-exist",
                },
            },
            root=prep_root,
            token="cccccccccccccccc",
        )
        unwaived = cli.run_decline_dispatched_review(
            token=record_noop.token, reason="想豁免但目标 lease 已不存在",
            mark_declined_scope_units_review_not_required=True,
            prep_root=prep_root, lock_root=lock_root, roots=roots, now=t0,
        )
        assert unwaived.status == "decline_incomplete", unwaived
        assert unwaived.exit_code == cli.EXIT_DECLINE_DISPATCHED_REVIEW_INCOMPLETE, unwaived
        assert unwaived.detail["review_not_required_declared"] is False, unwaived.detail
        assert any(
            step["step"] == "review_not_required_waiver"
            for step in unwaived.detail["failed_steps"]
        ), unwaived.detail
        # message 必须如实：不出现成功路径那句「已把 scope 声明为不需审核」，且明说没豁免。
        assert "并把本轮 scope 的 diff 声明为「不需审核」" not in unwaived.message, unwaived.message
        assert "没有" in unwaived.message and "不需审核" in unwaived.message, unwaived.message
        assert unwaived.next_steps, unwaived
        noop_rows = read_jsonl(Path(unwaived.detail["declined_reason_ledger"]))
        assert any(
            r.get("token") == record_noop.token
            and r.get("review_not_required_waiver_requested") is True
            and r.get("review_not_required_waiver_declared") is False
            for r in noop_rows
        ), noop_rows
    finally:
        if prev_log_root is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = prev_log_root


def test_agent_cli_error_paths_missing_and_terminal_tokens(tmp: Path) -> None:
    """错误路径：不存在的 token 在 start / dispatch / scope 上都归 EXIT_TOKEN_STATE；dispatched 终态
    token 上 scope / dispatch 亦被拒。"""
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)

    missing = "0" * 16
    s = cli.run_start(token=missing, reason="x", roots=roots, now=t0, prepare_pipeline=_agent_cli_stub_prepare(tmp))
    assert s.status == "error" and s.exit_code == cli.EXIT_TOKEN_STATE
    d = cli.run_dispatch(token=missing, strictness="standard", roots=roots, now=t0, reviewer_dispatcher=lambda **k: {"status": "ok"})
    assert d.status == "error" and d.exit_code == cli.EXIT_TOKEN_STATE
    sc = cli.run_scope_exclude(token=missing, paths=["x"], reason="r", roots=roots, now=t0)
    assert sc.status == "error" and sc.exit_code == cli.EXIT_TOKEN_STATE

    # dispatched 终态 token 上 scope / dispatch 被拒（走完整链到 dispatched 后再试）。
    wt = _agent_cli_worktree(tmp)
    tok = _mint_agent_decision_token(dts, roots, worktree=wt, fingerprint="fpTerm", now=t0)
    cli.run_start(token=tok, reason="开审", roots=roots, now=t0, prepare_pipeline=_agent_cli_stub_prepare(tmp))
    cli.run_dispatch(token=tok, strictness="light", roots=roots, now=t0, reviewer_dispatcher=lambda **k: {"status": "ok"})
    assert dts.read_decision_token(tok, root=roots.token_root, now=t0).state == dts.STATE_DISPATCHED
    guard_scope = cli.run_scope_exclude(token=tok, paths=["x"], reason="r", roots=roots, now=t0)
    assert guard_scope.status == "error" and guard_scope.exit_code == cli.EXIT_TOKEN_STATE
    guard_dispatch = cli.run_dispatch(token=tok, strictness="light", roots=roots, now=t0, reviewer_dispatcher=lambda **k: {"status": "ok"})
    assert guard_dispatch.status == "error" and guard_dispatch.exit_code == cli.EXIT_TOKEN_STATE


# ---------------------------------------------------------------------------
# S2：候选摘要（stop_decision_candidate_summary）+ 严格度参考档
# （review_strictness_reference_calculator / review-strictness-presets.json）
# ---------------------------------------------------------------------------

STOP_DECISION_CANDIDATE_SUMMARY = SCRIPT_DIR / "stop_decision_candidate_summary.py"
REVIEW_STRICTNESS_REFERENCE_CALCULATOR = SCRIPT_DIR / "review_strictness_reference_calculator.py"
REVIEW_STRICTNESS_PRESETS = (
    SCRIPT_DIR.parent / "config" / "review-strictness-presets.json"
)


def _load_candidate_summary_modules():
    """加载 S2 三件套所需模块（候选摘要 / 严格度计算器 / 高水位 / diff_tracker）。

    候选摘要模块 top-level import 中性核 committed_round_detection 的同源下界 resolver，并惰性
    import Stop hook 引擎 stop_review_validate_fix（复用 workspace 守卫 / id 提取）；此处只需保证
    ROOT + SCRIPT_DIR 在 sys.path 上，pyroot 与 core.* import 即可解析。review_highwater_marker
    随 S10h 迁 core，故从 core.session_scope_allocation import。
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import review_strictness_reference_calculator as _calc  # noqa: PLC0415
    from core.session_scope_allocation import review_highwater_marker as _hw  # noqa: PLC0415
    import stop_decision_candidate_summary as _summary  # noqa: PLC0415
    import core.session_scope_allocation.reviewable_unit_diff_tracker as _dt  # noqa: PLC0415

    return _summary, _calc, _hw, _dt


def _stop_candidate_repo(tmp: Path) -> tuple[Path, str]:
    """初始化隔离 git repo（单文件 base 提交），返回 (repo, base_sha)。"""
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    base = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    return repo, base


def _candidate_summary_log_root_env(log_root: Path):
    """上下文：把 CODEX_RVF_LOG_ROOT 指到 tmp，退出还原（默认 resolver 经 log_root() 读高水位/
    round-baseline；隔离防污染真实 ~/.rvf）。"""
    import contextlib  # noqa: PLC0415

    @contextlib.contextmanager
    def _ctx():
        key = "CODEX_RVF_LOG_ROOT"
        prev = os.environ.get(key)
        os.environ[key] = str(log_root)
        try:
            yield
        finally:
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev

    return _ctx()


def test_review_strictness_reference_weights_and_classification(tmp: Path) -> None:
    """R13：路径分类 + 非空行计数 + 删除降权 + 代码1.0/文档0.3 + generated归零 + 不拆comment。"""
    _summary, calc, _hw, _dt = _load_candidate_summary_modules()

    # 分类优先级：generated > documentation > test > code。
    assert calc.classify_review_path("src/app.py") == calc.CATEGORY_CODE
    assert calc.classify_review_path("docs/guide.md") == calc.CATEGORY_DOCUMENTATION
    assert calc.classify_review_path("tests/test_x.py") == calc.CATEGORY_TEST
    assert calc.classify_review_path("pkg/foo_test.go") == calc.CATEGORY_TEST
    assert calc.classify_review_path("a/FooTest.java") == calc.CATEGORY_TEST
    # 'latest.py' / 'manifest.py' 不得被误判 test（词尾 test 但非测试）。
    assert calc.classify_review_path("latest.py") == calc.CATEGORY_CODE
    assert calc.classify_review_path("manifest.py") == calc.CATEGORY_CODE
    assert calc.classify_review_path("dist/app.min.js") == calc.CATEGORY_GENERATED
    assert calc.classify_review_path("pnpm-lock.yaml") == calc.CATEGORY_GENERATED
    # 文档判定先于 test：tests/ 下的 .md 仍按文档计。
    assert calc.classify_review_path("tests/fixtures/sample.md") == calc.CATEGORY_DOCUMENTATION

    res = calc.compute_strictness_reference(
        [
            {
                "path": "src/a.py",
                # 非空 added=3（x=1 / # comment kept / y=2），2 行空白被忽略；不拆 comment，
                # 注释行照常计入非空 added。
                "added_lines": ["x=1", "", "  ", "# comment kept", "y=2"],
                "deleted_lines": ["old1", "old2", "old3", "old4"],
            },
            {"path": "docs/g.md", "added_lines": ["line"] * 10, "deleted_lines": []},
            {
                "path": "dist/b.min.js",
                "added_lines": ["z"] * 999,
                "deleted_lines": ["w"] * 999,
            },
        ]
    )
    per = {f["path"]: f for f in res["per_file"]}
    assert per["src/a.py"]["nonblank_added_lines"] == 3
    assert per["src/a.py"]["blank_lines_ignored"] == 2
    assert per["src/a.py"]["nonblank_deleted_lines"] == 4
    # 加权 = 1.0 * (3 + 0.25*4) = 4.0（删除降权 0.25）。
    assert per["src/a.py"]["weighted_effective_lines"] == 4.0
    # 文档 0.3：10 * 0.3 = 3.0。
    assert per["docs/g.md"]["weighted_effective_lines"] == 3.0
    # generated 归零（增删都不计）。
    assert per["dist/b.min.js"]["weighted_effective_lines"] == 0.0
    assert per["dist/b.min.js"]["generated_zeroed"] is True
    # 不拆 comment 规则显式化。
    assert res["rule_parameters"]["comment_lines_are_not_split_out"] is True
    # 总加权 7.0 < 40 → light。
    assert res["totals"]["weighted_effective_lines"] == 7.0
    assert res["suggested_strictness_tier"] == calc.STRICTNESS_TIER_LIGHT


def test_review_strictness_reference_oversized_elevate_and_tiers(tmp: Path) -> None:
    """R24：非测试非生成物 >1000 非空行 → 建议 elevate；档位阈值边界。"""
    _summary, calc, _hw, _dt = _load_candidate_summary_modules()

    big_code = calc.compute_strictness_reference(
        [{"path": "src/big.py", "added_lines": ["x"] * 1201, "deleted_lines": []}]
    )
    assert big_code["oversized_change_elevate_recommended"] is True
    assert big_code["suggested_strictness_tier"] == calc.STRICTNESS_TIER_STRICT

    # 测试文件不计入 elevate（R24「不含测试」）。
    big_test = calc.compute_strictness_reference(
        [{"path": "tests/test_big.py", "added_lines": ["x"] * 1201, "deleted_lines": []}]
    )
    assert big_test["oversized_change_elevate_recommended"] is False

    # generated 不计入 elevate。
    big_gen = calc.compute_strictness_reference(
        [{"path": "dist/x.min.js", "added_lines": ["x"] * 5000, "deleted_lines": []}]
    )
    assert big_gen["oversized_change_elevate_recommended"] is False

    # 空输入 → light、不 elevate（稳定空态）。
    empty = calc.compute_strictness_reference([])
    assert empty["suggested_strictness_tier"] == calc.STRICTNESS_TIER_LIGHT
    assert empty["oversized_change_elevate_recommended"] is False

    # standard 档边界：加权落在 [light上限, standard上限) → standard。
    mid = calc.compute_strictness_reference(
        [{"path": "src/mid.py", "added_lines": ["x"] * 100, "deleted_lines": []}]
    )
    assert mid["suggested_strictness_tier"] == calc.STRICTNESS_TIER_STANDARD


def test_review_strictness_presets_schema(tmp: Path) -> None:
    """R14：3 档 preset schema —— light 单 reviewer 不用廉价模型；strict 提升 effort +
    更多 provider + per-phase 模型选择。"""
    data = json.loads(REVIEW_STRICTNESS_PRESETS.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    presets = data["presets"]
    assert set(presets) == {"light", "standard", "strict"}

    required_fields = {
        "strictness_tier_name",
        "external_reviewer_count_target",
        "single_reviewer_forbids_cheap_model",
        "reviewer_model_cost_tier_floor",
        "reviewer_reasoning_effort_level",
        "additional_external_provider_count_beyond_double_review_baseline",
        "use_all_available_external_providers",
        "per_phase_model_selection_enabled",
    }
    for name, preset in presets.items():
        missing = required_fields - set(preset)
        assert not missing, (name, missing)
        assert preset["strictness_tier_name"] == name

    effort_order = data["reasoning_effort_level_ascending_order"]
    cost_order = data["reviewer_model_cost_tier_ascending_order"]
    light = presets["light"]
    standard = presets["standard"]
    strict = presets["strict"]

    # light：恰好 1 路 reviewer，且禁用廉价模型（cost floor 不是最廉价档）。
    assert light["external_reviewer_count_target"] == 1
    assert light["single_reviewer_forbids_cheap_model"] is True
    assert light["reviewer_model_cost_tier_floor"] in cost_order
    assert cost_order.index(light["reviewer_model_cost_tier_floor"]) >= 1

    # reviewer 数单调不减：light <= standard <= strict。
    assert (
        light["external_reviewer_count_target"]
        <= standard["external_reviewer_count_target"]
        <= strict["external_reviewer_count_target"]
    )

    # strict：reasoning effort 相对 standard 提升。
    assert effort_order.index(strict["reviewer_reasoning_effort_level"]) > effort_order.index(
        standard["reviewer_reasoning_effort_level"]
    )
    # strict：更多 external provider（额外计数 > standard 或拉满可用池）。
    assert (
        strict["additional_external_provider_count_beyond_double_review_baseline"]
        > standard["additional_external_provider_count_beyond_double_review_baseline"]
        or strict["use_all_available_external_providers"] is True
    )
    # per-phase 模型选择并入 strict（仅 strict 开启）。
    assert strict["per_phase_model_selection_enabled"] is True
    assert standard["per_phase_model_selection_enabled"] is False
    assert light["per_phase_model_selection_enabled"] is False

    # Tier-1 池全集必须与出厂 reviewer-registry.json 逐字同源（R15/R20 补集 − 主 harness
    # 在 S7 落地）。这里刻意**不再抄一份字面量**：两处各写一份 harness 名单，迟早会漂——
    # 加 kimi_code / oh_my_pi 时就正好撞上，preset 里还写着三条而 registry 已是五条，
    # 而这个断言当时是绿的（它只在验证自己抄得对不对）。
    registry_harnesses = set(
        json.loads(
            (SCRIPT_DIR.parent / "config" / "reviewer-registry.json").read_text(
                encoding="utf-8"
            )
        )["harnesses"]
    )
    assert (
        set(data["reviewer_harness_pool_tier1_full_membership"]) == registry_harnesses
    ), (data["reviewer_harness_pool_tier1_full_membership"], sorted(registry_harnesses))


def test_candidate_summary_includes_committed_but_unreviewed(tmp: Path) -> None:
    """R1/R18：候选摘要必含「已提交但未审」的 committed-round 改动（下界经同源高水位 resolver）。"""
    summary_mod, _calc, hw, _dt = _load_candidate_summary_modules()
    repo, base = _stop_candidate_repo(tmp)
    log_root = tmp / "logroot"

    # 本轮提交（committed-but-unreviewed）。
    (repo / "f.txt").write_text("base\ncommitted-line\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "round1"], cwd=repo)
    # 叠加未提交改动 + 一个 untracked 文件。
    (repo / "f.txt").write_text("base\ncommitted-line\ndirty-line\n", encoding="utf-8")
    (repo / "g.py").write_text("new\ncontent\n", encoding="utf-8")

    with _candidate_summary_log_root_env(log_root):
        # 高水位 = base（== 上一次 review 覆盖到的 HEAD）；默认 resolver 经 log_root() 读它。
        hw.write_review_highwater(
            task_id=None, session_id="sess-A", reviewed_head=base, repo=str(repo), root=log_root
        )
        summ = summary_mod.build_candidate_summary(
            event={"cwd": str(repo)},
            cwd=str(repo),
            repo=str(repo),
            ledger=None,
            log_root_override=log_root,
            session_id_resolver=lambda _e: "sess-A",
            task_id_resolver=lambda _e: None,
        )

    by_path = {c["path"]: c for c in summ["candidates"]}
    assert "f.txt" in by_path
    assert by_path["f.txt"]["change_state"] in (
        summary_mod.CHANGE_STATE_COMMITTED,
        summary_mod.CHANGE_STATE_COMMITTED_AND_UNCOMMITTED,
    )
    assert "g.py" in by_path
    assert by_path["g.py"]["change_state"] == summary_mod.CHANGE_STATE_UNCOMMITTED
    assert summ["has_committed_but_unreviewed"] is True
    assert summ["committed_round_baseline"] == base
    # R9 口径切换（S6）：lower_bound_source = 「已审」裁决权威 = review_state（有 committed 窗口种子）；
    # 窗口种子来源另置 committed_round_window_seed_source（此处 baseline==高水位）；无 reviewed 单元
    # 故 review_state 过滤为空。
    assert summ["lower_bound_source"] == summary_mod.LOWER_BOUND_SOURCE_TRACKER_REVIEW_STATE
    assert (
        summ["committed_round_window_seed_source"]
        == summary_mod.COMMITTED_ROUND_WINDOW_SEED_REVIEW_HIGHWATER
    )
    assert summ["review_state_filtered_committed_path_count"] == 0
    # bootstrap-size 并入摘要（R25）。
    assert summ["bootstrap_size"]["candidate_path_count"] == 2
    # 严格度参考档随附（调 2 号交付物）。
    assert "suggested_strictness_tier" in summ["strictness_reference"]


def test_candidate_summary_fingerprint_stable_and_change_sensitive(tmp: Path) -> None:
    """R25：候选集指纹在内容不变时稳定；文件内容变化时敏感变化；复原后回到原值。"""
    summary_mod, _calc, _hw, _dt = _load_candidate_summary_modules()
    repo, base = _stop_candidate_repo(tmp)
    log_root = tmp / "logroot"
    (repo / "f.txt").write_text("base\ndirtyA\n", encoding="utf-8")

    def build():
        return summary_mod.build_candidate_summary(
            event={"cwd": str(repo)},
            cwd=str(repo),
            repo=str(repo),
            ledger=None,
            log_root_override=log_root,
            workspace_guard=lambda **_k: None,
            task_id_resolver=lambda _e: None,
            session_id_resolver=lambda _e: "sess-A",
            committed_baseline_resolver=lambda _rp, _t, _s: base,
            committed_base_ref_resolver=lambda _rp: None,
        )

    fp1 = build()["candidate_fingerprint"]
    # 稳定性：不改内容再算一次。
    assert build()["candidate_fingerprint"] == fp1
    assert fp1.startswith("sha256:")

    # 变化敏感性：改内容 → 指纹变。
    original = (repo / "f.txt").read_text(encoding="utf-8")
    (repo / "f.txt").write_text(original + "extra\n", encoding="utf-8")
    fp2 = build()["candidate_fingerprint"]
    assert fp2 != fp1

    # 复原 → 回到原指纹。
    (repo / "f.txt").write_text(original, encoding="utf-8")
    assert build()["candidate_fingerprint"] == fp1


def test_candidate_summary_scope_fingerprint_ignores_content_but_tracks_paths_and_head(tmp: Path) -> None:
    """工作范围指纹（闸 2b 用）：内容变 → 不变；新增纳入路径 → 变；HEAD 变 → 变。内容指纹照旧敏感。
    空摘要也带稳定的工作范围指纹，但不携带身份子字典（无候选根本到不了铸造）。"""
    summary_mod, _calc, _hw, _dt = _load_candidate_summary_modules()
    repo, base = _stop_candidate_repo(tmp)
    log_root = tmp / "logroot"
    (repo / "f.txt").write_text("base\ndirtyA\n", encoding="utf-8")

    def build():
        return summary_mod.build_candidate_summary(
            event={"cwd": str(repo)},
            cwd=str(repo),
            repo=str(repo),
            ledger=None,
            log_root_override=log_root,
            workspace_guard=lambda **_k: None,
            task_id_resolver=lambda _e: None,
            session_id_resolver=lambda _e: "sess-A",
            committed_baseline_resolver=lambda _rp, _t, _s: base,
            committed_base_ref_resolver=lambda _rp: None,
        )

    first = build()
    identity = first["candidate_scope_identity_for_declined_session_snooze"]
    assert first["candidate_scope_fingerprint"] == identity["candidate_scope_fingerprint"]
    assert identity["worktree_head_commit_sha"] == base
    assert identity["committed_round_baseline"] == base
    assert identity["included_candidate_paths"] == ["f.txt"]
    assert identity["unreviewed_nonblank_changed_line_estimate"] >= 1
    assert identity["candidate_scope_fingerprint_algorithm_version"] == (
        summary_mod.CANDIDATE_SCOPE_FINGERPRINT_ALGORITHM_VERSION
    )

    # 内容变：内容指纹变，工作范围指纹不变。
    (repo / "f.txt").write_text("base\ndirtyA\ndirtyB\n", encoding="utf-8")
    content_changed = build()
    assert content_changed["candidate_fingerprint"] != first["candidate_fingerprint"]
    assert content_changed["candidate_scope_fingerprint"] == first["candidate_scope_fingerprint"]

    # 新增纳入路径：工作范围指纹变。
    (repo / "g.txt").write_text("new\n", encoding="utf-8")
    path_added = build()
    assert path_added["candidate_scope_fingerprint"] != first["candidate_scope_fingerprint"]
    (repo / "g.txt").unlink()

    # HEAD 变（提交一个无关文件，f.txt 保持 dirty）：工作范围指纹变。
    (repo / "unrelated.txt").write_text("x\n", encoding="utf-8")
    run(["git", "add", "unrelated.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "unrelated"], cwd=repo)
    head_moved = build()
    assert head_moved["candidate_scope_identity_for_declined_session_snooze"]["worktree_head_commit_sha"] != base
    assert head_moved["candidate_scope_fingerprint"] != content_changed["candidate_scope_fingerprint"]

    empty = summary_mod._empty_summary(repo=None)
    assert empty["candidate_scope_fingerprint"].startswith("sha256:")
    assert empty["candidate_scope_identity_for_declined_session_snooze"] is None


def test_candidate_summary_ownership_filters_other_session(tmp: Path) -> None:
    """Q4=a：ownership 作为 filter —— tracker 缓存归属他 session 的路径被排除出候选。"""
    summary_mod, _calc, _hw, dt = _load_candidate_summary_modules()
    repo, _base = _stop_candidate_repo(tmp)
    log_root = tmp / "logroot"
    (repo / "b.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "b.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "add-b"], cwd=repo)
    (repo / "f.txt").write_text("base\ndirtyF\n", encoding="utf-8")
    (repo / "b.txt").write_text("base\ndirtyB\n", encoding="utf-8")

    # 播种 tracker：f.txt 归 sess-OTHER，b.txt 归 sess-A（当前）。
    for session_id, run_id, path in (
        ("sess-OTHER", "rO", "f.txt"),
        ("sess-A", "rA", "b.txt"),
    ):
        owned = dt.OwnedUnit(path=path, unit="path", hunk_anchor=None)
        dt.register_claims(
            repo=repo,
            session_id=session_id,
            run_id=run_id,
            worktree=repo,
            branch=None,
            owned_paths=[path],
            apply_patch_paths={path},
            exec_only_paths=set(),
            owned_units_override=[(owned, "apply_patch")],
            log_root_override=log_root,
        )

    summ = summary_mod.build_candidate_summary(
        event={"cwd": str(repo)},
        cwd=str(repo),
        repo=str(repo),
        ledger=None,
        log_root_override=log_root,
        workspace_guard=lambda **_k: None,
        task_id_resolver=lambda _e: None,
        session_id_resolver=lambda _e: "sess-A",
        committed_baseline_resolver=lambda _rp, _t, _s: None,
        committed_base_ref_resolver=lambda _rp: None,
    )
    by_path = {c["path"]: c for c in summ["candidates"]}
    assert by_path["f.txt"]["ownership"] == summary_mod.OWNERSHIP_OTHER_SESSION_OWNED
    assert by_path["f.txt"]["included_in_candidate_set"] is False
    assert by_path["b.txt"]["ownership"] == summary_mod.OWNERSHIP_SESSION_OWNED
    assert by_path["b.txt"]["included_in_candidate_set"] is True
    assert summ["candidate_path_count"] == 1
    assert summ["filtered_out_other_session_path_count"] == 1


def _write_serena_session_start_files(repo: Path) -> None:
    """模拟 Serena 在会话启动时往 cwd 写的两个文件（形态 A：新 worktree 首次激活）。"""
    (repo / ".serena").mkdir(exist_ok=True)
    (repo / ".serena" / ".gitignore").write_text("/cache\n/project.local.yml\n", encoding="utf-8")
    (repo / ".serena" / "project.yml").write_text(
        'project_name: "repo"\nlanguage_servers:\n- python\nencoding: "utf-8"\n', encoding="utf-8"
    )


def _candidate_summary_without_committed_window(summary_mod, repo: Path, log_root: Path, session_id: str = "sess-A"):
    return summary_mod.build_candidate_summary(
        event={"cwd": str(repo)},
        cwd=str(repo),
        repo=str(repo),
        ledger=None,
        log_root_override=log_root,
        workspace_guard=lambda **_k: None,
        task_id_resolver=lambda _e: None,
        session_id_resolver=lambda _e: session_id,
        committed_baseline_resolver=lambda _rp, _t, _s: None,
        committed_base_ref_resolver=lambda _rp: None,
    )


def test_candidate_summary_excludes_serena_generated_untracked_files(tmp: Path) -> None:
    """只有 Serena 启动时生成的 .serena/ 未跟踪文件是脏的 → 候选集为空，不该铸决策点。

    被排除的路径仍留在 candidates 清单里（included=False + 排除原因），指纹等于空候选集指纹。"""
    summary_mod, _calc, _hw, _dt = _load_candidate_summary_modules()
    repo, _base = _stop_candidate_repo(tmp)
    log_root = tmp / "logroot"
    _write_serena_session_start_files(repo)

    summ = _candidate_summary_without_committed_window(summary_mod, repo, log_root)

    by_path = {c["path"]: c for c in summ["candidates"]}
    assert set(by_path) == {".serena/.gitignore", ".serena/project.yml"}, sorted(by_path)
    for candidate in by_path.values():
        assert candidate["included_in_candidate_set"] is False
        assert candidate["candidate_set_exclusion_reason"] == (
            summary_mod.CANDIDATE_SET_EXCLUSION_REASON_REVIEW_SCOPE_EXCLUDED_PATH_PREFIX
        )
    assert summ["candidate_path_count"] == 0
    assert summ["bootstrap_size"]["candidate_path_count"] == 0
    assert summ["filtered_out_review_scope_excluded_path_prefix_count"] == 2
    assert summ["filtered_out_other_session_path_count"] == 0
    assert ".serena/" in summ["review_scope_excluded_path_prefixes"]
    assert summ["candidate_fingerprint"] == summary_mod.compute_candidate_fingerprint([])


def test_candidate_summary_excludes_tracked_serena_rewrite_without_touching_real_work_fingerprint(
    tmp: Path,
) -> None:
    """形态 B：仓库已跟踪的 .serena/project.yml 被 Serena 就地重写（gitignore 治不了的那种）。

    与真实改动并存时，候选集只剩真实改动，且内容指纹与工作范围指纹都和「.serena 没被动过」时一致——
    Serena 的重写不会让 declined 冷却失效、也不会让横幅规模虚高。"""
    summary_mod, _calc, _hw, _dt = _load_candidate_summary_modules()
    repo, _base = _stop_candidate_repo(tmp)
    log_root = tmp / "logroot"
    _write_serena_session_start_files(repo)
    run(["git", "add", ".serena"], cwd=repo)
    run(["git", "commit", "-q", "-m", "track serena project"], cwd=repo)
    (repo / "src.py").write_text("def real_work():\n    return 1\n", encoding="utf-8")

    only_real_work = _candidate_summary_without_committed_window(summary_mod, repo, log_root)
    assert only_real_work["candidate_path_count"] == 1
    assert only_real_work["filtered_out_review_scope_excluded_path_prefix_count"] == 0

    (repo / ".serena" / "project.yml").write_text(
        'project_name: "repo"\nlanguage_servers:\n- python\nencoding: "utf-8"\nline_ending:\n',
        encoding="utf-8",
    )
    porcelain = run(["git", "status", "--porcelain"], cwd=repo).stdout
    assert " M .serena/project.yml" in porcelain, porcelain
    with_serena_rewrite = _candidate_summary_without_committed_window(summary_mod, repo, log_root)

    by_path = {c["path"]: c for c in with_serena_rewrite["candidates"]}
    assert by_path[".serena/project.yml"]["included_in_candidate_set"] is False
    assert by_path[".serena/project.yml"]["candidate_set_exclusion_reason"] == (
        summary_mod.CANDIDATE_SET_EXCLUSION_REASON_REVIEW_SCOPE_EXCLUDED_PATH_PREFIX
    )
    assert by_path["src.py"]["included_in_candidate_set"] is True
    assert by_path["src.py"]["candidate_set_exclusion_reason"] is None
    assert with_serena_rewrite["candidate_path_count"] == 1
    assert with_serena_rewrite["filtered_out_review_scope_excluded_path_prefix_count"] == 1
    assert with_serena_rewrite["candidate_fingerprint"] == only_real_work["candidate_fingerprint"]
    assert (
        with_serena_rewrite["candidate_scope_fingerprint"]
        == only_real_work["candidate_scope_fingerprint"]
    )
    assert with_serena_rewrite["bootstrap_size"] == only_real_work["bootstrap_size"]


def test_candidate_summary_honors_repo_local_ignore_file_and_keeps_exclusion_counts_separate(
    tmp: Path,
) -> None:
    """.review-validate-fix-ignore 在发起前的候选阶段就生效（以前只在 start 后的 packet 里生效）；
    前缀排除与他 session 归属排除各自计数，互不串味。"""
    summary_mod, _calc, _hw, dt = _load_candidate_summary_modules()
    prefixes_mod = importlib.import_module("review_scope_excluded_path_prefixes")
    repo, _base = _stop_candidate_repo(tmp)
    log_root = tmp / "logroot"
    (repo / ".review-validate-fix-ignore").write_text("# 生成物\ngenerated/\n", encoding="utf-8")
    (repo / "b.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", ".review-validate-fix-ignore", "b.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "ignore file + b"], cwd=repo)
    (repo / "generated").mkdir()
    (repo / "generated" / "bundle.js").write_text("console.log(1)\n", encoding="utf-8")
    (repo / "f.txt").write_text("base\ndirtyF\n", encoding="utf-8")
    (repo / "b.txt").write_text("base\ndirtyB\n", encoding="utf-8")

    assert prefixes_mod.load_review_scope_excluded_path_prefixes(repo, []) == [".serena/", "generated/"]
    assert prefixes_mod.path_matches_review_scope_excluded_path_prefix("generated/bundle.js", ["generated/"])
    assert not prefixes_mod.path_matches_review_scope_excluded_path_prefix("generated.txt", ["generated/"])

    for session_id, run_id, path in (
        ("sess-OTHER", "rO", "f.txt"),
        ("sess-A", "rA", "b.txt"),
    ):
        owned = dt.OwnedUnit(path=path, unit="path", hunk_anchor=None)
        dt.register_claims(
            repo=repo,
            session_id=session_id,
            run_id=run_id,
            worktree=repo,
            branch=None,
            owned_paths=[path],
            apply_patch_paths={path},
            exec_only_paths=set(),
            owned_units_override=[(owned, "apply_patch")],
            log_root_override=log_root,
        )

    summ = _candidate_summary_without_committed_window(summary_mod, repo, log_root)

    by_path = {c["path"]: c for c in summ["candidates"]}
    assert by_path["generated/bundle.js"]["candidate_set_exclusion_reason"] == (
        summary_mod.CANDIDATE_SET_EXCLUSION_REASON_REVIEW_SCOPE_EXCLUDED_PATH_PREFIX
    )
    assert by_path["f.txt"]["candidate_set_exclusion_reason"] == (
        summary_mod.CANDIDATE_SET_EXCLUSION_REASON_OTHER_SESSION_OWNED
    )
    assert by_path["b.txt"]["included_in_candidate_set"] is True
    assert summ["candidate_path_count"] == 1
    assert summ["filtered_out_other_session_path_count"] == 1
    assert summ["filtered_out_review_scope_excluded_path_prefix_count"] == 1
    assert summ["review_scope_excluded_path_prefixes"] == [".serena/", "generated/"]


def test_candidate_summary_workspace_guard_short_circuits(tmp: Path) -> None:
    """先过 kanban_task_workspace_guard：命中 → 短路空候选（不审 unrelated dirty repo）。"""
    summary_mod, _calc, _hw, _dt = _load_candidate_summary_modules()
    repo, _base = _stop_candidate_repo(tmp)
    (repo / "f.txt").write_text("base\ndirty\n", encoding="utf-8")

    blocked = summary_mod.build_candidate_summary(
        event={"cwd": str(repo)},
        cwd=str(repo),
        repo=str(repo),
        ledger=None,
        workspace_guard=lambda **_k: {
            "action": "skip",
            "reason_code": "kanban_task_workspace_mismatch",
        },
    )
    assert blocked["workspace_guard_blocked"] is True
    assert blocked["candidate_path_count"] == 0
    assert blocked["candidates"] == []
    assert blocked["workspace_guard_payload"]["reason_code"] == "kanban_task_workspace_mismatch"


def test_candidate_fingerprint_serialization_format(tmp: Path) -> None:
    """R25 指纹序列化格式：空态稳定、顺序无关、分隔符安全、内容摘要敏感。"""
    summary_mod, _calc, _hw, _dt = _load_candidate_summary_modules()
    fp = summary_mod.compute_candidate_fingerprint

    # 空态稳定且带版本前缀。
    empty = fp([])
    assert empty == fp([])
    assert empty.startswith("sha256:")

    # 顺序无关（内部按 UTF-8 字节序排序）。
    a = fp([("src/a.py", "h1"), ("src/b.py", "h2")])
    b = fp([("src/b.py", "h2"), ("src/a.py", "h1")])
    assert a == b

    # 内容摘要敏感：同路径不同摘要 → 不同指纹。
    assert fp([("src/a.py", "h1")]) != fp([("src/a.py", "h2")])
    # 路径集敏感：多一个候选 → 不同指纹。
    assert fp([("src/a.py", "h1")]) != fp([("src/a.py", "h1"), ("src/b.py", "h2")])

    # 分隔符安全：naive 拼接会让 ("ab","") 与 ("a","b") 撞车；NUL 分隔杜绝此歧义。
    assert fp([("ab", "")]) != fp([("a", "b")])
    assert fp([("a", "bc"), ("d", "")]) != fp([("a", "b"), ("cd", "")])


# ---------------------------------------------------------------------------
# S6：R9 真相源迁移（SHA marker → tracker review_state；方案 A）
# ---------------------------------------------------------------------------

def test_select_committed_paths_reviewed_by_session_filters_reviewed_owned(tmp: Path) -> None:
    """# OBSERVATION-WINDOW(R9) 过滤器单元用例：本 session 拥有且全 reviewed 的 committed 路径被
    判「已审」剔除；无 tracker 记录（bootstrap）返回空集；review 前（assigned/available）不剔除；
    他 session 视角 / 缺 session_id 一律不剔除（保守偏「重审」）。review_state='reviewed' 的读取
    幂等（durable，重复读仍剔除）——覆盖「有记录 / 无记录 bootstrap / 单调性」三态。"""
    dt, sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)  # f.txt base 提交
    logs = tmp / "logs"

    # 无记录 bootstrap：tracker DB 尚不存在 → 空集（退化 SHA-only 窗口，不误剔）。
    assert dt.select_committed_paths_reviewed_by_session(
        repo=repo, session_id="s1", candidate_paths={"f.txt"}, log_root_override=logs
    ) == set()

    # 本轮提交 f.txt（committed 单元），transcript 归属 s1。
    transcript = _committed_round_transcript(
        tmp, repo, session_id="s1", path="f.txt", old="base", new="changed"
    )
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "round work"], cwd=repo)
    sm.build_manifest(
        repo, transcript, tracker_run_id="r1", tracker_log_root=logs, committed_baseline=baseline
    )
    alloc = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
    )
    assert alloc["status"] == "allocated", alloc

    # review 前（assigned，未 reviewed）：不剔除。
    assert dt.select_committed_paths_reviewed_by_session(
        repo=repo, session_id="s1", candidate_paths={"f.txt"}, log_root_override=logs
    ) == set()

    # 完成 review（mark_reviewed 默认 True）→ f.txt review_state='reviewed'。
    dt.complete_review_scope(
        repo=repo, lease_id=alloc["lease_id"], scope_hash=alloc["scope_hash"], run_id="r1",
        log_root_override=logs,
    )
    after = _units_full_by_path(alloc["tracker_dir"], "f.txt")
    assert any(rev == "reviewed" for (_obs, rev) in after.values()), after
    assert all(obs == "committed" for (obs, _rev) in after.values()), after

    # 有记录（reviewed + 本 session 拥有）→ 剔除。durable：重复读幂等仍剔除。
    for _ in range(2):
        assert dt.select_committed_paths_reviewed_by_session(
            repo=repo, session_id="s1", candidate_paths={"f.txt"}, log_root_override=logs
        ) == {"f.txt"}
    # 他 session 视角：非本 session 拥有 → 不剔除（review_state 全局为真，但归属不匹配）。
    assert dt.select_committed_paths_reviewed_by_session(
        repo=repo, session_id="sess-OTHER", candidate_paths={"f.txt"}, log_root_override=logs
    ) == set()
    # session_id 缺失 → 空集（无法确认归属，保守不剔除）。
    assert dt.select_committed_paths_reviewed_by_session(
        repo=repo, session_id=None, candidate_paths={"f.txt"}, log_root_override=logs
    ) == set()


def test_select_committed_paths_reviewed_by_session_partial_reviewed_not_excluded(tmp: Path) -> None:
    """过滤器只在**全部** committed 单元 reviewed 时才剔除：同路径有未审 committed 单元残留则保留
    （偏「重审」安全方向）。这里用『review 后同路径再提交新 committed 改动』制造未审残留单元。"""
    dt, sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"

    transcript = _committed_round_transcript(
        tmp, repo, session_id="s1", path="f.txt", old="base", new="changed"
    )
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "round work 1"], cwd=repo)
    sm.build_manifest(
        repo, transcript, tracker_run_id="r1", tracker_log_root=logs, committed_baseline=baseline
    )
    alloc = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
    )
    dt.complete_review_scope(
        repo=repo, lease_id=alloc["lease_id"], scope_hash=alloc["scope_hash"], run_id="r1",
        log_root_override=logs,
    )
    # 同路径再提交一处新改动 → 引入一个新的 committed 单元（未审 available）。
    (repo / "f.txt").write_text("changed\nmore-unreviewed\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "round work 2 (unreviewed)"], cwd=repo)
    dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r2", reviewer_id="rev2",
        log_root_override=logs, auto_claim_observed=True, committed_baseline=baseline, dry_run=True,
    )
    states = {rev for (_obs, rev) in _units_full_by_path(alloc["tracker_dir"], "f.txt").values()}
    assert "reviewed" in states and states - {"reviewed"}, states  # 混合：有 reviewed 也有未审
    # 非全 reviewed → 不剔除。
    assert dt.select_committed_paths_reviewed_by_session(
        repo=repo, session_id="s1", candidate_paths={"f.txt"}, log_root_override=logs
    ) == set()


def test_candidate_summary_excludes_reviewed_committed_keeps_dirty(tmp: Path) -> None:
    """S6 联调：build_candidate_summary 经 review_state 过滤剔除已被本 session 审过的 committed
    路径，同时保留未审 dirty 改动；``lower_bound_source`` 口径切到 review_state 权威、窗口种子来源
    另置 ``committed_round_window_seed_source``（与 detection/allocation 同一 commit 原子切换）。"""
    summary_mod, _calc, _hw, dt = _load_candidate_summary_modules()
    _dt2, sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"

    # committed 本轮工作 f.txt（将被审），transcript 归属 s1。
    transcript = _committed_round_transcript(
        tmp, repo, session_id="s1", path="f.txt", old="base", new="changed"
    )
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "round work"], cwd=repo)
    sm.build_manifest(
        repo, transcript, tracker_run_id="r1", tracker_log_root=logs, committed_baseline=baseline
    )
    alloc = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=False, committed_baseline=baseline,
    )
    dt.complete_review_scope(
        repo=repo, lease_id=alloc["lease_id"], scope_hash=alloc["scope_hash"], run_id="r1",
        log_root_override=logs,
    )
    # 未审 dirty 改动 g.py（应保留）。
    (repo / "g.py").write_text("new\ncontent\n", encoding="utf-8")

    summ = summary_mod.build_candidate_summary(
        event={"cwd": str(repo)},
        cwd=str(repo),
        repo=str(repo),
        ledger=None,
        log_root_override=logs,
        workspace_guard=lambda **_k: None,
        task_id_resolver=lambda _e: None,
        session_id_resolver=lambda _e: "s1",
        committed_baseline_resolver=lambda _rp, _t, _s: baseline,
        committed_base_ref_resolver=lambda _rp: None,
    )
    by_path = {c["path"]: c for c in summ["candidates"]}
    # 已审 committed f.txt 被 review_state 过滤剔除（不在候选）。
    assert "f.txt" not in by_path, summ["candidates"]
    # 未审 dirty g.py 保留。
    assert "g.py" in by_path
    assert by_path["g.py"]["change_state"] == summary_mod.CHANGE_STATE_UNCOMMITTED
    assert summ["review_state_filtered_committed_path_count"] == 1
    assert summ["candidate_path_count"] == 1  # 仅 g.py
    assert summ["has_committed_but_unreviewed"] is False
    # lower_bound_source 口径=review_state 权威；窗口种子来源另行透明化（round-baseline 兜底或高水位）。
    assert summ["lower_bound_source"] == summary_mod.LOWER_BOUND_SOURCE_TRACKER_REVIEW_STATE
    assert summ["committed_round_window_seed_source"] in (
        summary_mod.COMMITTED_ROUND_WINDOW_SEED_REVIEW_HIGHWATER,
        summary_mod.COMMITTED_ROUND_WINDOW_SEED_ROUND_BASELINE,
    )


# ---------------------------------------------------------------------------
# 「背靠背再问一轮」与「规模数字虚高」修复：dirty 侧已审观测 + 未审行数估算 + start 语义
# ---------------------------------------------------------------------------

def _dirty_reviewed_repo(tmp: Path):
    """建一个 repo：``f.txt`` 已提交 base；会话 s1 把它改脏（**不提交**）并完整走过一轮 review。

    终态 = 一个 ``observed_state='dirty'`` 且 ``review_state='reviewed'`` 的单元，归属 s1。
    这正是「上一轮 RVF 刚审完、修复落盘还没提交」的现场——也就是背靠背再问一轮的那个现场。
    """
    dt, sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")  # dirty，刻意不 commit
    # 归属用 allocator 的 auto-claim 播种：工作树观测到的 dirty 单元直接记到 s1 名下。比走
    # transcript + build_manifest 更贴近本用例要断言的东西（tracker 里有一个 dirty+reviewed+owned
    # 的单元），也不必依赖 apply_patch hunk 锚点在 dirty 侧对得上。
    alloc = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=True,
    )
    assert alloc["status"] == "allocated", alloc
    dt.complete_review_scope(
        repo=repo, lease_id=alloc["lease_id"], scope_hash=alloc["scope_hash"], run_id="r1",
        log_root_override=logs,
    )
    return dt, sm, repo, logs, baseline, alloc


def _candidate_summary_for(summary_mod, repo: Path, logs: Path, **overrides):
    kwargs = dict(
        event={"cwd": str(repo)},
        cwd=str(repo),
        repo=str(repo),
        ledger=None,
        log_root_override=logs,
        workspace_guard=lambda **_k: None,
        task_id_resolver=lambda _e: None,
        session_id_resolver=lambda _e: "s1",
        committed_baseline_resolver=lambda _rp, _t, _s: None,
        committed_base_ref_resolver=lambda _rp: None,
    )
    kwargs.update(overrides)
    return summary_mod.build_candidate_summary(**kwargs)


def test_dirty_reviewed_lines_deducted_but_candidate_set_never_shrinks(tmp: Path) -> None:
    """背靠背场景的核心用例：上一轮审过的 dirty 行从**规模估算**里扣掉，但**候选集一个都不少**。

    这是本次修复的形状：横幅原先只报文件级全量 delta，一次「刚审完、修复没提交」的再征询看起来
    和一千多行全新工作一模一样。扣减只动数字，不动候选集——dirty 侧没有 commit 那种不可变锚点，
    按 tracker 剔除路径就是凭空开漏审面。"""
    summary_mod, _calc, _hw, _dt = _load_candidate_summary_modules()
    dt, _sm, repo, logs, _baseline, _alloc = _dirty_reviewed_repo(tmp)

    # ① tracker 侧只读观测：dirty 单元已 reviewed，且能还原出被审过的行文本。
    obs = dt.select_dirty_path_reviewed_change_by_session(
        repo=repo, session_id="s1", candidate_paths=["f.txt"], log_root_override=logs
    )
    assert "f.txt" in obs, obs
    assert obs["f.txt"].reviewed_unit_count >= 1, obs["f.txt"]
    assert "changed" in obs["f.txt"].reviewed_added_lines, obs["f.txt"]
    assert "base" in obs["f.txt"].reviewed_deleted_lines, obs["f.txt"]
    # 他 session / 无 session_id 视角一律拿不到已审证据（与 committed 侧同口径的保守方向）。
    assert dt.select_dirty_path_reviewed_change_by_session(
        repo=repo, session_id="sess-OTHER", candidate_paths=["f.txt"], log_root_override=logs
    ).get("f.txt").reviewed_unit_count == 0
    assert dt.select_dirty_path_reviewed_change_by_session(
        repo=repo, session_id=None, candidate_paths=["f.txt"], log_root_override=logs
    ) == {}

    # ② 候选摘要：候选集不缩小，但未审估算归零。
    summ = _candidate_summary_for(summary_mod, repo, logs)
    by_path = {c["path"]: c for c in summ["candidates"]}
    assert "f.txt" in by_path, summ["candidates"]
    assert summ["candidate_path_count"] == 1
    # 全量口径原样保留（下游既有断言不受影响）。
    assert by_path["f.txt"]["nonblank_added_lines"] == 1
    assert by_path["f.txt"]["nonblank_deleted_lines"] == 1
    assert summ["bootstrap_size"]["total_nonblank_added_lines"] == 1
    assert summ["bootstrap_size"]["total_nonblank_deleted_lines"] == 1
    # 未审口径：已审那一行被扣掉。
    assert by_path["f.txt"]["unreviewed_nonblank_added_lines"] == 0
    assert by_path["f.txt"]["unreviewed_nonblank_deleted_lines"] == 0
    est = summ["unreviewed_line_estimate"]
    assert est["nonblank_added_lines"] == 0 and est["nonblank_deleted_lines"] == 0, est
    assert est["estimate_basis"] == summary_mod.ESTIMATE_BASIS_HUNK_FILTERED, est
    assert est["hunk_filtered_path_count"] == 1 and est["fallback_path_count"] == 0, est
    assert summ["strictness_reference_unreviewed_estimate"]["totals"]["nonblank_added_lines"] == 0

    # ③ 横幅：把「已审 / 未审」两桶如实渲染出来，并对「未审为 0」给出 decline 建议。
    stop_hook = summary_mod._load_stop_hook_module()
    rendered = "\n".join(
        stop_hook._render_decision_point_unreviewed_estimate_lines(
            unreviewed_estimate=est,
            unreviewed_strictness=summ["strictness_reference_unreviewed_estimate"],
            bootstrap=summ["bootstrap_size"],
        )
    )
    assert "已在此前的 review 中审过" in rendered, rendered
    assert "未审量为 0" in rendered and "decline" in rendered, rendered


def test_dirty_unreviewed_estimate_falls_back_to_full_when_worktree_changed_after_review(
    tmp: Path,
) -> None:
    """review 之后工作树**又改过** → 新内容推导出新 unit_id，tracker 里查不到 → 整份按未审计。

    这是本估算唯一的失败方向，也是它敢做扣减的前提：身份是内容寻址的（canonical hash 不含 ``@@``
    头），对不上就是新工作。若这里退化成「还是按上一轮扣」，修复本身就成了漏审源。"""
    summary_mod, _calc, _hw, _dt = _load_candidate_summary_modules()
    dt, _sm, repo, logs, _baseline, _alloc = _dirty_reviewed_repo(tmp)

    # 审完之后又写了新工作（同一个文件，内容变了）。
    (repo / "f.txt").write_text("changed\nbrand-new-unreviewed-work\n", encoding="utf-8")

    obs = dt.select_dirty_path_reviewed_change_by_session(
        repo=repo, session_id="s1", candidate_paths=["f.txt"], log_root_override=logs
    )
    assert obs["f.txt"].reviewed_unit_count == 0, obs["f.txt"]

    summ = _candidate_summary_for(summary_mod, repo, logs)
    by_path = {c["path"]: c for c in summ["candidates"]}
    # 全量 = 未审：一行都没扣。
    assert by_path["f.txt"]["unreviewed_nonblank_added_lines"] == by_path["f.txt"]["nonblank_added_lines"]
    est = summ["unreviewed_line_estimate"]
    assert est["nonblank_added_lines"] == summ["bootstrap_size"]["total_nonblank_added_lines"] > 0, est
    assert est["estimate_basis"] == summary_mod.ESTIMATE_BASIS_NO_REVIEWED_UNITS, est


def test_dirty_unreviewed_estimate_reports_stale_when_reviewed_lines_do_not_match(tmp: Path) -> None:
    """tracker 声称审过某些行、却一行都对不上 → 报 ``stale``、整份回退全量，横幅改口说实话。

    自校验分支的专用用例（真实 git 状态下这一支很罕见，但它正是防「第二代假绿」的那道闸：一个
    失效的扣减绝不能渲染成一次成功的扣减）。"""
    summary_mod, _calc, _hw, _dt = _load_candidate_summary_modules()
    _dt2, _sm, _rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    (repo / "g.py").write_text("alpha\nbeta\n", encoding="utf-8")

    class _StaleObservation:
        whole_path_identity_reviewed = False
        reviewed_added_lines = ("a-line-that-is-not-in-the-diff",)
        reviewed_deleted_lines = ()

    summ = _candidate_summary_for(
        summary_mod, repo, logs,
        dirty_reviewed_change_resolver=lambda *_a: {"g.py": _StaleObservation()},
    )
    by_path = {c["path"]: c for c in summ["candidates"]}
    assert by_path["g.py"]["reviewed_line_estimate_basis"] == (
        summary_mod.ESTIMATE_BASIS_STALE_TRACKER_OBSERVATION
    )
    est = summ["unreviewed_line_estimate"]
    assert est["estimate_basis"] == summary_mod.ESTIMATE_BASIS_STALE_TRACKER_OBSERVATION, est
    assert est["nonblank_added_lines"] == summ["bootstrap_size"]["total_nonblank_added_lines"] == 2
    assert est["stale_observation_path_count"] == 1 and est["fallback_path_count"] == 1, est

    stop_hook = summary_mod._load_stop_hook_module()
    rendered = "\n".join(
        stop_hook._render_decision_point_unreviewed_estimate_lines(
            unreviewed_estimate=est,
            unreviewed_strictness=summ["strictness_reference_unreviewed_estimate"],
            bootstrap=summ["bootstrap_size"],
        )
    )
    # 诚实告警，而不是静默省略这一段（省略会被读成「没有已审的部分」）。
    assert "估算不可用" in rendered and "全量" in rendered, rendered


def test_dirty_unreviewed_estimate_treats_new_file_as_whole_path_identity(tmp: Path) -> None:
    """新增文件没有 hunk 锚点（``_canonical_hash_added`` 是整文件身份）→ 整条判定，不做行级折算。"""
    summary_mod, _calc, _hw, _dt = _load_candidate_summary_modules()
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"

    (repo / "n.py").write_text("one\ntwo\n", encoding="utf-8")  # untracked 新文件
    alloc = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=True,
    )
    assert alloc["status"] == "allocated", alloc
    dt.complete_review_scope(
        repo=repo, lease_id=alloc["lease_id"], scope_hash=alloc["scope_hash"], run_id="r1",
        log_root_override=logs,
    )

    obs = dt.select_dirty_path_reviewed_change_by_session(
        repo=repo, session_id="s1", candidate_paths=["n.py"], log_root_override=logs
    )
    assert obs["n.py"].unit_kind == "untracked_file", obs["n.py"]
    assert obs["n.py"].whole_path_identity_reviewed is True, obs["n.py"]
    assert obs["n.py"].reviewed_added_lines == (), obs["n.py"]  # 整条判定，不返回行

    summ = _candidate_summary_for(summary_mod, repo, logs)
    by_path = {c["path"]: c for c in summ["candidates"]}
    assert "n.py" in by_path  # 候选集仍不缩小
    assert by_path["n.py"]["nonblank_added_lines"] == 2
    assert by_path["n.py"]["unreviewed_nonblank_added_lines"] == 0
    assert by_path["n.py"]["reviewed_line_estimate_basis"] == (
        summary_mod.ESTIMATE_BASIS_WHOLE_PATH_REVIEWED
    )


def test_whole_path_reviewed_deduction_falls_back_when_delta_spans_unreviewed_committed(
    tmp: Path,
) -> None:
    """整路径已审扣减**只覆盖 dirty 一半**，被扣的却是 baseline→工作树的合并 delta → 必须整份回退。

    现场：同一路径上既有「本轮已提交但未审」的内容改动，又有「已审的 dirty chmod」（path_only 单元，
    没有 hunk 锚点 → ``whole_path_identity_reviewed=True``）。修复前 ``_estimate_unreviewed_lines_for_path``
    直接 ``return [], []``，把未审的已提交内容一并算成已审，横幅渲染出「⚠ 未审量为 0 …… 通常应当
    decline」——把没审过的已提交工作说成审过了，是实打实的漏审诱导。

    锁住的行为：未审量回到全量、basis 落在如实标注的回退取值、横幅改口说实话且不再建议 decline。"""
    summary_mod, _calc, _hw, _dt = _load_candidate_summary_modules()
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"

    # ① 本轮提交、且从未被审的内容改动。
    (repo / "g.py").write_text("committed-unreviewed-1\ncommitted-unreviewed-2\n", encoding="utf-8")
    run(["git", "add", "g.py"], cwd=repo)
    run(["git", "commit", "-q", "-m", "committed round work"], cwd=repo)
    head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()

    # ② 同一路径上的 dirty chmod：没有 hunk 锚点 → path_only 整路径身份单元。
    os.chmod(repo / "g.py", 0o755)
    alloc = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=True,
    )
    assert alloc["status"] == "allocated", alloc
    dt.complete_review_scope(
        repo=repo, lease_id=alloc["lease_id"], scope_hash=alloc["scope_hash"], run_id="r1",
        log_root_override=logs,
    )
    obs = dt.select_dirty_path_reviewed_change_by_session(
        repo=repo, session_id="s1", candidate_paths=["g.py"], log_root_override=logs
    )
    # 前提锚点：tracker 侧确实给出整路径已审观测（否则本用例会退化成空转的假绿）。
    assert obs["g.py"].unit_kind == "path_only", obs["g.py"]
    assert obs["g.py"].whole_path_identity_reviewed is True, obs["g.py"]

    summ = _candidate_summary_for(
        summary_mod, repo, logs,
        committed_baseline_resolver=lambda _rp, _t, _s: baseline,
    )
    by_path = {c["path"]: c for c in summ["candidates"]}
    assert by_path["g.py"]["change_state"] == summary_mod.CHANGE_STATE_COMMITTED_AND_UNCOMMITTED
    # 未审量 == 全量：一行都没扣（未审的已提交内容不再被冒充成已审）。
    assert by_path["g.py"]["nonblank_added_lines"] == 2
    assert by_path["g.py"]["unreviewed_nonblank_added_lines"] == 2
    assert by_path["g.py"]["reviewed_line_estimate_basis"] == (
        summary_mod.ESTIMATE_BASIS_WHOLE_PATH_REVIEWED_SPANS_UNREVIEWED_COMMITTED
    )
    est = summ["unreviewed_line_estimate"]
    assert est["nonblank_added_lines"] == 2, est
    assert est["estimate_basis"] == (
        summary_mod.ESTIMATE_BASIS_WHOLE_PATH_REVIEWED_SPANS_UNREVIEWED_COMMITTED
    ), est
    # 回退必须计进 fallback 桶、且不得计进「扣减确实生效」桶——否则失效的扣减看起来仍像成功的扣减。
    assert est["fallback_path_count"] == 1 and est["hunk_filtered_path_count"] == 0, est
    assert (
        summary_mod.ESTIMATE_BASIS_WHOLE_PATH_REVIEWED_SPANS_UNREVIEWED_COMMITTED
        not in summary_mod.ESTIMATE_BASES_WITH_EFFECTIVE_DEDUCTION
    )

    # ③ 横幅：如实说明估算不可用，且不再渲染那句诱导 decline 的「未审量为 0」。
    stop_hook = summary_mod._load_stop_hook_module()
    rendered = "\n".join(
        stop_hook._render_decision_point_unreviewed_estimate_lines(
            unreviewed_estimate=est,
            unreviewed_strictness=summ["strictness_reference_unreviewed_estimate"],
            bootstrap=summ["bootstrap_size"],
        )
    )
    assert "估算不可用" in rendered and "全量" in rendered, rendered
    assert "已提交但未审" in rendered, rendered
    assert "未审量为 0" not in rendered, rendered
    # 也不能反过来谎称「拿不到 tracker 观测」——观测是有的，只是覆盖面窄于被扣的 delta。
    assert "拿不到 tracker 观测" not in rendered, rendered

    # ④ 反向锚点：同一现场、下界取到 HEAD（本轮无已提交层）→ delta 就是纯 dirty，扣减照常生效。
    # 少了这一条，上面的修复可能是把所有整路径扣减一刀砍掉，而不是只砍跨层的那一支。
    summ_head_baseline = _candidate_summary_for(
        summary_mod, repo, logs,
        committed_baseline_resolver=lambda _rp, _t, _s: head,
    )
    by_path_head = {c["path"]: c for c in summ_head_baseline["candidates"]}
    assert by_path_head["g.py"]["change_state"] == summary_mod.CHANGE_STATE_UNCOMMITTED
    assert by_path_head["g.py"]["reviewed_line_estimate_basis"] == (
        summary_mod.ESTIMATE_BASIS_WHOLE_PATH_REVIEWED
    )


def test_dirty_reviewed_observation_only_pays_git_for_paths_with_tracker_reviewed_units(
    tmp: Path,
) -> None:
    """性能回归锁：dirty 已审观测只对「tracker 里真有本 session 已审单元」的候选路径付 git 代价。

    这条观测在**每一次 Stop** 的轻量候选摘要里都跑，而 ``_classify_path`` 每条路径要起
    ``git status`` / ``git diff --numstat`` / ``git diff -U3`` 等多个子进程。逐候选观测会让
    ``stop_decision_candidate_summary`` 的「轻量」铁律随候选数线性破产（120 个 dirty 文件实测
    ~7s 墙钟、其中不到 1s 是 CPU，其余全是子进程开销）。

    断言用 ``_classify_path`` 的**调用集合**而不是墙钟：墙钟在负载下必 flake，调用集合是结构性的
    —— 一旦有人改回逐候选观测，它立刻从 1 条涨到 12 条即红。

    同时锁住这次提速**不许**换来的语义损失：返回值里一条候选路径都不能少（候选集永不因 tracker
    数据缩小），且扣不掉的一律算未审。
    """
    summary_mod, _calc, _hw, _dt = _load_candidate_summary_modules()
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"

    # 11 条「本 session 从没审过」的陪跑路径，先随 base 一起提交（此刻全是干净的）。
    noise_paths = [f"noise_{i:02d}.txt" for i in range(11)]
    for noise in noise_paths:
        (repo / noise).write_text("base\n", encoding="utf-8")
    run(["git", "add", *noise_paths], cwd=repo)
    run(["git", "commit", "-q", "-m", "noise base"], cwd=repo)

    # 只有 f.txt 被改脏并完整走过一轮 review → 它是唯一一条 tracker 里有 dirty+reviewed+owned 单元的路径。
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")
    alloc = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=True,
    )
    assert alloc["status"] == "allocated", alloc
    dt.complete_review_scope(
        repo=repo, lease_id=alloc["lease_id"], scope_hash=alloc["scope_hash"], run_id="r1",
        log_root_override=logs,
    )

    # review 之后陪跑路径才变脏 —— 它们在 tracker 里连单元都没有，更谈不上已审。
    for noise in noise_paths:
        (repo / noise).write_text("brand-new-unreviewed-work\n", encoding="utf-8")

    candidate_paths = ["f.txt", *noise_paths]
    classified_paths: list[str] = []
    original_classify_path = dt._classify_path

    def _counting_classify_path(repo_arg, path_arg):
        classified_paths.append(path_arg)
        return original_classify_path(repo_arg, path_arg)

    dt._classify_path = _counting_classify_path
    try:
        obs = dt.select_dirty_path_reviewed_change_by_session(
            repo=repo, session_id="s1", candidate_paths=candidate_paths, log_root_override=logs
        )
    finally:
        dt._classify_path = original_classify_path

    # ① 性能形状：只对唯一那条有已审单元的路径起 git 子进程。
    assert classified_paths == ["f.txt"], classified_paths

    # ② 语义不变：12 条候选一条不少，未审的照旧是未审。
    assert set(obs) == set(candidate_paths), sorted(obs)
    assert obs["f.txt"].reviewed_unit_count >= 1, obs["f.txt"]
    assert "changed" in obs["f.txt"].reviewed_added_lines, obs["f.txt"]
    for noise in noise_paths:
        skipped = obs[noise]
        assert skipped.reviewed_unit_count == 0, skipped
        assert skipped.whole_path_identity_reviewed is False, skipped
        assert skipped.reviewed_added_lines == () and skipped.reviewed_deleted_lines == (), skipped
        # 跳过观测用独立 unit_kind 自陈，而不是伪装成一次真观测。
        assert skipped.unit_kind == (
            dt.DIRTY_REVIEWED_OBSERVATION_UNIT_KIND_SKIPPED_NO_REVIEWED_UNITS_IN_TRACKER
        ), skipped

    # ③ 下游摘要：候选集完整，只有已审那条被扣减，陪跑路径全额计入未审。
    summ = _candidate_summary_for(summary_mod, repo, logs)
    by_path = {c["path"]: c for c in summ["candidates"]}
    assert set(by_path) == set(candidate_paths), sorted(by_path)
    assert by_path["f.txt"]["unreviewed_nonblank_added_lines"] == 0
    assert by_path["f.txt"]["reviewed_line_estimate_basis"] == summary_mod.ESTIMATE_BASIS_HUNK_FILTERED
    for noise in noise_paths:
        assert by_path[noise]["unreviewed_nonblank_added_lines"] == (
            by_path[noise]["nonblank_added_lines"]
        ) > 0, by_path[noise]
        assert by_path[noise]["reviewed_line_estimate_basis"] == (
            summary_mod.ESTIMATE_BASIS_NO_REVIEWED_UNITS
        ), by_path[noise]


def _agentic_initiation_allocation_for(tmp: Path, repo: Path, logs: Path, transcript: Path, **kwargs):
    """在隔离 log root 下调一次发起侧 tracker scope 分配（真实 allocator，不打桩）。"""
    cli = load_rvf_agent_decision_cli_module()
    original = os.environ.get("CODEX_RVF_LOG_ROOT")
    os.environ["CODEX_RVF_LOG_ROOT"] = str(logs)
    try:
        return cli, cli.allocate_agentic_initiation_tracker_review_scope(
            repo=str(repo),
            session_id="s1",
            transcript_path=str(transcript),
            run_id=kwargs.pop("run_id", "r-alloc"),
            output_scope_path=tmp / "tracker-scope.json",
            **kwargs,
        )
    finally:
        if original is None:
            os.environ.pop("CODEX_RVF_LOG_ROOT", None)
        else:
            os.environ["CODEX_RVF_LOG_ROOT"] = original


def test_start_reports_no_unreviewed_scope_instead_of_silently_rescoping_everything(
    tmp: Path,
) -> None:
    """全已审的一批 diff 再 start → 报 ``no_unreviewed_scope``，**不**静默扩成无 lease 的全量重审。

    这一条掐断的是背靠背闭环的发动机：旧行为下 allocator 报 empty 就降级成 manual-all-uncommitted，
    而无 lease 的一轮审完标不了 reviewed，同一批 diff 于是在之后每一次 Stop 又被提议一遍。"""
    dt, _sm, repo, logs, _baseline, _alloc = _dirty_reviewed_repo(tmp)
    transcript = _committed_round_transcript(
        tmp, repo, session_id="s1", path="f.txt", old="base", new="changed"
    )

    cli, allocation = _agentic_initiation_allocation_for(tmp, repo, logs, transcript)
    assert allocation["status"] == cli.AGENTIC_INITIATION_TRACKER_SCOPE_NO_UNREVIEWED_SCOPE, allocation
    assert allocation["tracker_lease_id"] is None, allocation  # 无 lease 泄漏
    assert allocation["tracker_scope_path"] is None, allocation
    assert allocation["allocator_status"] == "empty", allocation
    assert "已全部审过" in (allocation["degradation_reason"] or ""), allocation

    # 显式 --force-full-rescope → 回到历史降级口径（知情后重审的出口仍在）。
    _cli2, forced = _agentic_initiation_allocation_for(
        tmp, repo, logs, transcript, run_id="r-alloc-forced", force_full_rescope=True
    )
    assert forced["status"] == cli.AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_ALLOCATOR_EMPTY, forced
    assert forced["session_manifest_has_owned_paths"] is True, forced
    assert "--force-full-rescope" in (forced["degradation_reason"] or ""), forced


def test_start_keeps_legacy_degradation_when_units_are_held_by_an_in_flight_lease(
    tmp: Path,
) -> None:
    """empty 的另一半成因（被在途 lease 持有）必须保持旧的「多审一遍」降级——并发下多审是安全的。

    与上一条成对：修复只拆掉「全已审」那一支，绝不能顺手把并发路径也改掉。"""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"
    transcript = _committed_round_transcript(
        tmp, repo, session_id="s1", path="f.txt", old="base", new="changed"
    )
    (repo / "f.txt").write_text("changed\n", encoding="utf-8")
    held = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=True,
    )
    assert held["status"] == "allocated", held  # lease 在途，**不** complete

    cli, allocation = _agentic_initiation_allocation_for(tmp, repo, logs, transcript)
    assert allocation["status"] == cli.AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_ALLOCATOR_EMPTY, allocation
    assert allocation["session_manifest_has_owned_paths"] is True, allocation
    # 成因判据看的是 review_state 分布，不是 allocator 的 leased_excluded_count（那个恒为 0，
    # 因为 assigned 单元在候选查询阶段就被滤掉、走不到 anti-join）。
    assert "assigned=1" in (allocation["degradation_reason"] or ""), allocation
    assert "已全部审过" not in (allocation["degradation_reason"] or ""), allocation


def test_start_keeps_legacy_degradation_when_session_owns_reviewed_path_only_units(
    tmp: Path,
) -> None:
    """``path_only`` 已审单元在场 → **不**走「全已审」早退，保守回到旧的全量多审降级。

    ``_canonical_hash_path_only`` 只哈希路径（不含 mode、不含 change 方向），所以 ``path_only``
    的 ``reviewed`` 只证明「这个路径被审过一次」，证明不了「当前这次改动被审过」。本用例走一遍
    chmod 往返来钉死这一点：``100644→100755`` 审掉并提交后，相反方向的 ``100755→100644`` 复用
    同一个 unit_id、allocator 照样报 empty、计数照样是 ``reviewed=1``——早退分支若只看 review_state
    分布，就会把一次真的没审过的可执行位回退静默跳掉。多审一遍只是浪费，跳过是漏审。"""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, _baseline = _committed_round_repo(tmp)
    logs = tmp / "logs"

    # ① 第一次 mode 改动（100644 → 100755）：只有 mode 变，无内容 diff ⇒ tracker 兜底成 path_only。
    os.chmod(repo / "f.txt", 0o755)
    alloc = dt.allocate_review_scope(
        repo=repo, session_id="s1", run_id="r1", reviewer_id="rev1",
        log_root_override=logs, auto_claim_observed=True,
    )
    assert alloc["status"] == "allocated", alloc
    tracker_dir = alloc["tracker_dir"]
    first_units = _units_full_by_path(tracker_dir, "f.txt")
    assert len(first_units) == 1, first_units
    dt.complete_review_scope(
        repo=repo, lease_id=alloc["lease_id"], scope_hash=alloc["scope_hash"], run_id="r1",
        log_root_override=logs,
    )
    run(["git", "add", "-A"], cwd=repo)
    run(["git", "commit", "-q", "-m", "chmod +x"], cwd=repo)

    # ② 相反方向的 mode 改动（100755 → 100644）：这是**没审过**的新工作。
    os.chmod(repo / "f.txt", 0o644)

    counts = dt.summarize_session_unit_review_states(
        repo=repo, session_id="s1", log_root_override=logs
    )
    # 身份塌缩如实呈现：没有新单元，reviewed 那一行就是上一次 chmod 留下的 path_only。
    assert _units_full_by_path(tracker_dir, "f.txt").keys() == first_units.keys()
    assert counts["available"] == 0 and counts["assigned"] == 0, counts
    assert counts["reviewed"] == 1, counts
    assert counts["path_only_reviewed"] == 1, counts

    transcript = _committed_round_transcript(
        tmp, repo, session_id="s1", path="f.txt", old="base", new="base"
    )
    cli, allocation = _agentic_initiation_allocation_for(tmp, repo, logs, transcript)
    # 关键断言：**不是** no_unreviewed_scope（那会让这次 mode 回退永远不被审）。
    assert allocation["status"] != cli.AGENTIC_INITIATION_TRACKER_SCOPE_NO_UNREVIEWED_SCOPE, allocation
    assert allocation["status"] == cli.AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_ALLOCATOR_EMPTY, allocation
    assert allocation["allocator_status"] == "empty", allocation
    assert allocation["session_manifest_has_owned_paths"] is True, allocation
    # 降级理由必须点名 path_only，否则读者只看到 reviewed=1/available=0 会以为早退分支坏了。
    assert "path_only" in (allocation["degradation_reason"] or ""), allocation
    assert "已全部审过" not in (allocation["degradation_reason"] or ""), allocation


def test_run_start_no_unreviewed_scope_holds_token_acknowledged_and_offers_force_flag(
    tmp: Path,
) -> None:
    """``run_start`` 拿到「无未审单元」→ 专用回执（非 start_failed），token 停在 acknowledged。

    刻意不复用 ``_fail``：那会把一个正常结论渲染成故障，并诱导 agent 去 ``--retry`` 一个重试多少
    次都一样的状态。回执必须同时给出 decline 与 ``--force-full-rescope`` 两条出路。"""
    cli = load_rvf_agent_decision_cli_module()
    dts = load_rvf_decision_token_store_module()
    roots = _agent_cli_roots(cli, tmp)
    t0 = _fixed_utc(0)
    wt = _agent_cli_worktree(tmp)
    tok = _mint_agent_decision_token(dts, roots, worktree=wt, now=t0)

    def _pipeline_no_unreviewed(**_kwargs):
        return {
            "status": cli.PREPARE_STATUS_NO_UNREVIEWED_SCOPE,
            "run_id": "run-nus", "run_dir": str(tmp / "runs" / "run-nus"),
            "artifacts": {}, "error": None, "prep_token": None, "bootstrap_gate": None,
            "initiation_run_id": "run-nus", "initiation_run_dir": str(tmp / "runs" / "run-nus"),
            "tracker_scope_allocation": {
                "status": cli.AGENTIC_INITIATION_TRACKER_SCOPE_NO_UNREVIEWED_SCOPE,
                "tracker_scope_path": None, "tracker_lease_id": None, "tracker_scope_hash": None,
                "tracker_unit_count": 0, "session_manifest_has_owned_paths": True,
                "degradation_reason": "tracker 报本会话名下**没有任何未审单元**", "allocator_status": "empty",
                "error": None,
            },
        }

    result = cli.run_start(
        token=tok, reason="开审", roots=roots, now=t0,
        prepare_pipeline=_pipeline_no_unreviewed,
    )
    assert result.status == "no_unreviewed_scope", result
    assert result.exit_code == cli.EXIT_NO_UNREVIEWED_SCOPE, result
    assert result.token_state == dts.STATE_ACKNOWLEDGED, result
    assert "已全部审过" in result.message, result.message
    steps = "\n".join(result.next_steps or [])
    assert "decline" in steps and "--force-full-rescope" in steps, steps
    # token 仍是非终态：decline 这条出路必须真的还走得通。
    declined = cli.run_decline(token=tok, decline_kind="structural_not_needed", reason="上一轮 RVF 产物，无未审单元", roots=roots, now=t0)
    assert declined.exit_code == cli.EXIT_OK, declined


def review_support_test_cases(root: Path) -> list[tuple[str, object]]:
    return [
        # --- 修复包 F4：谢绝语义与重新征询 ---
        (
            "decline_kind_is_required_and_unknown_kind_is_rejected_without_touching_the_token",
            lambda: test_decline_kind_is_required_and_unknown_kind_is_rejected_without_touching_the_token(root / "f4-0"),
        ),
        (
            "deferred_until_condition_decline_writes_neither_fingerprint_cooldown_nor_session_scope_snooze",
            lambda: test_deferred_until_condition_decline_writes_neither_fingerprint_cooldown_nor_session_scope_snooze(root / "f4-1"),
        ),
        (
            "deferred_until_condition_decline_is_reasked_only_after_new_turn_head_change_or_content_change",
            lambda: test_deferred_until_condition_decline_is_reasked_only_after_new_turn_head_change_or_content_change(root / "f4-2"),
        ),
        (
            "structural_decline_supersedes_earlier_deferred_until_condition_decline",
            lambda: test_structural_decline_supersedes_earlier_deferred_until_condition_decline(root / "f4-3"),
        ),
        (
            "explicit_user_request_overrides_deferred_until_condition_decline_and_records_provenance",
            lambda: test_explicit_user_request_overrides_deferred_until_condition_decline_and_records_provenance(root / "f4-4"),
        ),
        (
            "circuit_breaker_refused_mint_requests_do_not_keep_the_breaker_tripped",
            lambda: test_circuit_breaker_refused_mint_requests_do_not_keep_the_breaker_tripped(root / "f4-5"),
        ),
        (
            "extra_token_payload_fields_cannot_override_system_written_explicit_user_request_fields",
            lambda: test_extra_token_payload_fields_cannot_override_system_written_explicit_user_request_fields(root / "f4-10"),
        ),
        (
            "kanban_task_committed_round_baseline_bootstraps_from_branch_point_when_never_reviewed",
            lambda: test_kanban_task_committed_round_baseline_bootstraps_from_branch_point_when_never_reviewed(root / "f4-8"),
        ),
        (
            "retrospective_deferred_declines_are_reasked_while_structural_declines_stay_snoozed",
            lambda: test_retrospective_deferred_declines_are_reasked_while_structural_declines_stay_snoozed(root / "f4-9"),
        ),
        # --- S2：候选摘要 + 严格度参考档 ---
        (
            "review_strictness_reference_weights_and_classification",
            lambda: test_review_strictness_reference_weights_and_classification(
                root / "strictness-weights"
            ),
        ),
        (
            "review_strictness_reference_oversized_elevate_and_tiers",
            lambda: test_review_strictness_reference_oversized_elevate_and_tiers(
                root / "strictness-elevate"
            ),
        ),
        (
            "review_strictness_presets_schema",
            lambda: test_review_strictness_presets_schema(root / "strictness-presets"),
        ),
        (
            "candidate_summary_includes_committed_but_unreviewed",
            lambda: test_candidate_summary_includes_committed_but_unreviewed(
                root / "candidate-committed"
            ),
        ),
        (
            "candidate_summary_fingerprint_stable_and_change_sensitive",
            lambda: test_candidate_summary_fingerprint_stable_and_change_sensitive(
                root / "candidate-fingerprint"
            ),
        ),
        (
            "candidate_summary_scope_fingerprint_ignores_content_but_tracks_paths_and_head",
            lambda: test_candidate_summary_scope_fingerprint_ignores_content_but_tracks_paths_and_head(
                root / "candidate-scope-fingerprint"
            ),
        ),
        (
            "candidate_summary_ownership_filters_other_session",
            lambda: test_candidate_summary_ownership_filters_other_session(
                root / "candidate-ownership"
            ),
        ),
        (
            "candidate_summary_excludes_serena_generated_untracked_files",
            lambda: test_candidate_summary_excludes_serena_generated_untracked_files(
                root / "candidate-serena-untracked"
            ),
        ),
        (
            "candidate_summary_excludes_tracked_serena_rewrite_without_touching_real_work_fingerprint",
            lambda: test_candidate_summary_excludes_tracked_serena_rewrite_without_touching_real_work_fingerprint(
                root / "candidate-serena-tracked-rewrite"
            ),
        ),
        (
            "candidate_summary_honors_repo_local_ignore_file_and_keeps_exclusion_counts_separate",
            lambda: test_candidate_summary_honors_repo_local_ignore_file_and_keeps_exclusion_counts_separate(
                root / "candidate-repo-local-ignore-file"
            ),
        ),
        (
            "candidate_summary_workspace_guard_short_circuits",
            lambda: test_candidate_summary_workspace_guard_short_circuits(
                root / "candidate-workspace-guard"
            ),
        ),
        (
            "candidate_fingerprint_serialization_format",
            lambda: test_candidate_fingerprint_serialization_format(
                root / "candidate-fp-format"
            ),
        ),
        # --- S6：R9 真相源迁移（SHA marker → tracker review_state；方案 A）---
        (
            "select_committed_paths_reviewed_by_session_filters_reviewed_owned",
            lambda: test_select_committed_paths_reviewed_by_session_filters_reviewed_owned(
                root / "s6-review-state-filter-reviewed-owned"
            ),
        ),
        (
            "select_committed_paths_reviewed_by_session_partial_reviewed_not_excluded",
            lambda: test_select_committed_paths_reviewed_by_session_partial_reviewed_not_excluded(
                root / "s6-review-state-filter-partial"
            ),
        ),
        (
            "candidate_summary_excludes_reviewed_committed_keeps_dirty",
            lambda: test_candidate_summary_excludes_reviewed_committed_keeps_dirty(
                root / "s6-summary-excludes-reviewed-committed"
            ),
        ),
        # --- 背靠背再问一轮 / 规模数字虚高：dirty 已审观测 + 未审估算 + start 语义 ---
        (
            "dirty_reviewed_lines_deducted_but_candidate_set_never_shrinks",
            lambda: test_dirty_reviewed_lines_deducted_but_candidate_set_never_shrinks(
                root / "dirty-reviewed-deducted"
            ),
        ),
        (
            "dirty_unreviewed_estimate_falls_back_to_full_when_worktree_changed_after_review",
            lambda: test_dirty_unreviewed_estimate_falls_back_to_full_when_worktree_changed_after_review(
                root / "dirty-unreviewed-worktree-changed"
            ),
        ),
        (
            "dirty_unreviewed_estimate_reports_stale_when_reviewed_lines_do_not_match",
            lambda: test_dirty_unreviewed_estimate_reports_stale_when_reviewed_lines_do_not_match(
                root / "dirty-unreviewed-stale"
            ),
        ),
        (
            "dirty_unreviewed_estimate_treats_new_file_as_whole_path_identity",
            lambda: test_dirty_unreviewed_estimate_treats_new_file_as_whole_path_identity(
                root / "dirty-unreviewed-new-file"
            ),
        ),
        (
            "whole_path_reviewed_deduction_falls_back_when_delta_spans_unreviewed_committed",
            lambda: test_whole_path_reviewed_deduction_falls_back_when_delta_spans_unreviewed_committed(
                root / "whole-path-reviewed-spans-committed"
            ),
        ),
        (
            "dirty_reviewed_observation_only_pays_git_for_paths_with_tracker_reviewed_units",
            lambda: test_dirty_reviewed_observation_only_pays_git_for_paths_with_tracker_reviewed_units(
                root / "dirty-reviewed-observation-narrowed-git"
            ),
        ),
        (
            "start_reports_no_unreviewed_scope_instead_of_silently_rescoping_everything",
            lambda: test_start_reports_no_unreviewed_scope_instead_of_silently_rescoping_everything(
                root / "start-no-unreviewed-scope"
            ),
        ),
        (
            "start_keeps_legacy_degradation_when_units_are_held_by_an_in_flight_lease",
            lambda: test_start_keeps_legacy_degradation_when_units_are_held_by_an_in_flight_lease(
                root / "start-leased-excluded-degradation"
            ),
        ),
        (
            "start_keeps_legacy_degradation_when_session_owns_reviewed_path_only_units",
            lambda: test_start_keeps_legacy_degradation_when_session_owns_reviewed_path_only_units(
                root / "start-path-only-chmod-roundtrip"
            ),
        ),
        (
            "run_start_no_unreviewed_scope_holds_token_acknowledged_and_offers_force_flag",
            lambda: test_run_start_no_unreviewed_scope_holds_token_acknowledged_and_offers_force_flag(
                root / "run-start-no-unreviewed-scope"
            ),
        ),
        (
            "dispatch_reviewers_routing_matrix",
            lambda: test_dispatch_reviewers_routing_matrix(root / "dispatch-routing"),
        ),
        (
            "dispatch_reviewers_pseudo_random_selection_seed_reproducible",
            lambda: test_dispatch_reviewers_pseudo_random_selection_seed_reproducible(
                root / "dispatch-selection-seed"
            ),
        ),
        (
            "temporary_disable_window_generalized_component",
            lambda: test_temporary_disable_window_generalized_component(
                root / "temporary-disable-window"
            ),
        ),
        (
            "guardian_analyze_completion_milestones_and_retirement",
            lambda: test_guardian_analyze_completion_milestones_and_retirement(
                root / "guardian-milestones"
            ),
        ),
        (
            "guardian_handoff_doc_resolution_works_in_repo_and_degrades_when_absent",
            lambda: test_guardian_handoff_doc_resolution_works_in_repo_and_degrades_when_absent(
                root / "guardian-handoff-resolution"
            ),
        ),
        (
            "guardian_failed_milestones_retry_and_retire_only_after_success",
            lambda: test_guardian_failed_milestones_retry_and_retire_only_after_success(
                root / "guardian-milestone-retry"
            ),
        ),
        (
            "guardian_counter_read_modify_write_is_serialized",
            lambda: test_guardian_counter_read_modify_write_is_serialized(
                root / "guardian-counter-lock"
            ),
        ),
        (
            "guardian_initiation_liveness_alarm_paths",
            lambda: test_guardian_initiation_liveness_alarm_paths(
                root / "guardian-liveness"
            ),
        ),
        (
            "guardian_liveness_utc_day_bucket_boundary",
            lambda: test_guardian_liveness_utc_day_bucket_boundary(
                root / "guardian-liveness-bucket-boundary"
            ),
        ),
        (
            "detached_wrapper_post_exit_argv_appended_with_failsafe",
            lambda: test_detached_wrapper_post_exit_argv_appended_with_failsafe(
                root / "detached-post-exit"
            ),
        ),
        (
            "detached_wrapper_executes_real_guardian_cli_post_exit",
            lambda: test_detached_wrapper_executes_real_guardian_cli_post_exit(
                root / "detached-real-guardian-cli"
            ),
        ),
        (
            "rvf_analyze_thread_wires_guardian_post_exit_cli_signature",
            lambda: test_rvf_analyze_thread_wires_guardian_post_exit_cli_signature(
                root / "analyze-guardian-post-exit-wire"
            ),
        ),
        (
            "dispatch_reviewers_temporarily_disables_claude_code_external_reviewer",
            lambda: test_dispatch_reviewers_temporarily_disables_claude_code_external_reviewer(
                root / "dispatch-disable-claude-reviewer"
            ),
        ),
        (
            "dispatch_reviewers_same_harness_double_instance_distinct_ids",
            lambda: test_dispatch_reviewers_same_harness_double_instance_distinct_ids(
                root / "dispatch-double-instance"
            ),
        ),
        (
            "dispatch_reviewers_plan_artifact_schema",
            lambda: test_dispatch_reviewers_plan_artifact_schema(root / "dispatch-plan-schema"),
        ),
        (
            "dispatch_reviewers_executes_two_external",
            lambda: test_dispatch_reviewers_executes_two_external(root / "dispatch-execute"),
        ),
        (
            "dispatch_reviewers_execute_backfills_review_env",
            lambda: test_dispatch_reviewers_execute_backfills_review_env(root / "dispatch-env-backfill"),
        ),
        (
            "run_alternative_reviewer_usage_limit_codex_json_error",
            lambda: test_run_alternative_reviewer_usage_limit_codex_json_error(
                root / "usage-limit-codex-json"
            ),
        ),
        (
            "run_alternative_reviewer_usage_limit_stderr_text",
            lambda: test_run_alternative_reviewer_usage_limit_stderr_text(
                root / "usage-limit-stderr-text"
            ),
        ),
        (
            "run_alternative_reviewer_usage_limit_no_false_positive_success",
            lambda: test_run_alternative_reviewer_usage_limit_no_false_positive_success(
                root / "usage-limit-fp-success"
            ),
        ),
        (
            "run_alternative_reviewer_usage_limit_no_false_positive_invalid_result",
            lambda: test_run_alternative_reviewer_usage_limit_no_false_positive_invalid_result(
                root / "usage-limit-fp-invalid"
            ),
        ),
        (
            "harness_limit_cooldown_record_active_sweep",
            lambda: test_harness_limit_cooldown_record_active_sweep(root / "cooldown-unit"),
        ),
        (
            "dispatch_reviewers_reroutes_on_usage_limit",
            lambda: test_dispatch_reviewers_reroutes_on_usage_limit(root / "dispatch-reroute"),
        ),
        (
            "dispatch_reviewers_reroute_id_collision",
            lambda: test_dispatch_reviewers_reroute_id_collision(root / "dispatch-reroute-collision"),
        ),
        (
            "dispatch_reviewers_probe_excludes_cooldown",
            lambda: test_dispatch_reviewers_probe_excludes_cooldown(root / "dispatch-probe-cooldown"),
        ),
        (
            "dispatch_reviewers_failclose_when_main_exhausted",
            lambda: test_dispatch_reviewers_failclose_when_main_exhausted(
                root / "dispatch-failclose-main"
            ),
        ),
        (
            "dispatch_reviewers_reroutes_on_leg_without_review_result",
            lambda: test_dispatch_reviewers_reroutes_on_leg_without_review_result(
                root / "dispatch-reroute-no-result"
            ),
        ),
        (
            "dispatch_reviewers_failclose_when_leg_without_review_result_unfilled",
            lambda: test_dispatch_reviewers_failclose_when_leg_without_review_result_unfilled(
                root / "dispatch-failclose-no-result"
            ),
        ),
        (
            "dispatch_reviewers_rc_zero_without_review_result_is_not_effective",
            lambda: test_dispatch_reviewers_rc_zero_without_review_result_is_not_effective(
                root / "dispatch-rc0-no-result"
            ),
        ),
        (
            "dispatch_reviewers_review_request_leg_is_pending_not_rerouted",
            lambda: test_dispatch_reviewers_review_request_leg_is_pending_not_rerouted(
                root / "dispatch-review-request-pending"
            ),
        ),
        (
            "dispatch_reviewers_usage_limit_with_pending_request_is_not_all_usage_limit",
            lambda: test_dispatch_reviewers_usage_limit_with_pending_request_is_not_all_usage_limit(
                root / "dispatch-usage-plus-request"
            ),
        ),
        (
            "dispatch_reviewers_blocked_on_reviewer_request_exit_code_frees_detached_lock",
            lambda: test_dispatch_reviewers_blocked_on_reviewer_request_exit_code_frees_detached_lock(
                root / "dispatch-blocked-request-exit-code"
            ),
        ),
        (
            "dispatch_reviewers_failclose_mixed_usage_limit_and_auth_failure",
            lambda: test_dispatch_reviewers_failclose_mixed_usage_limit_and_auth_failure(
                root / "dispatch-failclose-mixed"
            ),
        ),
        (
            "dispatch_reviewers_wait_status_surfaces_plan_outcome",
            lambda: test_dispatch_reviewers_wait_status_surfaces_plan_outcome(
                root / "dispatch-wait-plan-outcome"
            ),
        ),
        (
            "diff_tracker_observes_committed_round_units",
            lambda: test_diff_tracker_observes_committed_round_units(root / "committed-observe"),
        ),
        (
            "committed_round_packet_diff_uses_baseline",
            lambda: test_committed_round_packet_diff_uses_baseline(root / "committed-packet-baseline"),
        ),
        (
            "committed_round_packet_baseline_absent_is_head_equivalent",
            lambda: test_committed_round_packet_baseline_absent_is_head_equivalent(
                root / "committed-packet-head-equiv"
            ),
        ),
        (
            "committed_round_packet_captures_committed_and_dirty",
            lambda: test_committed_round_packet_captures_committed_and_dirty(
                root / "committed-packet-mixed"
            ),
        ),
        (
            "committed_round_packet_base_ref_falls_back_when_not_ancestor",
            lambda: test_committed_round_packet_base_ref_falls_back_when_not_ancestor(
                root / "committed-packet-guard"
            ),
        ),
        (
            "committed_round_manifest_to_packet_reviews_committed_work",
            lambda: test_committed_round_manifest_to_packet_reviews_committed_work(
                root / "committed-manifest-to-packet"
            ),
        ),
        (
            "committed_round_tracker_scope_renders_path_level_when_baseline",
            lambda: test_committed_round_tracker_scope_renders_path_level_when_baseline(
                root / "committed-tracker-scope-pathlevel"
            ),
        ),
        (
            "committed_round_base_ref_drives_manifest_attribution",
            lambda: test_committed_round_base_ref_drives_manifest_attribution(
                root / "committed-base-ref-attribution"
            ),
        ),
        (
            "committed_unit_dedup_reviewed_not_resurrected",
            lambda: test_committed_unit_dedup_reviewed_not_resurrected(root / "committed-dedup"),
        ),
        (
            "added_file_unit_identity_stable_dirty_staged_committed",
            lambda: test_added_file_unit_identity_stable_dirty_staged_committed(root / "added-identity"),
        ),
        (
            "empty_added_file_unit_identity_stable",
            lambda: test_empty_added_file_unit_identity_stable(root / "empty-added-identity"),
        ),
        (
            "committed_added_file_dedup_reviewed_not_redispatched",
            lambda: test_committed_added_file_dedup_reviewed_not_redispatched(root / "added-dedup"),
        ),
        (
            "complete_review_scope_mark_reviewed_gate",
            lambda: test_complete_review_scope_mark_reviewed_gate(root / "mark-reviewed-gate"),
        ),
        (
            "release_tracker_lease_noop_completion_does_not_mark_reviewed",
            lambda: test_release_tracker_lease_noop_completion_does_not_mark_reviewed(root / "noop-finalize"),
        ),
        (
            "seal_round_baseline_to_head_advances_marker",
            lambda: test_seal_round_baseline_to_head_advances_marker(root / "seal-marker"),
        ),
        (
            "review_highwater_marker_round_trip",
            lambda: test_review_highwater_marker_round_trip(root / "highwater-marker"),
        ),
        (
            "seal_advances_review_highwater",
            lambda: test_seal_advances_review_highwater(root / "seal-highwater"),
        ),
        (
            "advance_review_highwater_only_on_did_review",
            lambda: test_advance_review_highwater_only_on_did_review(root / "finalize-highwater"),
        ),
        (
            "review_highwater_write_is_monotonic_with_repo",
            lambda: test_review_highwater_write_is_monotonic_with_repo(root / "highwater-monotonic"),
        ),
        (
            "parent_harness_identity_normalizes_every_signal_layer_to_one_token",
            lambda: test_parent_harness_identity_normalizes_every_signal_layer_to_one_token(
                root / "harness-identity-layers"
            ),
        ),
        (
            "round_baseline_marker_is_not_shared_across_parent_harnesses",
            lambda: test_round_baseline_marker_is_not_shared_across_parent_harnesses(
                root / "round-baseline-per-harness"
            ),
        ),
        (
            "exact_harness_scope_marker_candidates_outrank_unspecified_fallback",
            lambda: test_exact_harness_scope_marker_candidates_outrank_unspecified_fallback(
                root / "marker-precision-ordering"
            ),
        ),
        (
            "scope_markers_do_not_collide_across_repositories_sharing_a_task_id",
            lambda: test_scope_markers_do_not_collide_across_repositories_sharing_a_task_id(
                root / "marker-repo-identity"
            ),
        ),
        (
            "committed_observation_excludes_base_branch_sync_merge",
            lambda: test_committed_observation_excludes_base_branch_sync_merge(root / "committed-merge"),
        ),
        (
            "committed_round_changed_paths_excludes_rebased_base_commits",
            lambda: test_committed_round_changed_paths_excludes_rebased_base_commits(
                root / "committed-rebase"
            ),
        ),
        (
            "committed_round_base_ref_self_disables_when_head_on_base",
            lambda: test_committed_round_base_ref_self_disables_when_head_on_base(
                root / "committed-base-self-disable"
            ),
        ),
        (
            "committed_round_base_ref_excludes_ff_to_base",
            lambda: test_committed_round_base_ref_excludes_ff_to_base(
                root / "committed-base-ff-to-base"
            ),
        ),
        (
            "build_manifest_includes_committed_round_owned_paths",
            lambda: test_build_manifest_includes_committed_round_owned_paths(root / "committed-manifest"),
        ),
        (
            "build_manifest_attributes_committed_round_without_transcript_evidence",
            lambda: test_build_manifest_attributes_committed_round_without_transcript_evidence(
                root / "committed-no-evidence"
            ),
        ),
        (
            "build_manifest_recovers_ownership_from_new_codex_patch_apply_end",
            lambda: test_build_manifest_recovers_ownership_from_new_codex_patch_apply_end(
                root / "new-codex-patch-apply-end"
            ),
        ),
        (
            "build_manifest_new_codex_guard_flags_schema_drift",
            lambda: test_build_manifest_new_codex_guard_flags_schema_drift(root / "new-codex-drift-guard"),
        ),
        (
            "build_manifest_no_schema_drift_flag_without_edit_evidence",
            lambda: test_build_manifest_no_schema_drift_flag_without_edit_evidence(
                root / "new-codex-no-drift"
            ),
        ),
        (
            "build_manifest_old_codex_patch_apply_end_not_double_counted",
            lambda: test_build_manifest_old_codex_patch_apply_end_not_double_counted(
                root / "old-codex-no-double-count"
            ),
        ),
        (
            "build_manifest_resume_mixed_apply_patch_and_patch_apply_end",
            lambda: test_build_manifest_resume_mixed_apply_patch_and_patch_apply_end(
                root / "resume-mixed-apply-patch"
            ),
        ),
        (
            "build_manifest_new_codex_rename_edit_attributes_target",
            lambda: test_build_manifest_new_codex_rename_edit_attributes_target(
                root / "new-codex-rename-edit"
            ),
        ),
        (
            "build_manifest_readonly_command_with_apply_patch_substring_is_not_edit_evidence",
            lambda: test_build_manifest_readonly_command_with_apply_patch_substring_is_not_edit_evidence(
                root / "readonly-apply-patch-substring"
            ),
        ),
        (
            "schema_drift_suspected_triggers_loud_skip_via_stop_gate",
            lambda: test_schema_drift_suspected_triggers_loud_skip_via_stop_gate(
                root / "schema-drift-loud-skip"
            ),
        ),
        (
            "committed_round_changed_paths_excludes_skip_review_trailer",
            lambda: test_committed_round_changed_paths_excludes_skip_review_trailer(
                root / "committed-skip-trailer"
            ),
        ),
        (
            "committed_then_dirty_same_path_resolves_to_dirty",
            lambda: test_committed_then_dirty_same_path_resolves_to_dirty(root / "committed-then-dirty"),
        ),
        (
            "committed_unit_gone_after_reset_is_superseded",
            lambda: test_committed_unit_gone_after_reset_is_superseded(root / "committed-superseded"),
        ),
        (
            "round_baseline_marker_round_trip",
            lambda: test_round_baseline_marker_round_trip(root / "round-baseline-marker"),
        ),
        (
            "round_baseline_marker_expiry_extension_does_not_overwrite_concurrent_developer_prompt_baseline",
            lambda: test_round_baseline_marker_expiry_extension_does_not_overwrite_concurrent_developer_prompt_baseline(
                root / "round-baseline-extension-vs-developer-prompt-serialized"
            ),
        ),
        (
            "rvf_user_prompt_submit_captures_round_baseline",
            lambda: test_rvf_user_prompt_submit_captures_round_baseline(root / "round-baseline-capture"),
        ),
        # --- 带外投递的发起横幅落地（INV-A 达成信号 / 不 arm 锁 / 检测器互斥 / 撤回中和） ---
        (
            "rvf_user_prompt_submit_observes_delivered_decision_point_banner",
            lambda: test_rvf_user_prompt_submit_observes_delivered_decision_point_banner(
                root / "banner-observed"
            ),
        ),
        (
            "rvf_user_prompt_submit_decision_point_banner_never_arms_in_flight_lock",
            lambda: test_rvf_user_prompt_submit_decision_point_banner_never_arms_in_flight_lock(
                root / "banner-no-arm"
            ),
        ),
        (
            "rvf_user_prompt_submit_decision_point_banner_misses_every_other_detector",
            lambda: test_rvf_user_prompt_submit_decision_point_banner_misses_every_other_detector(
                root / "banner-detectors"
            ),
        ),
        (
            "rvf_user_prompt_submit_withdrawn_decision_point_banner_advances_nothing",
            lambda: test_rvf_user_prompt_submit_withdrawn_decision_point_banner_advances_nothing(
                root / "banner-withdrawn"
            ),
        ),
        (
            "rvf_user_prompt_submit_expired_banner_never_claims_token_was_advanced",
            lambda: test_rvf_user_prompt_submit_expired_banner_never_claims_token_was_advanced(
                root / "banner-expired-not-advanced"
            ),
        ),
        (
            "rvf_user_prompt_submit_resubmitted_decision_point_marker_never_reinjects_body",
            lambda: test_rvf_user_prompt_submit_resubmitted_decision_point_marker_never_reinjects_body(
                root / "banner-marker-resubmitted"
            ),
        ),
        (
            "rvf_user_prompt_submit_decision_point_marker_without_body_text_advances_nothing",
            lambda: test_rvf_user_prompt_submit_decision_point_marker_without_body_text_advances_nothing(
                root / "banner-marker-without-body"
            ),
        ),
        (
            "rvf_user_prompt_submit_concurrent_decision_point_marker_arrivals_inject_body_exactly_once",
            lambda: test_rvf_user_prompt_submit_concurrent_decision_point_marker_arrivals_inject_body_exactly_once(
                root / "banner-marker-concurrent-arrivals"
            ),
        ),
        (
            "allocate_review_scope_includes_committed_round",
            lambda: test_allocate_review_scope_includes_committed_round(root / "committed-allocate-e2e"),
        ),
        (
            "rvf_handoff_cli_notify",
            lambda: test_rvf_handoff_cli_notify(root / "handoff-notify"),
        ),
        (
            "rvf_handoff_marker_tail_and_summary",
            lambda: test_rvf_handoff_marker_tail_and_summary(),
        ),
        (
            "rvf_handoff_resolve_kanban_task_url",
            lambda: test_rvf_handoff_resolve_kanban_task_url(root / "handoff-url"),
        ),
        (
            "rvf_handoff_notify_requires_terminal_notifier",
            lambda: test_rvf_handoff_notify_requires_terminal_notifier(root / "handoff-missing"),
        ),
        (
            "rvf_handoff_maybe_trigger_kanban_notification",
            lambda: test_rvf_handoff_maybe_trigger_kanban_notification(root / "handoff-kanban-trigger"),
        ),
        ("rvf_prep_file_round_trip_and_sweep", lambda: test_rvf_prep_file_round_trip_and_sweep(root / "prep-file")),
        (
            "rvf_prep_file_revive_expired_restamps_ttl",
            lambda: test_rvf_prep_file_revive_expired_restamps_ttl(root / "prep-file-revive"),
        ),
        (
            "rvf_user_prompt_submit_dispatches_shared_workflow",
            lambda: test_rvf_user_prompt_submit_dispatches_shared_workflow(root / "prompt-submit-token"),
        ),
        (
            "rvf_user_prompt_submit_revives_expired_prep_when_run_artifacts_exist",
            lambda: test_rvf_user_prompt_submit_revives_expired_prep_when_run_artifacts_exist(
                root / "prompt-submit-revive"
            ),
        ),
        (
            "rvf_user_prompt_submit_declines_reviving_stale_prep",
            lambda: test_rvf_user_prompt_submit_declines_reviving_stale_prep(
                root / "prompt-submit-stale-revive"
            ),
        ),
        (
            "rvf_user_prompt_submit_reports_no_prep_when_expired_and_run_dir_missing",
            lambda: test_rvf_user_prompt_submit_reports_no_prep_when_expired_and_run_dir_missing(
                root / "prompt-submit-revive-missing"
            ),
        ),
        (
            "rvf_user_prompt_submit_marker_without_token",
            lambda: test_rvf_user_prompt_submit_marker_without_token(root / "prompt-submit-marker"),
        ),
        (
            "rvf_user_prompt_submit_arms_kanban_followup_lock_on_delivery",
            lambda: test_rvf_user_prompt_submit_arms_kanban_followup_lock_on_delivery(
                root / "prompt-submit-followup-arm"
            ),
        ),
        (
            "kanban_followup_marker_family_roots_follow_rvf_home_when_no_dedicated_root_is_set",
            lambda: test_kanban_followup_marker_family_roots_follow_rvf_home_when_no_dedicated_root_is_set(
                root / "kanban-followup-marker-family-roots-follow-rvf-home"
            ),
        ),
        (
            "kanban_followup_pending_marker_round_trip",
            lambda: test_kanban_followup_pending_marker_round_trip(
                root / "kanban-followup-pending"
            ),
        ),
        (
            "kanban_followup_iter_pending_and_stamp_notified",
            lambda: test_kanban_followup_iter_pending_and_stamp_notified(
                root / "kanban-followup-iter-stamp"
            ),
        ),
        (
            "awaiting_dispatched_agent_marker_arm_iter_clear_round_trip",
            lambda: test_awaiting_dispatched_agent_marker_arm_iter_clear_round_trip(
                root / "awaiting-dispatched-arm-iter-clear"
            ),
        ),
        (
            "awaiting_dispatched_agent_marker_stale_is_lazily_cleared",
            lambda: test_awaiting_dispatched_agent_marker_stale_is_lazily_cleared(
                root / "awaiting-dispatched-stale"
            ),
        ),
        (
            "awaiting_dispatched_agent_marker_cli_round_trip",
            lambda: test_awaiting_dispatched_agent_marker_cli_round_trip(
                root / "awaiting-dispatched-cli"
            ),
        ),
        (
            "notify_kanban_followup_stranded",
            lambda: test_notify_kanban_followup_stranded(root / "kanban-followup-notify"),
        ),
        (
            "rvf_user_prompt_submit_clears_pending_on_delivery",
            lambda: test_rvf_user_prompt_submit_clears_pending_on_delivery(
                root / "prompt-submit-followup-pending-clear"
            ),
        ),
        (
            "rvf_user_prompt_submit_structured_manual_detection_catches_namespaced",
            lambda: test_rvf_user_prompt_submit_structured_manual_detection_catches_namespaced(
                root / "prompt-submit-structured-manual"
            ),
        ),
        (
            "rvf_user_prompt_submit_manual_mints_decision_token_and_injects_via_additional_context",
            lambda: test_rvf_user_prompt_submit_manual_mints_decision_token_and_injects_via_additional_context(
                root / "prompt-submit-manual-mint"
            ),
        ),
        (
            "rvf_user_prompt_submit_manual_dedup_retries_minted_delivery_receipt",
            lambda: test_rvf_user_prompt_submit_manual_dedup_retries_minted_delivery_receipt(
                root / "prompt-submit-manual-dedup-receipt"
            ),
        ),
        (
            "rvf_user_prompt_submit_hook_stdout_confirms_manual_delivery",
            lambda: test_rvf_user_prompt_submit_hook_stdout_confirms_manual_delivery(
                root / "prompt-submit-manual-stdout-receipt"
            ),
        ),
        (
            "parse_manual_scope_directive_variants",
            lambda: test_parse_manual_scope_directive_variants(),
        ),
        (
            "rvf_user_prompt_submit_manual_scope_directive_surfaces_hint_in_injection",
            lambda: test_rvf_user_prompt_submit_manual_scope_directive_surfaces_hint_in_injection(
                root / "prompt-submit-manual-scope"
            ),
        ),
        (
            "rvf_user_prompt_submit_manual_no_candidates_emits_systemmessage_no_ledger",
            lambda: test_rvf_user_prompt_submit_manual_no_candidates_emits_systemmessage_no_ledger(
                root / "prompt-submit-manual-empty"
            ),
        ),
        (
            "rvf_user_prompt_submit_manual_declined_cooldown_is_overridden_by_user",
            lambda: test_rvf_user_prompt_submit_manual_declined_cooldown_is_overridden_by_user(
                root / "prompt-submit-manual-cooldown"
            ),
        ),
        (
            "rvf_user_prompt_submit_manual_declined_cooldown_override_survives_clear_failure",
            lambda: test_rvf_user_prompt_submit_manual_declined_cooldown_override_survives_clear_failure(
                root / "prompt-submit-manual-cooldown-clear-fails"
            ),
        ),
        (
            "rvf_user_prompt_submit_manual_is_exempt_from_mint_circuit_breaker",
            lambda: test_rvf_user_prompt_submit_manual_is_exempt_from_mint_circuit_breaker(
                root / "prompt-submit-manual-breaker-exempt"
            ),
        ),
        (
            "rvf_user_prompt_submit_developer_prompt_rebases_snooze_baseline_but_banner_delivery_does_not",
            lambda: test_rvf_user_prompt_submit_developer_prompt_rebases_snooze_baseline_but_banner_delivery_does_not(
                root / "prompt-submit-developer-prompt-rebases-session-scope-snooze"
            ),
        ),
        (
            "rvf_user_prompt_submit_manual_trigger_tells_agent_it_overrode_declined_session_scope_snooze",
            lambda: test_rvf_user_prompt_submit_manual_trigger_tells_agent_it_overrode_declined_session_scope_snooze(
                root / "prompt-submit-manual-overrides-session-scope-snooze"
            ),
        ),
        (
            "rvf_user_prompt_submit_manual_trigger_does_not_first_rebase_the_snooze_it_is_about_to_release",
            lambda: test_rvf_user_prompt_submit_manual_trigger_does_not_first_rebase_the_snooze_it_is_about_to_release(
                root / "prompt-submit-manual-skips-snooze-rebase"
            ),
        ),
        (
            "rvf_user_prompt_submit_marker_resubmitted_after_rewind_withdraws_the_still_injected_token",
            lambda: test_rvf_user_prompt_submit_marker_resubmitted_after_rewind_withdraws_the_still_injected_token(
                root / "prompt-submit-marker-resubmitted-after-rewind"
            ),
        ),
        (
            "rvf_user_prompt_submit_marker_arriving_while_first_injection_is_still_being_handed_to_host_never_withdraws_token",
            lambda: test_rvf_user_prompt_submit_marker_arriving_while_first_injection_is_still_being_handed_to_host_never_withdraws_token(
                root / "prompt-submit-marker-arriving-while-first-injection-in-flight"
            ),
        ),
        (
            "rvf_user_prompt_submit_marker_arriving_before_first_injection_hook_output_reaches_host_never_withdraws_token",
            lambda: test_rvf_user_prompt_submit_marker_arriving_before_first_injection_hook_output_reaches_host_never_withdraws_token(
                root / "prompt-submit-marker-arriving-before-first-injection-hook-output-reaches-host"
            ),
        ),
        (
            "rvf_user_prompt_submit_first_injection_handoff_completed_credential_is_never_visible_before_its_content_is_fully_written",
            lambda: test_rvf_user_prompt_submit_first_injection_handoff_completed_credential_is_never_visible_before_its_content_is_fully_written(
                root / "prompt-submit-first-injection-credential-content-atomicity"
            ),
        ),
        (
            "rvf_user_prompt_submit_first_marker_arrival_injects_body_even_if_clearing_the_delivery_intent_raises",
            lambda: test_rvf_user_prompt_submit_first_marker_arrival_injects_body_even_if_clearing_the_delivery_intent_raises(
                root / "prompt-submit-first-arrival-clear-intent-raises"
            ),
        ),
        (
            "rvf_user_prompt_submit_claude_task_notification_quoting_the_manual_trigger_is_not_a_manual_trigger",
            lambda: test_rvf_user_prompt_submit_claude_task_notification_quoting_the_manual_trigger_is_not_a_manual_trigger(
                root / "prompt-submit-task-notification-quotes-manual-trigger"
            ),
        ),
        (
            "rvf_user_prompt_submit_new_turn_releases_deferred_until_condition_decline_but_rvf_banner_does_not",
            lambda: test_rvf_user_prompt_submit_new_turn_releases_deferred_until_condition_decline_but_rvf_banner_does_not(
                root / "ups-deferred-turn-boundary"
            ),
        ),
        (
            "rvf_user_prompt_submit_claude_task_notification_does_not_reset_stop_hook_reentry_budget",
            lambda: test_rvf_user_prompt_submit_claude_task_notification_does_not_reset_stop_hook_reentry_budget(
                root / "prompt-submit-task-notification-keeps-reentry-budget"
            ),
        ),
        (
            "rvf_user_prompt_submit_claude_task_notification_does_not_rebase_declined_session_scope_snooze",
            lambda: test_rvf_user_prompt_submit_claude_task_notification_does_not_rebase_declined_session_scope_snooze(
                root / "prompt-submit-task-notification-keeps-snooze-baseline"
            ),
        ),
        (
            "rvf_user_prompt_submit_claude_task_notification_keeps_round_baseline_head_but_extends_its_expiry",
            lambda: test_rvf_user_prompt_submit_claude_task_notification_keeps_round_baseline_head_but_extends_its_expiry(
                root / "prompt-submit-task-notification-keeps-round-baseline-head"
            ),
        ),
        (
            "rvf_user_prompt_submit_claude_task_notification_keeps_previous_turn_park_eligibility",
            lambda: test_rvf_user_prompt_submit_claude_task_notification_keeps_previous_turn_park_eligibility(
                root / "prompt-submit-task-notification-keeps-park-eligibility"
            ),
        ),
        (
            "rvf_user_prompt_submit_claude_task_notification_quoting_rvf_literals_triggers_nothing",
            lambda: test_rvf_user_prompt_submit_claude_task_notification_quoting_rvf_literals_triggers_nothing(
                root / "prompt-submit-task-notification-quotes-rvf-literals"
            ),
        ),
        (
            "rvf_user_prompt_submit_manual_retired_packet_prep_functions_removed",
            lambda: test_rvf_user_prompt_submit_manual_retired_packet_prep_functions_removed(
                root / "prompt-submit-manual-retired"
            ),
        ),
        (
            "rvf_user_prompt_submit_bootstrap_confirm_gate_abolished",
            lambda: test_rvf_user_prompt_submit_bootstrap_confirm_gate_abolished(
                root / "prompt-submit-bootstrap-abolished"
            ),
        ),
        (
            "rvf_user_prompt_submit_manual_substring_does_not_falsely_trigger",
            lambda: test_rvf_user_prompt_submit_manual_substring_does_not_falsely_trigger(root / "prompt-submit-substring"),
        ),
        (
            "rvf_user_prompt_submit_handoff_literal_does_not_falsely_trigger",
            lambda: test_rvf_user_prompt_submit_handoff_literal_does_not_falsely_trigger(
                root / "prompt-submit-handoff-literal"
            ),
        ),
        (
            "rvf_user_prompt_submit_namespaced_subskill_does_not_falsely_trigger",
            lambda: test_rvf_user_prompt_submit_namespaced_subskill_does_not_falsely_trigger(
                root / "prompt-submit-namespaced-subskill"
            ),
        ),
        (
            "rvf_user_prompt_submit_failed_prepare_records_state_without_blocking",
            lambda: test_rvf_user_prompt_submit_failed_prepare_records_state_without_blocking(root / "prompt-submit-failed"),
        ),
        (
            "claude_plugin_hooks_declare_user_prompt_submit",
            lambda: test_claude_plugin_hooks_declare_user_prompt_submit(),
        ),
        (
            "claude_plugin_hooks_declare_post_tool_use_with_write_matcher",
            lambda: test_claude_plugin_hooks_declare_post_tool_use_with_write_matcher(),
        ),
        (
            "cline_kanban_client_park_unpark_is_parked",
            lambda: test_cline_kanban_client_park_unpark_is_parked(root / "cline-kanban-park"),
        ),
        (
            "post_tool_use_parks_eligible_edit_turn",
            lambda: test_post_tool_use_parks_eligible_edit_turn(root / "post-tool-use-park"),
        ),
        (
            "post_tool_use_skips_token_turn",
            lambda: test_post_tool_use_skips_token_turn(root / "post-tool-use-skip-token"),
        ),
        (
            "post_tool_use_skips_non_edit_or_non_kanban",
            lambda: test_post_tool_use_skips_non_edit_or_non_kanban(
                root / "post-tool-use-skip-nonedit"
            ),
        ),
        (
            "user_prompt_submit_marks_park_eligibility",
            lambda: test_user_prompt_submit_marks_park_eligibility(
                root / "ups-park-eligibility"
            ),
        ),
        (
            "claude_hook_entry_detects_foreign_invocation",
            lambda: test_claude_hook_entry_detects_foreign_invocation(),
        ),
        (
            "claude_hook_entry_dispatches_claude_invocation",
            lambda: test_claude_hook_entry_dispatches_claude_invocation(),
        ),
        (
            "claude_plugin_shim_codex_invocation_noop",
            lambda: test_claude_plugin_shim_codex_invocation_noop(root / "shim-codex-noop"),
        ),
        (
            "claude_plugin_stop_shim_codex_invocation_noop",
            lambda: test_claude_plugin_stop_shim_codex_invocation_noop(root / "stop-shim-codex-noop"),
        ),
        (
            "rvf_user_prompt_submit_backfills_child_session",
            lambda: test_rvf_user_prompt_submit_backfills_child_session(root / "prompt-submit-child"),
        ),
        (
            "prepare_run_from_prep_file_timeout_returns_immediately",
            lambda: test_prepare_run_from_prep_file_timeout_returns_immediately(root / "prepare-run-timeout"),
        ),
        (
            "rvf_user_prompt_submit_subprocess_stays_silent_in_hook_mode",
            lambda: test_rvf_user_prompt_submit_subprocess_stays_silent_in_hook_mode(root / "prompt-submit-silent"),
        ),
        (
            "rvf_user_prompt_submit_dispatch_no_prep_emits_user_visible_systemMessage",
            lambda: test_rvf_user_prompt_submit_dispatch_no_prep_emits_user_visible_systemMessage(
                root / "prompt-submit-no-prep"
            ),
        ),
        (
            "rvf_user_prompt_submit_render_hook_payload_merges_channels",
            lambda: test_rvf_user_prompt_submit_render_hook_payload_merges_channels(root / "prompt-submit-render"),
        ),
        ("check_review_output_lock_request", lambda: test_check_review_output_lock_request()),
        (
            "check_review_output_protocol_extension_requests",
            lambda: test_check_review_output_protocol_extension_requests(),
        ),
        (
            "review_result_artifact_no_issues_and_issues",
            lambda: test_review_result_artifact_no_issues_and_issues(root / "review-result-basic"),
        ),
        (
            "no_issues_requires_audit_summary",
            lambda: test_no_issues_requires_audit_summary(root / "no-issues-audit-summary"),
        ),
        (
            "review_result_artifact_requests_and_scope_exclusions",
            lambda: test_review_result_artifact_requests_and_scope_exclusions(root / "review-result-request"),
        ),
        (
            "review_result_artifact_rejects_malformed_and_mixed_state",
            lambda: test_review_result_artifact_rejects_malformed_and_mixed_state(root / "review-result-invalid"),
        ),
        (
            "issue_requires_kind",
            lambda: test_issue_requires_kind(root / "issue-requires-kind"),
        ),
        (
            "issue_requires_severity",
            lambda: test_issue_requires_severity(root / "issue-requires-severity"),
        ),
        (
            "check_rejects_issue_without_kind",
            lambda: test_check_rejects_issue_without_kind(root / "check-rejects-no-kind"),
        ),
        (
            "check_rejects_invalid_severity",
            lambda: test_check_rejects_invalid_severity(root / "check-rejects-bad-severity"),
        ),
        (
            "check_skill_contracts_requires_validate_fix_request_literals",
            lambda: test_check_skill_contracts_requires_validate_fix_request_literals(),
        ),
        (
            "contract_check_entrypoints_default_quiet_with_verbose_flag",
            lambda: test_contract_check_entrypoints_default_quiet_with_verbose_flag(),
        ),
        (
            "contract_check_parallel_test_steps_record_parallel_timing",
            lambda: test_contract_check_parallel_test_steps_record_parallel_timing(),
        ),
        (
            "contract_check_parallel_jobs_derive_from_host_cpu_capped_at_one_third",
            lambda: test_contract_check_parallel_jobs_derive_from_host_cpu_capped_at_one_third(),
        ),
        (
            "contract_check_timing_report_accounts_internal_steps",
            lambda: test_contract_check_timing_report_accounts_internal_steps(),
        ),
        (
            "run_ledger_summary_preserves_contract_timing_fields",
            lambda: test_run_ledger_summary_preserves_contract_timing_fields(
                root / "summary-preserve-contract-timing"
            ),
        ),
        (
            "rvf_logging_all_skill_dirs_default_to_user_local_run_logs",
            lambda: test_rvf_logging_all_skill_dirs_default_to_user_local_run_logs(
                root / "non-canonical-log-root"
            ),
        ),
        (
            "rvf_logging_does_not_fall_back_to_plugin_state_when_install_missing",
            lambda: test_rvf_logging_does_not_fall_back_to_plugin_state_when_install_missing(
                root / "skill-dir-fallback-state"
            ),
        ),
        (
            "check_review_output_accepts_wrapped_issue_continuation",
            lambda: test_check_review_output_accepts_wrapped_issue_continuation(),
        ),
        ("build_packet_metadata_and_scope", lambda: test_build_packet_metadata_and_scope(root / "packet")),
        (
            "build_packet_allows_clean_repo_with_manual_scope",
            lambda: test_build_packet_allows_clean_repo_with_manual_scope(root / "packet-clean-manual-scope"),
        ),
        (
            "session_manifest_extracts_apply_patch_and_command_candidates",
            lambda: test_session_manifest_extracts_apply_patch_and_command_candidates(root / "session-manifest"),
        ),
        (
            "session_manifest_does_not_claim_post_commit_same_path_background_dirty",
            lambda: test_session_manifest_does_not_claim_post_commit_same_path_background_dirty(
                root / "session-manifest-post-commit"
            ),
        ),
        (
            "session_manifest_claims_apply_patch_after_commit_cutoff",
            lambda: test_session_manifest_claims_apply_patch_after_commit_cutoff(
                root / "session-manifest-commit-cutoff"
            ),
        ),
        (
            "session_manifest_only_claims_matching_apply_patch_hunk",
            lambda: test_session_manifest_only_claims_matching_apply_patch_hunk(
                root / "session-manifest-live-hunk"
            ),
        ),
        (
            "session_manifest_records_edit_claim_user_context",
            lambda: test_session_manifest_records_edit_claim_user_context(
                root / "session-manifest-edit-claim-user-context"
            ),
        ),
        (
            "session_manifest_records_codex_message_user_context",
            lambda: test_session_manifest_records_codex_message_user_context(
                root / "session-manifest-codex-message-user-context"
            ),
        ),
        (
            "complete_review_scope_waits_for_all_edit_claim_units",
            lambda: test_complete_review_scope_waits_for_all_edit_claim_units(
                root / "complete-review-scope-edit-claim-units"
            ),
        ),
        (
            "session_manifest_suppresses_unresolved_without_tracker_watermark",
            lambda: test_session_manifest_suppresses_unresolved_without_tracker_watermark(
                root / "session-manifest-unresolved-no-watermark"
            ),
        ),
        (
            "session_manifest_reports_unresolved_apply_patch_hunk_after_tracker_watermark",
            lambda: test_session_manifest_reports_unresolved_apply_patch_hunk_after_tracker_watermark(
                root / "session-manifest-unresolved-with-watermark"
            ),
        ),
        (
            "session_manifest_uses_tracker_transcript_watermark",
            lambda: test_session_manifest_uses_tracker_transcript_watermark(
                root / "session-manifest-tracker-watermark"
            ),
        ),
        (
            "session_manifest_legacy_timestampless_transcript_fallback_warns",
            lambda: test_session_manifest_legacy_timestampless_transcript_fallback_warns(
                root / "session-manifest-legacy-fallback"
            ),
        ),
        (
            "session_manifest_resolves_exec_paths_from_command_workdir",
            lambda: test_session_manifest_resolves_exec_paths_from_command_workdir(
                root / "session-manifest-workdir"
            ),
        ),
        (
            "session_manifest_claims_claude_write_tool_paths",
            lambda: test_session_manifest_claims_claude_write_tool_paths(
                root / "session-manifest-claude-write"
            ),
        ),
        (
            "diagnose_stop_hook_scope_reports_stale_runtime_and_claude_write_gap",
            lambda: test_diagnose_stop_hook_scope_reports_stale_runtime_and_claude_write_gap(
                root / "diagnose-stop-hook-scope"
            ),
        ),
        (
            "build_packet_uses_session_manifest_as_scope_anchor",
            lambda: test_build_packet_uses_session_manifest_as_scope_anchor(root / "packet-manifest"),
        ),
        (
            "build_packet_filters_session_owned_diff_to_tracker_hunk",
            lambda: test_build_packet_filters_session_owned_diff_to_tracker_hunk(
                root / "packet-tracker-hunk"
            ),
        ),
        (
            "build_packet_rejects_session_manifest_for_different_repo",
            lambda: test_build_packet_rejects_session_manifest_for_different_repo(
                root / "packet-manifest-repo"
            ),
        ),
        (
            "build_packet_rejects_empty_session_owned_scope",
            lambda: test_build_packet_rejects_empty_session_owned_scope(root / "packet-manifest-empty"),
        ),
        (
            "build_packet_requires_session_context",
            lambda: test_build_packet_requires_session_context(root / "packet-requires-context"),
        ),
        (
            "build_packet_honors_review_validate_fix_ignore",
            lambda: test_build_packet_honors_review_validate_fix_ignore(root / "packet-ignore"),
        ),
        (
            "build_packet_treats_ignore_prefixes_as_literal_pathspecs",
            lambda: test_build_packet_treats_ignore_prefixes_as_literal_pathspecs(root / "packet-literal-ignore"),
        ),
        (
            "build_packet_excludes_nested_paths_under_review_scope_excluded_path_prefixes",
            lambda: test_build_packet_excludes_nested_paths_under_review_scope_excluded_path_prefixes(
                root / "packet-nested-excluded-path-prefixes"
            ),
        ),
        ("prepare_review_run_and_command_lock", lambda: test_prepare_review_run_and_command_lock(root / "prepare")),
        (
            "prepare_review_run_scope_contract_v3_records_base_drift_evidence",
            lambda: test_prepare_review_run_scope_contract_v3_records_base_drift_evidence(
                root / "prepare-scope-contract-v3"
            ),
        ),
        (
            "prepare_review_run_blocks_opted_in_repo_until_local_base_is_absorbed",
            lambda: test_prepare_review_run_blocks_opted_in_repo_until_local_base_is_absorbed(
                root / "prepare-local-base-bootstrap-gate"
            ),
        ),
        (
            "prepare_review_run_rejects_invalid_local_base_ref",
            lambda: test_prepare_review_run_rejects_invalid_local_base_ref(
                root / "prepare-invalid-local-base-ref"
            ),
        ),
        (
            "prepare_from_prep_file_waits_for_local_base_bootstrap_with_recoverable_gate",
            lambda: test_prepare_from_prep_file_waits_for_local_base_bootstrap_with_recoverable_gate(
                root / "bootstrap-gate-waiting"
            ),
        ),
        (
            "bootstrap_gate_reconciles_receipt_conflict_and_tamper_fail_closed",
            lambda: test_bootstrap_gate_reconciles_receipt_conflict_and_tamper_fail_closed(
                root / "bootstrap-gate-conflict-tamper"
            ),
        ),
        (
            "bootstrap_gate_reports_artifact_loss_and_opt_in_withdrawal_without_raising",
            lambda: test_bootstrap_gate_reports_artifact_loss_and_opt_in_withdrawal_without_raising(
                root / "bootstrap-gate-artifact-loss"
            ),
        ),
        (
            "bootstrap_gate_never_carries_environment_or_credential_values",
            lambda: test_bootstrap_gate_never_carries_environment_or_credential_values(
                root / "bootstrap-gate-no-env-leak"
            ),
        ),
        (
            "agent_cli_start_parks_on_bootstrap_then_resumes_same_run",
            lambda: test_agent_cli_start_parks_on_bootstrap_then_resumes_same_run(
                root / "bootstrap-agent-cli-resume"
            ),
        ),
        (
            "ups_stamps_waiting_bootstrap_onto_armed_kanban_followup_lock",
            lambda: test_ups_stamps_waiting_bootstrap_onto_armed_kanban_followup_lock(
                root / "bootstrap-ups-lock-stamp"
            ),
        ),
        (
            "bootstrap_replay_after_base_absorption_requires_new_review_start_boundary",
            lambda: test_bootstrap_replay_after_base_absorption_requires_new_review_start_boundary(
                root / "bootstrap-replay-same-run"
            ),
        ),
        (
            "held_bootstrap_gate_persists_verified_receipt_digest_so_swapped_receipt_is_tampered",
            lambda: test_held_bootstrap_gate_persists_verified_receipt_digest_so_swapped_receipt_is_tampered(
                root / "bootstrap-gate-digest-writeback"
            ),
        ),
        (
            "bootstrap_gate_g0_rejects_forged_origin_run_lineage_and_lost_run_artifacts",
            lambda: test_bootstrap_gate_g0_rejects_forged_origin_run_lineage_and_lost_run_artifacts(
                root / "bootstrap-gate-g0-forgery"
            ),
        ),
        (
            "bootstrap_gate_recovers_when_local_base_advances_during_the_wait",
            lambda: test_bootstrap_gate_recovers_when_local_base_advances_during_the_wait(
                root / "bootstrap-gate-base-advanced"
            ),
        ),
        (
            "agent_cli_start_retry_resumes_after_local_base_advances_during_the_wait",
            lambda: test_agent_cli_start_retry_resumes_after_local_base_advances_during_the_wait(
                root / "bootstrap-agent-cli-base-advanced"
            ),
        ),
        (
            "agent_cli_concurrent_bootstrap_resume_claims_prepare_exactly_once",
            lambda: test_agent_cli_concurrent_bootstrap_resume_claims_prepare_exactly_once(
                root / "bootstrap-agent-cli-replay-claim"
            ),
        ),
        (
            "bootstrap_replay_hard_bounds_a_stuck_prepare_in_a_killable_subprocess",
            lambda: test_bootstrap_replay_hard_bounds_a_stuck_prepare_in_a_killable_subprocess(
                root / "bootstrap-replay-hard-deadline"
            ),
        ),
        (
            "alternative_reviewer_config_helper_call_sites_declare_idle_timeout_classification",
            lambda: test_alternative_reviewer_config_helper_call_sites_declare_idle_timeout_classification(),
        ),
        (
            "alternative_reviewer_prompt_uses_session_env_refs",
            lambda: test_alternative_reviewer_prompt_uses_session_env_refs(root / "alternative-prompt-env"),
        ),
        (
            "alternative_reviewer_infers_scope_contract_from_inputs_layout",
            lambda: test_alternative_reviewer_infers_scope_contract_from_inputs_layout(
                root / "alternative-inputs-scope"
            ),
        ),
        (
            "alternative_reviewer_subprocess_receives_session_context_alias_and_scope_contract",
            lambda: test_alternative_reviewer_subprocess_receives_session_context_alias_and_scope_contract(
                root / "alternative-session-alias"
            ),
        ),
        (
            "alternative_reviewer_pre_run_health_refreshes_before_reviewer",
            lambda: test_alternative_reviewer_pre_run_health_refreshes_before_reviewer(
                root / "alternative-pre-run-health"
            ),
        ),
        (
            "alternative_reviewer_pre_run_health_failure_skips_reviewer",
            lambda: test_alternative_reviewer_pre_run_health_failure_skips_reviewer(
                root / "alternative-pre-run-health-failure"
            ),
        ),
        (
            "alternative_reviewer_pre_run_health_timeout_skips_reviewer",
            lambda: test_alternative_reviewer_pre_run_health_timeout_skips_reviewer(
                root / "alternative-pre-run-health-timeout"
            ),
        ),
        (
            "alternative_reviewer_user_local_override_applies_only_to_reviewer_process",
            lambda: test_alternative_reviewer_user_local_override_applies_only_to_reviewer_process(
                root / "alternative-user-local-override"
            ),
        ),
        (
            "alternative_reviewer_user_local_override_health_failure_fails_closed",
            lambda: test_alternative_reviewer_user_local_override_health_failure_fails_closed(
                root / "alternative-user-local-override-fail-closed"
            ),
        ),
        (
            "alternative_reviewer_environment_merge_precedence",
            lambda: test_alternative_reviewer_environment_merge_precedence(),
        ),
        (
            "alternative_reviewer_user_local_override_contract_rejects_invalid_configs",
            lambda: test_alternative_reviewer_user_local_override_contract_rejects_invalid_configs(
                root / "alternative-user-local-override-invalid-configs"
            ),
        ),
        (
            "alternative_reviewer_user_local_override_short_value_does_not_break_stream_json",
            lambda: test_alternative_reviewer_user_local_override_short_value_does_not_break_stream_json(
                root / "alternative-user-local-override-short-value"
            ),
        ),
        (
            "prepare_review_run_manual_all_uncommitted_allows_dirty_paths",
            lambda: test_prepare_review_run_manual_all_uncommitted_allows_dirty_paths(root / "prepare-manual-all"),
        ),
        (
            "command_lock_writes_lifecycle_events",
            lambda: test_command_lock_writes_lifecycle_events(root / "command-lock-lifecycle"),
        ),
        (
            "command_lock_respects_env_run_dir",
            lambda: test_command_lock_respects_env_run_dir(root / "command-lock-env-run-dir"),
        ),
        (
            "command_lock_logs_timeout_with_holder_metadata",
            lambda: test_command_lock_logs_timeout_with_holder_metadata(root / "command-lock-timeout"),
        ),
        (
            "prepare_review_run_can_build_session_manifest_from_transcript",
            lambda: test_prepare_review_run_can_build_session_manifest_from_transcript(root / "prepare-transcript"),
        ),
        (
            "prepare_review_run_requires_session_context",
            lambda: test_prepare_review_run_requires_session_context(root / "prepare-requires-context"),
        ),
        (
            "alternative_reviewer_idle_timeout_flag",
            lambda: test_alternative_reviewer_idle_timeout_flag(root / "alternative-timeout"),
        ),
        (
            "alternative_reviewer_delivers_entire_prompt_over_stdin_despite_watchdog_wakeups",
            lambda: test_alternative_reviewer_delivers_entire_prompt_over_stdin_despite_watchdog_wakeups(
                root / "alternative-prompt-stdin-delivery"
            ),
        ),
        (
            "alternative_reviewer_stdin_writer_thread_start_failure_cleans_up_reviewer",
            lambda: test_alternative_reviewer_stdin_writer_thread_start_failure_cleans_up_reviewer(
                root / "alternative-prompt-stdin-writer-start-failure"
            ),
        ),
        (
            "alternative_reviewer_activity_probe_keeps_silent_reviewer_alive",
            lambda: test_alternative_reviewer_activity_probe_keeps_silent_reviewer_alive(
                root / "alternative-probe-success"
            ),
        ),
        (
            "alternative_reviewer_requires_review_result_artifact",
            lambda: test_alternative_reviewer_requires_review_result_artifact(
                root / "alternative-missing-result"
            ),
        ),
        (
            "alternative_reviewer_records_request_as_pending_state",
            lambda: test_alternative_reviewer_records_request_as_pending_state(
                root / "alternative-request-pending"
            ),
        ),
        (
            "alternative_reviewer_activity_probe_failure_threshold_times_out",
            lambda: test_alternative_reviewer_activity_probe_failure_threshold_times_out(
                root / "alternative-probe-failure"
            ),
        ),
        (
            "alternative_reviewer_timeout_kills_child_process_group",
            lambda: test_alternative_reviewer_timeout_kills_child_process_group(root / "alternative-timeout-child"),
        ),
        (
            "alternative_reviewer_activity_refreshes_idle_timeout",
            lambda: test_alternative_reviewer_activity_refreshes_idle_timeout(root / "alternative-activity"),
        ),
        (
            "alternative_reviewer_claude_bash_tool_use_suspends_idle_timeout",
            lambda: test_alternative_reviewer_claude_bash_tool_use_suspends_idle_timeout(
                root / "alternative-bash-tool"
            ),
        ),
        (
            "alternative_reviewer_repeated_run_keeps_prior_artifacts",
            lambda: test_alternative_reviewer_repeated_run_keeps_prior_artifacts(root / "alternative-repeat-artifacts"),
        ),
        (
            "alternative_reviewer_long_command_wait_uses_check_interval",
            lambda: test_alternative_reviewer_long_command_wait_uses_check_interval(),
        ),
        (
            "alternative_reviewer_claude_stream_monitor_tracks_bash_tool_state",
            lambda: test_alternative_reviewer_claude_stream_monitor_tracks_bash_tool_state(),
        ),
        (
            "alternative_reviewer_claude_stream_json_extracts_result",
            lambda: test_alternative_reviewer_claude_stream_json_extracts_result(root / "alternative-stream-json"),
        ),
        (
            "alternative_reviewer_cursor_stream_json_extracts_result",
            lambda: test_alternative_reviewer_cursor_stream_json_extracts_result(root / "alternative-cursor-stream-json"),
        ),
        (
            "cursor_stream_tool_layer_health_classifier_and_summary",
            lambda: test_cursor_stream_tool_layer_health_classifier_and_summary(),
        ),
        (
            "alternative_reviewer_cursor_stream_monitor_detects_tool_layer_failure",
            lambda: test_alternative_reviewer_cursor_stream_monitor_detects_tool_layer_failure(),
        ),
        (
            "alternative_reviewer_cursor_tool_layer_failure_fast_aborts",
            lambda: test_alternative_reviewer_cursor_tool_layer_failure_fast_aborts(root / "cursor-tool-layer-fast-abort"),
        ),
        (
            "alternative_reviewer_cursor_command_not_claude_patched",
            lambda: test_alternative_reviewer_cursor_command_not_claude_patched(root / "alternative-cursor-command"),
        ),
        (
            "alternative_reviewer_cursor_autodetects_stream_json",
            lambda: test_alternative_reviewer_cursor_autodetects_stream_json(root / "alternative-cursor-autodetect"),
        ),
        (
            "alternative_reviewer_codex_json_extracts_agent_message",
            lambda: test_alternative_reviewer_codex_json_extracts_agent_message(root / "alternative-codex-json"),
        ),
        (
            "alternative_reviewer_codex_json_extracts_item_completed_agent_message",
            lambda: test_alternative_reviewer_codex_json_extracts_item_completed_agent_message(
                root / "alternative-codex-json-item-completed"
            ),
        ),
        (
            "alternative_reviewer_codex_json_reports_backend_challenge_html",
            lambda: test_alternative_reviewer_codex_json_reports_backend_challenge_html(
                root / "alternative-codex-challenge-html"
            ),
        ),
        (
            "alternative_reviewer_codex_exec_json_command_is_patched",
            lambda: test_alternative_reviewer_codex_exec_json_command_is_patched(root / "alternative-codex-command"),
        ),
        (
            "alternative_reviewer_codex_exec_after_global_options_is_patched",
            lambda: test_alternative_reviewer_codex_exec_after_global_options_is_patched(
                root / "alternative-codex-global-options"
            ),
        ),
        (
            "alternative_reviewer_codex_hooks_disable_is_not_duplicated",
            lambda: test_alternative_reviewer_codex_hooks_disable_is_not_duplicated(),
        ),
        (
            "alternative_reviewer_sets_codex_stop_hook_suppress_env",
            lambda: test_alternative_reviewer_sets_codex_stop_hook_suppress_env(root / "alternative-codex-suppress"),
        ),
        (
            "alternative_reviewer_legacy_claude_config_gets_stream_json",
            lambda: test_alternative_reviewer_legacy_claude_config_gets_stream_json(root / "alternative-legacy-config"),
        ),
        (
            "alternative_reviewer_respects_explicit_claude_text_output",
            lambda: test_alternative_reviewer_respects_explicit_claude_text_output(root / "alternative-text-config"),
        ),
        (
            "alternative_reviewer_non_claude_stream_json_command_is_not_patched",
            lambda: test_alternative_reviewer_non_claude_stream_json_command_is_not_patched(
                root / "alternative-wrapper"
            ),
        ),
        ("cline_kanban_client_session_field_readers", test_cline_kanban_client_session_field_readers),
        ("cline_kanban_client_detects_runtime_port", lambda: test_cline_kanban_client_detects_runtime_port()),
        (
            "cline_kanban_client_rejects_ambiguous_runtime_ports",
            lambda: test_cline_kanban_client_rejects_ambiguous_runtime_ports(),
        ),
        (
            "cline_kanban_client_reports_missing_stable_binary",
            test_cline_kanban_client_reports_missing_stable_binary,
        ),
        (
            "cline_kanban_client_accepts_cline_tmux_listener_from_foreign_cwd",
            lambda: test_cline_kanban_client_accepts_cline_tmux_listener_from_foreign_cwd(
                root / "cline-kanban-cline-tmux-listener"
            ),
        ),
        (
            "cline_kanban_client_accepts_cline_tmux_listener_through_parent_pane",
            test_cline_kanban_client_accepts_cline_tmux_listener_through_parent_pane,
        ),
        (
            "cline_kanban_client_rejects_listener_without_cline_tmux_session",
            lambda: test_cline_kanban_client_rejects_listener_without_cline_tmux_session(
                root / "cline-kanban-non-cline-tmux-listener"
            ),
        ),
        (
            "cline_kanban_client_accepts_workspace_payload_from_cline_tmux_listener",
            lambda: test_cline_kanban_client_accepts_workspace_payload_from_cline_tmux_listener(
                root / "cline-kanban-workspace-payload"
            ),
        ),
        (
            "cline_kanban_client_rejects_workspace_payload_without_cline_tmux_listener",
            lambda: test_cline_kanban_client_rejects_workspace_payload_without_cline_tmux_listener(
                root / "cline-kanban-workspace-payload-no-tmux"
            ),
        ),
        (
            "cline_kanban_client_does_not_start_when_listener_exists_but_list_fails",
            lambda: test_cline_kanban_client_does_not_start_when_listener_exists_but_list_fails(
                root / "cline-kanban-existing-listener-list-fails"
            ),
        ),
        (
            "cline_kanban_client_create_and_start_task",
            lambda: test_cline_kanban_client_create_and_start_task(root / "cline-kanban-client"),
        ),
        (
            "cline_kanban_client_rejects_main_worktree_mode",
            lambda: test_cline_kanban_client_rejects_main_worktree_mode(
                root / "cline-kanban-main-worktree-mode"
            ),
        ),
        (
            "cline_kanban_client_start_task_uses_session_cwd_workspace",
            lambda: test_cline_kanban_client_start_task_uses_session_cwd_workspace(
                root / "cline-kanban-session-cwd"
            ),
        ),
        (
            "cline_kanban_client_branch_mode_prefers_task_workspace_over_project_path",
            lambda: test_cline_kanban_client_branch_mode_prefers_task_workspace_over_project_path(
                root / "cline-kanban-task-workspace"
            ),
        ),
        (
            "cline_kanban_client_branch_mode_rejects_parent_project_workspace",
            lambda: test_cline_kanban_client_branch_mode_rejects_parent_project_workspace(
                root / "cline-kanban-parent-workspace"
            ),
        ),
        (
            "cline_kanban_client_message_accepts_response_without_task_id",
            lambda: test_cline_kanban_client_message_accepts_response_without_task_id(root / "cline-kanban-message"),
        ),
        (
            "prepare_review_run_writes_worktree_bootstrap",
            lambda: test_prepare_review_run_writes_worktree_bootstrap(root / "worktree-bootstrap"),
        ),
        (
            "prepare_review_run_worktree_bootstrap_respects_review_validate_fix_ignore",
            lambda: test_prepare_review_run_worktree_bootstrap_respects_review_validate_fix_ignore(
                root / "worktree-bootstrap-ignore"
            ),
        ),
        (
            "prepare_review_run_worktree_bootstrap_untracked_storage_names_do_not_collide",
            lambda: test_prepare_review_run_worktree_bootstrap_untracked_storage_names_do_not_collide(
                root / "worktree-bootstrap-name-collision"
            ),
        ),
        (
            "prepare_review_run_scope_file_matches_metadata_through_symlink_state",
            lambda: test_prepare_review_run_scope_file_matches_metadata_through_symlink_state(
                root / "prepare-symlink-state"
            ),
        ),
        (
            "apply_worktree_bootstrap_replays_tracked_and_untracked",
            lambda: test_apply_worktree_bootstrap_replays_tracked_and_untracked(root / "apply-bootstrap"),
        ),
        (
            "apply_worktree_bootstrap_rejects_mismatched_base_ref",
            lambda: test_apply_worktree_bootstrap_rejects_mismatched_base_ref(root / "apply-bootstrap-base-ref"),
        ),
        (
            "run_ledger_summary_preserves_cline_kanban_fields",
            lambda: test_run_ledger_summary_preserves_cline_kanban_fields(root / "summary-preserve-cline"),
        ),
        (
            "run_ledger_summary_preserves_rvf_state_fields",
            lambda: test_run_ledger_summary_preserves_rvf_state_fields(root / "summary-preserve-rvf-state"),
        ),
        (
            "cancel_rvf_run_marks_cancelled_and_trashes_cline_task",
            lambda: test_cancel_rvf_run_marks_cancelled_and_trashes_cline_task(root / "cancel-rvf-run"),
        ),
        (
            "cancel_rvf_run_ignores_stale_runner_pid_without_matching_command",
            lambda: test_cancel_rvf_run_ignores_stale_runner_pid_without_matching_command(),
        ),
        (
            "diff_tracker_register_creates_sqlite_and_events",
            lambda: test_diff_tracker_register_creates_sqlite_and_events(root / "diff-tracker-register"),
        ),
        (
            "diff_tracker_register_concurrent_writers",
            lambda: test_diff_tracker_register_concurrent_writers(root / "diff-tracker-concurrent"),
        ),
        (
            "canonical_patch_hash_stable_across_reruns",
            lambda: test_canonical_patch_hash_stable_across_reruns(root / "diff-tracker-stable"),
        ),
        (
            "canonical_patch_hash_stable_under_line_shift",
            lambda: test_canonical_patch_hash_stable_under_line_shift(root / "diff-tracker-line-shift"),
        ),
        (
            "canonical_patch_hash_changes_on_content_edit",
            lambda: test_canonical_patch_hash_changes_on_content_edit(root / "diff-tracker-content-edit"),
        ),
        (
            "migration_phase1_json_to_sqlite_idempotent",
            lambda: test_migration_phase1_json_to_sqlite_idempotent(root / "diff-tracker-migration"),
        ),
        (
            "migration_phase1_recovers_when_archive_predates_db_marker",
            lambda: test_migration_phase1_recovers_when_archive_predates_db_marker(
                root / "diff-tracker-migration-recover"
            ),
        ),
        (
            "diff_tracker_hunk_anchor_distinguishes_close_hunks",
            lambda: test_diff_tracker_hunk_anchor_distinguishes_close_hunks(
                root / "diff-tracker-close-hunks"
            ),
        ),
        (
            "diff_tracker_register_empty_owned_paths_preserves_session_claim",
            lambda: test_diff_tracker_register_empty_owned_paths_preserves_session_claim(
                root / "diff-tracker-empty-paths"
            ),
        ),
        (
            "diff_tracker_list_conflicts_reports_other_session_overlap",
            lambda: test_diff_tracker_list_conflicts_reports_other_session_overlap(root / "diff-tracker-conflicts"),
        ),
        (
            "diff_tracker_path_claim_conflicts_with_hunk_claim",
            lambda: test_diff_tracker_path_claim_conflicts_with_hunk_claim(root / "diff-tracker-path-vs-hunk"),
        ),
        (
            "session_manifest_writes_tracker_claim",
            lambda: test_session_manifest_writes_tracker_claim(root / "session-manifest-tracker"),
        ),
        (
            "build_packet_emits_cross_session_conflict_section",
            lambda: test_build_packet_emits_cross_session_conflict_section(root / "packet-cross-session"),
        ),
        (
            "build_packet_omits_cross_session_section_when_clean",
            lambda: test_build_packet_omits_cross_session_section_when_clean(root / "packet-cross-session-clean"),
        ),
        (
            "diff_tracker_disable_env_short_circuits",
            lambda: test_diff_tracker_disable_env_short_circuits(root / "diff-tracker-disabled"),
        ),
        (
            "diff_tracker_lock_timeout_degrades_gracefully",
            lambda: test_diff_tracker_lock_timeout_degrades_gracefully(root / "diff-tracker-lock-timeout"),
        ),
        (
            "tracker_scope_payload_splices_into_manifest",
            lambda: test_tracker_scope_payload_splices_into_manifest(root / "tracker-scope-splice"),
        ),
        (
            "tracker_scope_unlocks_scope_contract_v2_fields",
            lambda: test_tracker_scope_unlocks_scope_contract_v2_fields(root / "tracker-scope-contract-v2"),
        ),
        (
            "scope_contract_v2_emitted_without_tracker_scope",
            lambda: test_scope_contract_v2_emitted_without_tracker_scope(root / "tracker-scope-contract-v2-bare"),
        ),
        (
            "packet_emits_tracker_scope_section_when_present",
            lambda: test_packet_emits_tracker_scope_section_when_present(root / "tracker-scope-packet-present"),
        ),
        (
            "packet_omits_tracker_scope_section_when_absent",
            lambda: test_packet_omits_tracker_scope_section_when_absent(root / "tracker-scope-packet-absent"),
        ),
        (
            "packet_metadata_carries_tracker_scope_keys",
            lambda: test_packet_metadata_carries_tracker_scope_keys(root / "tracker-scope-metadata"),
        ),
        (
            "tracker_scope_payload_rejects_invalid_payloads",
            lambda: test_tracker_scope_payload_rejects_invalid_payloads(root / "tracker-scope-rejection"),
        ),
        (
            "tracker_scope_requires_session_manifest_or_transcript",
            lambda: test_tracker_scope_requires_session_manifest_or_transcript(root / "tracker-scope-d4"),
        ),
        (
            "tracker_scope_tolerates_unknown_keys",
            lambda: test_tracker_scope_tolerates_unknown_keys(root / "tracker-scope-unknown-keys"),
        ),
        (
            "review_env_exports_rvf_tracker_scope_path",
            lambda: test_review_env_exports_rvf_tracker_scope_path(root / "tracker-scope-env"),
        ),
        (
            "allocated_git_diff_uses_tracker_scope_paths",
            lambda: test_allocated_git_diff_uses_tracker_scope_paths(root / "tracker-scope-diff-filter"),
        ),
        (
            "existing_cross_session_conflicts_path_unchanged_with_tracker_scope",
            lambda: test_existing_cross_session_conflicts_path_unchanged_with_tracker_scope(root / "tracker-scope-cross-session"),
        ),
        # Slice 3 allocator T1-T15.
        (
            "allocate_review_scope_emits_valid_tracker_scope_json",
            lambda: test_allocate_review_scope_emits_valid_tracker_scope_json(root / "alloc-T1"),
        ),
        (
            "allocate_review_scope_empty_returns_no_unassigned_review_scope",
            lambda: test_allocate_review_scope_empty_returns_no_unassigned_review_scope(root / "alloc-T2"),
        ),
        (
            "allocate_review_scope_preserves_untracked_file_under_new_directory",
            lambda: test_allocate_review_scope_preserves_untracked_file_under_new_directory(root / "alloc-T2b"),
        ),
        (
            "allocate_review_scope_excludes_active_leased_units",
            lambda: test_allocate_review_scope_excludes_active_leased_units(root / "alloc-T3"),
        ),
        (
            "allocate_review_scope_inserts_lease_and_marks_units_assigned",
            lambda: test_allocate_review_scope_inserts_lease_and_marks_units_assigned(root / "alloc-T4"),
        ),
        (
            "allocate_review_scope_prunes_stale_leases_first",
            lambda: test_allocate_review_scope_prunes_stale_leases_first(root / "alloc-T5"),
        ),
        (
            "allocate_review_scope_concurrent_writers_serialize",
            lambda: test_allocate_review_scope_concurrent_writers_serialize(root / "alloc-T6"),
        ),
        (
            "fork_first_stop_takeover_transfers_unleased_units",
            lambda: test_fork_first_stop_takeover_transfers_unleased_units(root / "alloc-T7"),
        ),
        (
            "fork_takeover_skips_actively_leased_units",
            lambda: test_fork_takeover_skips_actively_leased_units(root / "alloc-T8"),
        ),
        (
            "scope_hash_is_sha256_of_sorted_unit_ids",
            lambda: test_scope_hash_is_sha256_of_sorted_unit_ids(root / "alloc-T9"),
        ),
        (
            "allocator_event_appended_to_events_jsonl",
            lambda: test_allocator_event_appended_to_events_jsonl(root / "alloc-T10"),
        ),
        (
            "manual_rvf_run_inserts_row_and_emits_event",
            lambda: test_manual_rvf_run_inserts_row_and_emits_event(root / "manual-run-T1"),
        ),
        (
            "manual_rvf_run_upserts_on_pk_conflict",
            lambda: test_manual_rvf_run_upserts_on_pk_conflict(root / "manual-run-T2"),
        ),
        (
            "manual_rvf_run_find_returns_latest_completed_at",
            lambda: test_manual_rvf_run_find_returns_latest_completed_at(root / "manual-run-T4"),
        ),
        (
            "manual_rvf_run_find_respects_ttl",
            lambda: test_manual_rvf_run_find_respects_ttl(root / "manual-run-T5"),
        ),
        (
            "manual_rvf_run_ensures_table_for_existing_v2_db",
            lambda: test_manual_rvf_run_ensures_table_for_existing_v2_db(root / "manual-run-T6"),
        ),
        (
            "manual_takeover_transfers_unleased_units",
            lambda: test_manual_takeover_transfers_unleased_units(root / "manual-takeover-T1"),
        ),
        (
            "manual_takeover_skips_actively_leased_units",
            lambda: test_manual_takeover_skips_actively_leased_units(root / "manual-takeover-T2"),
        ),
        (
            "manual_takeover_rejects_missing_parent_session",
            lambda: test_manual_takeover_rejects_missing_parent_session(root / "manual-takeover-T3"),
        ),
        (
            "manual_takeover_cli_records_takeover",
            lambda: test_manual_takeover_cli_records_takeover(root / "manual-takeover-cli"),
        ),
        (
            "record_manual_run_cli_writes_row",
            lambda: test_record_manual_run_cli_writes_row(root / "manual-record-cli"),
        ),
        (
            "allocate_review_scope_disable_env_short_circuits",
            lambda: test_allocate_review_scope_disable_env_short_circuits(root / "alloc-T11"),
        ),
        (
            "allocate_review_scope_busy_timeout_degrades",
            lambda: test_allocate_review_scope_busy_timeout_degrades(root / "alloc-T12"),
        ),
        (
            "allocate_review_scope_writes_paths_and_hunks",
            lambda: test_allocate_review_scope_writes_paths_and_hunks(root / "alloc-T13"),
        ),
        (
            "allocate_review_scope_dry_run_does_not_create_lease",
            lambda: test_allocate_review_scope_dry_run_does_not_create_lease(root / "alloc-T14"),
        ),
        (
            "allocate_review_scope_output_consumed_by_prepare_run",
            lambda: test_allocate_review_scope_output_consumed_by_prepare_run(root / "alloc-T15"),
        ),
        # Slice 4 lease lifecycle.
        (
            "tracker_schema_v2_migrates_lease_participants_table",
            lambda: test_tracker_schema_v2_migrates_lease_participants_table(root / "lease-T0"),
        ),
        (
            "tracker_schema_v4_rebuilds_legacy_tombstoned_review_state",
            lambda: test_tracker_schema_v4_rebuilds_legacy_tombstoned_review_state(root / "lease-T0b"),
        ),
        (
            "declare_units_review_not_required_survives_tracker_refresh",
            lambda: test_declare_units_review_not_required_survives_tracker_refresh(
                root / "waiver-T1"
            ),
        ),
        (
            "declare_units_review_not_required_is_not_inherited_by_changed_hunk",
            lambda: test_declare_units_review_not_required_is_not_inherited_by_changed_hunk(
                root / "waiver-T2"
            ),
        ),
        (
            "allocate_review_scope_unions_prior_scope_after_lease_expiry",
            lambda: test_allocate_review_scope_unions_prior_scope_after_lease_expiry(
                root / "union-T1"
            ),
        ),
        (
            "invalidate_reviewed_units_for_run_excludes_review_not_required_declarations",
            lambda: test_invalidate_reviewed_units_for_run_excludes_review_not_required_declarations(
                root / "waiver-T3"
            ),
        ),
        (
            "allocate_review_scope_union_list_excludes_units_held_by_active_lease",
            lambda: test_allocate_review_scope_union_list_excludes_units_held_by_active_lease(
                root / "union-T2"
            ),
        ),
        (
            "kanban_followup_trigger_presence_on_codex_rollout_without_uuid_chain_is_unknown_not_absent",
            lambda: test_kanban_followup_trigger_presence_on_codex_rollout_without_uuid_chain_is_unknown_not_absent(
                root / "trigger-presence-codex-rollout-T1"
            ),
        ),
        (
            "kanban_followup_trigger_transcript_scan_reads_only_bounded_tail",
            lambda: test_kanban_followup_trigger_transcript_scan_reads_only_bounded_tail(
                root / "trigger-presence-scan-T1"
            ),
        ),
        (
            "lease_acquire_creates_lease_and_assigns_units",
            lambda: test_lease_acquire_creates_lease_and_assigns_units(root / "lease-T1"),
        ),
        (
            "lease_acquire_rejects_when_any_unit_already_leased",
            lambda: test_lease_acquire_rejects_when_any_unit_already_leased(root / "lease-T2"),
        ),
        (
            "lease_acquire_rejects_tombstoned_unit",
            lambda: test_lease_acquire_rejects_tombstoned_unit(root / "lease-T2b"),
        ),
        (
            "lease_acquire_prunes_stale_leases_first",
            lambda: test_lease_acquire_prunes_stale_leases_first(root / "lease-T3"),
        ),
        (
            "stale_prune_does_not_release_unit_reacquired_by_fresh_lease",
            lambda: test_stale_prune_does_not_release_unit_reacquired_by_fresh_lease(root / "lease-T3b"),
        ),
        (
            "lease_refresh_extends_expires_at",
            lambda: test_lease_refresh_extends_expires_at(root / "lease-T4"),
        ),
        (
            "lease_refresh_returns_expired_when_past_ttl",
            lambda: test_lease_refresh_returns_expired_when_past_ttl(root / "lease-T5"),
        ),
        (
            "heartbeat_refreshes_tracker_lease_and_records_backend",
            lambda: test_heartbeat_refreshes_tracker_lease_and_records_backend(root / "lease-T5b"),
        ),
        (
            "heartbeat_treats_same_second_expiry_as_expired",
            lambda: test_heartbeat_treats_same_second_expiry_as_expired(root / "lease-T5c"),
        ),
        (
            "lease_release_completed_marks_units_reviewed",
            lambda: test_lease_release_completed_marks_units_reviewed(root / "lease-T6"),
        ),
        (
            "lease_release_idempotent",
            lambda: test_lease_release_idempotent(root / "lease-T7"),
        ),
        (
            "complete_review_scope_unions_contract_and_lease_units",
            lambda: test_complete_review_scope_unions_contract_and_lease_units(root / "lease-T7a"),
        ),
        (
            "complete_review_scope_keeps_partial_edit_claim_pending",
            lambda: test_complete_review_scope_keeps_partial_edit_claim_pending(root / "lease-T7a-partial-claim"),
        ),
        (
            "complete_review_scope_does_not_complete_failed_released_lease",
            lambda: test_complete_review_scope_does_not_complete_failed_released_lease(root / "lease-T7c"),
        ),
        (
            "complete_review_scope_supersedes_overlapping_active_lease",
            lambda: test_complete_review_scope_supersedes_overlapping_active_lease(root / "lease-T7d"),
        ),
        (
            "complete_review_scope_keeps_different_scope_active_lease",
            lambda: test_complete_review_scope_keeps_different_scope_active_lease(root / "lease-T7e"),
        ),
        (
            "lease_participants_finish_does_not_release_shared_lease",
            lambda: test_lease_participants_finish_does_not_release_shared_lease(root / "lease-T7b"),
        ),
        (
            "sweep_stale_releases_expired_active_leases",
            lambda: test_sweep_stale_releases_expired_active_leases(root / "lease-T8"),
        ),
        (
            "sweep_stale_releases_same_second_expired_lease",
            lambda: test_sweep_stale_releases_same_second_expired_lease(root / "lease-T8b"),
        ),
        (
            "sweep_stale_no_op_when_all_active_leases_fresh",
            lambda: test_sweep_stale_no_op_when_all_active_leases_fresh(root / "lease-T9"),
        ),
        (
            "run_alternative_reviewer_releases_lease_on_normal_exit",
            lambda: test_run_alternative_reviewer_releases_lease_on_normal_exit(root / "lease-T10"),
        ),
        (
            "run_alternative_reviewer_shared_lease_does_not_release_on_exit",
            lambda: test_run_alternative_reviewer_shared_lease_does_not_release_on_exit(root / "lease-T10b"),
        ),
        (
            "run_alternative_reviewer_releases_lease_on_codex_backend_challenge",
            lambda: test_run_alternative_reviewer_releases_lease_on_codex_backend_challenge(root / "lease-T11"),
        ),
        (
            "run_alternative_reviewer_releases_lease_on_timeout",
            lambda: test_run_alternative_reviewer_releases_lease_on_timeout(root / "lease-T12"),
        ),
        (
            "run_alternative_reviewer_sigterm_kills_child_before_release",
            lambda: test_run_alternative_reviewer_sigterm_kills_child_before_release(root / "lease-T12b"),
        ),
        (
            "lease_acquire_concurrent_writers_serialize",
            lambda: test_lease_acquire_concurrent_writers_serialize(root / "lease-T13"),
        ),
        (
            "invalidate_reviewed_units_for_run_reopens_and_is_idempotent",
            lambda: test_invalidate_reviewed_units_for_run_reopens_and_is_idempotent(root / "reopen-T1"),
        ),
        (
            "invalidate_reviewed_units_for_run_is_run_scoped",
            lambda: test_invalidate_reviewed_units_for_run_is_run_scoped(root / "reopen-T2"),
        ),
        (
            "invalidate_reviewed_units_for_run_excludes_tombstoned",
            lambda: test_invalidate_reviewed_units_for_run_excludes_tombstoned(root / "reopen-T3"),
        ),
        (
            "latest_reviewed_run_for_worktree_resolution",
            lambda: test_latest_reviewed_run_for_worktree_resolution(root / "reopen-T4"),
        ),
        (
            "rvf_analyze_thread_builds_claude_command",
            lambda: test_rvf_analyze_thread_builds_claude_command(),
        ),
        (
            "rvf_analyze_thread_builds_codex_command",
            lambda: test_rvf_analyze_thread_builds_codex_command(),
        ),
        (
            "rvf_analyze_thread_select_host",
            lambda: test_rvf_analyze_thread_select_host(root / "analyze-thread-host"),
        ),
        (
            "rvf_analyze_thread_status_file_schema",
            lambda: test_rvf_analyze_thread_status_file_schema(root / "analyze-thread-status"),
        ),
        (
            "rvf_analyze_thread_lock_blocks_second_launch",
            lambda: test_rvf_analyze_thread_lock_blocks_second_launch(root / "analyze-thread-lock"),
        ),
        (
            "rvf_detached_thread_status_two_phase",
            lambda: test_rvf_detached_thread_status_two_phase(root / "detached-two-phase"),
        ),
        (
            "rvf_detached_thread_lock_idempotent",
            lambda: test_rvf_detached_thread_lock_idempotent(root / "detached-lock"),
        ),
        (
            "rvf_detached_thread_launch_failed_releases_lock",
            lambda: test_rvf_detached_thread_launch_failed_releases_lock(root / "detached-failed"),
        ),
        (
            "rvf_detached_thread_reclaims_stale_lock_when_session_dead",
            lambda: test_rvf_detached_thread_reclaims_stale_lock_when_session_dead(
                root / "detached-stale-reclaim"
            ),
        ),
        (
            "rvf_detached_thread_keeps_lock_when_session_alive",
            lambda: test_rvf_detached_thread_keeps_lock_when_session_alive(
                root / "detached-stale-alive"
            ),
        ),
        (
            "rvf_detached_thread_keeps_lock_on_clean_finish",
            lambda: test_rvf_detached_thread_keeps_lock_on_clean_finish(
                root / "detached-stale-clean"
            ),
        ),
        (
            "rvf_detached_thread_keeps_lock_when_tmux_probe_fails",
            lambda: test_rvf_detached_thread_keeps_lock_when_tmux_probe_fails(
                root / "detached-stale-probe-fail"
            ),
        ),
        (
            "rvf_detached_thread_run_with_timeout",
            lambda: test_rvf_detached_thread_run_with_timeout(),
        ),
        (
            "rvf_detached_thread_finalize_status_cli",
            lambda: test_rvf_detached_thread_finalize_status_cli(root / "detached-finalize"),
        ),
        (
            "dispatch_reviewers_detached_launch_wiring",
            lambda: test_dispatch_reviewers_detached_launch_wiring(root / "dispatch-detached"),
        ),
        (
            "dispatch_reviewers_detached_exports_codex_rvf_log_root",
            lambda: test_dispatch_reviewers_detached_exports_codex_rvf_log_root(
                root / "dispatch-detached-exports"
            ),
        ),
        (
            "dispatch_reviewers_detached_exports_current_task_and_native_session_identity",
            lambda: test_dispatch_reviewers_detached_exports_current_task_and_native_session_identity(
                root / "dispatch-detached-current-identity"
            ),
        ),
        (
            "dispatch_reviewers_detached_candidate_summary_ignores_stale_server_identity",
            lambda: test_dispatch_reviewers_detached_candidate_summary_ignores_stale_server_identity(
                root / "dispatch-detached-candidate-identity"
            ),
        ),
        (
            "dispatch_reviewers_detached_preserves_parent_harness_marker_and_candidate_summary",
            lambda: test_dispatch_reviewers_detached_preserves_parent_harness_marker_and_candidate_summary(
                root / "dispatch-detached-parent-harness-marker"
            ),
        ),
        (
            "dispatch_reviewers_wait_status_branches",
            lambda: test_dispatch_reviewers_wait_status_branches(root / "dispatch-wait"),
        ),
        (
            "decision_token_mint_and_legal_state_machine",
            lambda: test_decision_token_mint_and_legal_state_machine(root / "dts-legal-fsm"),
        ),
        (
            "decision_token_direct_minted_paths_to_ack_and_decline",
            lambda: test_decision_token_direct_minted_paths_to_ack_and_decline(root / "dts-direct-paths"),
        ),
        (
            "decision_token_illegal_transitions_rejected",
            lambda: test_decision_token_illegal_transitions_rejected(root / "dts-illegal"),
        ),
        (
            "decision_token_acknowledged_retry_and_rollback",
            lambda: test_decision_token_acknowledged_retry_and_rollback(root / "dts-retry-rollback"),
        ),
        (
            "decision_token_dedup_same_key_returns_existing",
            lambda: test_decision_token_dedup_same_key_returns_existing(root / "dts-dedup"),
        ),
        (
            "decision_token_declined_fingerprint_cooldown_write_query",
            lambda: test_decision_token_declined_fingerprint_cooldown_write_query(root / "dts-cooldown"),
        ),
        (
            "decision_token_mint_explicit_user_request_clears_declined_cooldown",
            lambda: test_decision_token_mint_explicit_user_request_clears_declined_cooldown(
                root / "dts-user-clears-cooldown"
            ),
        ),
        (
            "decision_token_mint_explicit_user_request_reports_override_even_when_cooldown_clear_fails",
            lambda: test_decision_token_mint_explicit_user_request_reports_override_even_when_cooldown_clear_fails(
                root / "dts-user-cooldown-clear-fails"
            ),
        ),
        (
            "decision_token_mint_explicit_user_request_is_exempt_from_circuit_breaker",
            lambda: test_decision_token_mint_explicit_user_request_is_exempt_from_circuit_breaker(
                root / "dts-user-breaker-exempt"
            ),
        ),
        (
            "decision_token_declined_session_scope_snooze_silences_content_change_within_same_scope",
            lambda: test_decision_token_declined_session_scope_snooze_silences_content_change_within_same_scope(
                root / "dts-session-scope-snooze-silences"
            ),
        ),
        (
            "decision_token_declined_session_scope_snooze_release_conditions",
            lambda: test_decision_token_declined_session_scope_snooze_release_conditions(
                root / "dts-session-scope-snooze-release"
            ),
        ),
        (
            "decision_token_explicit_user_request_clears_declined_session_scope_snooze",
            lambda: test_decision_token_explicit_user_request_clears_declined_session_scope_snooze(
                root / "dts-session-scope-snooze-explicit-clear"
            ),
        ),
        (
            "decision_token_declined_session_scope_snooze_releases_on_unreviewed_line_growth_alone",
            lambda: test_decision_token_declined_session_scope_snooze_releases_on_unreviewed_line_growth_alone(
                root / "decision-token-snooze-releases-on-line-growth-alone"
            ),
        ),
        (
            "decision_token_developer_prompt_rebase_rewrites_nothing_when_baseline_is_unchanged",
            lambda: test_decision_token_developer_prompt_rebase_rewrites_nothing_when_baseline_is_unchanged(
                root / "decision-token-snooze-rebase-unchanged-baseline"
            ),
        ),
        (
            "decision_token_declined_session_scope_snooze_read_modify_write_is_serialized_between_stop_and_user_prompt_submit",
            lambda: test_decision_token_declined_session_scope_snooze_read_modify_write_is_serialized_between_stop_and_user_prompt_submit(
                root / "decision-token-snooze-read-modify-write-serialized"
            ),
        ),
        (
            "decision_point_delivery_intent_read_merge_write_is_serialized_between_stop_receipt_and_user_prompt_submit_observation",
            lambda: test_decision_point_delivery_intent_read_merge_write_is_serialized_between_stop_receipt_and_user_prompt_submit_observation(
                root / "decision-point-delivery-intent-read-merge-write-serialized"
            ),
        ),
        (
            "decision_token_explicit_user_request_reports_it_overrode_agent_declined_session_scope_snooze",
            lambda: test_decision_token_explicit_user_request_reports_it_overrode_agent_declined_session_scope_snooze(
                root / "decision-token-explicit-user-request-overrode-session-scope-snooze"
            ),
        ),
        (
            "decision_token_developer_prompt_rebases_snooze_scope_expansion_baseline_without_releasing",
            lambda: test_decision_token_developer_prompt_rebases_snooze_scope_expansion_baseline_without_releasing(
                root / "dts-session-scope-snooze-developer-prompt-rebase"
            ),
        ),
        (
            "decision_token_mint_circuit_breaker_window",
            lambda: test_decision_token_mint_circuit_breaker_window(root / "dts-breaker"),
        ),
        (
            "decision_token_ttl_expiry_and_run_dir_revival",
            lambda: test_decision_token_ttl_expiry_and_run_dir_revival(root / "dts-revival"),
        ),
        (
            "decision_token_bypass_lost_expired_states",
            lambda: test_decision_token_bypass_lost_expired_states(root / "dts-bypass"),
        ),
        (
            "decision_token_read_missing_and_corrupt",
            lambda: test_decision_token_read_missing_and_corrupt(root / "dts-read-robust"),
        ),
        # --- S3：rvf-agent 决策 CLI（单一入口） ---
        (
            "agent_cli_start_success_chain_and_auto_scope",
            lambda: test_agent_cli_start_success_chain_and_auto_scope(root / "agent-cli-start-ok"),
        ),
        (
            "agent_cli_start_failure_rollback_and_retry",
            lambda: test_agent_cli_start_failure_rollback_and_retry(root / "agent-cli-start-retry"),
        ),
        (
            "agent_cli_start_preflight_and_provider_health_gates",
            lambda: test_agent_cli_start_preflight_and_provider_health_gates(root / "agent-cli-gates"),
        ),
        (
            "agent_cli_scope_operations_and_state_guard",
            lambda: test_agent_cli_scope_operations_and_state_guard(root / "agent-cli-scope"),
        ),
        (
            "agent_cli_scope_issue_ledger_threshold_idempotent",
            lambda: test_agent_cli_scope_issue_ledger_threshold_idempotent(root / "agent-cli-issue"),
        ),
        (
            "agent_cli_dispatch_finalizes_scope_and_records_strictness",
            lambda: test_agent_cli_dispatch_finalizes_scope_and_records_strictness(root / "agent-cli-dispatch"),
        ),
        (
            "agent_cli_dispatch_blocks_on_uncovered_candidates_until_claim_exclude_or_accept",
            lambda: test_agent_cli_dispatch_blocks_on_uncovered_candidates_until_claim_exclude_or_accept(
                root / "agent-cli-dispatch-uncovered-gate"
            ),
        ),
        (
            "agent_cli_start_arms_in_flight_receipt_and_returns_execution_handoff",
            lambda: test_agent_cli_start_arms_in_flight_receipt_and_returns_execution_handoff(
                root / "agent-cli-start-receipt"
            ),
        ),
        (
            "agent_cli_start_consumes_review_reopen_marker_before_allocate",
            lambda: test_agent_cli_start_consumes_review_reopen_marker_before_allocate(
                root / "agent-cli-start-reopen"
            ),
        ),
        (
            "agent_cli_dispatch_refuses_while_scope_of_work_is_still_the_hook_written_stub",
            lambda: test_agent_cli_dispatch_refuses_while_scope_of_work_is_still_the_hook_written_stub(
                root / "agent-cli-dispatch-stub-gate"
            ),
        ),
        (
            "agent_cli_dispatch_separates_blocked_on_reviewer_request_from_failure",
            lambda: test_agent_cli_dispatch_separates_blocked_on_reviewer_request_from_failure(
                root / "agent-cli-dispatch-request"
            ),
        ),
        (
            "default_reviewer_dispatcher_speaks_detached_execute_contract",
            lambda: test_default_reviewer_dispatcher_speaks_detached_execute_contract(
                root / "agent-cli-dispatcher-detached"
            ),
        ),
        (
            "kanban_followup_lock_key_separates_colliding_task_ids_across_repositories",
            lambda: test_kanban_followup_lock_key_separates_colliding_task_ids_across_repositories(
                root / "followup-lock-repo-key"
            ),
        ),
        (
            "agent_cli_decline_persists_reason_and_cooldown",
            lambda: test_agent_cli_decline_persists_reason_and_cooldown(root / "agent-cli-decline"),
        ),
        (
            "agent_cli_decline_dispatched_review_clears_lock_and_optionally_waives",
            lambda: test_agent_cli_decline_dispatched_review_clears_lock_and_optionally_waives(
                root / "agent-cli-decline-dispatched"
            ),
        ),
        (
            "agent_cli_error_paths_missing_and_terminal_tokens",
            lambda: test_agent_cli_error_paths_missing_and_terminal_tokens(root / "agent-cli-errors"),
        ),
        # --- RVF 降本 T1：`rvf` 快捷入口（执行段减停） ---
        (
            "rvf_shortcut_run_context_resolution_priority",
            lambda: test_rvf_shortcut_run_context_resolution_priority(root / "rvf-shortcut-context"),
        ),
        (
            "rvf_shortcut_infers_decision_token_for_start_and_dispatch",
            lambda: test_rvf_shortcut_infers_decision_token_for_start_and_dispatch(root / "rvf-shortcut-token"),
        ),
        (
            "rvf_shortcut_results_summary_reads_canonical_legs_only",
            lambda: test_rvf_shortcut_results_summary_reads_canonical_legs_only(root / "rvf-shortcut-results"),
        ),
        (
            "rvf_results_summary_marks_outside_scope_and_excluded_path_issues_without_invalidating_the_leg",
            lambda: test_rvf_results_summary_marks_outside_scope_and_excluded_path_issues_without_invalidating_the_leg(
                root / "rvf-results-outside-scope"
            ),
        ),
        (
            "rvf_backlog_submit_forwards_to_submission_script_and_renders_handoff_section",
            lambda: test_rvf_backlog_submit_forwards_to_submission_script_and_renders_handoff_section(
                root / "rvf-backlog-submit"
            ),
        ),
        (
            "rvf_shortcut_wait_mode_and_default_duration_by_host",
            lambda: test_rvf_shortcut_wait_mode_and_default_duration_by_host(root / "rvf-shortcut-wait-mode"),
        ),
        (
            "rvf_shortcut_fix_prepare_stop_apply_all_stopped_end_to_end",
            lambda: test_rvf_shortcut_fix_prepare_stop_apply_all_stopped_end_to_end(root / "rvf-shortcut-fix-loop"),
        ),
        (
            "rvf_shortcut_redispatch_reviewers_argv_carries_current_run_dir",
            lambda: test_rvf_shortcut_redispatch_reviewers_argv_carries_current_run_dir(root / "rvf-shortcut-redispatch"),
        ),
        (
            "rvf_command_line_shortcut_shim_install_and_invocation_resolution",
            lambda: test_rvf_command_line_shortcut_shim_install_and_invocation_resolution(root / "rvf-shortcut-shim"),
        ),
        (
            "current_agent_session_id_reads_claude_code_session_id",
            lambda: test_current_agent_session_id_reads_claude_code_session_id(root / "rvf-shortcut-session-id"),
        ),
        (
            "agent_cli_start_receipt_renders_intent_status_and_ordered_next_steps",
            lambda: test_agent_cli_start_receipt_renders_intent_status_and_ordered_next_steps(root / "agent-cli-start-receipt"),
        ),
        # --- T1/T2/T3 集成审计修复包 F2：执行段 ---
        (
            "rvf_wait_awaiting_marker_keeps_stop_silent_while_waiting_and_reengage_returns_after_exit_kill_or_ttl",
            lambda: test_rvf_wait_awaiting_marker_keeps_stop_silent_while_waiting_and_reengage_returns_after_exit_kill_or_ttl(
                root / "rvf-wait-awaiting-marker-stop-e2e"
            ),
        ),
        (
            "rvf_wait_awaiting_marker_armed_only_on_hosts_that_wake_main_agent_after_background_work",
            lambda: test_rvf_wait_awaiting_marker_armed_only_on_hosts_that_wake_main_agent_after_background_work(
                root / "rvf-wait-awaiting-marker-host-gate"
            ),
        ),
        (
            "rvf_fix_prepare_arms_awaiting_marker_per_attempt_and_full_stop_command_works_from_any_cwd",
            lambda: test_rvf_fix_prepare_arms_awaiting_marker_per_attempt_and_full_stop_command_works_from_any_cwd(
                root / "rvf-fix-prepare-awaiting-marker"
            ),
        ),
        (
            "rvf_validate_inside_fix_attempt_worktree_or_suppressed_subprocess_stays_off_main_run",
            lambda: test_rvf_validate_inside_fix_attempt_worktree_or_suppressed_subprocess_stays_off_main_run(
                root / "rvf-validate-off-main-run"
            ),
        ),
        (
            "rvf_land_passthrough_commit_defaults_to_most_recent_prepare_run_of_this_repository",
            lambda: test_rvf_land_passthrough_commit_defaults_to_most_recent_prepare_run_of_this_repository(
                root / "rvf-land-passthrough"
            ),
        ),
        (
            "rvf_apply_orders_attempts_by_created_at_without_baseline_patch_mtime_fallback",
            lambda: test_rvf_apply_orders_attempts_by_created_at_without_baseline_patch_mtime_fallback(
                root / "rvf-apply-created-at-order"
            ),
        ),
        (
            "rvf_receipts_carry_explicit_run_dir_only_when_current_session_is_unresolvable",
            lambda: test_rvf_receipts_carry_explicit_run_dir_only_when_current_session_is_unresolvable(
                root / "rvf-receipts-explicit-run-dir"
            ),
        ),
        (
            "rvf_execution_segment_docs_teach_rvf_validate_background_timeout_and_no_polling_protocol",
            lambda: test_rvf_execution_segment_docs_teach_rvf_validate_background_timeout_and_no_polling_protocol(
                root / "rvf-execution-segment-docs"
            ),
        ),
    ]


def selected_test_cases(
    cases: list[tuple[str, object]],
    *,
    shard_count: int,
    shard_index: int,
) -> list[tuple[str, object]]:
    if shard_count <= 1:
        return cases
    return [case for index, case in enumerate(cases) if index % shard_count == shard_index]


def _timing_sink() -> Path | None:
    raw = os.environ.get("RVF_TEST_TIMING_JSONL")
    return Path(raw) if raw else None


def _record_timing(sink: Path, name: str, duration_ms: int, status: str) -> None:
    record = {"name": name, "duration_ms": duration_ms, "status": status}
    sink.parent.mkdir(parents=True, exist_ok=True)
    with sink.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        )


def _run_case_by_name(
    root_str: str, name: str
) -> tuple[str, bool, str | None, int]:
    """Worker: rebuild the registry (lambdas aren't picklable) and run
    one case by name in an isolated process."""
    cases = dict(review_support_test_cases(Path(root_str)))
    started = time.perf_counter()
    try:
        cases[name]()
        ok, tb = True, None
    except BaseException:
        ok, tb = False, traceback.format_exc()
    duration_ms = int((time.perf_counter() - started) * 1000)
    return name, ok, tb, duration_ms


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="parallel worker processes (default 1 = serial; the "
        "contract orchestrator never sets this). Best-effort dev "
        "accelerator: keep jobs <= cpu/2 — workers also spawn "
        "subprocess children, and a few reviewer tests use a fixed "
        "idle timeout that can starve under oversubscription.",
    )
    args = parser.parse_args()
    if args.shard_count < 1:
        raise SystemExit("--shard-count must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise SystemExit("--shard-index must be in [0, shard-count)")
    if args.jobs < 1:
        raise SystemExit("--jobs must be >= 1")
    if args.jobs > 1:
        _cpu = os.cpu_count() or 2
        if args.jobs * 2 > _cpu:
            print(
                f"warning: --jobs {args.jobs} oversubscribes ~{_cpu} cores "
                f"(workers spawn subprocess children); timeout-coupled "
                f"reviewer tests may flake. Recommended: --jobs <= {_cpu // 2}.",
                file=sys.stderr,
            )

    # 分片之前无条件跑一次完整性守卫：注册表 = 唯一索引，任何 def test_* 漏登记即红。
    # 用任意 root 构造未分片的全量注册表即可（lambda 不被调用，root 值无关）。
    assert_every_defined_test_is_registered(
        globals(),
        registered_names_from_case_tuples(review_support_test_cases(Path("/"))),
        source_path=__file__,
        intentionally_unregistered=INTENTIONALLY_UNREGISTERED,
    )

    suffix = (
        f" shard {args.shard_index + 1}/{args.shard_count}"
        if args.shard_count > 1
        else ""
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        # 注意：``RVF_HOME`` 的强制隔离**不在这里**，而在本文件模块导入期（见文件顶部
        # ``_ISOLATED_RVF_HOME_FOR_TESTS``）。放在 ``main()`` 里只能盖住手写 runner 这一条
        # 入口，bare pytest 直接收集本文件时不经过 ``main()`` ⇒ 隔离整个失效。别再往回搬。
        cases = selected_test_cases(
            review_support_test_cases(root),
            shard_count=args.shard_count,
            shard_index=args.shard_index,
        )
        timing_sink = _timing_sink()

        if args.jobs <= 1:
            for name, test_case in cases:
                if timing_sink is None:
                    test_case()
                    continue
                started = time.perf_counter()
                status = "completed"
                try:
                    test_case()
                except BaseException:
                    status = "failed"
                    raise
                finally:
                    duration_ms = int((time.perf_counter() - started) * 1000)
                    _record_timing(timing_sink, name, duration_ms, status)
        else:
            results: dict[str, tuple[bool, str | None, int]] = {}
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=args.jobs
            ) as pool:
                futures = [
                    pool.submit(_run_case_by_name, str(root), name)
                    for name, _ in cases
                ]
                for fut in concurrent.futures.as_completed(futures):
                    name, ok, tb, duration_ms = fut.result()
                    results[name] = (ok, tb, duration_ms)
            # Report/record in original registry order so failures and
            # the timing JSONL are deterministic regardless of completion
            # order.
            if timing_sink is not None:
                for name, _ in cases:
                    ok, _tb, duration_ms = results[name]
                    _record_timing(
                        timing_sink,
                        name,
                        duration_ms,
                        "completed" if ok else "failed",
                    )
            for name, _ in cases:
                ok, tb, _duration_ms = results[name]
                if not ok:
                    raise AssertionError(
                        f"test case {name!r} failed:\n{tb}"
                    )
    print(f"review support script tests OK{suffix}")
    return 0



# Bounded split: the Slice-3 allocator/lease/tracker tests live in a
# submodule; inject the shared deps then bind the 54 test names so the
# (unchanged) registry lambdas resolve them. Registry order / shard
# identity are preserved.
from _rvf_review_support import allocator_lease_tracker as _alt
_alt.inject(
    run=run,
    read_jsonl=read_jsonl,
    init_repo=init_repo,
    write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse=write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse,
    HANG_PREVENTION_ACTIVITY_PROBE_TIMEOUT_SECONDS=HANG_PREVENTION_ACTIVITY_PROBE_TIMEOUT_SECONDS,
    write_alternative_reviewer_config_with_idle_timeout_as_asserted_timeout_behavior=write_alternative_reviewer_config_with_idle_timeout_as_asserted_timeout_behavior,
    clean_review_result_python=clean_review_result_python,
    load_diff_tracker_module=load_diff_tracker_module,
    _slice_2b_repo_with_two_dirty=_slice_2b_repo_with_two_dirty,
    _slice_2b_prepare=_slice_2b_prepare,
    ROOT=ROOT,
    SCRIPT_DIR=SCRIPT_DIR,
    DIFF_TRACKER=DIFF_TRACKER,
    PREPARE_REVIEW_RUN=PREPARE_REVIEW_RUN,
    RUN_ALTERNATIVE_REVIEWER=RUN_ALTERNATIVE_REVIEWER,
)
globals().update({_n: getattr(_alt, _n) for _n in _alt.__all__})


# 有界拆分：rvf_user_prompt_submit hook 测试簇移入子模块；inject 共享依赖后重绑 22 个测试名
# （S5 退役 2 个 manual packet/prepare 用例，新增 6 个 manual 决策点 + bootstrap 废除用例），
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import rvf_user_prompt_submit_hook as _ups
_ups.inject(
    run=run,
    read_jsonl=read_jsonl,
    init_repo=init_repo,
    load_rvf_user_prompt_submit_module=load_rvf_user_prompt_submit_module,
    load_rvf_prep_file_module=load_rvf_prep_file_module,
    load_kanban_followup_lock_module=load_kanban_followup_lock_module,
    _committed_round_repo=_committed_round_repo,
    _round_baseline_committed_modules=_round_baseline_committed_modules,
    RVF_USER_PROMPT_SUBMIT=RVF_USER_PROMPT_SUBMIT,
    SCRIPT_DIR=SCRIPT_DIR,
)
globals().update({_n: getattr(_ups, _n) for _n in _ups.__all__})


# 有界拆分：alternative reviewer CLI 适配器行为（codex/cursor/claude 流式、空闲、路由、预检） 测试簇移入子模块；inject 共享依赖后重绑 38 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import alternative_reviewer_cli_adapter_behaviors as _altrev
_altrev.inject(
    RUN_ALTERNATIVE_REVIEWER=RUN_ALTERNATIVE_REVIEWER,
    clean_review_result_python=clean_review_result_python,
    init_repo=init_repo,
    load_alternative_reviewer_module=load_alternative_reviewer_module,
    read_jsonl=read_jsonl,
    run=run,
    write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse=write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse,
    HANG_PREVENTION_ACTIVITY_PROBE_TIMEOUT_SECONDS=HANG_PREVENTION_ACTIVITY_PROBE_TIMEOUT_SECONDS,
    write_alternative_reviewer_config_with_idle_timeout_as_asserted_timeout_behavior=write_alternative_reviewer_config_with_idle_timeout_as_asserted_timeout_behavior,
    _cursor_tool_call_line=_cursor_tool_call_line,
)
globals().update({_n: getattr(_altrev, _n) for _n in _altrev.__all__})


# 有界拆分：cline-kanban client 与通知 测试簇移入子模块；inject 共享依赖后重绑 15 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import cline_kanban_client_and_notify as _clkb
_clkb.inject(
    run=run,
    init_repo=init_repo,
    load_cline_kanban_client_module=load_cline_kanban_client_module,
    CLINE_KANBAN_CLIENT=CLINE_KANBAN_CLIENT,
)
globals().update({_n: getattr(_clkb, _n) for _n in _clkb.__all__})


# 有界拆分：session manifest 记录与 ownership claims 测试簇移入子模块；inject 共享依赖后重绑 13 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import session_manifest_records_and_claims as _sessman
_sessman.inject(
    run=run,
    init_repo=init_repo,
    write_codex_transcript=write_codex_transcript,
    load_diff_tracker_module=load_diff_tracker_module,
    SESSION_MANIFEST=SESSION_MANIFEST,
)
globals().update({_n: getattr(_sessman, _n) for _n in _sessman.__all__})


# 有界拆分：dispatch reviewers 选路与 detached 派发 测试簇移入子模块；inject 共享依赖后重绑 13 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import dispatch_reviewers_routing_and_detached as _disprev
_disprev.inject(
    load_dispatch_reviewers_module=load_dispatch_reviewers_module,
    load_harness_limit_cooldown_module=load_harness_limit_cooldown_module,
    write_fake_tmux_script=write_fake_tmux_script,
    write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse=write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse,
    HANG_PREVENTION_ACTIVITY_PROBE_TIMEOUT_SECONDS=HANG_PREVENTION_ACTIVITY_PROBE_TIMEOUT_SECONDS,
    write_alternative_reviewer_config_with_idle_timeout_as_asserted_timeout_behavior=write_alternative_reviewer_config_with_idle_timeout_as_asserted_timeout_behavior,
    _dispatch_registry=_dispatch_registry,
    _make_usage_or_clean_registry=_make_usage_or_clean_registry,
    _with_cooldown_env=_with_cooldown_env,
    init_repo=init_repo,
    SCRIPT_DIR=SCRIPT_DIR,
    # detached 幂等锁的 staleness 现场构造器：dispatch 侧「非完成态终态退出码 ⇒ 锁可重夺」
    # 的闭合断言要用（退出码语义归 dispatch_reviewers.py 所有，故断言留在本簇）。
    load_rvf_detached_thread_module=load_rvf_detached_thread_module,
    _seed_detached_stale_lock=_seed_detached_stale_lock,
    _launch_detached_with_staleness_env=_launch_detached_with_staleness_env,
)
globals().update({_n: getattr(_disprev, _n) for _n in _disprev.__all__})


# 有界拆分：review packet 组装与省略规则 测试簇移入子模块；inject 共享依赖后重绑 12 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import build_packet_assembly_and_omissions as _bldpkt
_bldpkt.inject(
    init_repo=init_repo,
    run=run,
    write_codex_transcript=write_codex_transcript,
    load_diff_tracker_module=load_diff_tracker_module,
    BUILD_PACKET=BUILD_PACKET,
    SESSION_MANIFEST=SESSION_MANIFEST,
)
globals().update({_n: getattr(_bldpkt, _n) for _n in _bldpkt.__all__})


# 有界拆分：rvf detached 线程生命周期 测试簇移入子模块；inject 共享依赖后重绑 9 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import rvf_detached_thread_lifecycle as _detthread
_detthread.inject(
    load_rvf_detached_thread_module=load_rvf_detached_thread_module,
    write_realexec_tmux_script=write_realexec_tmux_script,
    write_fake_tmux_script=write_fake_tmux_script,
    _detached_status_payload=_detached_status_payload,
    _seed_detached_stale_lock=_seed_detached_stale_lock,
    _launch_detached_with_staleness_env=_launch_detached_with_staleness_env,
    SCRIPT_DIR=SCRIPT_DIR,
)
globals().update({_n: getattr(_detthread, _n) for _n in _detthread.__all__})


# 有界拆分：diff tracker 注册与 scope 测试簇移入子模块；inject 共享依赖后重绑 9 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import diff_tracker_register_and_scope as _difftrk
_difftrk.inject(
    run=run,
    read_jsonl=read_jsonl,
    init_repo=init_repo,
    load_diff_tracker_module=load_diff_tracker_module,
    _round_baseline_committed_modules=_round_baseline_committed_modules,
    _committed_round_repo=_committed_round_repo,
    SCRIPT_DIR=SCRIPT_DIR,
    ROOT=ROOT,
)
globals().update({_n: getattr(_difftrk, _n) for _n in _difftrk.__all__})


# 有界拆分：prepare review run 与 worktree bootstrap 测试簇移入子模块；inject 共享依赖后重绑 8 个测试名，
# 让（未改动的）注册表 lambda 解析到它们。注册顺序 / 分片身份保持不变。
from _rvf_review_support import prepare_review_run_bootstrap as _preprev
_preprev.inject(
    run=run_with_independent_review_start_boundary,
    init_repo=init_repo,
    write_codex_transcript=write_codex_transcript,
    PREPARE_REVIEW_RUN=PREPARE_REVIEW_RUN,
    COMMAND_LOCK=COMMAND_LOCK,
    APPLY_WORKTREE_BOOTSTRAP=APPLY_WORKTREE_BOOTSTRAP,
    # pre-review local-base bootstrap 等待生命周期簇所需（进程内直调 prepare / gate / 入口 A CLI）。
    load_prepare_review_run_module=load_prepare_review_run_module,
    load_rvf_bootstrap_gate_module=load_rvf_bootstrap_gate_module,
    load_rvf_prep_file_module=load_rvf_prep_file_module,
    load_rvf_decision_token_store_module=load_rvf_decision_token_store_module,
    load_rvf_agent_decision_cli_module=load_rvf_agent_decision_cli_module,
    load_rvf_user_prompt_submit_module=load_rvf_user_prompt_submit_module,
    load_kanban_followup_lock_module=load_kanban_followup_lock_module,
    _agent_cli_roots=_agent_cli_roots,
    _mint_agent_decision_token=_mint_agent_decision_token,
)
globals().update({_n: getattr(_preprev, _n) for _n in _preprev.__all__})
# RVF 降本 T1：`rvf` 快捷入口测试簇（上下文推断 / wait 摘要 / fix 循环批量化 / shim）。
from _rvf_review_support import rvf_command_line_shortcut_entrypoint_behaviors as _rvfshortcut
_rvfshortcut.inject(
    ROOT=ROOT,
    SCRIPT_DIR=SCRIPT_DIR,
    load_rvf_agent_decision_cli_module=load_rvf_agent_decision_cli_module,
    load_rvf_decision_token_store_module=load_rvf_decision_token_store_module,
    _agent_cli_roots=_agent_cli_roots,
    _mint_agent_decision_token=_mint_agent_decision_token,
    _agent_cli_stub_prepare=_agent_cli_stub_prepare,
    _agent_cli_worktree=_agent_cli_worktree,
    _fixed_utc=_fixed_utc,
)
globals().update({_n: getattr(_rvfshortcut, _n) for _n in _rvfshortcut.__all__})
# 修复包 F4：谢绝分型 / 撤回自己的谢绝 / Kanban task 首轮分支点 bootstrap / 熔断不自我续命 / 回溯回放。
from _rvf_review_support import decline_kind_reopen_and_kanban_task_committed_round_bootstrap as _f4decline
_f4decline.inject(
    ROOT=ROOT,
    load_rvf_decision_token_store_module=load_rvf_decision_token_store_module,
    load_rvf_agent_decision_cli_module=load_rvf_agent_decision_cli_module,
    _decision_token_store_roots=_decision_token_store_roots,
    _fixed_utc=_fixed_utc,
    _candidate_summary_carrying_scope_identity=_candidate_summary_carrying_scope_identity,
    _agent_cli_roots=_agent_cli_roots,
    _agent_cli_worktree=_agent_cli_worktree,
    _mint_agent_decision_token=_mint_agent_decision_token,
)
globals().update({_n: getattr(_f4decline, _n) for _n in _f4decline.__all__})



if __name__ == "__main__":
    raise SystemExit(main())
