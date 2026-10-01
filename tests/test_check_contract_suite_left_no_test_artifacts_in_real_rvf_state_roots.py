#!/usr/bin/env python3
"""行为 gate：契约套件测试卫生守卫本身的判据（F5·H5b）。

守卫的价值全在判据上：它必须**认出**引用本次套件私有 TMPDIR 的新文件，同时**放过**活会话
并发写入的条目——否则在任何一台有活 RVF 会话的机器上都会恒红，很快就会被人关掉。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD_SCRIPT = (
    ROOT / "scripts" / "check_contract_suite_left_no_test_artifacts_in_real_rvf_state_roots.py"
)
SUITE_PRIVATE_TEMPORARY_DIRECTORY_NAME = "rvf-contract-suite-private-tmp.AbC123"


def _load_guard_module():
    spec = importlib.util.spec_from_file_location("contract_suite_rvf_state_guard", GUARD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _set_change_times_to_the_past(path: Path, seconds_ago: float) -> None:
    past = time.time() - seconds_ago
    os.utime(path, (past, past))


def _find(guard, state_root: Path, suite_started_at: float):
    return guard.find_leaked_contract_test_artifacts_in_real_rvf_state_roots(
        state_roots=[state_root],
        suite_private_temporary_directory_name=SUITE_PRIVATE_TEMPORARY_DIRECTORY_NAME,
        suite_started_at_epoch_seconds=suite_started_at,
    )


def test_new_file_that_references_the_suite_temporary_directory_is_reported(tmp_path: Path) -> None:
    guard = _load_guard_module()
    state_root = tmp_path / "real-rvf-home"
    lock_directory = state_root / "kanban-followup-in-progress"
    lock_directory.mkdir(parents=True)
    suite_started_at = time.time()
    leaked = lock_directory / "sess-s1--repo-repo-2eff08f36370.json"
    leaked.write_text(
        '{"repo": "/private/var/folders/x/T/'
        f'{SUITE_PRIVATE_TEMPORARY_DIRECTORY_NAME}/tmpabc/bootstrap-agent-cli-resume/repo"}}',
        encoding="utf-8",
    )
    found = _find(guard, state_root, suite_started_at)
    assert [item.path for item in found] == [leaked]
    assert found[0].top_level_state_directory == str(lock_directory)


def test_concurrent_live_session_writes_are_not_reported(tmp_path: Path) -> None:
    """活会话在套件期间写的条目不可能引用本次套件的私有目录名 ⇒ 必须放过。"""
    guard = _load_guard_module()
    state_root = tmp_path / "real-rvf-home"
    token_directory = state_root / "agentic-initiation-decision-tokens"
    token_directory.mkdir(parents=True)
    suite_started_at = time.time()
    (token_directory / "0251895421283e84.json").write_text(
        '{"worktree": "/Users/someone/.cline/worktrees/8dc89/cline-kanban"}', encoding="utf-8"
    )
    (token_directory / "another-suite.json").write_text(
        '{"repo": "/var/folders/x/T/rvf-contract-suite-private-tmp.ZZZ999/tmpq/repo"}',
        encoding="utf-8",
    )
    assert _find(guard, state_root, suite_started_at) == []


def test_leak_inside_a_newly_created_deep_run_directory_is_reported(tmp_path: Path) -> None:
    """剪枝只跳过「套件开始前就没再变过」的目录；新建的 run 目录必须被下钻到底。"""
    guard = _load_guard_module()
    state_root = tmp_path / "real-rvf-home"
    runs = state_root / "user-local-data" / "run-logs" / "runs"
    old_run = runs / "rvf-20260801T000000Z-stop-hook-aaaaaaaa"
    (old_run / "artifacts").mkdir(parents=True)
    (old_run / "artifacts" / "stop-event.json").write_text("{}", encoding="utf-8")
    for path in (old_run / "artifacts" / "stop-event.json", old_run / "artifacts", old_run):
        _set_change_times_to_the_past(path, 3600)
    suite_started_at = time.time()
    new_run_artifacts = runs / "rvf-20260930T031726Z-prepare-run-bbbbbbbb" / "artifacts"
    new_run_artifacts.mkdir(parents=True)
    leaked = new_run_artifacts / "stop-event.json"
    leaked.write_text(
        f'{{"cwd": "/var/folders/x/T/{SUITE_PRIVATE_TEMPORARY_DIRECTORY_NAME}/tmp1/prepare/repo"}}',
        encoding="utf-8",
    )
    assert [item.path for item in _find(guard, state_root, suite_started_at)] == [leaked]


def test_copied_file_that_keeps_an_old_mtime_is_still_reported(tmp_path: Path) -> None:
    """``shutil.copy2`` 保留源 mtime；ctime 仍是新的，守卫必须靠它接住。"""
    guard = _load_guard_module()
    source = tmp_path / "source.json"
    source.write_text(
        f'{{"run_dir": "/var/folders/x/T/{SUITE_PRIVATE_TEMPORARY_DIRECTORY_NAME}/tmp1/runs"}}',
        encoding="utf-8",
    )
    _set_change_times_to_the_past(source, 3600)
    state_root = tmp_path / "real-rvf-home"
    (state_root / "kanban-followup-dispatched").mkdir(parents=True)
    suite_started_at = time.time()
    copied = state_root / "kanban-followup-dispatched" / "task-x.json"
    shutil.copy2(source, copied)
    assert copied.stat().st_mtime < suite_started_at - 60
    assert [item.path for item in _find(guard, state_root, suite_started_at)] == [copied]


def test_state_roots_follow_the_same_environment_as_production(tmp_path: Path) -> None:
    guard = _load_guard_module()
    home = tmp_path / "home"
    assert guard.resolve_real_rvf_state_roots_that_contract_tests_must_not_write(
        environment={}, home_directory=home
    ) == [home / ".rvf"]
    separate_user_local_data = tmp_path / "elsewhere" / "user-local-data"
    assert guard.resolve_real_rvf_state_roots_that_contract_tests_must_not_write(
        environment={
            "RVF_HOME": str(tmp_path / "rvf-home"),
            "RVF_USER_LOCAL_DATA_ROOT": str(separate_user_local_data),
        },
        home_directory=home,
    ) == [tmp_path / "rvf-home", separate_user_local_data]


def test_state_roots_include_production_overrides_of_run_log_and_session_hook_state_roots(
    tmp_path: Path,
) -> None:
    """生产代码的 run-log 根认 ``CODEX_RVF_LOG_ROOT`` → ``CODEX_RVF_STATE_DIR``，session-hook 状态
    目录认 ``CODEX_RVF_SESSION_HOOK_STATE_DIR`` → ``CODEX_RVF_STATE_DIR``；守卫扫的根必须同口径，
    否则继承了这些覆盖的测试写进覆盖根时守卫看不见（假绿）。嵌在已扫根之内的覆盖不重复列出。"""
    guard = _load_guard_module()
    home = tmp_path / "home"
    rvf_home = tmp_path / "rvf-home"
    run_log_root_outside_rvf_home = tmp_path / "overridden-run-log-root"
    session_hook_state_dir_outside_rvf_home = tmp_path / "overridden-session-hook-state-dir"
    assert guard.resolve_real_rvf_state_roots_that_contract_tests_must_not_write(
        environment={
            "RVF_HOME": str(rvf_home),
            "RVF_USER_LOCAL_DATA_ROOT": str(rvf_home / "user-local-data"),
            "CODEX_RVF_LOG_ROOT": str(run_log_root_outside_rvf_home),
            "CODEX_RVF_SESSION_HOOK_STATE_DIR": str(session_hook_state_dir_outside_rvf_home),
        },
        home_directory=home,
    ) == [rvf_home, run_log_root_outside_rvf_home, session_hook_state_dir_outside_rvf_home]

    runtime_state_dir_outside_rvf_home = tmp_path / "overridden-runtime-state-dir"
    assert guard.resolve_real_rvf_state_roots_that_contract_tests_must_not_write(
        environment={
            "RVF_HOME": str(rvf_home),
            "CODEX_RVF_STATE_DIR": str(runtime_state_dir_outside_rvf_home),
        },
        home_directory=home,
    ) == [rvf_home, home / ".rvf" / "user-local-data", runtime_state_dir_outside_rvf_home]

    assert guard.resolve_real_rvf_state_roots_that_contract_tests_must_not_write(
        environment={"CODEX_RVF_LOG_ROOT": str(home / ".rvf" / "user-local-data" / "run-logs")},
        home_directory=home,
    ) == [home / ".rvf"]


def test_state_root_that_contains_an_earlier_root_absorbs_it_regardless_of_order(
    tmp_path: Path,
) -> None:
    """嵌套去重不能依赖解析顺序：后出现的覆盖根若是先前根的祖先，先前根应被吸收，免得重复计数。"""
    guard = _load_guard_module()
    home = tmp_path / "home"
    enclosing_run_log_root = tmp_path / "enclosing"
    assert guard.resolve_real_rvf_state_roots_that_contract_tests_must_not_write(
        environment={
            "RVF_HOME": str(enclosing_run_log_root / "rvf-home"),
            "RVF_USER_LOCAL_DATA_ROOT": str(enclosing_run_log_root / "user-local-data"),
            "CODEX_RVF_LOG_ROOT": str(enclosing_run_log_root),
        },
        home_directory=home,
    ) == [enclosing_run_log_root]


def test_command_line_reports_leak_written_into_overridden_run_log_root_outside_rvf_home(
    tmp_path: Path,
) -> None:
    """复现假绿：runner 环境里设了 ``CODEX_RVF_LOG_ROOT``，测试把 ``latest.json`` 写进覆盖根，
    守卫必须判红而不是报 0 个泄漏。"""
    home = tmp_path / "home"
    rvf_home = tmp_path / "rvf-home"
    rvf_home.mkdir()
    overridden_run_log_root = tmp_path / "overridden-run-log-root"
    suite_started_at = time.time()
    leaked = overridden_run_log_root / "latest.json"
    leaked.parent.mkdir(parents=True)
    leaked.write_text(
        f'{{"run_dir": "/var/folders/x/T/{SUITE_PRIVATE_TEMPORARY_DIRECTORY_NAME}/tmp1/runs/r"}}',
        encoding="utf-8",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("RVF_", "CODEX_RVF_"))
    }
    environment.update(
        {
            "HOME": str(home),
            "RVF_HOME": str(rvf_home),
            "RVF_USER_LOCAL_DATA_ROOT": str(rvf_home / "user-local-data"),
            "CODEX_RVF_LOG_ROOT": str(overridden_run_log_root),
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(GUARD_SCRIPT),
            "--suite-private-temporary-directory",
            str(tmp_path / SUITE_PRIVATE_TEMPORARY_DIRECTORY_NAME),
            "--suite-started-at-epoch-seconds",
            str(suite_started_at),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert completed.returncode == 1, (completed.stdout, completed.stderr)
    assert str(leaked.resolve()) in completed.stderr


def test_command_line_refuses_a_directory_that_is_not_the_suite_private_one(tmp_path: Path) -> None:
    """传个普通 /tmp 进来会让判据退化成「任何提到 tmp 的新文件都算泄漏」——必须拒绝。"""
    completed = subprocess.run(
        [
            sys.executable,
            str(GUARD_SCRIPT),
            "--suite-private-temporary-directory",
            str(tmp_path),
            "--suite-started-at-epoch-seconds",
            str(time.time()),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2, completed.stderr
