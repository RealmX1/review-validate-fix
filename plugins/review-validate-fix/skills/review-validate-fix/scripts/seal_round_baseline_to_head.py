#!/usr/bin/env python3
"""把本轮 round-baseline 标记推进到当前 HEAD（rvf-land 收尾封窗）。

为什么需要它：committed-round 漏审检测以 ``round_baseline_marker`` 记录的
「上一条 user prompt 提交时的 HEAD」为窗口下界（见 ``round_baseline_marker`` /
``diff_tracker._list_committed_round_changed_paths``）。``$rvf-land`` 在同一个
prompt-turn 内提交了一段「刚被完整 RVF review 过、当时还是 dirty」的工作；但没有
任何环节推进该 marker，于是紧随的 Stop hook 仍读旧 baseline（新 commit 的父），把
刚 land 的 commit 重新纳入 ``baseline..HEAD`` 窗口 → 对已审工作多派一轮 review。

封窗 = rvf-land 提交成功后调用本脚本，把 marker 的 ``baseline_head`` 覆盖成新
HEAD，等价于 rvf-land 断言「到此 HEAD 为止的工作都已审」。这样下一次 Stop 的
committed-round 窗口立刻变空，多派消失——与「下一条新 prompt 会把 marker 刷到新
HEAD」是同一个语义，只是不必等用户再发一条 prompt。

严格 best-effort：任何失败（非 git 仓库、detached/空 HEAD、拿不到 task/session
键、IO 错误）都只打印一行 skip 并以 0 退出，绝不让 rvf-land 因封窗失败而中断。
marker 优先 task-keyed（``current_kanban_task_id`` 走 event/env 回退），cline-kanban
task 下从 env 即可解析；非 kanban 会话拿不到键时此步 no-op（committed-round 漏审仍由
dirty 闸 + finalize 的 no-op 守卫兜底）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import round_baseline_marker

SEAL_PROMPT_EXCERPT = "rvf-land seal (round baseline advanced to landed HEAD)"


def _git_toplevel(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return top or None


def _git_head_oid(repo: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    # A fresh repo with no commits prints nothing / the literal "HEAD".
    return head if head and head != "HEAD" else None


def _resolve_task_id() -> str | None:
    try:
        from rvf_analyze_advisory import current_kanban_task_id  # noqa: PLC0415

        # Empty event ⇒ resolver falls back to KANBAN_TASK_ID / CLINE_KANBAN_TASK_ID
        # / KANBAN_HOOK_TASK_ID env, which a cline-kanban task exports.
        return current_kanban_task_id({})
    except Exception:
        return None


def _resolve_session_id() -> str | None:
    for key in ("CODEX_SESSION_ID", "CLAUDE_SESSION_ID", "RVF_SESSION_ID"):
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip()
    return None


def seal_round_baseline_to_head(repo_arg: str | None = None) -> dict[str, Any]:
    """封窗本体。返回 diagnostic dict；``sealed`` 为 True 表示 marker 已推进。

    永不抛异常：所有失败折叠成 ``{"sealed": False, "reason": ...}``。
    """
    cwd = repo_arg or os.getcwd()
    repo = _git_toplevel(cwd)
    if repo is None:
        return {"sealed": False, "reason": "not_a_git_repo", "cwd": cwd}
    head = _git_head_oid(repo)
    if head is None:
        return {"sealed": False, "reason": "no_head_commit", "repo": repo}
    task_id = _resolve_task_id()
    session_id = _resolve_session_id()
    if not task_id and not session_id:
        return {
            "sealed": False,
            "reason": "no_marker_key",
            "repo": repo,
            "baseline_head": head,
        }
    try:
        marker_path = round_baseline_marker.write_round_baseline_marker(
            task_id=task_id,
            session_id=session_id,
            baseline_head=head,
            repo=repo,
            prompt_excerpt=SEAL_PROMPT_EXCERPT,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort, never break rvf-land
        return {"sealed": False, "reason": f"write_failed:{exc}", "repo": repo}
    if marker_path is None:
        return {"sealed": False, "reason": "write_returned_none", "repo": repo}
    return {
        "sealed": True,
        "repo": repo,
        "baseline_head": head,
        "marker_path": str(marker_path),
        "task_id": task_id,
        "session_id": session_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Advance the RVF round-baseline marker to HEAD after rvf-land commit (best-effort).",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Repo / working dir to resolve git toplevel + HEAD from (default: cwd).",
    )
    args = parser.parse_args(argv)
    result = seal_round_baseline_to_head(args.repo)
    if result.get("sealed"):
        print(
            f"RVF_ROUND_BASELINE_SEALED head={result.get('baseline_head')} "
            f"marker={result.get('marker_path')}"
        )
    else:
        print(f"RVF_ROUND_BASELINE_SEAL_SKIPPED reason={result.get('reason')}")
    # Always succeed:封窗失败绝不能让 rvf-land 收尾失败。
    return 0


if __name__ == "__main__":
    sys.exit(main())
