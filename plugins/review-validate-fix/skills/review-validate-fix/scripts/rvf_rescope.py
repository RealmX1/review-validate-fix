#!/usr/bin/env python3
"""失败再入：武装一次性 rescope state（agent 写、下次 Stop 消费）。

场景：用户把 RVF handoff 拿回「早先实现刚完成那一刻」，**实测判断实现本身未达成
原始目标**（与 RVF 的 fix 是否达标无关）。主 agent 准备修用户观察暴露的问题前，
先经本脚本 ``arm`` 武装一个带 ``target_run_id`` 的 rescope marker；随后任何新增改动
即时触发的下一次 Stop，会消费该 marker、按 ``target_run_id`` 把那次实现仍存在的
``reviewed`` units 翻回 ``available``，使新一轮 RVF 的 scope = 「该实现 units ∪ 本次
fix delta」全量重审。

``target_run_id`` 解析优先级（高 → 低）：
  1. 显式 ``--target-run-id``（agent / 用户覆盖）；
  2. 粘贴的 handoff（``--handoff`` / ``--handoff-text-file`` / ``--stdin``）里的 run_id；
  3. tracker：本 worktree 最近一次「仍有 reviewed units」的 RVF run
     （``diff_tracker.latest_reviewed_run_for_worktree``）；
  4. ``log_root()/latest.json`` 的 run_id（log-root 级全局指针，不分 worktree，仅兜底）。

marker 维度与消费侧（Stop hook）保持一致：优先 task_id（kanban），无则 session_id。
task_id 未显式给出时，回退到 kanban env（``KANBAN_TASK_ID`` /
``CLINE_KANBAN_TASK_ID`` / ``KANBAN_HOOK_TASK_ID``）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import diff_tracker
import review_reopen_marker
from rvf_handoff_intake import RVF_RUN_RE
from rvf_logging import log_root


KANBAN_TASK_ID_ENV_KEYS = (
    "KANBAN_TASK_ID",
    "CLINE_KANBAN_TASK_ID",
    "KANBAN_HOOK_TASK_ID",
)


def _resolve_task_id(explicit: str | None) -> str | None:
    if explicit and explicit.strip():
        return explicit.strip()
    for key in KANBAN_TASK_ID_ENV_KEYS:
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip()
    return None


def _read_handoff_text(args: argparse.Namespace) -> str | None:
    if getattr(args, "stdin", False):
        return sys.stdin.read()
    if getattr(args, "handoff", None):
        try:
            return Path(args.handoff).expanduser().read_text(encoding="utf-8")
        except OSError:
            return None
    if getattr(args, "handoff_text_file", None):
        try:
            return Path(args.handoff_text_file).expanduser().read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def _run_id_from_handoff(text: str | None) -> str | None:
    if not text:
        return None
    match = RVF_RUN_RE.search(text)
    return match.group(1) if match else None


def _run_id_from_latest_json(log_root_dir: Path) -> str | None:
    try:
        data = json.loads((log_root_dir / "latest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        run_id = data.get("run_id")
        if isinstance(run_id, str) and run_id.strip():
            return run_id.strip()
    return None


def resolve_target_run_id(
    *,
    repo: Path,
    explicit_run_id: str | None,
    handoff_text: str | None,
    log_root_override: Path | None,
) -> tuple[str | None, str]:
    """返回 ``(target_run_id, source)``；解析失败 source 为 ``"unresolved"``。"""
    if explicit_run_id and explicit_run_id.strip():
        return explicit_run_id.strip(), "explicit"

    from_handoff = _run_id_from_handoff(handoff_text)
    if from_handoff:
        return from_handoff, "handoff"

    try:
        tracker_result = diff_tracker.latest_reviewed_run_for_worktree(
            repo=repo, log_root_override=log_root_override
        )
    except Exception:
        tracker_result = {"status": "error", "run_id": None}
    if tracker_result.get("status") == "found" and tracker_result.get("run_id"):
        return tracker_result["run_id"], "tracker_latest_reviewed_run"

    log_root_dir = log_root_override if log_root_override is not None else log_root()
    from_latest = _run_id_from_latest_json(log_root_dir)
    if from_latest:
        return from_latest, "latest_json_fallback"

    return None, "unresolved"


def _cmd_arm(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    log_root_override = (
        Path(args.log_root).expanduser().resolve() if args.log_root else None
    )
    task_id = _resolve_task_id(args.task_id)
    session_id = args.session_id.strip() if args.session_id and args.session_id.strip() else None

    if not task_id and not session_id:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "no_marker_context",
                    "detail": (
                        "需要 --task-id 或 --session-id（或设置 KANBAN_TASK_ID / "
                        "CLINE_KANBAN_TASK_ID）才能武装 rescope marker，"
                        "且必须与下次 Stop event 携带的 task/session 一致。"
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    handoff_text = _read_handoff_text(args)
    target_run_id, source = resolve_target_run_id(
        repo=repo,
        explicit_run_id=args.target_run_id,
        handoff_text=handoff_text,
        log_root_override=log_root_override,
    )

    if not target_run_id:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "target_run_id_unresolved",
                    "detail": (
                        "无法解析最近一次已 RVF 的实现 run。请显式传 "
                        "--target-run-id，或粘贴该实现 RVF run 的 handoff 正文。"
                    ),
                    "repo": str(repo),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1

    marker_path = review_reopen_marker.write_review_reopen_marker(
        task_id=task_id,
        session_id=session_id,
        target_run_id=target_run_id,
        repo=str(repo),
        reason=args.reason,
        source=args.source,
        root=log_root_override,
    )

    result: dict[str, Any] = {
        "status": "armed" if marker_path is not None else "error",
        "target_run_id": target_run_id,
        "run_id_source": source,
        "marker_path": str(marker_path) if marker_path is not None else None,
        "task_id": task_id,
        "session_id": session_id,
        "repo": str(repo),
        "reason": args.reason,
        "source": args.source,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if marker_path is not None else 1


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="subcommand")

    arm = subparsers.add_parser(
        "arm",
        help="武装一次性 rescope marker，供下次 Stop 消费、按 target_run_id 重开评审范围。",
    )
    arm.add_argument("--repo", default=".", help="当前主会话 repo / worktree，默认当前目录。")
    arm.add_argument(
        "--target-run-id",
        default=None,
        help="显式指定最近一次已 RVF 的实现 run id（最高优先级）。",
    )
    handoff_group = arm.add_mutually_exclusive_group()
    handoff_group.add_argument("--handoff", help="handoff markdown 文件路径（用于提取 run_id）。")
    handoff_group.add_argument(
        "--handoff-text-file", help="包含 pasted handoff 内容的临时文本文件。"
    )
    handoff_group.add_argument(
        "--stdin", action="store_true", help="从 stdin 读取 handoff 内容以提取 run_id。"
    )
    arm.add_argument(
        "--task-id",
        default=None,
        help="kanban task id（marker 主维度）；缺省回退到 kanban env。",
    )
    arm.add_argument(
        "--session-id",
        default=None,
        help="会话 id（无 task_id 时的 marker 维度）。",
    )
    arm.add_argument("--reason", default="failed_impl_reentry")
    arm.add_argument("--source", default="rvf_rescope")
    arm.add_argument(
        "--log-root",
        default=None,
        help="Override CODEX_RVF_LOG_ROOT for this invocation (test hook).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)
    if args.subcommand == "arm":
        return _cmd_arm(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
