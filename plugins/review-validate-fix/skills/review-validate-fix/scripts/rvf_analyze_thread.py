#!/usr/bin/env python3
"""把 RVF finalize 之后的 ``$rvf-analyze`` LLM 补全派进一个 detached tmux 线程。

设计动机：finalize 已经把 deterministic analysis scaffold（``summary.md`` /
``causality.json`` 骨架）落盘，剩下的只是一次有界、只读、与 worktree 无关的
LLM 补全。把它从「自注入回同一会话／Kanban task」改成后台 tmux 线程后，刚跑完
review-validate-fix 的会话立即 idle，用户无需等待 analyze 完成。

当前形态对用户**不可见**：analyze 线程跑在一个按 run 命名的 tmux session 里，
全部可观测信息（冻结 prompt、stdout/stderr 日志、launch/exit 状态）落在
``<run_dir>/artifacts/analysis/`` 下的 ``.analyze-thread.*`` 文件中。

> 未来 Kanban GUI 适配点：自研 Cline Kanban GUI 接入后，应当把这里的 tmux
> session（``rvf-analyze-<run_name>``）与 ``.analyze-thread.status.json`` /
> ``.analyze-thread.log`` 接进 GUI，做 analyze agent 的可视化与 workflow 集成
> （进度展示、attach 进 session、把 analyze 结果回填进 task 视图等）。在此之前
> 刻意保持「不直接可见、仅磁盘可观测」的形态，不做额外 UI 建模。

自抑制 lynchpin：detached agent 自己结束 turn 会触发它**自己的** Stop hook。
线程 env 注入 ``RVF_SUPPRESS_STOP_HOOK=1``（主防线，命中
``should_suppress``）与 ``RVF_ANALYZE_THREAD=1``（副防线，
``evaluate_stop_event`` 早退守卫），避免后台 analyze 递归触发新一轮 RVF。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# 自举 SCRIPT_DIR 上 sys.path（与 subagent_capture 同款），保证下列 sibling 与
# _rvf_pyroot import 在任何加载上下文（脚本运行 / 被 advisory import / 测试经
# canonical loader spec 加载）下都解析得到。
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rvf_pyroot  # noqa: E402,F401  — 把 pyroot 加入 sys.path，供 adapters.* import

from rvf_logging import safe_token  # noqa: E402
from trajectory_distill import HOST_CLAUDE, HOST_CODEX, detect_transcript_format  # noqa: E402

# detached-launch 通用机制（O_EXCL 幂等锁 + 两阶段原子 status + catch-all）抽到共享
# helper，analyze 线程与 reviewer dispatch 两路复用；analyze 专属决策（host 选择 /
# prompt 冻结 / 自抑制 env）仍留在本模块。
from rvf_detached_thread import (  # noqa: E402
    LAUNCH_FAILED,
    LAUNCH_LAUNCHED,
    _atomic_write_json,
    _iso_now,
    launch_detached,
)

from adapters.codex.subagent import (  # noqa: E402
    build_analyze_command as _codex_build_analyze_command,
)
from adapters.claude_code.subagent import (  # noqa: E402
    build_analyze_command as _claude_build_analyze_command,
)


STATUS_SCHEMA_VERSION = 1
SUPPRESS_STOP_HOOK_ENV = "RVF_SUPPRESS_STOP_HOOK"
ANALYZE_THREAD_ENV = "RVF_ANALYZE_THREAD"

PROMPT_FILENAME = ".analyze-thread.prompt.md"
LOG_FILENAME = ".analyze-thread.log"
STATUS_FILENAME = ".analyze-thread.status.json"
LOCK_FILENAME = ".analyze-thread.lock"

# 与 rvf_analyze_advisory._SESSION_PATH_KEYS 对齐：inline 一份避免循环 import
# （advisory 在 top-level import 本模块）。
_SESSION_PATH_KEYS = (
    "transcript_path",
    "session_path",
    "conversation_path",
    "log_path",
    "session_file",
)


def claude_bin() -> str:
    return os.environ.get("CODEX_RVF_CLAUDE_BIN", "claude")


def codex_bin() -> str:
    # 与 codex_stop_review_validate_fix.codex_bin 等价；inline 避免循环 import。
    return os.environ.get("CODEX_RVF_CODEX_BIN", "codex")


def _parent_transcript_path(event: dict[str, Any]) -> Path | None:
    for key in _SESSION_PATH_KEYS:
        raw = event.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            candidate = Path(raw).expanduser()
        except (OSError, ValueError):
            continue
        if candidate.exists():
            return candidate
    return None


def select_host(event: dict[str, Any]) -> str:
    """根据父会话 transcript 选 analyze 线程的 harness。

    Claude Code transcript → ``HOST_CLAUDE``；Codex rollout → ``HOST_CODEX``；
    无法识别（transcript 缺失 / 未知格式）→ 回退 ``HOST_CODEX``，与
    ``default_cline_kanban_agent_id`` 的兜底约定一致。
    """
    path = _parent_transcript_path(event)
    if path is not None:
        try:
            host = detect_transcript_format(path)
        except Exception:  # noqa: BLE001 - host 探测失败按兜底处理。
            host = None
        if host == HOST_CLAUDE:
            return HOST_CLAUDE
        if host == HOST_CODEX:
            return HOST_CODEX
    return HOST_CODEX


def build_analyze_command(host: str) -> tuple[list[str], bool]:
    """返回 ``(argv, uses_stdin)``：headless analyze agent 的调用向量。

    本函数是**调用侧 host 分派 facade**：按 ``host`` 选 ``adapters/<host>/
    subagent.py`` 的 ``build_analyze_command`` 构造各自的 argv，与观测侧
    ``subagent_capture`` 的分派形态对称。返回 ``(argv, uses_stdin)`` tuple 形态保持
    不变——``launch_detached_analyze_thread`` 等下游无需改动。

    prompt 一律走 stdin（``cat <prompt> | <argv>``），故 ``uses_stdin`` 两 host 都为
    True。未知 host 兜底到 Codex 向量，与 ``select_host`` 的兜底约定一致。
    bin 由本层解析（``claude_bin()`` / ``codex_bin()``）后传入 adapter，使 adapter
    不耦合 RVF 的 ``CODEX_RVF_*_BIN`` env 约定。

    permission-mode 在 ``adapters/claude_code/subagent.py`` 内固定为
    ``bypassPermissions``（``acceptEdits`` 只放行 Edit、不放 Read / Bash，而
    rvf-analyze skill 要 Read reference 文档、要 Bash 跑确定性后端脚本——headless
    没人弹窗批准会让 agent 干净退 0 但零产出，见线上 rvf-20260530T185312Z-...-
    30a814b9 的 ``.analyze-thread.log`` permission_denials 证据）。codex 侧
    ``--ask-for-approval never`` 已是同效力。
    """
    if host == HOST_CLAUDE:
        command = _claude_build_analyze_command(claude_bin=claude_bin())
    else:  # HOST_CODEX（含未知 host 的兜底）
        command = _codex_build_analyze_command(codex_bin=codex_bin())
    return command.argv, command.uses_stdin


def launch_detached_analyze_thread(
    *,
    event: dict[str, Any],
    ledger: Any,
    analysis: dict[str, str],
    finalize_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 analyze agent 派进 ``rvf-analyze-<run_name>`` tmux session（detached）。

    detached-launch 通用机制（O_EXCL 幂等锁 / 两阶段原子 status / catch-all）由
    ``rvf_detached_thread.launch_detached`` 提供；本函数只负责 analyze 专属决策——
    host 选择、prompt 冻结、自抑制 env（``RVF_SUPPRESS_STOP_HOOK`` /
    ``RVF_ANALYZE_THREAD``）与返回字段拼装（供 advisory 写入 ledger / summary /
    systemMessage）。整体 catch-all：任何异常收敛成 ``launch_failed``，**绝不**让线程
    启动失败打断 finalize/handoff 主路径。
    """
    del finalize_record  # 目前不需要，保留签名以备未来透传。
    run_dir = analysis["run_dir"]
    run_name = Path(run_dir).name
    analysis_dir = Path(analysis["summary_md_path"]).expanduser().parent
    tmux_session = f"rvf-analyze-{safe_token(run_name)}"
    prompt_path = analysis_dir / PROMPT_FILENAME
    log_path = analysis_dir / LOG_FILENAME
    status_path = analysis_dir / STATUS_FILENAME
    lock_path = analysis_dir / LOCK_FILENAME

    base = {
        "run_dir": run_dir,
        "run_name": run_name,
        "tmux_session": tmux_session,
        "prompt_path": str(prompt_path),
        "log_path": str(log_path),
        "status_path": str(status_path),
        "lock_path": str(lock_path),
    }

    try:
        host = select_host(event)
        agent_argv, uses_stdin = build_analyze_command(host)

        analysis_dir.mkdir(parents=True, exist_ok=True)

        # 冻结 prompt（懒 import 避免与 advisory 的循环 import）。
        from rvf_analyze_advisory import rvf_analyze_followup_prompt

        prompt_text = rvf_analyze_followup_prompt(analysis)
        prompt_path.write_text(prompt_text, encoding="utf-8")

        exports = {
            SUPPRESS_STOP_HOOK_ENV: "1",
            ANALYZE_THREAD_ENV: "1",
        }
        started_at = _iso_now()
        status_payload = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "run_dir": run_dir,
            "run_name": run_name,
            "host": host,
            "tmux_session": tmux_session,
            "command": agent_argv,
            "pid": None,
            "started_at": started_at,
            "armed_at": started_at,
            "returncode": None,
            "finished_at": None,
            "launch_status": LAUNCH_LAUNCHED,
            "error": None,
        }

        # analyze prompt 一律走 stdin（``cat <prompt> | <agent>``）；不施加总超时
        # backstop（analyze 补全自有边界，保持既有行为不变）。
        result = launch_detached(
            session_name=tmux_session,
            argv=agent_argv,
            stdin_path=prompt_path if uses_stdin else None,
            log_path=log_path,
            status_path=status_path,
            lock_path=lock_path,
            status_payload=status_payload,
            exports=exports,
            launch_env={**os.environ, **ledger.env(), **exports},
            idempotency_key=f"rvf-analyze:{run_name}",
        )
        return {
            **base,
            "host": host,
            "agent_command": agent_argv,
            "command": result.get("tmux_command"),
            "launch_status": result["launch_status"],
            "returncode": result["returncode"],
            "error": result["error"],
        }
    except Exception as exc:  # noqa: BLE001 - 启动失败绝不阻断 finalize/handoff。
        error = f"{type(exc).__name__}: {exc}"
        try:
            _atomic_write_json(
                status_path,
                {
                    "schema_version": STATUS_SCHEMA_VERSION,
                    "run_dir": run_dir,
                    "run_name": run_name,
                    "tmux_session": tmux_session,
                    "launch_status": LAUNCH_FAILED,
                    "error": error,
                    "started_at": _iso_now(),
                    "returncode": None,
                    "finished_at": None,
                },
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            lock_path.unlink()
        except OSError:
            pass
        return {
            **base,
            "launch_status": LAUNCH_FAILED,
            "returncode": None,
            "error": error,
        }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "rvf_analyze_thread is import-only. The detached-launch mechanism "
            "(including the --finalize-status status callback) now lives in "
            "rvf_detached_thread.py."
        )
    )
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
