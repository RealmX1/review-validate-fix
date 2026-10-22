#!/usr/bin/env python3
"""dispatch reviewers 选路与 detached 派发 测试簇。

从 tests/test_review_support_scripts.py 有界抽出（导航用拆分，行为不变）。共享 helper/常量
（run/read_jsonl/load_*_module/路径常量等）仍归 aggregator 所有，经 inject() 在注册表运行前推入
本模块 globals，避免与 __main__ 脚本循环导入。注册表 lambda 不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from independent_review_start_boundary_fixture import environment_with_synthetic_independent_review_start_boundary

# 由 aggregator（tests/test_review_support_scripts.py）在导入后 inject 注入共享依赖。
__all__ = [
    'test_dispatch_reviewers_detached_launch_wiring',
    'test_dispatch_reviewers_detached_exports_codex_rvf_log_root',
    'test_dispatch_reviewers_detached_exports_current_task_and_native_session_identity',
    'test_dispatch_reviewers_detached_candidate_summary_ignores_stale_server_identity',
    'test_dispatch_reviewers_detached_preserves_parent_harness_marker_and_candidate_summary',
    'test_dispatch_reviewers_wait_status_branches',
    'test_dispatch_reviewers_routing_matrix',
    'test_dispatch_reviewers_pseudo_random_selection_seed_reproducible',
    'test_temporary_disable_window_generalized_component',
    'test_dispatch_reviewers_temporarily_disables_claude_code_external_reviewer',
    'test_dispatch_reviewers_same_harness_double_instance_distinct_ids',
    'test_dispatch_reviewers_plan_artifact_schema',
    'test_dispatch_reviewers_executes_two_external',
    'test_dispatch_reviewers_execute_backfills_review_env',
    'test_dispatch_reviewers_reroutes_on_usage_limit',
    'test_dispatch_reviewers_reroute_id_collision',
    'test_dispatch_reviewers_reroutes_on_leg_without_review_result',
    'test_dispatch_reviewers_failclose_when_leg_without_review_result_unfilled',
    'test_dispatch_reviewers_rc_zero_without_review_result_is_not_effective',
    'test_dispatch_reviewers_review_request_leg_is_pending_not_rerouted',
    'test_dispatch_reviewers_usage_limit_with_pending_request_is_not_all_usage_limit',
    'test_dispatch_reviewers_blocked_on_reviewer_request_exit_code_frees_detached_lock',
    'test_dispatch_reviewers_failclose_mixed_usage_limit_and_auth_failure',
    'test_dispatch_reviewers_wait_status_surfaces_plan_outcome',
    'test_dispatch_reviewers_probe_excludes_cooldown',
    'test_dispatch_reviewers_failclose_when_main_exhausted',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_dispatch_reviewers_detached_launch_wiring(root: Path) -> None:
    """--execute --detached：self-fork dispatch_reviewers.py --execute 进 tmux，立即返回 status 路径。"""
    import contextlib
    import io

    d = load_dispatch_reviewers_module()
    root.mkdir(parents=True, exist_ok=True)
    fake_tmux = write_fake_tmux_script(root / "tmux.py")
    calls = root / "calls.jsonl"
    run_dir = root / "runs" / "rvf-disp-unit"
    reviewers_dir = run_dir / "artifacts" / "reviewers"
    reviewers_dir.mkdir(parents=True, exist_ok=True)

    class _Ledger:
        run_id = "rvf-disp-unit"

        def __init__(self, rd: Path) -> None:
            self.run_dir = rd

        def env(self) -> dict:
            return {"RVF_RUN_ID": self.run_id}

        def event(self, **_kw) -> None:
            pass

    args = argparse.Namespace(
        registry="reg.json",
        probe_mode="preflight",
        probe_timeout=60.0,
        main_harness="auto",
        transcript=None,
        main_harness_file=None,
        assume_available=None,
        require_external=False,
        total_timeout=2700.0,
    )
    saved = {
        k: os.environ.get(k)
        for k in ("RVF_TMUX_BIN", "FAKE_TMUX_CALLS", "FAKE_TMUX_RETURNCODE")
    }
    os.environ["RVF_TMUX_BIN"] = str(fake_tmux)
    os.environ["FAKE_TMUX_CALLS"] = str(calls)
    os.environ["FAKE_TMUX_RETURNCODE"] = "0"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = d.launch_detached_dispatch(
                args,
                _Ledger(run_dir),
                reviewers_dir,
                repo="/repo",
                review_packet="/pkt",
                session_context="/sow",
                scope_contract="/sc",
            )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    out = buf.getvalue()
    assert rc == 0, out
    assert "RVF_DISPATCH_STATUS=" in out and "RVF_DISPATCH_LAUNCH=launched" in out
    status = json.loads(
        (reviewers_dir / ".dispatch-thread.status.json").read_text(encoding="utf-8")
    )
    assert status["launch_status"] == "launched"
    assert status["tmux_session"] == "rvf-dispatch-rvf-disp-unit"
    child = status["command"]
    assert "--execute" in child and "--detached" not in child
    assert "--rvf-run-id" in child and "rvf-disp-unit" in child
    assert "--rvf-run-dir" in child and "--repo" in child and "/repo" in child
    recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert len(recorded) == 1
    assert recorded[0]["argv"][:4] == [
        "new-session",
        "-d",
        "-s",
        "rvf-dispatch-rvf-disp-unit",
    ]


def test_dispatch_reviewers_detached_exports_codex_rvf_log_root(root: Path) -> None:
    """FU-3：detached 派发把 ``ledger.env()``（含 CODEX_RVF_LOG_ROOT）显式写进 tmux 内层
    wrapper shell 的 ``export X=Y;`` 行——reviewer 子进程不再依赖 tmux server 的 env 继承，
    其 diff-tracker DB 与 prepare 写 lease 的库一致，消除 lease_not_found。"""
    import contextlib
    import io

    d = load_dispatch_reviewers_module()
    root.mkdir(parents=True, exist_ok=True)
    fake_tmux = write_fake_tmux_script(root / "tmux.py")
    calls = root / "calls.jsonl"
    run_dir = root / "runs" / "rvf-disp-unit"
    reviewers_dir = run_dir / "artifacts" / "reviewers"
    reviewers_dir.mkdir(parents=True, exist_ok=True)
    log_root = root / "rvf-log-root"

    class _Ledger:
        run_id = "rvf-disp-unit"

        def __init__(self, rd: Path) -> None:
            self.run_dir = rd

        def env(self) -> dict:
            # 模拟 RunLedger.env()：含决定 diff-tracker DB 落点的 CODEX_RVF_LOG_ROOT。
            return {
                "RVF_RUN_ID": self.run_id,
                "CODEX_RVF_LOG_ROOT": str(log_root),
                "RVF_RUN_DIR": str(self.run_dir),
            }

        def event(self, **_kw) -> None:
            pass

    args = argparse.Namespace(
        registry="reg.json",
        probe_mode="preflight",
        probe_timeout=60.0,
        main_harness="auto",
        transcript=None,
        main_harness_file=None,
        assume_available=None,
        require_external=False,
        total_timeout=2700.0,
    )
    saved = {
        k: os.environ.get(k)
        for k in ("RVF_TMUX_BIN", "FAKE_TMUX_CALLS", "FAKE_TMUX_RETURNCODE")
    }
    os.environ["RVF_TMUX_BIN"] = str(fake_tmux)
    os.environ["FAKE_TMUX_CALLS"] = str(calls)
    os.environ["FAKE_TMUX_RETURNCODE"] = "0"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = d.launch_detached_dispatch(
                args,
                _Ledger(run_dir),
                reviewers_dir,
                repo="/repo",
                review_packet="/pkt",
                session_context="/sow",
                scope_contract="/sc",
            )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert rc == 0, buf.getvalue()
    recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert len(recorded) == 1
    # tmux new-session -d -s <name> <shell>：内层 wrapper shell 是最后一个参数。
    shell_command = recorded[0]["argv"][-1]
    # 关键断言：CODEX_RVF_LOG_ROOT 必须以正确的值显式 export 进内层 shell。
    assert (
        f"export CODEX_RVF_LOG_ROOT={shlex.quote(str(log_root))};" in shell_command
    ), shell_command
    assert f"export RVF_RUN_DIR={shlex.quote(str(run_dir))};" in shell_command


def test_dispatch_reviewers_detached_exports_current_task_and_native_session_identity(root: Path) -> None:
    """当前身份与缺失项都须显式覆盖 server 环境；RVF run 环境仍以 ledger 为准。"""
    import contextlib
    import io
    from types import SimpleNamespace
    from unittest.mock import patch

    d = load_dispatch_reviewers_module()
    from core.host_adapter.current_agent_session_identity_from_environment import (
        CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES,
    )
    from core.run_ledger.run_ledger import RunLedger

    identity_names = (
        "KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID",
        *CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES,
        "CLAUDECODE", "CURSOR_AGENT", "CODEX_RVF_CLINE_KANBAN_AGENT_ID", "AI_AGENT",
    )
    ledger = SimpleNamespace(
        run_id="synthetic-current-run", correlation_id="synthetic-current-correlation",
        root=root / "日志 'with quotes'", run_dir=root / "synthetic-run",
        event=lambda **kwargs: None,
    )
    ledger.env = lambda: RunLedger.env(ledger)
    identity_cases = [{name: "当前身份 'quoted'"} for name in identity_names]
    identity_cases += [{name: f"current-{name}" for name in identity_names}, {}]
    for index, current_identity in enumerate(identity_cases):
        environment = {name: value for name, value in os.environ.items() if name not in identity_names}
        environment.update(current_identity, RVF_RUN_ID="stale-parent-run", UNRELATED_ENVIRONMENT_VALUE="not-an-export")
        with patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(io.StringIO()):
            with patch.object(d, "_build_detached_child_argv", return_value=[sys.executable, "-c", "pass"]), patch.object(
                d, "launch_detached", return_value={"launch_status": "launched"},
            ) as launched:
                assert d.launch_detached_dispatch(
                    argparse.Namespace(total_timeout=30), ledger, root / str(index),
                    repo="/synthetic-repository", review_packet=None, session_context=None,
                    scope_contract="/synthetic-scope-contract",
                ) == 0
        arguments = launched.call_args.kwargs
        expected_exports = {**{name: current_identity.get(name, "") for name in identity_names}, **ledger.env()}
        assert arguments["exports"] == expected_exports
        assert all(arguments["launch_env"][name] == value for name, value in expected_exports.items())
        assert arguments["launch_env"]["UNRELATED_ENVIRONMENT_VALUE"] == "not-an-export"


def test_dispatch_reviewers_detached_candidate_summary_ignores_stale_server_identity(root: Path) -> None:
    """执行真实 wrapper，模拟 server 固定旧环境；候选必须与当前父环境的结果一致。"""
    import contextlib
    import io
    from types import SimpleNamespace
    from unittest.mock import patch

    d = load_dispatch_reviewers_module()
    from core.host_adapter.current_agent_session_identity_from_environment import (
        CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES,
    )
    from core.run_ledger.run_ledger import RunLedger

    identity_names = (
        "KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID",
        *CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES,
    )
    clean_environment = {
        name: value for name, value in os.environ.items()
        if name not in identity_names
        and not name.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_", "GIT_"))
    }
    clean_environment.update(
        RVF_HOME=str(root / "isolated-rvf-home"),
        RVF_USER_LOCAL_DATA_ROOT=str(root / "isolated-user-local-data"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    repository = root / "isolated-committed-task-repository"
    repository.mkdir(parents=True)

    def git(*arguments):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *arguments],
            cwd=repository, env=clean_environment, check=True, capture_output=True, text=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "隔离身份回归")
    git("config", "user.email", "detached-identity@example.invalid")
    (repository / "task-content.txt").write_text("base\n", encoding="utf-8")
    git("add", "task-content.txt")
    git("commit", "-qm", "test: 建立隔离基线")
    git("checkout", "-qb", "synthetic-task-branch")
    (repository / "task-content.txt").write_text("base\ntask change\n", encoding="utf-8")
    git("commit", "-qam", "test: 建立隔离任务提交")
    original_head = git("rev-parse", "HEAD")

    probe_source = (
        "import json,sys\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "import _rvf_pyroot\n"
        "from stop_decision_candidate_summary import build_candidate_summary\n"
        "from core.host_adapter.current_agent_session_identity_from_environment import resolve_current_agent_session_id_from_environment\n"
        "session=resolve_current_agent_session_id_from_environment()\n"
        "summary=build_candidate_summary(event={'session_id':session,'cwd':sys.argv[2]},cwd=sys.argv[2],repo=sys.argv[2],ledger=None)\n"
        "keys=('kanban_task_id','session_id','candidate_path_count','candidate_fingerprint','committed_round_baseline')\n"
        "print(json.dumps({'native_session':session,'summary':{key:summary.get(key) for key in keys}}))\n"
    )
    probe_argv = [sys.executable, "-B", "-c", probe_source, str(SCRIPT_DIR), str(repository)]
    server_environment_path = root / "fixed-tmux-server-environment.json"
    fake_tmux = root / "tmux-with-fixed-server-environment.py"
    fake_tmux.write_text(
        f"#!{sys.executable}\n"
        "import json,os,subprocess,sys\n"
        "from pathlib import Path\n"
        "environment=json.loads(Path(os.environ['FIXED_TMUX_SERVER_ENVIRONMENT_PATH']).read_text())\n"
        "raise SystemExit(subprocess.run(['/bin/sh','-c',sys.argv[-1]],env=environment).returncode)\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)
    current_identity_cases = [
        {"KANBAN_TASK_ID": "synthetic-task-a", "CODEX_THREAD_ID": "synthetic-session-a"},
        {"CLINE_KANBAN_TASK_ID": "synthetic-task-b", "CLAUDE_CODE_SESSION_ID": "synthetic-session-b"},
        {"KANBAN_HOOK_TASK_ID": "synthetic-task-c", "CODEX_SESSION_ID": "synthetic-session-c"},
        {"KANBAN_TASK_ID": "synthetic-priority-task", "CLINE_KANBAN_TASK_ID": "synthetic-lower-priority-task",
         "RVF_SESSION_ID": "synthetic-override-session", "CLAUDE_CODE_SESSION_ID": "synthetic-lower-priority-session",
         "CODEX_THREAD_ID": "synthetic-lower-priority-thread"},
        {},
    ]
    for server_has_stale_identity in (False, True):
        server_environment = dict(clean_environment)
        if server_has_stale_identity:
            server_environment.update({name: "synthetic-stale-server-identity" for name in identity_names})
        server_environment_path.write_text(json.dumps(server_environment), encoding="utf-8")
        for index, current_identity in enumerate(current_identity_cases):
            parent_environment = {**clean_environment, **current_identity}
            expected = json.loads(subprocess.run(
                probe_argv, env=parent_environment, check=True, capture_output=True, text=True,
            ).stdout)
            assert expected["summary"]["candidate_path_count"] == (1 if current_identity else 0)
            case_directory = root / f"server-stale-{server_has_stale_identity}-parent-{index}"
            ledger = SimpleNamespace(
                run_id=case_directory.name, correlation_id="synthetic-detached-identity",
                root=root / "isolated-log-root", run_dir=case_directory, event=lambda **kwargs: None,
            )
            ledger.env = lambda: RunLedger.env(ledger)
            parent_environment.update(
                RVF_TMUX_BIN=str(fake_tmux), FIXED_TMUX_SERVER_ENVIRONMENT_PATH=str(server_environment_path),
            )
            with patch.dict(os.environ, parent_environment, clear=True), patch.object(
                d, "_build_detached_child_argv", return_value=probe_argv,
            ), contextlib.redirect_stdout(io.StringIO()):
                assert d.launch_detached_dispatch(
                    argparse.Namespace(total_timeout=30), ledger, case_directory / "reviewers",
                    repo=str(repository), review_packet=None, session_context=None,
                    scope_contract="/synthetic-scope-contract",
                ) == 0
            reviewers_directory = case_directory / "reviewers"
            status = json.loads((reviewers_directory / d.DISPATCH_STATUS_FILENAME).read_text())
            log = (reviewers_directory / d.DISPATCH_LOG_FILENAME).read_text()
            assert status["returncode"] == 0, (status, log)
            assert json.loads(log.splitlines()[0]) == expected
    assert git("rev-parse", "HEAD") == original_head
    assert git("status", "--porcelain") == ""


def test_dispatch_reviewers_detached_preserves_parent_harness_marker_and_candidate_summary(root: Path) -> None:
    """真实 marker 按父 harness 选中；四个信号的传递与缺失清空都不能改变候选。"""
    import contextlib
    import io
    from types import SimpleNamespace
    from unittest.mock import patch

    d = load_dispatch_reviewers_module()
    from core.host_adapter.current_agent_session_identity_from_environment import (
        CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES,
    )
    from core.host_adapter.parent_harness_identity import resolve_parent_harness_identity
    from core.run_ledger.run_ledger import RunLedger
    from core.session_scope_allocation.round_baseline_marker import write_round_baseline_marker

    harness_signals = {
        "CLAUDECODE": "1",
        "CURSOR_AGENT": "1",
        "CODEX_RVF_CLINE_KANBAN_AGENT_ID": "codex",
        "AI_AGENT": "kimi_1-0_agent",
    }
    identity_names = (
        "KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID",
        *CURRENT_AGENT_SESSION_ID_ENVIRONMENT_VARIABLE_NAMES, *harness_signals,
    )
    log_root = root / "isolated-log-root"
    clean_environment = {
        name: value for name, value in os.environ.items()
        if name not in identity_names
        and not name.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_", "GIT_"))
    }
    clean_environment.update(
        RVF_HOME=str(root / "isolated-rvf-home"),
        RVF_USER_LOCAL_DATA_ROOT=str(root / "isolated-user-local-data"),
        CODEX_RVF_LOG_ROOT=str(log_root), PYTHONDONTWRITEBYTECODE="1",
    )
    repository = root / "isolated-harness-marker-repository"
    repository.mkdir(parents=True)

    def git(*arguments):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *arguments],
            cwd=repository, env=clean_environment, check=True, capture_output=True, text=True,
        ).stdout.strip()

    # 没有 main/base ref 的受支持路径直接消费 marker，task 分支点兜底不会掩盖错键。
    git("init", "-q", "-b", "synthetic-local-base")
    git("config", "user.name", "隔离 harness 回归")
    git("config", "user.email", "detached-harness@example.invalid")
    (repository / "task-content.txt").write_text("base\n", encoding="utf-8")
    git("add", "task-content.txt")
    git("commit", "-qm", "test: 建立隔离 marker 基线")
    baseline = git("rev-parse", "HEAD")
    git("checkout", "-qb", "synthetic-task-branch")
    (repository / "task-content.txt").write_text("base\ntask change\n", encoding="utf-8")
    git("commit", "-qam", "test: 建立隔离 harness 任务提交")
    original_head = git("rev-parse", "HEAD")

    probe_source = (
        "import json,os,sys\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "import _rvf_pyroot\n"
        "from stop_decision_candidate_summary import build_candidate_summary\n"
        "from core.host_adapter.parent_harness_identity import resolve_parent_harness_identity\n"
        "from core.session_scope_allocation.round_baseline_marker import read_round_baseline_marker\n"
        "session=os.environ['RVF_SESSION_ID']\n"
        "marker=read_round_baseline_marker(task_id=os.environ['KANBAN_TASK_ID'],session_id=session,repo=sys.argv[2])\n"
        "summary=build_candidate_summary(event={'session_id':session,'cwd':sys.argv[2]},cwd=sys.argv[2],repo=sys.argv[2],ledger=None)\n"
        "keys=('kanban_task_id','session_id','candidate_path_count','candidate_fingerprint','committed_round_baseline')\n"
        "print(json.dumps({'harness':resolve_parent_harness_identity(),'marker_path':marker['_marker_path'],'marker_baseline':marker['baseline_head'],'summary':{key:summary.get(key) for key in keys}}))\n"
    )
    probe_argv = [sys.executable, "-B", "-c", probe_source, str(SCRIPT_DIR), str(repository)]
    server_environment_path = root / "fixed-tmux-server-environment.json"
    fake_tmux = root / "tmux-with-fixed-harness-environment.py"
    fake_tmux.write_text(
        f"#!{sys.executable}\n"
        "import json,os,subprocess,sys\n"
        "from pathlib import Path\n"
        "environment=json.loads(Path(os.environ['FIXED_TMUX_SERVER_ENVIRONMENT_PATH']).read_text())\n"
        "raise SystemExit(subprocess.run(['/bin/sh','-c',sys.argv[-1]],env=environment).returncode)\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)
    for signal_name, signal_value in harness_signals.items():
        for parent_has_signal in (True, False):
            case_name = f"{signal_name}-parent-{'present' if parent_has_signal else 'absent'}"
            shared_identity = {"KANBAN_TASK_ID": case_name, "RVF_SESSION_ID": "synthetic-shared-session"}
            parent_environment = {**clean_environment, **shared_identity}
            server_environment = {**clean_environment, **shared_identity}
            (parent_environment if parent_has_signal else server_environment)[signal_name] = signal_value
            parent_harness = resolve_parent_harness_identity(environ=parent_environment)
            server_harness = resolve_parent_harness_identity(environ=server_environment)
            assert parent_harness != server_harness
            for harness, head in ((parent_harness, baseline), (server_harness, original_head)):
                write_round_baseline_marker(
                    task_id=case_name, session_id=shared_identity["RVF_SESSION_ID"],
                    baseline_head=head, repo=str(repository), parent_harness_identity=harness,
                    root=log_root,
                )
            expected = json.loads(subprocess.run(
                probe_argv, env=parent_environment, check=True, capture_output=True, text=True,
            ).stdout)
            stale = json.loads(subprocess.run(
                probe_argv, env=server_environment, check=True, capture_output=True, text=True,
            ).stdout)
            assert expected["marker_baseline"] == baseline
            assert expected["summary"]["committed_round_baseline"] == baseline
            assert expected["summary"]["candidate_path_count"] == 1
            assert stale["marker_baseline"] == original_head
            assert stale["summary"]["candidate_path_count"] == 0
            assert stale["summary"]["candidate_fingerprint"] != expected["summary"]["candidate_fingerprint"]
            server_environment_path.write_text(json.dumps(server_environment), encoding="utf-8")
            case_directory = root / case_name
            ledger = SimpleNamespace(
                run_id=case_name, correlation_id="synthetic-detached-harness",
                root=log_root, run_dir=case_directory, event=lambda **kwargs: None,
            )
            ledger.env = lambda: RunLedger.env(ledger)
            parent_environment.update(
                RVF_TMUX_BIN=str(fake_tmux), FIXED_TMUX_SERVER_ENVIRONMENT_PATH=str(server_environment_path),
            )
            with patch.dict(os.environ, parent_environment, clear=True), patch.object(
                d, "_build_detached_child_argv", return_value=probe_argv,
            ), contextlib.redirect_stdout(io.StringIO()):
                assert d.launch_detached_dispatch(
                    argparse.Namespace(total_timeout=30), ledger, case_directory / "reviewers",
                    repo=str(repository), review_packet=None, session_context=None,
                    scope_contract="/synthetic-scope-contract",
                ) == 0
            reviewers_directory = case_directory / "reviewers"
            status = json.loads((reviewers_directory / d.DISPATCH_STATUS_FILENAME).read_text())
            log = (reviewers_directory / d.DISPATCH_LOG_FILENAME).read_text()
            assert status["returncode"] == 0, (status, log)
            assert json.loads(log.splitlines()[0]) == expected, (case_name, expected, log)
    assert git("rev-parse", "HEAD") == original_head
    assert git("status", "--porcelain") == ""


def test_dispatch_reviewers_wait_status_branches(root: Path) -> None:
    """waiter 终态判定：running / done(finished_at) / done(launch_failed) / 缺文件→running。"""
    d = load_dispatch_reviewers_module()
    root.mkdir(parents=True, exist_ok=True)
    status_path = root / "s.json"

    status_path.write_text(
        json.dumps({"launch_status": "launched", "finished_at": None, "returncode": None}),
        encoding="utf-8",
    )
    running = d.wait_for_dispatch_status(status_path, max_wait=0.0)
    assert running["state"] == "running", running

    status_path.write_text(
        json.dumps({"launch_status": "launched", "finished_at": "t1", "returncode": 0}),
        encoding="utf-8",
    )
    done = d.wait_for_dispatch_status(status_path, max_wait=0.0)
    assert done["state"] == "done" and done["returncode"] == 0, done

    status_path.write_text(
        json.dumps({"launch_status": "launch_failed", "finished_at": None, "error": "boom"}),
        encoding="utf-8",
    )
    failed = d.wait_for_dispatch_status(status_path, max_wait=0.0)
    assert failed["state"] == "done" and failed["launch_status"] == "launch_failed", failed

    missing = d.wait_for_dispatch_status(root / "missing.json", max_wait=0.0)
    assert missing["state"] == "running", missing


def test_dispatch_reviewers_routing_matrix(root: Path) -> None:
    """路由矩阵 R0–R3（S7·R15/R20 后）：测试用例名 ↔ 规则 id。

    | 场景 | M | A | rule | slots |
    |------|---|---|------|-------|
    | R0 main=claude | claude_code | cursor,claude_code,codex | R0 | cursor,codex（受迫配对，保 priority 序） |
    | R0 main=codex  | codex       | cursor,claude_code,codex | R0 | cursor,claude_code（受迫配对） |
    | R0 main=cursor(override) | cursor | cursor,claude_code,codex | R0 | claude_code,codex |
    | R1 cursor+codex | claude_code | cursor,codex | R1 | cursor,codex |
    | R1 no-cursor | claude_code | claude_code,codex | R1 | claude_code,codex（旧 R4 修饰已退役，无 cursor_unavailable） |
    | R2 only==M | codex | codex | R2 | codex-cli-a,codex-cli-b |
    | R2 only!=M | claude_code | codex | R2 | codex-cli-a,codex-cli-b + mismatch |
    | R3 zero | claude_code | (none) | R3 | needs_last_resort_fallback |

    三路 Tier-1 registry 下非主池恒 ≤2，故本矩阵全部落受迫配对（selection_mode=
    forced_pair，无 seed）；伪随机选二的可复现性由
    ``test_dispatch_reviewers_pseudo_random_selection_seed_reproducible`` 覆盖。
    """
    d = load_dispatch_reviewers_module()
    reg = _dispatch_registry()
    after_temporary_override_expiry = datetime.datetime(
        2026, 7, 10, 12, 1, tzinfo=datetime.timezone.utc
    )

    def harnesses(plan):
        return [r["harness_id"] for r in plan["reviewers"]]

    def warns(plan):
        return {w["code"] for w in plan["warnings"]}

    # R0 — 两路非主（三路 registry：非主池恰 2 → 受迫配对，保 priority 序）
    p = d.route(
        "claude_code",
        ["cursor", "claude_code", "codex"],
        reg,
        now_utc=after_temporary_override_expiry,
    )
    assert p["routing_rule"] == "R0", p
    assert harnesses(p) == ["cursor", "codex"], p
    assert all(r["dispatch_mode"] == "external_cli" for r in p["reviewers"]), p
    assert p["status"] == "planned" and p["needs_last_resort_fallback"] is False
    # 选材遥测块：受迫配对不含 seed，池与选择一致可审计。
    assert p["reviewer_selection"]["selection_mode"] == "forced_pair", p
    assert p["reviewer_selection"]["selected_harnesses"] == ["cursor", "codex"], p
    assert "selection_seed" not in p["reviewer_selection"], p

    p = d.route(
        "codex",
        ["cursor", "claude_code", "codex"],
        reg,
        now_utc=after_temporary_override_expiry,
    )
    assert p["routing_rule"] == "R0" and harnesses(p) == ["cursor", "claude_code"], p

    # R0 主=cursor（仅显式覆盖可达）→ 两路非主，cursor 不占 slot
    p = d.route(
        "cursor",
        ["cursor", "claude_code", "codex"],
        reg,
        now_utc=after_temporary_override_expiry,
    )
    assert p["routing_rule"] == "R0" and set(harnesses(p)) == {"claude_code", "codex"}, p

    # R1 — 恰两路 external（含 cursor）
    p = d.route("claude_code", ["cursor", "codex"], reg, now_utc=after_temporary_override_expiry)
    assert p["routing_rule"] == "R1" and set(harnesses(p)) == {"cursor", "codex"}, p

    # R1 — cursor 不可用：仍两路 external（主以 external 跑）。旧 R4 cursor_unavailable
    # 修饰随「池内无偏序」退役（R15/R20）：cursor 缺席只是池组成，不再是降级 warning。
    p = d.route(
        "claude_code",
        ["claude_code", "codex"],
        reg,
        now_utc=after_temporary_override_expiry,
    )
    assert p["routing_rule"] == "R1" and set(harnesses(p)) == {"claude_code", "codex"}, p
    assert "cursor_unavailable" not in warns(p), p
    assert p["reviewer_selection"]["main_harness_used_as_external"] is True, p
    assert all(r["dispatch_mode"] == "external_cli" for r in p["reviewers"]), p

    # R2 — 同 harness 双 external，only==M（info）
    p = d.route("codex", ["codex"], reg, now_utc=after_temporary_override_expiry)
    assert p["routing_rule"] == "R2", p
    assert [r["reviewer_id"] for r in p["reviewers"]] == ["codex-cli-a", "codex-cli-b"], p
    assert "only_main_harness_available" in warns(p), p
    assert p["reviewer_selection"]["selection_mode"] == "same_harness_double", p

    # R2 — only!=M：必须 mismatch warning
    p = d.route("claude_code", ["codex"], reg, now_utc=after_temporary_override_expiry)
    assert p["routing_rule"] == "R2" and "available_reviewer_harness_mismatch" in warns(p), p

    # R3 — 零可用：默认 needs_last_resort_fallback，不 fail
    p = d.route("claude_code", [], reg, now_utc=after_temporary_override_expiry)
    assert p["routing_rule"] == "R3" and p["needs_last_resort_fallback"] is True, p
    assert p["status"] == "planned" and p["reviewers"] == [], p

    # R3 — require_external：fail-close
    p = d.route(
        "claude_code",
        [],
        reg,
        require_external_only=True,
        now_utc=after_temporary_override_expiry,
    )
    assert p["status"] == "failed" and p.get("reason") == "no_reviewer_harness_available", p


def test_dispatch_reviewers_pseudo_random_selection_seed_reproducible(root: Path) -> None:
    """S7·R15/R20 池内无偏序 = 伪随机选二，seed 落遥测且严格可复现。

    构造选择池 >2 的场景（主 harness 不在 Tier-1 可用集 → 非主池 = 3）：
    - 同 seed 两次 route → 选择（含 slot 顺序）逐位相同；
    - seed 缺省 → 现场生成并记录进 ``reviewer_selection.selection_seed``，用该 seed
      重放 route 得到完全相同的选择（记录在案即可复现）；
    - 选择恒 ⊆ 池、恒两腿、池按字典序规范化。
    """
    d = load_dispatch_reviewers_module()
    reg = _dispatch_registry()
    now = datetime.datetime(2026, 7, 10, 12, 1, tzinfo=datetime.timezone.utc)
    tier1 = ["cursor", "claude_code", "codex"]

    def harnesses(plan):
        return [r["harness_id"] for r in plan["reviewers"]]

    # 同 seed 可复现（含 slot 顺序）。
    p_a = d.route("outside_tier1_main", tier1, reg, now_utc=now, selection_seed=42)
    p_b = d.route("outside_tier1_main", tier1, reg, now_utc=now, selection_seed=42)
    assert p_a["reviewer_selection"]["selection_mode"] == "pseudo_random_two", p_a
    assert p_a["reviewer_selection"]["selection_seed"] == 42, p_a
    assert harnesses(p_a) == harnesses(p_b), (p_a, p_b)
    assert p_a["reviewer_selection"]["selection_pool"] == sorted(tier1), p_a

    # seed 缺省 → 生成并记录；用记录的 seed 重放得到相同选择。
    p_auto = d.route("outside_tier1_main", tier1, reg, now_utc=now)
    recorded_seed = p_auto["reviewer_selection"]["selection_seed"]
    assert isinstance(recorded_seed, int), p_auto
    p_replay = d.route(
        "outside_tier1_main", tier1, reg, now_utc=now, selection_seed=recorded_seed
    )
    assert harnesses(p_replay) == harnesses(p_auto), (p_auto, p_replay)

    # 选择恒两腿且 ⊆ 池；入序无关（池先做字典序规范化再采样）。
    p_shuffled_input = d.route(
        "outside_tier1_main",
        ["codex", "cursor", "claude_code"],
        reg,
        now_utc=now,
        selection_seed=42,
    )
    assert harnesses(p_shuffled_input) == harnesses(p_a), (p_shuffled_input, p_a)
    assert len(harnesses(p_a)) == 2 and set(harnesses(p_a)) <= set(tier1), p_a


def test_temporary_disable_window_generalized_component(root: Path) -> None:
    """R21 时间盒泛化：core/temporary_disable_window 声明式组件的通用语义。

    - 生效中：active_override 返回「reason + disabled_subject + expires_at_utc(Z 格式)
      + metadata 原样并入」的载荷;
    - 到期即自动失效（含恰好等于截止时刻）,无需改码;
    - naive datetime 按 UTC 解释；
    - dispatch_reviewers 的 claude-code 时间盒已改为本组件声明式实例（委托 + 键名
      remap ``disabled_subject``→``disabled_harness`` 保持既有契约）。
    """
    d = load_dispatch_reviewers_module()
    window_cls = d.TemporaryDisableWindow
    window = window_cls(
        disabled_subject="demo_capability",
        reason="temporary_disable_demo_capability_for_incident_123",
        expires_at_utc=datetime.datetime(2026, 8, 1, 0, 0, 0, tzinfo=datetime.timezone.utc),
        metadata={"incident": "123"},
    )
    active_at = datetime.datetime(2026, 7, 31, 23, 59, tzinfo=datetime.timezone.utc)
    override = window.active_override(active_at)
    assert override == {
        "reason": "temporary_disable_demo_capability_for_incident_123",
        "disabled_subject": "demo_capability",
        "expires_at_utc": "2026-08-01T00:00:00Z",
        "incident": "123",
    }, override
    # 恰达截止时刻 → 失效（窗口是 [声明, expires_at) 半开区间）。
    assert window.active_override(datetime.datetime(2026, 8, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)) is None
    # naive datetime 按 UTC 解释。
    assert window.active_override(datetime.datetime(2026, 7, 31, 23, 59)) is not None
    assert window.is_active(datetime.datetime(2026, 9, 1)) is False

    # 既有 claude-code 用例已声明为本组件实例，且委托层保持 disabled_harness 键名契约。
    declared = d.TEMPORARY_DISABLE_CLAUDE_CODE_EXTERNAL_REVIEWER_WINDOW
    assert isinstance(declared, window_cls), declared
    assert declared.disabled_subject == d.HOST_CLAUDE
    legacy = d.temporary_disable_claude_code_external_reviewer_override(
        datetime.datetime(2026, 7, 10, 11, 59, tzinfo=datetime.timezone.utc)
    )
    assert legacy is not None and legacy["disabled_harness"] == d.HOST_CLAUDE, legacy
    assert "disabled_subject" not in legacy, legacy


def test_dispatch_reviewers_temporarily_disables_claude_code_external_reviewer(root: Path) -> None:
    """临时 policy：覆盖期内 Claude Code 不作为 external reviewer。

    关键场景：主实现 agent 是 Codex，三路 harness 本来都可用时，external reviewer
    从历史默认的 cursor+claude_code 改为 cursor+codex；过期后恢复历史矩阵。
    """
    d = load_dispatch_reviewers_module()
    reg = _dispatch_registry()
    before_expiry = datetime.datetime(2026, 7, 10, 11, 59, tzinfo=datetime.timezone.utc)
    after_expiry = datetime.datetime(2026, 7, 10, 12, 1, tzinfo=datetime.timezone.utc)

    def harnesses(plan):
        return [r["harness_id"] for r in plan["reviewers"]]

    def warns(plan):
        return {w["code"] for w in plan["warnings"]}

    override = d.temporary_disable_claude_code_external_reviewer_override(before_expiry)
    assert override is not None
    assert override["disabled_harness"] == d.HOST_CLAUDE
    assert override["expires_at_utc"] == "2026-07-10T12:00:00Z"
    assert override["expires_after_requested_date"] == "2026-07-09"

    p = d.route("codex", ["cursor", "claude_code", "codex"], reg, now_utc=before_expiry)
    assert p["routing_rule"] == "R1", p
    assert harnesses(p) == ["cursor", "codex"], p
    assert "claude_code_external_reviewer_temporarily_disabled" in warns(p), p
    assert p["external_reviewer_selection_override"]["disabled_harnesses"] == ["claude_code"], p
    assert p["external_reviewer_selection_override"]["excluded_available_harnesses"] == [
        "claude_code"
    ], p

    p = d.route("claude_code", ["claude_code"], reg, now_utc=before_expiry)
    assert p["routing_rule"] == "R3", p
    assert p["reviewers"] == [], p

    p = d.route("codex", ["cursor", "claude_code", "codex"], reg, now_utc=after_expiry)
    assert p["routing_rule"] == "R0", p
    assert harnesses(p) == ["cursor", "claude_code"], p
    assert "external_reviewer_selection_override" not in p, p


def test_dispatch_reviewers_same_harness_double_instance_distinct_ids(root: Path) -> None:
    d = load_dispatch_reviewers_module()
    reg = _dispatch_registry()
    p = d.route("cursor", ["cursor"], reg)
    ids = [r["reviewer_id"] for r in p["reviewers"]]
    labels = [r["label"] for r in p["reviewers"]]
    assert len(set(ids)) == 2, ids
    assert ids == ["cursor-cli-a", "cursor-cli-b"], ids
    assert labels == [
        "alternative-reviewer:cursor-cli#a",
        "alternative-reviewer:cursor-cli#b",
    ], labels


def test_dispatch_reviewers_plan_artifact_schema(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    reg_path = root / "registry.json"
    reg_path.write_text(json.dumps(_dispatch_registry()), encoding="utf-8")
    run_dir = root / "run"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "dispatch_reviewers.py"),
            "--registry",
            str(reg_path),
            "--assume-available",
            "cursor,codex",
            "--main-harness",
            "codex",
            "--rvf-run-dir",
            str(run_dir),
            "--plan-only",
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, RVF_USER_LOCAL_DATA_ROOT=str(root / "user-local-state")),
    )
    assert completed.returncode == 0, completed.stderr
    plan_path = run_dir / "artifacts" / "reviewers" / "reviewer-plan.json"
    assert plan_path.exists(), completed.stdout
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    for key in (
        "schema_version",
        "main_harness",
        "available_harnesses",
        "routing_rule",
        "reviewers",
        "warnings",
        "needs_last_resort_fallback",
        "fallbacks",
        "status",
    ):
        assert key in plan, (key, plan)
    assert plan["fallbacks"] == [], plan
    assert plan["status"] == "planned" and plan["main_harness"] == "codex"
    assert plan["routing_rule"] == "R1" and len(plan["reviewers"]) == 2
    for r in plan["reviewers"]:
        for key in ("slot", "harness_id", "dispatch_mode", "label", "config_path", "reviewer_id"):
            assert key in r, (key, r)
        assert r["dispatch_mode"] == "external_cli", r


def test_dispatch_reviewers_executes_two_external(root: Path) -> None:
    """fake CLI shim 并行双 external：两路 review-result.json 路径存在、reviewer_id 唯一不撞目录。"""
    d = load_dispatch_reviewers_module()
    root.mkdir(parents=True, exist_ok=True)
    fake_cmd = [
        "bash",
        "-c",
        'cat >/dev/null; python3 "$RVF_WRITE_REVIEW_RESULT" no-issues '
        '--out "$RVF_REVIEW_RESULT" --audit-summary "fake $RVF_REVIEWER_ID reviewed scope"',
    ]
    reg = {"schema_version": 1, "harnesses": {}}
    for hid in ("alpha", "beta"):
        config_path = root / f"alt-{hid}.json"
        config_path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "label": f"alternative-reviewer:{hid}-cli",
                    "command": fake_cmd,
                    "allow_repo_cwd": True,
                    "pre_run_health": False,
                    "output_format": "text",
                }
            ),
            encoding="utf-8",
        )
        reg["harnesses"][hid] = {
            "harness_id": hid,
            "label_prefix": f"alternative-reviewer:{hid}-cli",
            "config_path": str(config_path),
            "dispatch_mode": "external_cli",
            "enabled": True,
            "priority_default": 100 if hid == "alpha" else 50,
        }
    plan = d.route("codex", ["alpha", "beta"], reg)
    assert plan["routing_rule"] == "R1" and len(plan["reviewers"]) == 2
    run_dir = root / "run"
    artifacts_dir = run_dir / "artifacts"
    packet = root / "packet.md"
    packet.write_text("## Review Packet\n\nintegration test\n", encoding="utf-8")
    plan = d.execute_plan(
        plan,
        repo=None,
        review_packet=str(packet),
        session_context=None,
        scope_contract=None,
        run_id="dispatch-exec-test",
        run_dir=str(run_dir),
        artifacts_dir=artifacts_dir,
    )
    assert plan["status"] == "completed", plan
    reviewer_dirs = sorted(p.name for p in (artifacts_dir / "reviewers").iterdir() if p.is_dir())
    assert reviewer_dirs == ["alpha-cli", "beta-cli"], reviewer_dirs
    seen_paths = set()
    for r in plan["reviewers"]:
        assert r["returncode"] == 0, r
        result_path = Path(r["review_result_path"])
        assert result_path.exists(), r
        seen_paths.add(str(result_path))
    assert len(seen_paths) == 2, seen_paths


def test_dispatch_reviewers_execute_backfills_review_env(root: Path) -> None:
    """回归（RVF-001）：`source review-env.sh; dispatch_reviewers.py --execute` 不带显式
    --repo/--review-packet/--session-context 时，应从 review-env.sh 导出的
    RVF_REPO / RVF_REVIEW_PACKET / RVF_SCOPE_OF_WORK 回填，否则子进程 reviewer 因缺参失败。"""
    root.mkdir(parents=True, exist_ok=True)
    fake_cmd = [
        "bash",
        "-c",
        'cat >/dev/null; python3 "$RVF_WRITE_REVIEW_RESULT" no-issues '
        '--out "$RVF_REVIEW_RESULT" --audit-summary "env-backfill ok"',
    ]
    reg = {"schema_version": 1, "harnesses": {}}
    for hid in ("alpha", "beta"):
        config_path = root / f"alt-{hid}.json"
        config_path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "label": f"alternative-reviewer:{hid}-cli",
                    "command": fake_cmd,
                    "allow_repo_cwd": True,
                    "pre_run_health": False,
                    "output_format": "text",
                }
            ),
            encoding="utf-8",
        )
        reg["harnesses"][hid] = {
            "harness_id": hid,
            "label_prefix": f"alternative-reviewer:{hid}-cli",
            "config_path": str(config_path),
            "dispatch_mode": "external_cli",
            "enabled": True,
            "priority_default": 100 if hid == "alpha" else 50,
        }
    reg_path = root / "registry.json"
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    packet = root / "packet.md"
    packet.write_text("## Review Packet\n\nenv backfill\n", encoding="utf-8")
    sow = root / "scope-of-work.md"
    sow.write_text("## scope\n", encoding="utf-8")
    run_dir = root / "run"
    env = {k: v for k, v in os.environ.items() if not k.startswith("CODEX_RVF_")}
    # emulate `source review-env.sh`: packet/scope only via env, NOT CLI args.
    # (RVF_REPO omitted — review-packet alone is sufficient for the reviewer kernel;
    # setting it to a non-git tmp dir would trip check_repo. The env-backfill code path
    # is identical for repo/packet/scope.)
    env["RVF_REVIEW_PACKET"] = str(packet)
    env["RVF_SCOPE_OF_WORK"] = str(sow)
    # probe 结果缓存落在 $RVF_USER_LOCAL_DATA_ROOT 下；不隔离的话这些用例会读写**真实**
    # 状态根，并且互相删对方写进去的条目（--execute 那些用例在 reroute 时会 invalidate）。
    env["RVF_USER_LOCAL_DATA_ROOT"] = str(root / "user-local-state")
    repo = init_repo(root / "reviewed-repository")
    env["RVF_REPO"] = str(repo)
    env = environment_with_synthetic_independent_review_start_boundary(repo=repo, environment=env, run_id="env-backfill", run_dir=run_dir)
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "dispatch_reviewers.py"),
            "--registry",
            str(reg_path),
            "--main-harness",
            "codex",
            "--assume-available",
            "alpha,beta",
            "--rvf-run-id",
            "env-backfill",
            "--rvf-run-dir",
            str(run_dir),
            "--execute",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    plan = json.loads(
        (run_dir / "artifacts" / "reviewers" / "reviewer-plan.json").read_text(encoding="utf-8")
    )
    assert plan["status"] == "completed", plan
    for r in plan["reviewers"]:
        assert r["returncode"] == 0, r
        assert Path(r["review_result_path"]).exists(), r


def test_dispatch_reviewers_reroutes_on_usage_limit(tmp_path: Path) -> None:
    """alpha 撞额度(125) → 轮内 reroute 到 gamma：status completed、fallbacks 记录、cooldown 落盘。"""
    d = load_dispatch_reviewers_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg = _make_usage_or_clean_registry(
        tmp_path, {"alpha": ("usage", 100), "beta": ("clean", 90), "gamma": ("clean", 80)}
    )
    # 主 harness 不在池 → 池 3 路触发伪随机选二；钉死 seed=4（选出 alpha+beta）
    # 使「alpha 撞额度 → reroute 到 gamma」的剧本确定可复现。
    plan = d.route("codex", ["alpha", "beta", "gamma"], reg, selection_seed=4)
    assert plan["routing_rule"] == "R0", plan
    assert {r["harness_id"] for r in plan["reviewers"]} == {"alpha", "beta"}, plan["reviewers"]
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nx\n", encoding="utf-8")
    repo = init_repo(tmp_path / "repo")
    with _with_cooldown_env(cooldown_root):
        plan = d.execute_plan(
            plan,
            repo=str(repo),
            review_packet=str(packet),
            session_context=None,
            scope_contract=None,
            run_id="reroute-test",
            run_dir=str(run_dir),
            artifacts_dir=artifacts_dir,
            registry=reg,
            main_harness="codex",
            available=["alpha", "beta", "gamma"],
        )
    assert plan["status"] == "completed", plan
    assert "alpha" in plan["cooldown_recorded"], plan
    assert (cooldown_root / "harness-alpha.json").exists(), list(cooldown_root.glob("*"))
    assert len(plan["fallbacks"]) == 1, plan["fallbacks"]
    fb = plan["fallbacks"][0]
    assert fb["from"] == "alpha" and fb["to"] == "gamma", fb
    assert len(plan["reviewers"]) == 2
    assert {r["harness_id"] for r in plan["reviewers"]} == {"beta", "gamma"}
    for r in plan["reviewers"]:
        assert r["returncode"] == 0, r
        assert Path(r["review_result_path"]).exists(), r


def test_dispatch_reviewers_reroute_id_collision(tmp_path: Path) -> None:
    """两腿均撞额度、只剩一个 eligible harness → 两替换 leg id 不碰撞(-fb1/-fb2)、两份 artifact 都在。"""
    d = load_dispatch_reviewers_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg = _make_usage_or_clean_registry(
        tmp_path, {"alpha": ("usage", 100), "beta": ("usage", 90), "gamma": ("clean", 80)}
    )
    # 钉死 seed=4（伪随机选出 alpha+beta）使「双腿全撞额度 → 全 reroute 到 gamma」确定复现。
    plan = d.route("codex", ["alpha", "beta", "gamma"], reg, selection_seed=4)
    assert plan["routing_rule"] == "R0"
    assert {r["harness_id"] for r in plan["reviewers"]} == {"alpha", "beta"}, plan["reviewers"]
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nx\n", encoding="utf-8")
    repo = init_repo(tmp_path / "repo")
    with _with_cooldown_env(cooldown_root):
        plan = d.execute_plan(
            plan,
            repo=str(repo),
            review_packet=str(packet),
            session_context=None,
            scope_contract=None,
            run_id="collision-test",
            run_dir=str(run_dir),
            artifacts_dir=artifacts_dir,
            registry=reg,
            main_harness="codex",
            available=["alpha", "beta", "gamma"],
        )
    assert plan["status"] == "completed", plan
    assert {"alpha", "beta"} <= set(plan["cooldown_recorded"]), plan
    assert len(plan["fallbacks"]) == 2, plan["fallbacks"]
    ids = sorted(r["reviewer_id"] for r in plan["reviewers"])
    assert ids == ["gamma-cli-fb1", "gamma-cli-fb2"], ids
    seen = set()
    for r in plan["reviewers"]:
        assert r["returncode"] == 0, r
        assert Path(r["review_result_path"]).exists(), r
        seen.add(r["review_result_path"])
    assert len(seen) == 2, seen


def test_dispatch_reviewers_reroutes_on_leg_without_review_result(tmp_path: Path) -> None:
    """alpha 认证失败(rc1、无 review-result) → 轮内 reroute 到 gamma；不写 cooldown。

    回归 rvf-20260811T170157Z：cursor 登录过期那腿 rc=1、目录里没有 review-result.json，
    原实现只对额度耗尽(125)做 reroute，于是既不补位也不报错，plan 记 partial 就收工。
    """
    d = load_dispatch_reviewers_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg = _make_usage_or_clean_registry(
        tmp_path, {"alpha": ("auth", 100), "beta": ("clean", 90), "gamma": ("clean", 80)}
    )
    # 同 usage-limit 测试：钉死 seed=4 让伪随机选出 alpha+beta，剧本确定可复现。
    plan = d.route("codex", ["alpha", "beta", "gamma"], reg, selection_seed=4)
    assert {r["harness_id"] for r in plan["reviewers"]} == {"alpha", "beta"}, plan["reviewers"]
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nx\n", encoding="utf-8")
    repo = init_repo(tmp_path / "repo")
    with _with_cooldown_env(cooldown_root):
        plan = d.execute_plan(
            plan,
            repo=str(repo),
            review_packet=str(packet),
            session_context=None,
            scope_contract=None,
            run_id="no-result-reroute-test",
            run_dir=str(run_dir),
            artifacts_dir=artifacts_dir,
            registry=reg,
            main_harness="codex",
            available=["alpha", "beta", "gamma"],
        )
    assert plan["status"] == "completed", plan
    assert plan["effective_reviewer_count"] == 2, plan
    # 认证失败不是额度问题：绝不能把 alpha 禁到后续 run 去。
    assert plan["cooldown_recorded"] == [], plan["cooldown_recorded"]
    assert not (cooldown_root / "harness-alpha.json").exists(), list(cooldown_root.glob("*"))
    assert len(plan["fallbacks"]) == 1, plan["fallbacks"]
    fb = plan["fallbacks"][0]
    assert (fb["from"], fb["to"], fb["reason"]) == ("alpha", "gamma", "no_review_result"), fb
    assert {r["harness_id"] for r in plan["reviewers"]} == {"beta", "gamma"}
    for r in plan["reviewers"]:
        assert Path(r["review_result_path"]).exists(), r


def test_dispatch_reviewers_failclose_when_leg_without_review_result_unfilled(
    tmp_path: Path,
) -> None:
    """认证失败腿补不上（池里只有两个 harness）→ status=failed + 专属 reason，不留 partial。"""
    d = load_dispatch_reviewers_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg = _make_usage_or_clean_registry(tmp_path, {"alpha": ("auth", 100), "beta": ("clean", 90)})
    plan = d.route("codex", ["alpha", "beta"], reg)
    assert plan["routing_rule"] == "R1", plan
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nx\n", encoding="utf-8")
    repo = init_repo(tmp_path / "repo")
    with _with_cooldown_env(cooldown_root):
        plan = d.execute_plan(
            plan,
            repo=str(repo),
            review_packet=str(packet),
            session_context=None,
            scope_contract=None,
            run_id="no-result-failclose-test",
            run_dir=str(run_dir),
            artifacts_dir=artifacts_dir,
            registry=reg,
            main_harness="codex",
            available=["alpha", "beta"],
        )
    # 一腿有效、一腿彻底没结论 ⇒ 不是 double-review，必须 fail-close 而不是停在 partial。
    assert plan["status"] == "failed", plan
    assert plan["reason"] == "reviewer_leg_produced_no_review_result", plan
    assert plan["effective_reviewer_count"] == 1, plan
    codes = [w["code"] for w in plan["warnings"]]
    assert "reviewer_leg_produced_no_review_result" in codes, plan["warnings"]
    assert plan["cooldown_recorded"] == [], plan["cooldown_recorded"]


def test_dispatch_reviewers_rc_zero_without_review_result_is_not_effective(tmp_path: Path) -> None:
    """腿 rc=0 却没写 review-result 也不算数：仍触发 reroute，effective 只数真有结论的腿。"""
    d = load_dispatch_reviewers_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg = _make_usage_or_clean_registry(
        tmp_path,
        {"alpha": ("rc0_no_result", 100), "beta": ("clean", 90), "gamma": ("clean", 80)},
    )
    plan = d.route("codex", ["alpha", "beta", "gamma"], reg, selection_seed=4)
    assert {r["harness_id"] for r in plan["reviewers"]} == {"alpha", "beta"}, plan["reviewers"]
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nx\n", encoding="utf-8")
    repo = init_repo(tmp_path / "repo")
    with _with_cooldown_env(cooldown_root):
        plan = d.execute_plan(
            plan,
            repo=str(repo),
            review_packet=str(packet),
            session_context=None,
            scope_contract=None,
            run_id="rc0-no-result-test",
            run_dir=str(run_dir),
            artifacts_dir=artifacts_dir,
            registry=reg,
            main_harness="codex",
            available=["alpha", "beta", "gamma"],
        )
    assert plan["status"] == "completed", plan
    assert plan["effective_reviewer_count"] == 2, plan
    assert len(plan["fallbacks"]) == 1, plan["fallbacks"]
    assert plan["fallbacks"][0]["from"] == "alpha", plan["fallbacks"]
    assert {r["harness_id"] for r in plan["reviewers"]} == {"beta", "gamma"}


def test_dispatch_reviewers_review_request_leg_is_pending_not_rerouted(tmp_path: Path) -> None:
    """alpha 返回合法 kind: request(rc0) → 不换腿、不算失败腿，plan 冒 blocked_on_reviewer_request。

    request 是 review 协议的待处理终态：主会话要按 Contract Gate 满足/驳回后让**同一** reviewer
    重试。若按 `no_review_result` 换腿，替换腿一成功 plan 就变 completed，request 被静默丢弃。
    """
    d = load_dispatch_reviewers_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg = _make_usage_or_clean_registry(
        tmp_path,
        {"alpha": ("review_request", 100), "beta": ("clean", 90), "gamma": ("clean", 80)},
    )
    # 同族测试：钉死 seed=4 让伪随机选出 alpha+beta（gamma 留作「本不该被用上」的替换候选）。
    plan = d.route("codex", ["alpha", "beta", "gamma"], reg, selection_seed=4)
    assert {r["harness_id"] for r in plan["reviewers"]} == {"alpha", "beta"}, plan["reviewers"]
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nx\n", encoding="utf-8")
    repo = init_repo(tmp_path / "repo")
    with _with_cooldown_env(cooldown_root):
        plan = d.execute_plan(
            plan,
            repo=str(repo),
            review_packet=str(packet),
            session_context=None,
            scope_contract=None,
            run_id="review-request-pending-test",
            run_dir=str(run_dir),
            artifacts_dir=artifacts_dir,
            registry=reg,
            main_harness="codex",
            available=["alpha", "beta", "gamma"],
        )
    # 关键回归：request 腿原样留在 plan 里，没有被 gamma 顶掉。
    assert plan["fallbacks"] == [], plan["fallbacks"]
    assert {r["harness_id"] for r in plan["reviewers"]} == {"alpha", "beta"}, plan["reviewers"]
    assert plan["status"] == "blocked_on_reviewer_request", plan
    assert plan["reason"] == "reviewer_review_request_pending", plan
    assert plan["effective_reviewer_count"] == 1, plan
    pending_ids = plan["review_request_pending_reviewer_ids"]
    assert pending_ids == ["alpha-cli"], pending_ids
    codes = [w["code"] for w in plan["warnings"]]
    assert "reviewer_review_request_pending" in codes, plan["warnings"]
    assert "reviewer_leg_produced_no_review_result" not in codes, plan["warnings"]
    # request 不是环境故障，更不是额度问题：不写 cooldown。
    assert plan["cooldown_recorded"] == [], plan["cooldown_recorded"]


def test_dispatch_reviewers_usage_limit_with_pending_request_is_not_all_usage_limit(
    tmp_path: Path,
) -> None:
    """一腿额度耗尽 + 一腿合法 kind: request 且额度腿补不上 → 不得谎称「全部额度耗尽」。

    request 腿被排除出 `unfilled_without_review_result`，旧实现的
    `ok == 0 and not unfilled_without_review_result` 因而成立，reason 落到
    `all_reviewers_usage_limit_exhausted`：主会话据此只去等配额恢复，那条必须走
    Contract Gate / Request Handling 的 request 被 reason 整条盖住。
    """
    d = load_dispatch_reviewers_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg = _make_usage_or_clean_registry(
        tmp_path, {"alpha": ("usage", 100), "beta": ("review_request", 90)}
    )
    plan = d.route("codex", ["alpha", "beta"], reg)
    assert plan["routing_rule"] == "R1", plan
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nx\n", encoding="utf-8")
    repo = init_repo(tmp_path / "repo")
    with _with_cooldown_env(cooldown_root):
        plan = d.execute_plan(
            plan,
            repo=str(repo),
            review_packet=str(packet),
            session_context=None,
            scope_contract=None,
            run_id="usage-plus-request-test",
            run_dir=str(run_dir),
            artifacts_dir=artifacts_dir,
            registry=reg,
            main_harness="codex",
            available=["alpha", "beta"],
        )
    # 额度腿补不上 ⇒ 仍 fail-close；但「全部额度耗尽」是假的，reason 必须自报混合。
    assert plan["status"] == "failed", plan
    assert plan["effective_reviewer_count"] == 0, plan
    assert plan["reason"] == (
        "reviewer_usage_limit_unfilled" + d.REVIEW_REQUEST_PENDING_REASON_SUFFIX
    ), plan
    codes = [w["code"] for w in plan["warnings"]]
    assert "all_reviewers_usage_limit_exhausted" not in codes, plan["warnings"]
    assert "reviewer_review_request_pending" in codes, plan["warnings"]
    # request 腿既没被换掉，也没被当成失败腿写 cooldown。
    assert plan["fallbacks"] == [], plan["fallbacks"]
    assert plan["review_request_pending_reviewer_ids"] == ["beta-cli"], plan
    assert plan["cooldown_recorded"] == ["alpha"], plan["cooldown_recorded"]
    assert not (cooldown_root / "harness-beta.json").exists(), list(cooldown_root.glob("*"))


def test_dispatch_reviewers_blocked_on_reviewer_request_exit_code_frees_detached_lock(
    tmp_path: Path,
) -> None:
    """`blocked_on_reviewer_request` 必须以专用非 0 码收尾，否则 detached 幂等锁把 run 锁死。

    旧实现下该终态与 completed 同样 rc=0：detached helper 的 staleness 判定
    (`_detached_run_finished_clean`) 认定「run 已干净完成」→ 拒绝重夺锁 → 主会话按协议
    处置完 request 再跑 `--execute --detached` 永远只拿到 `already_running`，本 run 再也
    产不出 `status: completed` 的 reviewed-candidate 证据。
    """
    d = load_dispatch_reviewers_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg = _make_usage_or_clean_registry(
        tmp_path, {"alpha": ("review_request", 100), "beta": ("clean", 90)}
    )
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    run_dir = tmp_path / "run"
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nx\n", encoding="utf-8")
    repo = init_repo(tmp_path / "repo")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CODEX_RVF_")}
    env["RVF_HARNESS_LIMIT_COOLDOWN_ROOT"] = str(cooldown_root)
    # probe 结果缓存落在 $RVF_USER_LOCAL_DATA_ROOT 下；不隔离的话这些用例会读写**真实**
    # 状态根，并且互相删对方写进去的条目（--execute 那些用例在 reroute 时会 invalidate）。
    env["RVF_USER_LOCAL_DATA_ROOT"] = str(tmp_path / "user-local-state")
    env = environment_with_synthetic_independent_review_start_boundary(repo=repo, environment=env, run_id="blocked-exit-code-test", run_dir=run_dir)
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "dispatch_reviewers.py"),
            "--registry",
            str(reg_path),
            "--main-harness",
            "codex",
            "--assume-available",
            "alpha,beta",
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--rvf-run-id",
            "blocked-exit-code-test",
            "--rvf-run-dir",
            str(run_dir),
            "--execute",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    plan = json.loads(
        (run_dir / "artifacts" / "reviewers" / "reviewer-plan.json").read_text(encoding="utf-8")
    )
    assert plan["status"] == "blocked_on_reviewer_request", plan
    assert plan["status"] == d.PLAN_STATUS_BLOCKED_ON_REVIEWER_REQUEST, plan
    # 专用码：既不是 completed 的 0（会让锁永久保留），也不是 failed 的 1（无人出故障）。
    assert completed.returncode == d.DISPATCH_BLOCKED_ON_REVIEWER_REQUEST_EXIT_CODE, (
        completed.returncode,
        completed.stderr[-2000:],
    )
    assert d.DISPATCH_BLOCKED_ON_REVIEWER_REQUEST_EXIT_CODE not in (0, 1)

    # 端到端闭合：带该退出码的 status.json + 已死 tmux session ⇒ 锁判陈旧、重夺并重派，
    # 而不是永远 already_running。
    detached = load_rvf_detached_thread_module()
    lock_root = tmp_path / "detached"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path, status_path = _seed_detached_stale_lock(
        lock_root, returncode=d.DISPATCH_BLOCKED_ON_REVIEWER_REQUEST_EXIT_CODE
    )
    assert detached._detached_run_finished_clean(status_path) is False
    result, recorded = _launch_detached_with_staleness_env(
        detached, lock_root, lock_path=lock_path, status_path=status_path, has_session_rc="1"
    )
    assert result["launch_status"] == "launched", result
    assert any(a[:1] == ["new-session"] for a in recorded), recorded


def test_dispatch_reviewers_failclose_mixed_usage_limit_and_auth_failure(tmp_path: Path) -> None:
    """一腿额度耗尽 + 一腿认证失败且都补不上 → 混合 reason，不得谎称「全部额度耗尽」。

    旧实现只要有额度腿且有效腿数为 0 就发 `all_reviewers_usage_limit_exhausted`，主会话据此
    去等配额恢复，同时需要修的认证故障被整条藏住（连 warning 都不发）。
    """
    d = load_dispatch_reviewers_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg = _make_usage_or_clean_registry(tmp_path, {"alpha": ("usage", 100), "beta": ("auth", 90)})
    plan = d.route("codex", ["alpha", "beta"], reg)
    assert plan["routing_rule"] == "R1", plan
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nx\n", encoding="utf-8")
    repo = init_repo(tmp_path / "repo")
    with _with_cooldown_env(cooldown_root):
        plan = d.execute_plan(
            plan,
            repo=str(repo),
            review_packet=str(packet),
            session_context=None,
            scope_contract=None,
            run_id="mixed-failure-test",
            run_dir=str(run_dir),
            artifacts_dir=artifacts_dir,
            registry=reg,
            main_harness="codex",
            available=["alpha", "beta"],
        )
    assert plan["status"] == "failed", plan
    assert plan["effective_reviewer_count"] == 0, plan
    assert plan["reason"] == "reviewer_legs_failed_mixed_usage_limit_and_no_review_result", plan
    codes = [w["code"] for w in plan["warnings"]]
    # 两类故障各自的 warning 都要在，非额度腿不得被额度结论盖住。
    assert "reviewer_leg_produced_no_review_result" in codes, plan["warnings"]
    assert any(code.endswith("usage_limit_unfilled") for code in codes), plan["warnings"]
    assert "all_reviewers_usage_limit_exhausted" not in codes, plan["warnings"]
    # 只有额度腿写 cooldown；认证失败腿绝不能被禁到后续 run。
    assert plan["cooldown_recorded"] == ["alpha"], plan["cooldown_recorded"]
    assert not (cooldown_root / "harness-beta.json").exists(), list(cooldown_root.glob("*"))


def test_dispatch_reviewers_wait_status_surfaces_plan_outcome(root: Path) -> None:
    """--wait-status 必须把 plan 的聚合结果一起冒出来，而不只是派发进程的 returncode。

    回归 rvf-20260811T170157Z：派发进程 exit 0、plan 却是 partial(一腿无结论)，轮询只打印
    returncode=0，照 SKILL.md 协议轮询的主会话据此当作「派发成功」。
    """
    d = load_dispatch_reviewers_module()
    root.mkdir(parents=True, exist_ok=True)
    status_path = root / ".dispatch-thread.status.json"
    plan_path = root / "reviewer-plan.json"
    status_path.write_text(
        json.dumps({"launch_status": "launched", "finished_at": "t1", "returncode": 0}),
        encoding="utf-8",
    )

    # 派发进程 rc=0，但 plan 只有一腿有结论 → 必须报出 partial，绝不能只显示 returncode=0。
    plan_path.write_text(
        json.dumps(
            {
                "status": "partial",
                "reviewers": [{"reviewer_id": "a"}, {"reviewer_id": "b"}],
                "effective_reviewer_count": 1,
            }
        ),
        encoding="utf-8",
    )
    done = d.wait_for_dispatch_status(status_path, max_wait=0.0)
    assert done["state"] == "done" and done["returncode"] == 0, done
    assert done["plan_status"] == "partial", done
    assert done["effective_reviewer_count"] == 1 and done["reviewer_count"] == 2, done

    # fail-close 的 reason 也要带出来供排障。
    plan_path.write_text(
        json.dumps(
            {
                "status": "failed",
                "reason": "reviewer_leg_produced_no_review_result",
                "reviewers": [{"reviewer_id": "a"}, {"reviewer_id": "b"}],
                "effective_reviewer_count": 1,
            }
        ),
        encoding="utf-8",
    )
    failed = d.wait_for_dispatch_status(status_path, max_wait=0.0)
    assert failed["plan_status"] == "failed", failed
    assert failed["plan_reason"] == "reviewer_leg_produced_no_review_result", failed

    # 两腿都有结论才是 completed。
    plan_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "reviewers": [{"reviewer_id": "a"}, {"reviewer_id": "b"}],
                "effective_reviewer_count": 2,
            }
        ),
        encoding="utf-8",
    )
    ok = d.wait_for_dispatch_status(status_path, max_wait=0.0)
    assert ok["plan_status"] == "completed" and ok["effective_reviewer_count"] == 2, ok

    # plan 还没写出来 → unknown（不是乐观值）。
    plan_path.unlink()
    unknown = d.wait_for_dispatch_status(status_path, max_wait=0.0)
    assert unknown["plan_status"] == "unknown", unknown


def test_dispatch_reviewers_probe_excludes_cooldown(tmp_path: Path) -> None:
    """tmp root 预置 alpha 冷却 → 真实 probe 排除 alpha → route 落他者 + harness_limit_cooldown_active 警告。"""
    cd = load_harness_limit_cooldown_module()
    load_dispatch_reviewers_module()  # 确保 SCRIPT_DIR 在 sys.path
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg_path = tmp_path / "registry.json"
    reg = {"schema_version": 1, "harnesses": {}}
    for hid, prio in (("alpha", 100), ("beta", 50)):
        cfg = tmp_path / f"alt-{hid}.json"
        write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse(
            cfg,
            [sys.executable, "-c", "pass"],
            activity_check_interval_seconds=0.05,
            output_format="text",
            health_command=[sys.executable, "-c", ""],
            pre_run_health=True,
        )
        reg["harnesses"][hid] = {
            "harness_id": hid,
            "label_prefix": f"alternative-reviewer:{hid}-cli",
            "config_path": str(cfg),
            "dispatch_mode": "external_cli",
            "enabled": True,
            "priority_default": prio,
        }
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    with _with_cooldown_env(cooldown_root):
        # env 已指向 cooldown_root（无 SUBDIR）；用 env 路径 record，确保子进程读同一处。
        cd.record("alpha", reason="usage_limit_exhausted")
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "dispatch_reviewers.py"),
                "--registry",
                str(reg_path),
                "--main-harness",
                "codex",
                "--probe-mode",
                "preflight",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            # 本用例是四处里唯一真的跑真实 probe 的：不隔离状态根就会把 alpha/beta 的
            # 探针结论写进真实 cache 目录。
            env=dict(
                os.environ,
                RVF_USER_LOCAL_DATA_ROOT=str(tmp_path / "user-local-state"),
            ),
            check=False,
        )
    assert "harness_limit_cooldown_active" in completed.stderr, completed.stderr
    plan = json.loads(completed.stdout)
    assert "alpha" not in plan["available_harnesses"], plan
    assert "beta" in plan["available_harnesses"], plan


def test_dispatch_reviewers_failclose_when_main_exhausted(tmp_path: Path) -> None:
    """external 补不上 + 主 harness 耗尽 → status=failed + main_harness_usage_limit_exhausted，不置伪 R3。"""
    d = load_dispatch_reviewers_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    cooldown_root = tmp_path / "cooldown"
    reg = _make_usage_or_clean_registry(
        tmp_path, {"alpha": ("usage", 100), "beta": ("clean", 90)}
    )
    plan = d.route("alpha", ["alpha", "beta"], reg)
    assert plan["routing_rule"] == "R1", plan
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nx\n", encoding="utf-8")
    repo = init_repo(tmp_path / "repo")
    with _with_cooldown_env(cooldown_root):
        plan = d.execute_plan(
            plan,
            repo=str(repo),
            review_packet=str(packet),
            session_context=None,
            scope_contract=None,
            run_id="failclose-test",
            run_dir=str(run_dir),
            artifacts_dir=artifacts_dir,
            registry=reg,
            main_harness="alpha",
            available=["alpha", "beta"],
        )
    assert plan["status"] == "failed", plan
    assert plan.get("reason") == "main_harness_usage_limit_exhausted", plan
    assert plan["needs_last_resort_fallback"] is False, plan
    assert any(w["code"] == "main_harness_usage_limit_exhausted" for w in plan["warnings"]), plan["warnings"]
    assert "alpha" in plan["cooldown_recorded"], plan
