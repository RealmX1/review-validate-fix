#!/usr/bin/env python3
"""主 Agent「正在等待已派发后台/外部 agent」的 wait-on 登记 marker。

RVF Stop hook 的底层假设是「主 agent plain-text 停下 = 完成了一个可审单元」。唯一的例外是
主 agent 把回合 park 掉去**等一个它刚派发的后台/外部 agent**（典型 = 全局 delegate-to-cursor
skill 的 WRITE 模式：派一个后台实现 agent、留脏工作树、纯文本停下等它收编）——此时它没完成、
只是在等，不该被自动触发一轮新 RVF。

RVF **内**的「等已派发 reviewer」由 in-progress 锁 + force-continue（Fix A `ab3dc07`）覆盖；
本 marker 补的是 RVF **外 / 实现期**那条：RVF 不是派发方、没有自有 chokepoint 能 arm，能写
marker 的只有「主 agent 用的 dispatch 层」。故本模块做成一个**单调用契约**：
- writer（dispatch 层 / 待建 orchestration 系统）在「派后台 agent 且打算等它」时 ``arm``、
  在「消费完结果」时 ``clear``；
- reader（Stop hook 的 ``awaiting_dispatched_agent_decision`` 闸）在每次 Stop 查「本 session
  是否还有未完成的 wait-on 派发」，有则静默跳过本轮 RVF。

信号只能是 marker file、不能是 agent 文本 sentinel：Stop hook 在 Claude 下读不到主 agent 自己
最后一轮文本（``rvf_handoff.latest_assistant_message`` 只解析 Codex rollout schema），而 marker
file 信号 Stop hook 已在 harness 无关地扫（in-progress 锁 / pending，均在 ``~/.rvf/`` 下）。

# ponytail: awaiting orchestration overhaul —— 本 marker + arm/clear helper 是**过渡契约**，
# 刻意做成单调用接口。待建的 agent orchestration 系统应被设计成**原生**服务这套「wait-on 派发
# 登记（arm / clear / query 是否还有未完成 wait-on 派发）」能力：届时登记是 orchestration
# unified dispatch 的内建职责，writer 全部归 orchestration，RVF 侧只保留 Stop 闸作为 reader。
# 与之配套的另一处待收敛是 dispatch_reviewers.py 的 detached-thread + status.json + in-progress
# 锁那套「RVF 内 reviewer 等待」——两处应一并归并到 orchestration 的同一条 wait-on 登记/查询路径，
# 使「RVF 内 reviewer 等待」与「RVF 外实现期派发等待」走同一处，Stop 侧只需读一处。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from core.run_ledger.run_ledger import safe_token  # noqa: E402

# 复用 in-progress 锁族的 marker 原语，不重写：原子写 / 过期判定 / iso 时间 / 锁根 env / 版本。
# 本模块与 kanban_followup_lock 同属一族紧耦合的 ~/.rvf marker，复用其稳定私有原语是刻意选择
# （见 plan「复用，不要重写」），避免两份 atomic-write / 过期判定漂移。
from kanban_followup_lock import (
    LOCK_ROOT_ENV,
    MARKER_VERSION,
    STATUS_ACTIVE,
    _atomic_write,
    marker_status,
)


SUBDIR_NAME = "awaiting-dispatched-agent"
AWAITING_STATE = "awaiting_dispatched_agent"
# 后台实现 agent（如 delegate-to-cursor WRITE 模式）可能跑很久，故默认给比 pending(15min) 远长的
# 上界。TTL = 「writer 忘 clear / 后台 agent 静默死」的兜底自释放窗口：到期→STALE→读侧惰性清。
DEFAULT_TTL_SECONDS = 2 * 60 * 60
TTL_ENV = "CODEX_RVF_AWAITING_DISPATCHED_AGENT_TTL_SECONDS"


def _root(root: Path | None = None) -> Path:
    if root is not None:
        return root.expanduser() / SUBDIR_NAME
    raw = os.environ.get(LOCK_ROOT_ENV)
    if raw and raw.strip():
        # in-progress 锁把该 env 视为「直接就是 in-progress 目录」；本族在其下另起子目录，
        # 与 in-progress / pending marker 物理隔离，避免同名文件互相覆盖。
        return Path(raw).expanduser() / SUBDIR_NAME
    return Path.home() / ".rvf" / SUBDIR_NAME


def _marker_path(
    *, main_session_id: str, dispatched_agent_id: str, root: Path | None = None
) -> Path:
    name = f"sess-{safe_token(main_session_id)}__dispatch-{safe_token(dispatched_agent_id)}.json"
    return _root(root) / name


def ttl_seconds() -> float:
    raw = os.environ.get(TTL_ENV)
    if raw is None or not raw.strip():
        return float(DEFAULT_TTL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_TTL_SECONDS)
    return max(0.0, value)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_after(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat()
        .replace("+00:00", "Z")
    )


def awaiting_marker_payload(
    *,
    main_session_id: str,
    dispatched_agent_id: str,
    dispatcher: str | None,
    description: str | None,
    repo: str | None,
    cwd: str | None,
    ttl: float,
) -> dict[str, Any]:
    return {
        "marker_version": MARKER_VERSION,
        "state": AWAITING_STATE,
        "wait_on": True,
        "main_session_id": main_session_id,
        "dispatched_agent_id": dispatched_agent_id,
        "dispatcher": dispatcher,
        "description": description,
        "dispatched_at": _iso_now(),
        "expires_at": _iso_after(ttl),
        "ttl_seconds": ttl,
        "repo": repo,
        "cwd": cwd,
    }


def arm_awaiting_dispatched_agent(
    *,
    main_session_id: str | None,
    dispatched_agent_id: str | None,
    dispatcher: str | None = None,
    description: str | None = None,
    repo: str | None = None,
    cwd: str | None = None,
    ttl_seconds_override: float | None = None,
    root: Path | None = None,
) -> Path | None:
    """登记一条「主 session 正在等待某个已派发 agent」的 wait-on marker，返回写入路径。

    main_session_id 与 dispatched_agent_id 都缺时无法定位 marker，返回 None（no-op）。
    """
    if not (isinstance(main_session_id, str) and main_session_id.strip()):
        return None
    if not (isinstance(dispatched_agent_id, str) and dispatched_agent_id.strip()):
        return None
    ttl = ttl_seconds() if ttl_seconds_override is None else max(0.0, float(ttl_seconds_override))
    payload = awaiting_marker_payload(
        main_session_id=main_session_id,
        dispatched_agent_id=dispatched_agent_id,
        dispatcher=dispatcher,
        description=description,
        repo=repo,
        cwd=cwd,
        ttl=ttl,
    )
    target = _marker_path(
        main_session_id=main_session_id, dispatched_agent_id=dispatched_agent_id, root=root
    )
    _atomic_write(target, payload)
    return target


def clear_awaiting_dispatched_agent(
    *,
    main_session_id: str | None,
    dispatched_agent_id: str | None,
    root: Path | None = None,
) -> list[str]:
    """清掉一条 wait-on marker（dispatcher 消费完派发结果时调用）。返回被删路径列表。"""
    if not (isinstance(main_session_id, str) and main_session_id.strip()):
        return []
    if not (isinstance(dispatched_agent_id, str) and dispatched_agent_id.strip()):
        return []
    path = _marker_path(
        main_session_id=main_session_id, dispatched_agent_id=dispatched_agent_id, root=root
    )
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        return []
    return [str(path)]


def iter_active_awaiting_for_session(
    main_session_id: str | None,
    *,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """枚举本 session 下所有 ACTIVE 的 wait-on marker，顺手惰性清掉 STALE/INVALID 的。

    一个 session 可有多个未完成 wait-on 派发，故按 ``sess-<sid>__dispatch-*.json`` glob。
    保守：只把 ACTIVE 计入「还在等」（→ Stop 跳过 RVF），STALE 惰性删后不计入（→ 放行）；
    masking 的最坏上界即 TTL。逐文件吞 OSError/JSON 错误，绝不因单个坏文件中断整轮扫描。
    """
    if not (isinstance(main_session_id, str) and main_session_id.strip()):
        return []
    base = _root(root)
    if not base.is_dir():
        return []
    prefix = f"sess-{safe_token(main_session_id)}__dispatch-"
    try:
        paths = sorted(base.glob(f"{prefix}*.json"))
    except OSError:
        return []
    active: list[dict[str, Any]] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if marker_status(payload) == STATUS_ACTIVE:
            payload.setdefault("_marker_path", str(path))
            active.append(payload)
            continue
        # STALE / INVALID → 惰性清（best-effort），让本 session 不被卡死/坏 marker 永久挡停。
        try:
            path.unlink()
        except (FileNotFoundError, OSError):
            pass
    return active


def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "主 Agent wait-on 派发登记：arm 一条「正在等待已派发 agent」的 marker，"
            "或在消费完结果时 clear 之。RVF Stop hook 据此在主 agent 仅 park 等待异步派发时不触发 RVF。"
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_arm = sub.add_parser("arm", help="登记一条 wait-on marker。")
    p_arm.add_argument("--session", required=True, help="主 Agent 的 session id。")
    p_arm.add_argument("--dispatch-id", required=True, help="被派发 agent 的标识。")
    p_arm.add_argument("--dispatcher", help="派发方名字（如 delegate-to-cursor）。")
    p_arm.add_argument("--description", help="人类可读的派发说明。")
    p_arm.add_argument("--repo", help="目标仓库路径。")
    p_arm.add_argument("--cwd", help="派发时的工作目录。")
    p_arm.add_argument("--ttl", type=float, help="自定义 TTL 秒数（默认见 DEFAULT_TTL_SECONDS）。")

    p_clear = sub.add_parser("clear", help="清掉一条 wait-on marker。")
    p_clear.add_argument("--session", required=True, help="主 Agent 的 session id。")
    p_clear.add_argument("--dispatch-id", required=True, help="被派发 agent 的标识。")

    p_list = sub.add_parser("list", help="列出本 session 下所有 ACTIVE wait-on marker（JSON）。")
    p_list.add_argument("--session", required=True, help="主 Agent 的 session id。")

    args = parser.parse_args(argv)

    if args.command == "arm":
        path = arm_awaiting_dispatched_agent(
            main_session_id=args.session,
            dispatched_agent_id=args.dispatch_id,
            dispatcher=args.dispatcher,
            description=args.description,
            repo=args.repo,
            cwd=args.cwd,
            ttl_seconds_override=args.ttl,
        )
        if path is None:
            print("arm 失败：session 与 dispatch-id 不能为空。", file=sys.stderr)
            return 2
        print(str(path))
        return 0

    if args.command == "clear":
        removed = clear_awaiting_dispatched_agent(
            main_session_id=args.session,
            dispatched_agent_id=args.dispatch_id,
        )
        print(json.dumps(removed, ensure_ascii=False))
        return 0

    if args.command == "list":
        active = iter_active_awaiting_for_session(args.session)
        print(json.dumps(active, ensure_ascii=False, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
