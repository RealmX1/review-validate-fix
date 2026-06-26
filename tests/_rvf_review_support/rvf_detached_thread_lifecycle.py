#!/usr/bin/env python3
"""rvf detached 线程生命周期 测试簇。

从 tests/test_review_support_scripts.py 有界抽出（导航用拆分，行为不变）。共享 helper/常量
（run/read_jsonl/load_*_module/路径常量等）仍归 aggregator 所有，经 inject() 在注册表运行前推入
本模块 globals，避免与 __main__ 脚本循环导入。注册表 lambda 不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# 由 aggregator（tests/test_review_support_scripts.py）在导入后 inject 注入共享依赖。
__all__ = [
    'test_rvf_detached_thread_status_two_phase',
    'test_rvf_detached_thread_lock_idempotent',
    'test_rvf_detached_thread_launch_failed_releases_lock',
    'test_rvf_detached_thread_reclaims_stale_lock_when_session_dead',
    'test_rvf_detached_thread_keeps_lock_when_session_alive',
    'test_rvf_detached_thread_keeps_lock_on_clean_finish',
    'test_rvf_detached_thread_keeps_lock_when_tmux_probe_fails',
    'test_rvf_detached_thread_run_with_timeout',
    'test_rvf_detached_thread_finalize_status_cli',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_rvf_detached_thread_status_two_phase(root: Path) -> None:
    """real-exec tmux：launch 写 launched，wrapper 退出后 --finalize-status 回写 returncode/finished_at。"""
    module = load_rvf_detached_thread_module()
    root.mkdir(parents=True, exist_ok=True)
    fake_tmux = write_realexec_tmux_script(root / "tmux.py")
    saved = os.environ.get("CODEX_RVF_TMUX_BIN")
    os.environ["CODEX_RVF_TMUX_BIN"] = str(fake_tmux)
    try:
        status_path = root / "s.status.json"
        result = module.launch_detached(
            session_name="rvf-detached-unit",
            argv=[sys.executable, "-c", "import sys; sys.exit(5)"],
            log_path=root / "s.log",
            status_path=status_path,
            lock_path=root / "s.lock",
            status_payload=_detached_status_payload(),
        )
    finally:
        if saved is None:
            os.environ.pop("CODEX_RVF_TMUX_BIN", None)
        else:
            os.environ["CODEX_RVF_TMUX_BIN"] = saved
    assert result["launch_status"] == "launched", result
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["launch_status"] == "launched"  # 启动期字段保留
    assert status["returncode"] == 5  # 退出码经 finalize 回写
    assert status["finished_at"]
    assert (root / "s.lock").exists()
    assert (root / "s.log").exists()


def test_rvf_detached_thread_lock_idempotent(root: Path) -> None:
    """每-run O_EXCL 锁：第二次 launch 命中 already_running，不再起 tmux。"""
    module = load_rvf_detached_thread_module()
    root.mkdir(parents=True, exist_ok=True)
    fake_tmux = write_fake_tmux_script(root / "tmux.py")
    calls = root / "calls.jsonl"
    saved = {
        k: os.environ.get(k)
        for k in ("CODEX_RVF_TMUX_BIN", "FAKE_TMUX_CALLS", "FAKE_TMUX_RETURNCODE")
    }
    os.environ["CODEX_RVF_TMUX_BIN"] = str(fake_tmux)
    os.environ["FAKE_TMUX_CALLS"] = str(calls)
    os.environ["FAKE_TMUX_RETURNCODE"] = "0"
    try:
        kw = dict(
            session_name="rvf-detached-unit",
            argv=["echo", "hi"],
            log_path=root / "s.log",
            status_path=root / "s.status.json",
            lock_path=root / "s.lock",
        )
        first = module.launch_detached(status_payload=_detached_status_payload(), **kw)
        second = module.launch_detached(status_payload=_detached_status_payload(), **kw)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert first["launch_status"] == "launched", first
    assert second["launch_status"] == "already_running", second
    # 第二次 launch 现在会先 has-session 探活（session 存活 → already_running），
    # 故按 new-session 子命令计数：全程只起过一次 tmux。
    recorded = [json.loads(line)["argv"] for line in calls.read_text(encoding="utf-8").splitlines()]
    assert len([a for a in recorded if a[:1] == ["new-session"]]) == 1, recorded


def test_rvf_detached_thread_launch_failed_releases_lock(root: Path) -> None:
    """tmux 非零退出 → launch_failed：status 记 error、锁被释放（便于重试）。"""
    module = load_rvf_detached_thread_module()
    root.mkdir(parents=True, exist_ok=True)
    fake_tmux = write_fake_tmux_script(root / "tmux.py")
    saved = {
        k: os.environ.get(k)
        for k in (
            "CODEX_RVF_TMUX_BIN",
            "FAKE_TMUX_CALLS",
            "FAKE_TMUX_RETURNCODE",
            "FAKE_TMUX_STDERR",
        )
    }
    os.environ["CODEX_RVF_TMUX_BIN"] = str(fake_tmux)
    os.environ["FAKE_TMUX_CALLS"] = str(root / "calls.jsonl")
    os.environ["FAKE_TMUX_RETURNCODE"] = "1"
    os.environ["FAKE_TMUX_STDERR"] = "boom: cannot create session"
    lock_path = root / "s.lock"
    status_path = root / "s.status.json"
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
    assert result["launch_status"] == "launch_failed", result
    assert "boom" in (result["error"] or "")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["launch_status"] == "launch_failed"
    assert "boom" in (status["error"] or "")
    assert not lock_path.exists()  # 锁释放，可重试


def test_rvf_detached_thread_reclaims_stale_lock_when_session_dead(root: Path) -> None:
    """FU-2：持锁 detached 线程已死（has-session≠0）且 run 未干净完成 → 重夺锁、重新 launch。"""
    module = load_rvf_detached_thread_module()
    root.mkdir(parents=True, exist_ok=True)
    lock_path, status_path = _seed_detached_stale_lock(root, returncode=None)
    result, recorded = _launch_detached_with_staleness_env(
        module, root, lock_path=lock_path, status_path=status_path, has_session_rc="1"
    )
    assert result["launch_status"] == "launched", result  # 重夺并重派
    assert ["has-session", "-t", "rvf-detached-unit"] in recorded  # 确实先探活
    assert any(a[:1] == ["new-session"] for a in recorded), recorded  # 确实重新起 tmux
    assert lock_path.exists()  # 重夺后锁仍由本次持有


def test_rvf_detached_thread_keeps_lock_when_session_alive(root: Path) -> None:
    """FU-2：持锁 session 仍存活（has-session==0）→ already_running，绝不重派。"""
    module = load_rvf_detached_thread_module()
    root.mkdir(parents=True, exist_ok=True)
    lock_path, status_path = _seed_detached_stale_lock(root, returncode=None)
    result, recorded = _launch_detached_with_staleness_env(
        module, root, lock_path=lock_path, status_path=status_path, has_session_rc="0"
    )
    assert result["launch_status"] == "already_running", result
    assert all(a[:1] != ["new-session"] for a in recorded), recorded  # 未起新 tmux
    assert lock_path.exists()  # 活锁保留


def test_rvf_detached_thread_keeps_lock_on_clean_finish(root: Path) -> None:
    """FU-2：session 已死但 run 已干净完成（returncode==0）→ already_running，幂等不重跑。"""
    module = load_rvf_detached_thread_module()
    root.mkdir(parents=True, exist_ok=True)
    lock_path, status_path = _seed_detached_stale_lock(root, returncode=0)
    result, recorded = _launch_detached_with_staleness_env(
        module, root, lock_path=lock_path, status_path=status_path, has_session_rc="1"
    )
    assert result["launch_status"] == "already_running", result
    assert all(a[:1] != ["new-session"] for a in recorded), recorded  # 不重跑已完成的 run
    assert lock_path.exists()


def test_rvf_detached_thread_keeps_lock_when_tmux_probe_fails(root: Path) -> None:
    """FU-2 安全不变量（RVF cursor review 发现）：tmux 探活本身失败（binary 不可用 / 超时
    / 异常）时无法确认 session 已死 → 保守保持 already_running、绝不删可能仍活的锁。

    回归：旧实现 `_tmux_session_alive` 异常→False，会被调用方当成「session 已死」误入重夺，
    在 tmux 临时不可用时删掉仍在跑的 detached session 的锁（破坏「绝不误删活锁」不变量）。
    """
    module = load_rvf_detached_thread_module()
    root.mkdir(parents=True, exist_ok=True)
    lock_path, status_path = _seed_detached_stale_lock(root, returncode=None)  # 未干净完成
    saved = {
        k: os.environ.get(k)
        for k in ("CODEX_RVF_TMUX_BIN", "FAKE_TMUX_CALLS", "FAKE_TMUX_RETURNCODE")
    }
    # 指向不存在的 tmux 二进制 → subprocess.run 抛 FileNotFoundError（模拟 tmux 临时不可用）。
    os.environ["CODEX_RVF_TMUX_BIN"] = str(root / "nonexistent-tmux-binary")
    os.environ.pop("FAKE_TMUX_CALLS", None)
    os.environ.pop("FAKE_TMUX_RETURNCODE", None)
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
    # 无法确认死亡 → 保守 already_running，锁保留（不重夺、不 launch）。
    assert result["launch_status"] == "already_running", result
    assert lock_path.exists()


def test_rvf_detached_thread_run_with_timeout(_root: Path | None = None) -> None:
    """--run-with-timeout：正常退出码透传；超时 killpg 整组并返回 124。"""
    helper = SCRIPT_DIR / "rvf_detached_thread.py"
    passthrough = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--run-with-timeout",
            "30",
            "--",
            sys.executable,
            "-c",
            "import sys; sys.exit(3)",
        ]
    ).returncode
    assert passthrough == 3
    timed_out = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--run-with-timeout",
            "1",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(10)",
        ]
    ).returncode
    assert timed_out == 124


def test_rvf_detached_thread_finalize_status_cli(root: Path) -> None:
    """--finalize-status：merge returncode/finished_at，保留 launch 期字段。"""
    root.mkdir(parents=True, exist_ok=True)
    helper = SCRIPT_DIR / "rvf_detached_thread.py"
    status_path = root / "s.status.json"
    status_path.write_text(
        json.dumps(
            {
                "launch_status": "launched",
                "started_at": "t0",
                "returncode": None,
                "finished_at": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rc = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--finalize-status",
            "--status-path",
            str(status_path),
            "--returncode",
            "7",
        ]
    ).returncode
    assert rc == 0
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["returncode"] == 7 and payload["finished_at"]
    assert payload["launch_status"] == "launched" and payload["started_at"] == "t0"

