#!/usr/bin/env python3
"""agentic_initiation_guardian_dispatch — 发起层守护体系(S8·R23)。

「hook 发起 → agent 发起」overhaul 落地后的**有界、临时**看门狗(评审分歧已裁决:
本模块复用现有 detached 派发机制、1/2/4/8/16 节点自动下线,非通用 orchestration):

1. **守护分析派发(节点计数)**:每次 RVF analyze 线程完成后计数 +1;累计数首次
   到达 {1,2,4,8,16} 节点时,派发一个守护分析 agent(detached,复用
   ``rvf_detached_thread.launch_detached``),按
   ``docs/agentic-initiation-overhaul/guardian-agent-handoff.md`` 审阅发起层健康度。
   计数到 16 后守护自动下线,并提示用户进行 R9 删除确认(注意:物理删除 SHA
   marker 前必须先把 reviewed_head 持久化进 tracker —— 方案 B 写侧,见 S6 注释)。
2. **wall-clock 心跳(独立于 analyze;评审共识修正:发起层死了 analyze 不着火)**:
   Stop hook 每次 Stop 事件 best-effort 记一次心跳;若最近 N 天(默认 3)内
   injection-attempt ledger 与 RVF review 完成数**同时为零**而 Stop 事件数非零,
   判定「发起层疑似静默死亡」并告警(systemMessage 由调用方转发)。

落盘(与 S1/S4 同一状态根家族,测试须 ``RVF_HOME`` 隔离,绝不写真实 ``~/.rvf``):
``$RVF_AGENTIC_INITIATION_GUARDIAN_ROOT`` > ``$RVF_HOME``/``~/.rvf`` 下
``agentic-initiation-guardian/``:

- ``guardian-analyze-completion-counter.json``:analyze 完成累计数 + 已派发节点。
- ``guardian-daily-event-counts.json``:按 UTC 日期分桶的 stop-event / review-completion
  计数(心跳分子分母,常数大小,不会无界增长)。
- ``guardian-analysis-<n>/``:第 n 节点守护分析的 prompt/log/status/lock。

全部 API **best-effort**:任何 OSError/异常吞掉并返回退化结果,守护体系的可观测性
记录**永不**阻断 Stop hook / finalize / analyze 主流程。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path,供 core.* import


SCHEMA_VERSION = 1
ENV_RVF_HOME = "RVF_HOME"
ENV_GUARDIAN_ROOT = "RVF_AGENTIC_INITIATION_GUARDIAN_ROOT"
GUARDIAN_DIRNAME = "agentic-initiation-guardian"
ANALYZE_COMPLETION_COUNTER_FILENAME = "guardian-analyze-completion-counter.json"
DAILY_EVENT_COUNTS_FILENAME = "guardian-daily-event-counts.json"

# 守护分析派发节点(R23):首次到达即派发;16 后守护下线。
GUARDIAN_DISPATCH_MILESTONES = (1, 2, 4, 8, 16)
GUARDIAN_RETIREMENT_MILESTONE = 16

# 心跳判定窗口(天):窗口内 stop>0 且 injection==0 且 review 完成==0 → 告警。
DEFAULT_INITIATION_LIVENESS_WINDOW_DAYS = 3

# 守护分析 agent 的 wall-clock backstop:守护分析是有界审阅任务,给足余量即可。
GUARDIAN_ANALYSIS_TOTAL_TIMEOUT_SECONDS = 2700.0

GUARDIAN_HANDOFF_DOC_RELPATH = "docs/agentic-initiation-overhaul/guardian-agent-handoff.md"

R9_DELETION_CONFIRMATION_PROMPT = (
    "守护体系已完成第 16 节点分析并自动下线(R23)。请进行 R9 观察期收尾确认:"
    "SHA marker 家族是否可物理删除。注意前置依赖——删除前必须先把 reviewed_head "
    "持久化进 tracker(方案 B 写侧,见 core/session_scope_allocation/"
    "reviewable_unit_diff_tracker.py 的 OBSERVATION-WINDOW(R9) 注释),"
    "否则 committed-round 窗口种子失源、orphan 检测退化。"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_now(now: datetime | None) -> datetime:
    if now is None:
        return _utc_now()
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _rvf_home() -> Path:
    configured = os.environ.get(ENV_RVF_HOME)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".rvf"


def guardian_state_root(root: str | Path | None = None) -> Path:
    """守护体系落盘根:显式参数 > 专用 env > ``$RVF_HOME``/``~/.rvf`` 下固定子目录。"""
    if root is not None:
        return Path(root).expanduser()
    configured = os.environ.get(ENV_GUARDIAN_ROOT)
    if configured:
        return Path(configured).expanduser()
    return _rvf_home() / GUARDIAN_DIRNAME


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


@contextmanager
def _guardian_counter_file_lock(path: Path) -> Iterator[None]:
    """用旁路 ``.lock`` 文件串行化守护计数 RMW；锁不可用时退化为无锁 best-effort。

    正常锁持有区只包含本地 JSON 读写，不包含 detached 派发等外部操作，因此并发者
    只会短暂等待。若目录/锁文件/``flock`` 本身不可用，仍让调用方尝试原有无锁路径；
    守护可观测性故障不得阻断 Stop/finalize/analyze 主流程。
    """
    handle = None
    locked = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.with_name(path.name + ".lock").open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        locked = True
    except OSError:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
            handle = None
    try:
        yield
    finally:
        if handle is not None:
            if locked:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                handle.close()
            except OSError:
                pass


def _resolve_guardian_handoff_doc() -> Path | None:
    """从当前脚本逐级上溯，解析可选的 repo 内 guardian handoff 文档。"""
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / GUARDIAN_HANDOFF_DOC_RELPATH
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# 守护分析派发(analyze 完成节点计数)
# ---------------------------------------------------------------------------


def _default_guardian_analysis_dispatcher(
    *,
    milestone: int,
    guardian_root: Path,
    metrics_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """默认守护分析派发器:复用 detached 派发机制拉起 headless 守护分析 agent。

    host 选择复用 analyze 线程的 facade(无 transcript → 兜底 codex);prompt =
    guardian handoff 文档指引 + 当次 metrics 快照。任何失败由调用方 catch-all。
    """
    from rvf_detached_thread import launch_detached  # noqa: PLC0415 - 惰性,守护派发是罕见路径
    from rvf_analyze_thread import build_analyze_command, select_host  # noqa: PLC0415
    from core.run_ledger.run_ledger import safe_token  # noqa: PLC0415

    analysis_dir = guardian_root / f"guardian-analysis-{milestone}"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    handoff_doc = _resolve_guardian_handoff_doc()
    prompt_lines = [
        "你是 RVF「hook 发起 → agent 发起」overhaul 的守护分析 agent(S8·R23,"
        f"第 {milestone}/16 节点)。",
        "",
        (
            "职责手册(必读,含决策登记簿/审计 TOP/SANTA 评审发现/观察指标定义):"
            + str(handoff_doc)
            if handoff_doc is not None
            else (
                f"职责手册相对路径:{GUARDIAN_HANDOFF_DOC_RELPATH}。当前部署副本未携带该文档;"
                "请从目标仓库根查找该路径,并先按下方 metrics 快照完成基础健康度核对。"
            )
        ),
        "",
        "本节点 metrics 快照:",
        "```json",
        json.dumps(metrics_snapshot, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "请按手册逐项核对观察指标,产出一份守护分析报告(markdown)写入"
        f" {analysis_dir / 'guardian-analysis-report.md'} ,重点标注:注入尝试 canary"
        " 对账缺口、token lost/熔断异常、发起层疑似静默死亡信号、需要 elevate 给用户的项。",
    ]
    prompt_path = analysis_dir / ".guardian-analysis.prompt.md"
    prompt_path.write_text("\n".join(prompt_lines), encoding="utf-8")

    host = select_host({})
    agent_argv, uses_stdin = build_analyze_command(host)
    session_name = f"rvf-guardian-{safe_token(f'milestone-{milestone}')}"
    status_path = analysis_dir / ".guardian-analysis.status.json"
    started_at = _coerce_now(None).isoformat().replace("+00:00", "Z")
    result = launch_detached(
        session_name=session_name,
        argv=agent_argv,
        stdin_path=prompt_path if uses_stdin else None,
        log_path=analysis_dir / ".guardian-analysis.log",
        status_path=status_path,
        lock_path=analysis_dir / ".guardian-analysis.lock",
        status_payload={
            "schema_version": SCHEMA_VERSION,
            "guardian_milestone": milestone,
            "host": host,
            "command": agent_argv,
            "started_at": started_at,
            "returncode": None,
            "finished_at": None,
            "error": None,
        },
        exports={"RVF_SUPPRESS_STOP_HOOK": "1"},
        idempotency_key=f"rvf-guardian:milestone-{milestone}",
        total_timeout_seconds=GUARDIAN_ANALYSIS_TOTAL_TIMEOUT_SECONDS,
    )
    return {
        "milestone": milestone,
        "host": host,
        "analysis_dir": str(analysis_dir),
        "launch_status": result.get("launch_status"),
        "error": result.get("error"),
    }


def record_analyze_completion(
    *,
    run_dir: str | None = None,
    now: datetime | None = None,
    root: str | Path | None = None,
    guardian_analysis_dispatcher: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """RVF analyze 线程完成后调用:累计数 +1;首达 {1,2,4,8,16} 节点则派发守护分析。

    返回结构化结果(``analyze_completion_count`` / ``milestone_dispatched`` /
    ``guardian_retired`` / ``retirement_prompt``)。**best-effort**:任何异常收敛为
    ``{"status": "guardian_record_failed", ...}``,绝不外抛。
    """
    try:
        current = _coerce_now(now)
        guardian_root = guardian_state_root(root)
        counter_path = guardian_root / ANALYZE_COMPLETION_COUNTER_FILENAME
        # 第一段短 RMW：先原子认领本次 analyze 完成计数。派发放在锁外，避免 tmux
        # 启动等待把并发 Stop/analyze 路径拖住；派发成功后再做第二段合并 RMW。
        with _guardian_counter_file_lock(counter_path):
            counter = _read_json_dict(counter_path)
            count = int(counter.get("analyze_completion_count") or 0) + 1
            dispatched_milestones = sorted(
                {
                    int(v)
                    for v in counter.get("dispatched_milestones") or []
                    if isinstance(v, (int, float))
                }
            )
            already_retired = bool(counter.get("guardian_retired")) or (
                GUARDIAN_RETIREMENT_MILESTONE in dispatched_milestones
            )
            _atomic_write_json(
                counter_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "analyze_completion_count": count,
                    "dispatched_milestones": dispatched_milestones,
                    "guardian_retired": already_retired,
                    "last_recorded_at": current.isoformat().replace("+00:00", "Z"),
                    "last_run_dir": run_dir,
                },
            )

        result: dict[str, Any] = {
            "status": "recorded",
            "analyze_completion_count": count,
            "milestone_dispatched": None,
            "guardian_retired": already_retired,
            "retirement_prompt": None,
        }

        due_milestones = [
            milestone
            for milestone in GUARDIAN_DISPATCH_MILESTONES
            if count >= milestone and milestone not in dispatched_milestones
        ]
        if not already_retired and due_milestones:
            milestone = max(due_milestones)
            dispatcher = guardian_analysis_dispatcher or _default_guardian_analysis_dispatcher
            try:
                dispatch_result = dispatcher(
                    milestone=milestone,
                    guardian_root=guardian_root,
                    metrics_snapshot=collect_guardian_metrics_snapshot(
                        now=current, root=guardian_root, run_dir=run_dir
                    ),
                )
                if (
                    isinstance(dispatch_result, dict)
                    and dispatch_result.get("launch_status") == "launch_failed"
                ):
                    raise RuntimeError(
                        str(dispatch_result.get("error") or "guardian detached launch failed")
                    )
                result["milestone_dispatched"] = milestone
                result["dispatch_result"] = dispatch_result

                # 第二段短 RMW：只在真实/幂等派发成功后合并节点。较高节点吸收所有
                # 尚未记录的较低 due 节点，避免一次恢复后形成补派风暴。
                with _guardian_counter_file_lock(counter_path):
                    latest = _read_json_dict(counter_path)
                    latest_dispatched = {
                        int(v)
                        for v in latest.get("dispatched_milestones") or []
                        if isinstance(v, (int, float))
                    }
                    latest_dispatched.update(
                        due for due in due_milestones if due <= milestone
                    )
                    was_retired = bool(latest.get("guardian_retired"))
                    now_retired = GUARDIAN_RETIREMENT_MILESTONE in latest_dispatched
                    latest["schema_version"] = SCHEMA_VERSION
                    latest["dispatched_milestones"] = sorted(latest_dispatched)
                    latest["guardian_retired"] = was_retired or now_retired
                    _atomic_write_json(counter_path, latest)
                dispatched_milestones = sorted(latest_dispatched)
                result["guardian_retired"] = was_retired or now_retired
                if now_retired and not was_retired:
                    result["retirement_prompt"] = R9_DELETION_CONFIRMATION_PROMPT
            except Exception as exc:  # noqa: BLE001 - 守护派发失败绝不阻断 analyze 完成路径
                result["dispatch_error"] = f"{type(exc).__name__}: {exc}"
        return result
    except Exception as exc:  # noqa: BLE001 - 守护记录永不阻断主流程
        return {"status": "guardian_record_failed", "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# wall-clock 心跳(独立于 analyze)
# ---------------------------------------------------------------------------


def _bump_daily_count(
    *,
    kind: str,
    now: datetime,
    root: str | Path | None,
) -> None:
    """把 ``kind`` 在当日(UTC)的计数 +1;文件按日期分桶,并裁剪 30 天前的旧桶。"""
    guardian_root = guardian_state_root(root)
    path = guardian_root / DAILY_EVENT_COUNTS_FILENAME
    with _guardian_counter_file_lock(path):
        payload = _read_json_dict(path)
        counts = payload.get(kind)
        counts = dict(counts) if isinstance(counts, dict) else {}
        day = current_day = now.date().isoformat()
        counts[day] = int(counts.get(current_day) or 0) + 1
        cutoff = (now - timedelta(days=30)).date().isoformat()
        counts = {d: c for d, c in counts.items() if d >= cutoff}
        payload[kind] = counts
        payload["schema_version"] = SCHEMA_VERSION
        _atomic_write_json(path, payload)


def _window_total(payload: dict[str, Any], kind: str, *, since_day: str) -> int:
    counts = payload.get(kind)
    if not isinstance(counts, dict):
        return 0
    total = 0
    for day, value in counts.items():
        if isinstance(day, str) and day >= since_day:
            try:
                total += int(value)
            except (TypeError, ValueError):
                continue
    return total


def record_stop_event_heartbeat(
    *, now: datetime | None = None, root: str | Path | None = None
) -> None:
    """Stop hook 每次 Stop 事件调用(best-effort):当日 stop-event 计数 +1。"""
    try:
        _bump_daily_count(kind="stop_event_counts_by_utc_day", now=_coerce_now(now), root=root)
    except Exception:  # noqa: BLE001 - 心跳记录永不阻断 Stop hook
        pass


def record_review_completion(
    *, now: datetime | None = None, root: str | Path | None = None
) -> None:
    """RVF review 真完成(finalize ``did_review``)时调用(best-effort):当日计数 +1。"""
    try:
        _bump_daily_count(kind="review_completion_counts_by_utc_day", now=_coerce_now(now), root=root)
    except Exception:  # noqa: BLE001 - 记录永不阻断 finalize
        pass


def _injection_attempt_count_in_window(*, since_day: str) -> int:
    """扫注入尝试 ledger 根目录,统计窗口内(按 ``recorded_at`` 日期)的记录数。

    best-effort:ledger 缺失/坏行一律计 0/跳过。ledger 按 worktree 分 jsonl,单机
    体量有限,窗口过滤在行级完成。
    """
    import agentic_initiation_injection_attempt_ledger as injection_attempt_ledger  # noqa: PLC0415

    ledger_root = injection_attempt_ledger.injection_attempt_ledger_root()
    if not ledger_root.is_dir():
        return 0
    total = 0
    for ledger_file in ledger_root.glob("*.jsonl"):
        try:
            text = ledger_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            recorded_at = record.get("recorded_at")
            if isinstance(recorded_at, str) and recorded_at[:10] >= since_day:
                total += 1
    return total


def _initiation_layer_has_historical_evidence(*, root: str | Path | None) -> bool:
    """发起层是否**曾经**活跃过(analyze 完成计数 / review 完成计数 / 注入 ledger 任一非零)。

    冷启动护栏:全新环境(fresh RVF_HOME / 测试隔离根)里发起层「尚未出生」而非
    「静默死亡」,首个 Stop 事件不应触发误报;告警语义收敛为「曾经活着、后来死了」。
    (代价:发起层若从部署第一天就坏,由 S9 E2E dogfood 与守护节点分析兜底,不靠心跳。)
    """
    guardian_root = guardian_state_root(root)
    counter = _read_json_dict(guardian_root / ANALYZE_COMPLETION_COUNTER_FILENAME)
    if int(counter.get("analyze_completion_count") or 0) > 0:
        return True
    payload = _read_json_dict(guardian_root / DAILY_EVENT_COUNTS_FILENAME)
    if _window_total(payload, "review_completion_counts_by_utc_day", since_day="0000-00-00") > 0:
        return True
    try:
        return _injection_attempt_count_in_window(since_day="0000-00-00") > 0
    except Exception:  # noqa: BLE001
        return False


def evaluate_initiation_layer_liveness(
    *,
    now: datetime | None = None,
    root: str | Path | None = None,
    window_days: int = DEFAULT_INITIATION_LIVENESS_WINDOW_DAYS,
) -> dict[str, Any] | None:
    """wall-clock 心跳判定:窗口内 Stop 事件非零而注入尝试与 review 完成同时为零 → 告警。

    ``window_days=N`` 按 UTC **日桶**计算:下界日与当日均包含,即共回看 ``N+1``
    个日桶，从而保证至少覆盖完整 ``N×24h``。Stop 分母已排除显式 suppress 的 detached
    会话；仍会包含通过护栏后但最终无候选的 Stop，这是当前保守 canary 的已知限制。

    前置冷启动护栏(见 :func:`_initiation_layer_has_historical_evidence`):发起层
    从未活跃过的环境不告警。返回告警 dict(调用方转 systemMessage)或 ``None``
    (健康/证据不足)。**best-effort**:任何异常返回 ``None``,绝不阻断 Stop hook。
    """
    try:
        current = _coerce_now(now)
        since_day = (current - timedelta(days=window_days)).date().isoformat()
        guardian_root = guardian_state_root(root)
        payload = _read_json_dict(guardian_root / DAILY_EVENT_COUNTS_FILENAME)
        stop_events = _window_total(payload, "stop_event_counts_by_utc_day", since_day=since_day)
        review_completions = _window_total(
            payload, "review_completion_counts_by_utc_day", since_day=since_day
        )
        if stop_events <= 0:
            return None
        injection_attempts = _injection_attempt_count_in_window(since_day=since_day)
        if injection_attempts > 0 or review_completions > 0:
            return None
        if not _initiation_layer_has_historical_evidence(root=root):
            return None
        return {
            "alarm": "initiation_layer_suspected_silent_death",
            "window_days": window_days,
            "since_utc_day": since_day,
            "stop_event_count": stop_events,
            "injection_attempt_count": injection_attempts,
            "review_completion_count": review_completions,
            "message": (
                f"RVF 发起层疑似静默死亡:按 UTC 日桶回看 {window_days} 天边界"
                f"(含当日共 {window_days + 1} 个桶,保证覆盖至少完整 {window_days * 24} 小时),"
                "Stop 事件 "
                f"{stop_events} 次,但注入尝试与 review 完成均为 0。"
                "请检查 Stop hook 决策点注入链路(injection-attempt ledger / 决策 token store),"
                "必要时按 docs/agentic-initiation-overhaul/guardian-agent-handoff.md 的恢复路径回滚。"
            ),
        }
    except Exception:  # noqa: BLE001 - 心跳判定永不阻断 Stop hook
        return None


def collect_guardian_metrics_snapshot(
    *,
    now: datetime | None = None,
    root: str | Path | None = None,
    run_dir: str | None = None,
    window_days: int = DEFAULT_INITIATION_LIVENESS_WINDOW_DAYS,
) -> dict[str, Any]:
    """给守护分析 agent 的观察指标快照(全部 best-effort,失败字段置 None)。"""
    current = _coerce_now(now)
    since_day = (current - timedelta(days=window_days)).date().isoformat()
    guardian_root = guardian_state_root(root)
    payload = _read_json_dict(guardian_root / DAILY_EVENT_COUNTS_FILENAME)
    counter = _read_json_dict(guardian_root / ANALYZE_COMPLETION_COUNTER_FILENAME)
    try:
        injection_attempts = _injection_attempt_count_in_window(since_day=since_day)
    except Exception:  # noqa: BLE001
        injection_attempts = None
    return {
        "snapshot_at": current.isoformat().replace("+00:00", "Z"),
        "window_days": window_days,
        "since_utc_day": since_day,
        "triggering_run_dir": run_dir,
        "analyze_completion_count": counter.get("analyze_completion_count"),
        "dispatched_milestones": counter.get("dispatched_milestones"),
        "stop_event_count_in_window": _window_total(
            payload, "stop_event_counts_by_utc_day", since_day=since_day
        ),
        "review_completion_count_in_window": _window_total(
            payload, "review_completion_counts_by_utc_day", since_day=since_day
        ),
        "injection_attempt_count_in_window": injection_attempts,
        "liveness_alarm": evaluate_initiation_layer_liveness(
            now=current, root=root, window_days=window_days
        ),
    }


# ---------------------------------------------------------------------------
# CLI(detached wrapper 的 post-exit 回调用)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RVF agentic-initiation guardian dispatch (S8·R23)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser(
        "record-analyze-completion",
        help="analyze 线程完成回调:计数 +1,节点首达则派发守护分析。",
    )
    record.add_argument("--run-dir", default=None, help="完成的 analyze 所属 RVF run dir。")
    args = parser.parse_args(argv)

    if args.command == "record-analyze-completion":
        result = record_analyze_completion(run_dir=args.run_dir)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
