#!/usr/bin/env python3
"""rvf-agent 决策 CLI（RVF overhaul S3：主 agent 发起审查的**单一入口**）。

RVF overhaul 把「要不要审、审什么」的发起决策权从 Stop hook 交给主 agent。Stop hook 降级为
决策点信使：它铸一枚决策 token、把 token 连同候选摘要注入给主 agent；主 agent 简析后经**本
CLI** 显式 ``start`` / ``scope …`` / ``dispatch`` 或 ``decline``。token 一律由主 agent 从注入
prompt 里拿到、显式回传（本 CLI 不枚举、不发现 token —— R18：不存在 ``candidates`` 子命令）。

子命令（实施计划 §四 S3 / §三 agent 侧流程）：

  * ``start --token --reason [--retry]``：token → ``acknowledged`` → **backend/launch 前置校验 +
    provider-health 门** → ``prepare_review_run`` 全套（原 UPS ``_run_shared_workflow`` 的重量级
    准备职能退役至此，本切片**不改 UPS 本身**，只把该职能在 CLI 里实现） → **自动 scope 产出**
    落盘（供 scope 子命令编辑）。任一步失败 → ``record_start_failure`` 回退**保持 acknowledged**，
    支持 ``start --retry``，不吞决策。
  * ``scope annotate --token --entry <path>:<note> [--entry …]``（批量注释）/
    ``scope exclude|include --token <path>… --reason``：改动自动 scope + 写遥测；scope 例外
    （exclude/include）按「同源 + 同类型」幂等键计数，n≥3 自动立 issue 进本地 scope-issue ledger
    （R17，不接外部 backend）。**仅 acknowledged 态可用**。
  * ``dispatch --token --strictness <light|standard|strict>``：scope 定稿 → 严格度终选（读 S2
    preset）→ 派发 reviewer（复用现有 ``dispatch_reviewers`` 语义，S7 才模块化，本切片不重写它）
    → ``mark_dispatched``（终态）。
  * ``decline --token --reason``：理由持久化到 context 之外（rewind 不可抹的落盘 ledger）+
    ``mark_declined``（指纹自动入 declined 冷却表，指纹未变不再注入）→ token 终态 ``declined``。

**明确不做**（R18/R22）：``candidates`` 子命令不存在；``_backfill_child_session`` /
``_resolve_child_transcript_path`` 不迁入本 CLI（留 UPS —— 它们必须在 child 的 UPS 事件到达后
才有数据，见 R22）。

落盘根一律显式参数 > 专用环境变量 > ``$RVF_HOME``/``~/.rvf`` 下固定子目录；测试须 tmp + HOME
隔离，绝不写真实 ``~/.rvf`` / ``~/.codex``。所有 legacy ``CODEX_RVF_*`` env 读取收敛到单一
accessor ``legacy_codex_rvf_env``（为任务 #11 env 迁移预留单点）。

输出对 agent 友好：每个子命令打印人类可读的成功/失败/下一步提示，并附一行
``RVF_AGENT_RESULT_JSON:`` 机器可解析 JSON；exit code 语义明确（见 ``EXIT_*``）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — 把 pyroot 上 sys.path，供 core.* / 兄弟脚本 import

import rvf_bootstrap_gate  # noqa: E402
import rvf_decision_token_store as token_store  # noqa: E402
import rvf_prep_file  # noqa: E402


# ---------------------------------------------------------------------------
# exit code 语义（对 agent / 调用方稳定）
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 2  # argparse 用法错误（缺参 / 非法子命令）
EXIT_TOKEN_STATE = 3  # token 缺失 / 已终态 / 非 acknowledged 态等**非可重试**决策错误
EXIT_START_FAILED = 4  # start 前置校验 / prepare 失败：token 回退保持 acknowledged，**可 --retry**
EXIT_DISPATCH_FAILED = 5  # dispatch 派发失败
# start 撞上 pre-review local-base bootstrap 门：**不是失败**，而是一个可恢复的等待态。token 保持
# acknowledged、gate 落盘、run 锚点挂上；agent 跑完同步 skill 后 `start --retry` 原地接续同一轮。
EXIT_BOOTSTRAP_REQUIRED = 6
# decline-dispatched-review 的**部分生效**：谢绝理由已落盘，但豁免 / 释放 lease / 清锁里至少一步
# 没按预期完成。刻意与 EXIT_OK 分开：这条命令的三步副作用全是 best-effort，历史实现却固定返回
# ok，DB/文件写失败时用户看到的是「谢绝成功」而实际 scope 与锁可能仍生效（谎报成功）。调用方按
# 非 0 即「不要把本轮当作已谢绝」处理；detail.failed_steps 给出逐步实况。
EXIT_DECLINE_DISPATCHED_REVIEW_INCOMPLETE = 7

# bootstrap 恢复重放的占位窗口 = prepare 预算 + 这段余量。占位在 prepare 收尾时立刻释放，余量只
# 覆盖「持有者被硬杀、来不及释放」的残留：过窗后下一次 `start --retry` 可接手，不会被永久堵死。
BOOTSTRAP_REPLAY_CLAIM_GRACE_SECONDS = 60.0


# ---------------------------------------------------------------------------
# 单一 legacy env accessor（任务 #11 env 迁移预留单点）
# ---------------------------------------------------------------------------

LEGACY_CODEX_RVF_ENV_PREFIX = "CODEX_RVF_"


def legacy_codex_rvf_env(suffix: str, default: str | None = None) -> str | None:
    """读取 legacy ``CODEX_RVF_<suffix>`` 环境变量的**唯一入口**。

    本 CLI 的新代码对 legacy ``CODEX_RVF_*`` env 的一切读取都必须经此函数收敛为单点，
    使任务 #11（env 命名迁移到非 codex 前缀）只需改这一处的映射。注意：被复用的既有模块
    （``prepare_review_run`` / ``run_ledger`` / ``rvf_prep_file`` 等）内部各自读它们自己的
    env，不在本单点约束内——本约束只覆盖**本 CLI 新增代码**。
    """
    return os.environ.get(f"{LEGACY_CODEX_RVF_ENV_PREFIX}{suffix}", default)


# ---------------------------------------------------------------------------
# 落盘根解析（显式参数 > 专用 env > $RVF_HOME/~/.rvf 下固定子目录）
# ---------------------------------------------------------------------------

ENV_RVF_HOME = "RVF_HOME"
ENV_SCOPE_ADJUSTMENT_ROOT = "RVF_AGENTIC_INITIATION_SCOPE_ADJUSTMENT_ROOT"
ENV_DECISION_TELEMETRY_ROOT = "RVF_AGENTIC_INITIATION_DECISION_TELEMETRY_ROOT"
ENV_SCOPE_ISSUE_LEDGER_ROOT = "RVF_AGENTIC_INITIATION_SCOPE_ISSUE_LEDGER_ROOT"
ENV_DECLINED_REASON_LEDGER_ROOT = "RVF_AGENTIC_INITIATION_DECLINED_REASON_LEDGER_ROOT"

SCOPE_ADJUSTMENT_DIRNAME = "agentic-initiation-scope-adjustments"
DECISION_TELEMETRY_DIRNAME = "agentic-initiation-decision-telemetry"
SCOPE_ISSUE_LEDGER_DIRNAME = "agentic-initiation-scope-issue-ledger"
DECLINED_REASON_LEDGER_DIRNAME = "agentic-initiation-declined-decision-reasons"

SCOPE_ADJUSTMENT_SCHEMA_VERSION = 1
SCOPE_ISSUE_SCHEMA_VERSION = 1

# scope 例外「同源 + 同类型」幂等键累计到此阈值自动立 issue（R17：n≥3）。
DEFAULT_SCOPE_EXCEPTION_ISSUE_THRESHOLD = 3

# 字段分隔符（ASCII Unit Separator，正常字段值不含）。
_KEY_FIELD_SEPARATOR = "\x1e"

# scope 例外操作类型。
SCOPE_OPERATION_EXCLUDE = "exclude"
SCOPE_OPERATION_INCLUDE = "include"
SCOPE_OPERATION_ANNOTATE = "annotate"


def _rvf_home() -> Path:
    configured = os.environ.get(ENV_RVF_HOME)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".rvf"


def _resolve_root(explicit: str | Path | None, env_name: str, dirname: str) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    configured = os.environ.get(env_name)
    if configured:
        return Path(configured).expanduser()
    return _rvf_home() / dirname


def scope_adjustment_root(root: str | Path | None = None) -> Path:
    return _resolve_root(root, ENV_SCOPE_ADJUSTMENT_ROOT, SCOPE_ADJUSTMENT_DIRNAME)


def decision_telemetry_root(root: str | Path | None = None) -> Path:
    return _resolve_root(root, ENV_DECISION_TELEMETRY_ROOT, DECISION_TELEMETRY_DIRNAME)


def scope_issue_ledger_root(root: str | Path | None = None) -> Path:
    return _resolve_root(root, ENV_SCOPE_ISSUE_LEDGER_ROOT, SCOPE_ISSUE_LEDGER_DIRNAME)


def declined_reason_ledger_root(root: str | Path | None = None) -> Path:
    return _resolve_root(root, ENV_DECLINED_REASON_LEDGER_ROOT, DECLINED_REASON_LEDGER_DIRNAME)


@dataclass(frozen=True)
class AgentDecisionRoots:
    """本 CLI 的一组落盘根（全部可注入 tmp 隔离）。

    ``token_root`` / ``cooldown_root`` 透传给 ``rvf_decision_token_store``；其余四根是本 CLI
    自有 artifact 的落盘位置。任一为 None 时按「专用 env > ~/.rvf 子目录」解析。
    """

    token_root: str | Path | None = None
    cooldown_root: str | Path | None = None
    scope_adjustment_root: str | Path | None = None
    telemetry_root: str | Path | None = None
    scope_issue_ledger_root: str | Path | None = None
    declined_reason_ledger_root: str | Path | None = None


# ---------------------------------------------------------------------------
# 时间戳 / 哈希 / 原子落盘小工具（与 token store 家族保持一致的语义）
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_now(now: datetime | None) -> datetime:
    return (now or utc_now()).astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return token_store.format_timestamp(value)


def _digest(*parts: str) -> str:
    joined = _KEY_FIELD_SEPARATOR.join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _ensure_parent_dir(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp.write_text(data, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    _ensure_parent_dir(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


# ---------------------------------------------------------------------------
# 遥测（R5：signal→响应采集，落 ~/.rvf/agentic-initiation-decision-telemetry/，路径可注入）
# ---------------------------------------------------------------------------

def _telemetry_path(token: str, root: str | Path | None) -> Path:
    return decision_telemetry_root(root) / f"{token}.jsonl"


def append_decision_telemetry(
    *,
    token: str,
    event: str,
    worktree: str | None,
    session_id: str | None,
    now: datetime,
    root: str | Path | None = None,
    detail: Mapping[str, Any] | None = None,
) -> Path:
    """向该 token 的决策轨迹 jsonl 追加一条遥测事件（每 token 一条轨迹，供 R5 signal 分析）。"""
    path = _telemetry_path(token, root)
    record: dict[str, Any] = {
        "token": token,
        "event": event,
        "worktree": worktree,
        "session_id": session_id,
        "at": _format_timestamp(now),
    }
    if detail:
        record["detail"] = dict(detail)
    _append_jsonl(path, record)
    return path


# ---------------------------------------------------------------------------
# scope 例外 issue ledger（R17：同源 + 同类型幂等键，n≥3 自动立本地 issue，不接外部 backend）
# ---------------------------------------------------------------------------

def _scope_exception_idempotency_key(worktree: str, operation_type: str) -> str:
    """R17 幂等键 = 同源（worktree）+ 同类型（exclude / include）。"""
    return _digest(worktree, operation_type)


def _scope_exception_events_path(key: str, root: str | Path | None) -> Path:
    return scope_issue_ledger_root(root) / "scope-exception-events" / f"{key}.jsonl"


def _scope_issue_path(key: str, root: str | Path | None) -> Path:
    return scope_issue_ledger_root(root) / f"{key}.json"


def record_scope_exception_and_maybe_file_issue(
    *,
    worktree: str,
    operation_type: str,
    path: str,
    reason: str | None,
    token: str,
    now: datetime,
    root: str | Path | None = None,
    threshold: int = DEFAULT_SCOPE_EXCEPTION_ISSUE_THRESHOLD,
) -> dict[str, Any]:
    """记录一次 scope 例外（exclude/include），并在同键**去重路径数** ≥ 阈值时幂等立/更新 issue。

    幂等键 = (worktree, operation_type)。计数以「该键下**去重后的 path 数**」为准（同一路径被反复
    exclude 不重复灌水）。到阈值时把 issue 落到 ``<ledger>/<key>.json``（幂等覆盖，一键一 issue），
    ``newly_filed`` 标记本次是否**首次**跨过阈值（用于「刚立项」提示）。返回计数 / 是否已立 issue /
    issue 路径。
    """
    key = _scope_exception_idempotency_key(worktree, operation_type)
    events_path = _scope_exception_events_path(key, root)
    _append_jsonl(
        events_path,
        {
            "worktree": worktree,
            "operation_type": operation_type,
            "path": path,
            "reason": reason,
            "token": token,
            "at": _format_timestamp(now),
        },
    )
    events = _read_jsonl(events_path)
    distinct_paths = sorted({str(ev.get("path")) for ev in events if ev.get("path")})
    count = len(distinct_paths)

    issue_path = _scope_issue_path(key, root)
    issue_already_existed = issue_path.is_file()
    issue_filed = count >= threshold
    newly_filed = False
    if issue_filed:
        newly_filed = not issue_already_existed
        first_at = events[0].get("at") if events else _format_timestamp(now)
        _atomic_write_json(
            issue_path,
            {
                "schema_version": SCOPE_ISSUE_SCHEMA_VERSION,
                "idempotency_key": key,
                "worktree": worktree,
                "operation_type": operation_type,
                "distinct_path_count": count,
                "threshold": threshold,
                "paths": distinct_paths,
                "first_observed_at": first_at,
                "last_updated_at": _format_timestamp(now),
                "note": (
                    f"同源（{worktree}）+ 同类型（scope {operation_type}）的 scope 例外已累计 "
                    f"{count} 个不同路径（阈值 {threshold}）——疑似候选全集构造有系统性偏差，"
                    "建议回访 signal / 收紧 ownership filter / 调整候选下界。R17：本地 ledger，不接外部 backend。"
                ),
            },
        )
    return {
        "idempotency_key": key,
        "operation_type": operation_type,
        "distinct_path_count": count,
        "threshold": threshold,
        "issue_filed": issue_filed,
        "newly_filed": newly_filed,
        "issue_path": str(issue_path) if issue_filed else None,
        "events_path": str(events_path),
    }


# ---------------------------------------------------------------------------
# 自动 scope 产出（CLI 自有 artifact，落 ~/.rvf/agentic-initiation-scope-adjustments/<token>.json）
# ---------------------------------------------------------------------------

def scope_adjustment_path(token: str, root: str | Path | None = None) -> Path:
    return scope_adjustment_root(root) / f"{token_store.validate_token(token)}.json"


def read_scope_adjustment(token: str, root: str | Path | None = None) -> dict[str, Any] | None:
    path = scope_adjustment_path(token, root)
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return parsed if isinstance(parsed, dict) else None


def write_scope_adjustment(overlay: dict[str, Any], *, token: str, root: str | Path | None = None) -> Path:
    path = scope_adjustment_path(token, root)
    _atomic_write_json(path, overlay)
    return path


def _initial_auto_scope_paths(
    candidate_summary: Mapping[str, Any] | None,
    prepare_artifacts: Mapping[str, Any] | None,
) -> list[str]:
    """自动 scope 的初始路径集 = S2 候选摘要里**纳入候选集**的路径（权威）；候选摘要缺失时退回
    prepare 产出的 scope.contract.json 的 ``primary_files``。两者皆空则空 scope（review 走
    manual-all-uncommitted 兜底，仍由 review packet 全量覆盖）。"""
    if isinstance(candidate_summary, Mapping):
        candidates = candidate_summary.get("candidates")
        if isinstance(candidates, list):
            paths = [
                str(c.get("path"))
                for c in candidates
                if isinstance(c, Mapping)
                and c.get("path")
                and c.get("included_in_candidate_set", True)
            ]
            if paths:
                return sorted(dict.fromkeys(paths))
    # 退回 scope.contract.json 的 primary_files（best-effort 读盘）。
    if isinstance(prepare_artifacts, Mapping):
        contract_path = prepare_artifacts.get("scope_contract")
        if isinstance(contract_path, str) and contract_path:
            try:
                contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
                primary = contract.get("primary_files")
                if isinstance(primary, list):
                    return sorted(dict.fromkeys(str(p) for p in primary if p))
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
    return []


def compute_effective_primary_files(overlay: Mapping[str, Any]) -> list[str]:
    """按 auto_scope − exclusions ∪ inclusions 计算 dispatch 定稿的 primary_files（保序、去重）。"""
    auto = [str(p) for p in (overlay.get("auto_scope_paths") or [])]
    excluded = {str(e.get("path")) for e in (overlay.get("exclusions") or []) if isinstance(e, Mapping)}
    included = [str(i.get("path")) for i in (overlay.get("inclusions") or []) if isinstance(i, Mapping) and i.get("path")]
    result: list[str] = []
    for candidate in auto:
        if candidate not in excluded and candidate not in result:
            result.append(candidate)
    for candidate in included:
        if candidate not in excluded and candidate not in result:
            result.append(candidate)
    return result


# ---------------------------------------------------------------------------
# 结果对象（结构化 + 人类可读）
# ---------------------------------------------------------------------------

@dataclass
class CommandResult:
    command: str
    # ok | error | start_failed | dispatch_failed | decline_incomplete
    # （decline_incomplete = decline-dispatched-review 谢绝已落盘但副作用未全部生效，见
    #  EXIT_DECLINE_DISPATCHED_REVIEW_INCOMPLETE）
    status: str
    exit_code: int
    token: str | None = None
    token_state: str | None = None
    message: str = ""
    next_steps: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "token": self.token,
            "token_state": self.token_state,
            "message": self.message,
            "next_steps": list(self.next_steps),
            "detail": dict(self.detail),
        }

    def render(self) -> str:
        head = f"[rvf-agent {self.command}] {self.status}"
        lines = [head]
        if self.token:
            state = f" ({self.token_state})" if self.token_state else ""
            lines.append(f"- token: {self.token}{state}")
        if self.message:
            lines.append(f"- {self.message}")
        for key in ("run_dir", "scope_artifact", "dispatch_plan_path", "declined_reason_ledger"):
            value = self.detail.get(key)
            if value:
                lines.append(f"- {key}: {value}")
        for step in self.next_steps:
            lines.append(f"- next: {step}")
        lines.append("RVF_AGENT_RESULT_JSON: " + json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# token 载入助手（公开 API 组合：valid 或 expired-revivable 即返回可推进 record）
# ---------------------------------------------------------------------------

class TokenLoadError(Exception):
    """token 缺失 / 损坏 / 已终态 / 非期望状态：非可重试决策错误（EXIT_TOKEN_STATE）。"""

    def __init__(self, message: str, *, token_state: str | None = None) -> None:
        super().__init__(message)
        self.token_state = token_state


def _load_advanceable_record(
    token: str,
    *,
    token_root: str | Path | None,
    now: datetime,
    ttl_seconds: int = token_store.DEFAULT_DECISION_TOKEN_TTL_SECONDS,
) -> token_store.DecisionTokenRecord:
    """读取一枚仍可推进的 token（valid，或 expired 但 run 在途可复活）。缺失 / 损坏 / 真过期 /
    已终态一律抛 ``TokenLoadError``。"""
    lookup = token_store.read_decision_token(token, root=token_root, now=now)
    if lookup.status == "valid" and lookup.payload is not None:
        return token_store.DecisionTokenRecord(token=lookup.token, path=lookup.path, payload=lookup.payload)
    if lookup.status == "expired":
        if lookup.revivable:
            return token_store.revive_decision_token(lookup, root=token_root, now=now, ttl_seconds=ttl_seconds)
        raise TokenLoadError(
            f"决策 token {lookup.token} 已过 TTL 且无在途 run 锚点，无法复活。", token_state=lookup.state
        )
    if lookup.status == "missing":
        raise TokenLoadError(f"决策 token {token} 不存在（可能从未铸造 / 已被清扫）。")
    raise TokenLoadError(
        f"决策 token {lookup.token} 无法推进：status={lookup.status}"
        + (f", state={lookup.state}" if lookup.state else "")
        + "。",
        token_state=lookup.state,
    )


def _require_acknowledged_record(
    token: str,
    *,
    token_root: str | Path | None,
    now: datetime,
) -> token_store.DecisionTokenRecord:
    """scope / dispatch 前置：token 必须处 ``acknowledged`` 态（start 已跑）。其余态抛
    ``TokenLoadError``（含友好指引）。"""
    record = _load_advanceable_record(token, token_root=token_root, now=now)
    if record.state != token_store.STATE_ACKNOWLEDGED:
        raise TokenLoadError(
            f"该操作要求 token 处 acknowledged 态（先跑 rvf-agent start），当前 state={record.state}。",
            token_state=record.state,
        )
    return record


# ---------------------------------------------------------------------------
# start：backend/launch 前置校验 + provider-health 门 + prepare 全套 + 自动 scope 产出
# ---------------------------------------------------------------------------

def resolve_agent_initiation_backend() -> str:
    """发起路径的 backend：一律 in-place ``manual``（review/validate/fix 在主 agent 自己的
    worktree 直接跑，external reviewer 由 dispatch 阶段的 dispatch_reviewers 派发）。仅当
    ``CODEX_RVF_MODE`` 显式关掉 RVF 时返回 ``off``（防呆，正常时 Stop hook 根本不铸 token）。"""
    mode = (legacy_codex_rvf_env("MODE") or "fork").strip().lower()
    if mode in ("off", "skip", "disabled", "disable"):
        return "off"
    return "manual"


def default_launch_preflight(*, backend: str, repo: str) -> tuple[bool, str | None]:
    """backend/launch 前置校验（in-place manual 发起）：backend 可用 + 目标 worktree 是 git 仓。

    返回 ``(ok, failure_reason)``；失败即 start 回退保持 acknowledged、可 --retry。
    """
    if backend in ("off", "dry-run"):
        return False, f"RVF backend={backend}，不支持 in-place agent 发起审查（请检查 CODEX_RVF_MODE）。"
    repo_path = Path(repo).expanduser()
    if not repo_path.is_dir():
        return False, f"目标 worktree 不存在: {repo}"
    if not (repo_path / ".git").exists():
        return False, f"目标 worktree 不是 git 仓库（无 .git）: {repo}"
    return True, None


def default_provider_health_requirements(backend: str) -> list[Any]:
    """provider-health 门的 requirement 解析（in-place 发起）。

    in-place ``manual`` backend 在主 agent 本机 worktree 直接跑 review，**无** child-session
    provider 认证前置（那是 gui/kanban 后端才有的 codex-login 前置）；external reviewer 的
    provider 认证在 dispatch 阶段由 ``dispatch_reviewers.probe_available`` 探测。故此处返回 ``[]``
    = 门通过。留作**注入点**：S4/S7 或测试可注入真实 ``ProviderHealthRequirement`` 行使 fail 路径。
    """
    return []


def _default_provider_health_runner(requirement: Any, timeout_seconds: float) -> dict[str, Any]:
    from core.provider_health.provider_health_guard import run_provider_health_requirement  # noqa: PLC0415

    return run_provider_health_requirement(requirement, timeout_seconds)


def default_prepare_pipeline(
    *,
    token_record: token_store.DecisionTokenRecord,
    repo: str,
    session_id: str | None,
    transcript_path: str | None,
    now: datetime,
    timeout_seconds: float = 60.0,
    resume_run_id: str | None = None,
    resume_run_dir: str | None = None,
    parent_attempt_run_id: str | None = None,
    parent_attempt_run_dir: str | None = None,
) -> dict[str, Any]:
    """默认重量级准备流水线：分配 run + 写 dispatch prep + 跑 ``prepare_run_from_prep_file`` 全套。

    这正是从 UPS ``_run_shared_workflow`` 退役至 CLI 的职能（本切片不改 UPS，只在此重新实现）。
    committed-round baseline（B1）从 token 的候选摘要透传，使「已提交但未审」也能按
    ``git diff <baseline>`` 渲染（避免 clean-worktree scope 退化家族）。返回归一化 dict：
    ``{status, run_id, run_dir, artifacts, error, prep_token, bootstrap_gate,
    initiation_run_id, initiation_run_dir}``。

    ``resume_run_id`` / ``resume_run_dir`` 在场时**复用同一个 run**（不新开）：这是
    「bootstrap 等待结束后恢复本轮」的落点，保证一轮 RVF 从头到尾只有一个 run / 一条 lease /
    一次 reviewer。仅当上一轮工件确实丢失、不得不重开时才由调用方改传 ``parent_attempt_*``，
    把旧 run 显式记成 parent（而不是把恢复伪装成第二轮独立 RVF）。
    """
    import prepare_review_run  # noqa: PLC0415 - 重量级，惰性 import
    from core.run_ledger.run_ledger import start_run  # noqa: PLC0415

    worktree = repo
    candidate_summary = token_record.payload.get("candidate_summary")
    committed_baseline = None
    committed_base_ref = None
    if isinstance(candidate_summary, Mapping):
        cb = candidate_summary.get("committed_round_baseline")
        committed_baseline = cb.strip() if isinstance(cb, str) and cb.strip() else None
        cbr = candidate_summary.get("committed_round_base_ref")
        committed_base_ref = cbr.strip() if isinstance(cbr, str) and cbr.strip() else None

    ledger = start_run(
        "agentic-initiation-start",
        repo=worktree,
        cwd=worktree,
        run_id=resume_run_id or None,
        run_dir=Path(resume_run_dir).expanduser() if resume_run_dir else None,
    )
    artifacts_dir = ledger.artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    target_flow = "flow-manual"
    payload: dict[str, Any] = {
        "origin_session_id": session_id,
        "origin_repo": worktree,
        "origin_cwd": worktree,
        "origin_transcript_path": transcript_path,
        "target_flow": target_flow,
        "target_worktree": worktree,
        "target_kanban_task_id": None,
        "target_session_id": session_id,
        "dispatch_origin": "agentic_initiation_start",
        "rvf_run": {
            "run_id": ledger.run_id,
            "run_dir": str(ledger.run_dir),
            "artifacts_dir": str(artifacts_dir),
            "scope_contract_path": str(artifacts_dir / "inputs" / "scope.contract.json"),
            "tracker_scope_path": None,
            "tracker_lease_id": None,
            "tracker_scope_hash": None,
        },
        "handoff_expectations": {
            "handoff_path": str(artifacts_dir / "handoff.md"),
            "expected_artifacts": ["review-result.json", "merge-table.md", "handoff.md"],
        },
        "workflow_constraints": {
            "pause_origin_edits": False,
            "in_place_mode": True,
        },
    }
    if committed_baseline:
        payload["committed_round_baseline"] = committed_baseline
        if committed_base_ref:
            payload["committed_round_base_ref"] = committed_base_ref

    rvf_prep_file.sweep_stale()
    record = rvf_prep_file.write_prep_file(payload)
    state = prepare_review_run.prepare_run_from_prep_file(
        record,
        timeout_seconds=timeout_seconds,
        transcript_override=Path(transcript_path).expanduser() if transcript_path else None,
    )
    gate = rvf_bootstrap_gate.gate_of(state)
    if gate is not None and parent_attempt_run_id and not gate.get("parent_attempt_run_id"):
        gate = {
            **gate,
            "parent_attempt_run_id": parent_attempt_run_id,
            "parent_attempt_run_dir": parent_attempt_run_dir,
        }
    return {
        "status": state.get("status"),
        "run_id": state.get("run_id"),
        "run_dir": state.get("run_dir"),
        "artifacts": state.get("artifacts") or {},
        "error": state.get("error"),
        "prep_token": record.token,
        "bootstrap_gate": gate,
        # bootstrap 等待路径上 prepare_run 在 start_run **之前**就抛了，故 state 里没有
        # run_id/run_dir；发起侧 ledger 的 run 目录才是该 token 唯一可锚的在途工件。没有它，
        # token 过 900s TTL 后 revive_decision_token 找不到锚点 → 一次大 base 吸收必然把整轮
        # 决策熬死（入口 A 的可恢复性硬缺口）。
        "initiation_run_id": ledger.run_id,
        "initiation_run_dir": str(ledger.run_dir),
    }


def run_start(
    *,
    token: str,
    reason: str,
    repo: str | None = None,
    session_id: str | None = None,
    transcript_path: str | None = None,
    retry: bool = False,
    roots: AgentDecisionRoots | None = None,
    now: datetime | None = None,
    prepare_pipeline: Callable[..., dict[str, Any]] | None = None,
    launch_preflight: Callable[..., tuple[bool, str | None]] | None = None,
    provider_health_requirements_resolver: Callable[[str], list[Any]] | None = None,
    provider_health_runner: Callable[[Any, float], dict[str, Any]] | None = None,
    provider_health_timeout_seconds: float = 12.0,
    prepare_timeout_seconds: float = 60.0,
) -> CommandResult:
    """``start`` 子命令：token→acknowledged → 前置校验 + provider-health 门 → prepare 全套 →
    自动 scope 产出。任一步失败 → record_start_failure 回退保持 acknowledged，可 --retry。"""
    roots = roots or AgentDecisionRoots()
    now_dt = _coerce_now(now)
    prepare_pipeline = prepare_pipeline or default_prepare_pipeline
    launch_preflight = launch_preflight or default_launch_preflight
    provider_health_requirements_resolver = (
        provider_health_requirements_resolver or default_provider_health_requirements
    )
    provider_health_runner = provider_health_runner or _default_provider_health_runner

    # ① token → acknowledged（非终态；retry 再走一轮 acknowledge，递增 start_attempt_count）。
    # 先探一次 TTL 状态：acknowledge 内部会把「过期但 run 在途」的 token 就地复活，复活后就再也
    # 看不出它曾经过期了。而这正是 bootstrap 等待期的常态（900s TTL ≪ 一次大 base 吸收），
    # 需要据此把等待态命名为 `bootstrap_interrupted_retryable`。
    store_expired_before_start = (
        getattr(
            token_store.read_decision_token(token, root=roots.token_root, now=now_dt), "status", None
        )
        == "expired"
    )
    try:
        record = token_store.acknowledge_decision_token(token, root=roots.token_root, now=now_dt)
    except token_store.DecisionTokenError as exc:
        state = getattr(
            token_store.read_decision_token(token, root=roots.token_root, now=now_dt), "state", None
        )
        return CommandResult(
            command="start",
            status="error",
            exit_code=EXIT_TOKEN_STATE,
            token=token,
            token_state=state,
            message=f"无法 acknowledge 决策 token：{exc}",
        )

    worktree = str(record.payload.get("worktree") or "")
    tok_session_id = session_id or (
        record.payload.get("session_id") if isinstance(record.payload.get("session_id"), str) else None
    )
    candidate_summary = record.payload.get("candidate_summary")
    repo = repo or worktree
    append_decision_telemetry(
        token=token, event="start_acknowledged", worktree=worktree, session_id=tok_session_id,
        now=now_dt, root=roots.telemetry_root,
        detail={"reason": reason, "retry": retry, "start_attempt_count": record.payload.get("start_attempt_count")},
    )

    def _fail(reason_text: str, *, phase: str, extra: dict[str, Any] | None = None) -> CommandResult:
        """任一重量级准备步骤失败：回退保持 acknowledged，写遥测，返回可 --retry 的失败结果。"""
        try:
            token_store.record_start_failure(token, reason=reason_text, root=roots.token_root, now=now_dt)
        except token_store.DecisionTokenError:
            pass
        append_decision_telemetry(
            token=token, event="start_failed", worktree=worktree, session_id=tok_session_id,
            now=now_dt, root=roots.telemetry_root, detail={"phase": phase, "reason": reason_text, **(extra or {})},
        )
        detail = {"phase": phase, **(extra or {})}
        return CommandResult(
            command="start", status="start_failed", exit_code=EXIT_START_FAILED,
            token=token, token_state=token_store.STATE_ACKNOWLEDGED, message=reason_text,
            next_steps=[f"rvf-agent start --token {token} --retry --reason <诊断后重试>"],
            detail=detail,
        )

    def _bootstrap_waiting(
        gate: Mapping[str, Any],
        reconciliation: rvf_bootstrap_gate.BootstrapGateReconciliation,
        *,
        run_anchor_dir: str | None,
    ) -> CommandResult:
        """撞上（或仍卡在）pre-review local-base bootstrap 门：**持久化等待态**，不是失败。

        token 保持 acknowledged（非终态、保留 ``--retry`` 权利与去重身份），gate 落进 token
        payload，run 锚点补齐——三者合起来让「跑同步 skill 期间 agent 退出 / token 过 TTL /
        host 重启」之后，同一枚 token 仍能复活并恢复**同一轮**，而不是被当成一轮已完结或
        不可恢复的失败。next_steps 是给模型看的确定性 self-injection checkpoint。
        """
        stamped = rvf_bootstrap_gate.attach_origin_decision_token(gate, token)
        try:
            token_store.attach_bootstrap_gate(
                token, gate=stamped, root=roots.token_root, now=now_dt
            )
        except token_store.DecisionTokenError:
            pass
        if run_anchor_dir:
            try:
                token_store.attach_run_anchor(
                    token, run_dir=run_anchor_dir, root=roots.token_root, now=now_dt
                )
            except token_store.DecisionTokenError:
                pass
        append_decision_telemetry(
            token=token, event="start_bootstrap_waiting", worktree=worktree,
            session_id=tok_session_id, now=now_dt, root=roots.telemetry_root,
            detail={"retry": retry, **reconciliation.to_dict()},
        )
        resume_command = f"rvf-agent start --token {token} --retry --reason <bootstrap 完成后恢复本轮>"
        return CommandResult(
            command="start",
            status="bootstrap_required",
            exit_code=EXIT_BOOTSTRAP_REQUIRED,
            token=token,
            token_state=token_store.STATE_ACKNOWLEDGED,
            message=reconciliation.diagnostic,
            next_steps=[
                *rvf_bootstrap_gate.bootstrap_waiting_agent_steps(stamped, reconciliation),
                f"④ {resume_command}",
            ],
            detail={
                "phase": reconciliation.phase,
                "bootstrap_outcome": reconciliation.outcome,
                "bootstrap_reason": reconciliation.reason,
                "bootstrap_gate": dict(stamped),
                "run_anchor_dir": run_anchor_dir,
                **(
                    {"conflict_paths": list(reconciliation.conflict_paths)}
                    if reconciliation.conflict_paths
                    else {}
                ),
            },
        )

    def _bootstrap_replay_in_flight(
        gate: Mapping[str, Any],
        reconciliation: rvf_bootstrap_gate.BootstrapGateReconciliation,
        *,
        in_flight_age_seconds: float | None,
        window_seconds: float,
    ) -> CommandResult:
        """并发的另一次 `start --retry` 已抢到本轮重放占位：返回**可重试的等待态**，不起第二次。

        与 `_bootstrap_waiting` 刻意分开：那条文案让 agent 去跑同步 skill，而此刻 base 早已吸收、
        receipt 也已校验通过，只差 prepare 收尾。再跑一次同步 skill 会产出**另一份** receipt，
        下一轮按 digest 判 tampered。故这里复用「别再动它、等一下再 --retry」的定向文案。
        """
        age_text = f"{in_flight_age_seconds:.0f}" if in_flight_age_seconds is not None else "0"
        resume_command = f"rvf-agent start --token {token} --retry --reason <稍后重试，等在途 prepare 收尾>"
        append_decision_telemetry(
            token=token, event="start_bootstrap_replay_in_flight", worktree=worktree,
            session_id=tok_session_id, now=now_dt, root=roots.telemetry_root,
            detail={
                "retry": retry,
                "in_flight_age_seconds": in_flight_age_seconds,
                "in_flight_window_seconds": window_seconds,
                **reconciliation.to_dict(),
            },
        )
        return CommandResult(
            command="start",
            status="bootstrap_replay_in_flight",
            exit_code=EXIT_BOOTSTRAP_REQUIRED,
            token=token,
            token_state=token_store.STATE_ACKNOWLEDGED,
            message=(
                "本轮 bootstrap 的 prepare 重放已由另一次 start 在途"
                f"（已在途 {age_text}s / 去重窗口 {window_seconds:.0f}s）；"
                "为避免同一个 run 目录被并发重复写入、并分配出第二条 lease，本次不再起第二次重放。"
            ),
            next_steps=[
                rvf_bootstrap_gate.bootstrap_replay_pending_reengage_reason(
                    gate, reconciliation, resume_hint=f"③ {resume_command}"
                ),
            ],
            detail={
                "phase": rvf_bootstrap_gate.PHASE_BOOTSTRAP_INTERRUPTED_RETRYABLE,
                "bootstrap_outcome": rvf_bootstrap_gate.OUTCOME_STILL_WAITING,
                "bootstrap_reason": "bootstrap_replay_in_flight",
                "in_flight_age_seconds": in_flight_age_seconds,
                "in_flight_window_seconds": window_seconds,
            },
        )

    # ①b 既有 bootstrap 等待 gate 的恢复判定（`start --retry` 的主路径）。
    # 仍在等 / 冲突未解 / 证据不可信 → 原地维持等待，**不重跑 prepare、不新开 run**；
    # receipt 有效 → 带着原 run 身份重放 prepare；工件丢失 → 显式记 parent 血缘后重开。
    resume_run_id: str | None = None
    resume_run_dir: str | None = None
    parent_attempt_run_id: str | None = None
    parent_attempt_run_dir: str | None = None
    resume_reconciliation: rvf_bootstrap_gate.BootstrapGateReconciliation | None = None
    existing_gate = rvf_bootstrap_gate.gate_of(record.payload)
    if rvf_bootstrap_gate.is_waiting_gate(existing_gate):
        assert existing_gate is not None  # narrow for readers
        reconciliation = rvf_bootstrap_gate.reconcile_bootstrap_gate(
            existing_gate, store_expired=store_expired_before_start
        )
        if reconciliation.holds_gate:
            anchored = record.payload.get("run_anchor_dir")
            return _bootstrap_waiting(
                existing_gate,
                reconciliation,
                run_anchor_dir=reconciliation.resume_run_dir
                or (anchored if isinstance(anchored, str) else None),
            )
        if reconciliation.outcome == rvf_bootstrap_gate.OUTCOME_RECEIPT_VALID:
            resume_run_id = reconciliation.resume_run_id
            resume_run_dir = reconciliation.resume_run_dir
            resume_reconciliation = reconciliation
            # 把校验通过的 receipt 路径/digest 盖回 token payload 里的 gate。下面的 prepare 可能
            # 失败（此时 token 保持 acknowledged、gate 继续被持有），没有这一步，`--retry` 的每
            # 一轮都只做身份校验：`bootstrap_receipt_digest_changed` 那条篡改判据在真实路径上
            # 永远不可达，一份被替换但仍能通过身份校验的 receipt 会被照单全收。
            existing_gate = rvf_bootstrap_gate.gate_with_verified_receipt(
                existing_gate, reconciliation
            )
            try:
                token_store.attach_bootstrap_gate(
                    token, gate=existing_gate, root=roots.token_root, now=now_dt
                )
            except token_store.DecisionTokenError:
                pass
        else:  # artifact_lost：只能重开，但把上一轮 run 显式记成 parent attempt
            parent_attempt_run_id = existing_gate.get("rvf_run_id")
            parent_attempt_run_dir = existing_gate.get("rvf_run_dir")
        append_decision_telemetry(
            token=token, event="start_bootstrap_reconciled", worktree=worktree,
            session_id=tok_session_id, now=now_dt, root=roots.telemetry_root,
            detail=reconciliation.to_dict(),
        )

    # ② backend/launch 前置校验。
    backend = resolve_agent_initiation_backend()
    ok, preflight_reason = launch_preflight(backend=backend, repo=repo)
    if not ok:
        return _fail(f"backend/launch 前置校验失败：{preflight_reason}", phase="launch_preflight", extra={"backend": backend})

    # ③ provider-health 门（in-place 无 requirement → 直通；有 requirement 且失败 → 回退）。
    requirements = provider_health_requirements_resolver(backend)
    health_results = [provider_health_runner(req, provider_health_timeout_seconds) for req in requirements]
    failed = [r for r in health_results if r.get("status") != "ok"]
    if failed:
        providers = ", ".join(sorted({str(r.get("provider")) for r in failed if r.get("provider")}))
        return _fail(
            f"provider-health 门未通过（providers={providers or '<unknown>'}）：请修复 provider 认证后 --retry。",
            phase="provider_health", extra={"backend": backend, "provider_health": failed},
        )

    # ④ prepare_review_run 全套。
    # ④a 恢复路径先原子 claim（入口 B `claim_bootstrap_replay_in_flight` 的对偶）：两个并发的
    # `start --retry` 会各自 reconcile 出 receipt_valid，再各自带**同一个** resume_run_id/dir 去
    # `start_run(run_id=X, run_dir=X)` —— 同一个 run 目录被并发写，并可能分配出第二条 tracker
    # lease，违反「恢复不得产生重复 lease / reviewer」。抢不到 = 另一个进程正在推进本轮 prepare，
    # 本次返回可重试的等待态（仍是 EXIT_BOOTSTRAP_REQUIRED，token 保持 acknowledged）。
    replay_claim_held = False
    replay_claim_window = max(float(prepare_timeout_seconds), 0.0) + BOOTSTRAP_REPLAY_CLAIM_GRACE_SECONDS
    if resume_run_id or resume_run_dir:
        claimed, in_flight_age = token_store.claim_bootstrap_replay_in_flight(
            token, window_seconds=replay_claim_window, root=roots.token_root, now=now_dt
        )
        if not claimed:
            assert existing_gate is not None and resume_reconciliation is not None
            return _bootstrap_replay_in_flight(
                existing_gate,
                resume_reconciliation,
                in_flight_age_seconds=in_flight_age,
                window_seconds=replay_claim_window,
            )
        replay_claim_held = True

    def _release_replay_claim() -> None:
        """本轮 prepare 已收尾（成功 / 失败 / 仍在等待）→ 交还占位，下一次 retry 可立刻再试。"""
        if replay_claim_held:
            token_store.release_bootstrap_replay_in_flight(token, roots.token_root)

    try:
        prepared = prepare_pipeline(
            token_record=record, repo=repo, session_id=tok_session_id,
            transcript_path=transcript_path, now=now_dt, timeout_seconds=prepare_timeout_seconds,
            resume_run_id=resume_run_id, resume_run_dir=resume_run_dir,
            parent_attempt_run_id=parent_attempt_run_id,
            parent_attempt_run_dir=parent_attempt_run_dir,
        )
    except Exception as exc:  # prepare 抛异常 → 回退可 retry，不吞决策
        _release_replay_claim()
        return _fail(f"prepare_review_run 异常：{type(exc).__name__}: {exc}", phase="prepare")

    # prepare 判定「task worktree 还没吸收 local base」：这不是失败，走等待态（见 _bootstrap_waiting）。
    # 必须排在下面的泛化 != completed 分支**之前**，否则会被当成一次不带恢复语义的 start 失败。
    if prepared.get("status") == rvf_bootstrap_gate.WAITING_STATUS:
        _release_replay_claim()
        fresh_gate = prepared.get("bootstrap_gate")
        if not isinstance(fresh_gate, Mapping):
            return _fail(
                "prepare 报告需要 local-base bootstrap，但未产出可恢复的 gate 记录。",
                phase="prepare", extra={"prepare_status": prepared.get("status")},
            )
        return _bootstrap_waiting(
            fresh_gate,
            rvf_bootstrap_gate.reconcile_bootstrap_gate(fresh_gate),
            run_anchor_dir=(
                prepared.get("initiation_run_dir")
                if isinstance(prepared.get("initiation_run_dir"), str)
                else None
            ),
        )

    if prepared.get("status") != "completed":
        _release_replay_claim()
        return _fail(
            f"prepare_review_run 未完成（status={prepared.get('status')}）：{prepared.get('error') or ''}".strip(),
            phase="prepare", extra={"prepare_status": prepared.get("status"), "prepare_error": prepared.get("error")},
        )

    run_dir = prepared.get("run_dir")
    run_id = prepared.get("run_id")
    artifacts = prepared.get("artifacts") or {}

    # ⑤ 挂载 run 锚点（供 TTL 复活判据），best-effort。
    if isinstance(run_dir, str) and run_dir:
        try:
            token_store.attach_run_anchor(token, run_dir=run_dir, root=roots.token_root, now=now_dt)
        except token_store.DecisionTokenError:
            pass

    # ⑤b prepare 真正成功 = 进入 REVIEWING，bootstrap 等待态退场：摘掉 gate，回落常规流程。
    if existing_gate is not None:
        try:
            token_store.attach_bootstrap_gate(token, gate=None, root=roots.token_root, now=now_dt)
        except token_store.DecisionTokenError:
            pass
        append_decision_telemetry(
            token=token, event="start_bootstrap_resolved", worktree=worktree,
            session_id=tok_session_id, now=now_dt, root=roots.telemetry_root,
            detail={
                "phase": rvf_bootstrap_gate.PHASE_REVIEWING,
                "resumed_run_id": resume_run_id,
                "parent_attempt_run_id": parent_attempt_run_id,
                "run_id": run_id,
            },
        )

    # ⑤c 本轮 prepare 已收尾且 gate 已退场 → 释放恢复占位（放在摘 gate **之后**：这样并发的另一次
    # start 在 gate 还挂着的窗口里也无法起第二次重放）。
    _release_replay_claim()

    # ⑥ 自动 scope 产出（落盘，供 scope 子命令编辑）。
    auto_scope_paths = _initial_auto_scope_paths(candidate_summary, artifacts)
    overlay = {
        "schema_version": SCOPE_ADJUSTMENT_SCHEMA_VERSION,
        "token": token,
        "worktree": worktree,
        "session_id": tok_session_id,
        "repo": repo,
        "run_id": run_id,
        "run_dir": run_dir,
        "created_at": _format_timestamp(now_dt),
        "updated_at": _format_timestamp(now_dt),
        "candidate_fingerprint": record.payload.get("candidate_fingerprint"),
        "auto_scope_paths": auto_scope_paths,
        "annotations": {},
        "exclusions": [],
        "inclusions": [],
        "prepare_artifacts": dict(artifacts),
    }
    scope_artifact_path = write_scope_adjustment(overlay, token=token, root=roots.scope_adjustment_root)

    append_decision_telemetry(
        token=token, event="start_prepared", worktree=worktree, session_id=tok_session_id,
        now=now_dt, root=roots.telemetry_root,
        detail={"run_id": run_id, "run_dir": run_dir, "auto_scope_path_count": len(auto_scope_paths)},
    )

    return CommandResult(
        command="start", status="ok", exit_code=EXIT_OK, token=token,
        token_state=token_store.STATE_ACKNOWLEDGED,
        message=f"审查准备就绪（自动 scope {len(auto_scope_paths)} 路径）。",
        next_steps=[
            f"rvf-agent scope annotate|exclude|include --token {token} …（可选，改动自动 scope）",
            f"rvf-agent dispatch --token {token} --strictness <light|standard|strict>",
        ],
        detail={
            "run_id": run_id, "run_dir": run_dir, "scope_artifact": str(scope_artifact_path),
            "auto_scope_paths": auto_scope_paths, "prepare_artifacts": dict(artifacts),
        },
    )


# ---------------------------------------------------------------------------
# scope annotate / exclude / include（仅 acknowledged 态）
# ---------------------------------------------------------------------------

def _load_overlay_for_scope(
    token: str, *, roots: AgentDecisionRoots, now: datetime
) -> tuple[token_store.DecisionTokenRecord, dict[str, Any]]:
    record = _require_acknowledged_record(token, token_root=roots.token_root, now=now)
    overlay = read_scope_adjustment(token, root=roots.scope_adjustment_root)
    if overlay is None:
        raise TokenLoadError(
            f"未找到 token {token} 的自动 scope 产出（先跑 rvf-agent start）。",
            token_state=record.state,
        )
    return record, overlay


def run_scope_annotate(
    *,
    token: str,
    entries: Sequence[tuple[str, str]],
    roots: AgentDecisionRoots | None = None,
    now: datetime | None = None,
) -> CommandResult:
    """``scope annotate``：批量给候选路径加注释（R19：annotate 批量；默认零注释）。"""
    roots = roots or AgentDecisionRoots()
    now_dt = _coerce_now(now)
    try:
        record, overlay = _load_overlay_for_scope(token, roots=roots, now=now_dt)
    except TokenLoadError as exc:
        return _token_error_result("scope annotate", token, exc)

    annotations = dict(overlay.get("annotations") or {})
    for path, note in entries:
        annotations[path] = note
    overlay["annotations"] = annotations
    overlay["updated_at"] = _format_timestamp(now_dt)
    write_scope_adjustment(overlay, token=token, root=roots.scope_adjustment_root)

    worktree = str(overlay.get("worktree") or record.payload.get("worktree") or "")
    append_decision_telemetry(
        token=token, event="scope_annotate", worktree=worktree,
        session_id=overlay.get("session_id"), now=now_dt, root=roots.telemetry_root,
        detail={"entries": [{"path": p, "note": n} for p, n in entries]},
    )
    return CommandResult(
        command="scope annotate", status="ok", exit_code=EXIT_OK, token=token,
        token_state=record.state,
        message=f"已注释 {len(entries)} 个路径（累计注释 {len(annotations)}）。",
        next_steps=[f"rvf-agent dispatch --token {token} --strictness <…>"],
        detail={"annotated_paths": [p for p, _ in entries]},
    )


def _run_scope_exception(
    *,
    command: str,
    operation_type: str,
    token: str,
    paths: Sequence[str],
    reason: str,
    roots: AgentDecisionRoots,
    now: datetime | None,
    issue_threshold: int,
) -> CommandResult:
    now_dt = _coerce_now(now)
    try:
        record, overlay = _load_overlay_for_scope(token, roots=roots, now=now_dt)
    except TokenLoadError as exc:
        return _token_error_result(command, token, exc)

    worktree = str(overlay.get("worktree") or record.payload.get("worktree") or "")
    key_field = "exclusions" if operation_type == SCOPE_OPERATION_EXCLUDE else "inclusions"
    entries = list(overlay.get(key_field) or [])
    existing_paths = {str(e.get("path")) for e in entries if isinstance(e, Mapping)}
    issue_results: list[dict[str, Any]] = []
    newly_added: list[str] = []
    for path in paths:
        if path not in existing_paths:
            entries.append({"path": path, "reason": reason, "at": _format_timestamp(now_dt)})
            existing_paths.add(path)
            newly_added.append(path)
        # 遥测 + issue ledger 计数：每个 path 都记（幂等键内去重路径数决定是否立 issue）。
        append_decision_telemetry(
            token=token, event=f"scope_{operation_type}", worktree=worktree,
            session_id=overlay.get("session_id"), now=now_dt, root=roots.telemetry_root,
            detail={"path": path, "reason": reason},
        )
        issue_results.append(
            record_scope_exception_and_maybe_file_issue(
                worktree=worktree, operation_type=operation_type, path=path, reason=reason,
                token=token, now=now_dt, root=roots.scope_issue_ledger_root, threshold=issue_threshold,
            )
        )
    overlay[key_field] = entries
    overlay["updated_at"] = _format_timestamp(now_dt)
    write_scope_adjustment(overlay, token=token, root=roots.scope_adjustment_root)

    latest_issue = issue_results[-1] if issue_results else {}
    newly_filed = any(r.get("newly_filed") for r in issue_results)
    message = f"scope {operation_type}：新增 {len(newly_added)} 路径（同键去重累计 {latest_issue.get('distinct_path_count', 0)}）。"
    if newly_filed:
        message += f" 已跨阈值 {latest_issue.get('threshold')} 自动立本地 scope-issue。"
    return CommandResult(
        command=command, status="ok", exit_code=EXIT_OK, token=token, token_state=record.state,
        message=message,
        next_steps=[f"rvf-agent dispatch --token {token} --strictness <…>"],
        detail={
            "operation_type": operation_type,
            "newly_added_paths": newly_added,
            "effective_primary_files": compute_effective_primary_files(overlay),
            "scope_issue": latest_issue,
        },
    )


def run_scope_exclude(
    *,
    token: str,
    paths: Sequence[str],
    reason: str,
    roots: AgentDecisionRoots | None = None,
    now: datetime | None = None,
    issue_threshold: int = DEFAULT_SCOPE_EXCEPTION_ISSUE_THRESHOLD,
) -> CommandResult:
    """``scope exclude``：把路径移出自动 scope（R10：运行时 artifact 默认不入，本操作反向剔除）。"""
    return _run_scope_exception(
        command="scope exclude", operation_type=SCOPE_OPERATION_EXCLUDE, token=token, paths=paths,
        reason=reason, roots=roots or AgentDecisionRoots(), now=now, issue_threshold=issue_threshold,
    )


def run_scope_include(
    *,
    token: str,
    paths: Sequence[str],
    reason: str,
    roots: AgentDecisionRoots | None = None,
    now: datetime | None = None,
    issue_threshold: int = DEFAULT_SCOPE_EXCEPTION_ISSUE_THRESHOLD,
) -> CommandResult:
    """``scope include``：把路径追加进自动 scope（R10：运行时 artifact 经 include 显式追加）。"""
    return _run_scope_exception(
        command="scope include", operation_type=SCOPE_OPERATION_INCLUDE, token=token, paths=paths,
        reason=reason, roots=roots or AgentDecisionRoots(), now=now, issue_threshold=issue_threshold,
    )


# ---------------------------------------------------------------------------
# dispatch：scope 定稿 + 严格度终选 + 派发 reviewer + mark_dispatched
# ---------------------------------------------------------------------------

PRESET_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "review-strictness-presets.json"
STRICTNESS_TIERS = ("light", "standard", "strict")


def load_strictness_preset(tier: str, *, config_path: str | Path | None = None) -> dict[str, Any]:
    """从 S2 preset 配置读某一档参数（R14：agent 只选档）。非法档 / 配置缺失即抛。"""
    path = Path(config_path).expanduser() if config_path else PRESET_CONFIG_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    presets = data.get("presets") if isinstance(data, dict) else None
    if not isinstance(presets, dict) or tier not in presets:
        raise ValueError(f"未知严格度档 {tier!r}（可选 {sorted(presets or {})}）")
    return presets[tier]


def finalize_scope_contract(
    overlay: Mapping[str, Any],
    *,
    effective_primary_files: list[str],
) -> dict[str, Any]:
    """scope 定稿：把 exclude/include 的净效果写回 prepare 产出的 scope.contract.json（改
    ``primary_files`` / ``excluded_path_prefixes`` 并**重算 scope_hash**，复用 prepare 的
    canonical 序列化以保证哈希一致）。best-effort：contract 缺失 / 不可读即跳过并返回状态。"""
    artifacts = overlay.get("prepare_artifacts")
    contract_path_raw = artifacts.get("scope_contract") if isinstance(artifacts, Mapping) else None
    if not isinstance(contract_path_raw, str) or not contract_path_raw:
        return {"finalized": False, "reason": "no_scope_contract_artifact"}
    contract_path = Path(contract_path_raw)
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"finalized": False, "reason": f"unreadable_scope_contract: {exc}"}
    if not isinstance(contract, dict):
        return {"finalized": False, "reason": "invalid_scope_contract"}

    excluded_paths = sorted(
        {str(e.get("path")) for e in (overlay.get("exclusions") or []) if isinstance(e, Mapping) and e.get("path")}
    )
    prior_excluded = [str(p) for p in (contract.get("excluded_path_prefixes") or [])]
    merged_excluded = list(dict.fromkeys([*prior_excluded, *excluded_paths]))

    import prepare_review_run  # noqa: PLC0415 - 复用 canonical 序列化 + 版本

    background_files = [str(p) for p in (contract.get("background_files") or [])]
    protected_files = [str(p) for p in (contract.get("protected_files") or [])]
    canonical_issues = contract.get("canonical_issues") or []
    fix_allowlist = [str(p) for p in (contract.get("fix_allowlist") or [])]
    version = contract.get("version", prepare_review_run.SCOPE_CONTRACT_VERSION)
    prior_canonical_scope = contract.get("canonical_scope")
    canonical_scope = dict(prior_canonical_scope) if isinstance(prior_canonical_scope, Mapping) else {}
    canonical_scope.update({
        "version": version,
        "repo": contract.get("repo"),
        "scope_mode": contract.get("scope_mode"),
        "primary_files": effective_primary_files,
        "background_files": background_files,
        "protected_files": protected_files,
        "canonical_issues": canonical_issues,
        "fix_allowlist": fix_allowlist,
        "excluded_path_prefixes": merged_excluded,
    })
    new_hash = hashlib.sha256(prepare_review_run.canonical_json_bytes(canonical_scope)).hexdigest()
    contract["primary_files"] = effective_primary_files
    contract["excluded_path_prefixes"] = merged_excluded
    contract["scope_hash"] = new_hash
    contract["canonical_scope"] = canonical_scope
    _atomic_write_json(contract_path, contract)
    return {
        "finalized": True,
        "scope_contract_path": str(contract_path),
        "primary_files": effective_primary_files,
        "excluded_path_prefixes": merged_excluded,
        "scope_hash": new_hash,
    }


def default_reviewer_dispatcher(
    *,
    repo: str | None,
    review_packet: str | None,
    scope_contract: str | None,
    session_context: str | None,
    run_id: str | None,
    run_dir: str | None,
    strictness: str,
    preset: Mapping[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    """默认 reviewer 派发：以子进程调用既有 ``dispatch_reviewers.py``（S7 才模块化，本切片不重写它）。

    ``dry_run=True`` 走 ``--dry-run``（打印 plan、不真拉起 reviewer）；否则 ``--execute``。严格度→
    可用池大小的 clamp / 伪随机选二 / per-phase 由 S7 落地；本切片先把 preset 的
    ``external_reviewer_count_target`` 作为 ``--require-external`` 的意图透传（≥2 时要求真有 external）。
    """
    dispatch_script = Path(__file__).resolve().parent / "dispatch_reviewers.py"
    argv = [sys.executable, str(dispatch_script)]
    if repo:
        argv += ["--repo", repo]
    if review_packet:
        argv += ["--review-packet", review_packet]
    if scope_contract:
        argv += ["--scope-contract", scope_contract]
    if session_context:
        argv += ["--session-context", session_context]
    if run_id:
        argv += ["--rvf-run-id", run_id]
    if run_dir:
        argv += ["--rvf-run-dir", run_dir]
    argv += ["--dry-run"] if dry_run else ["--execute"]
    if int(preset.get("external_reviewer_count_target") or 0) >= 2:
        argv += ["--require-external"]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    except (OSError, ValueError) as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "argv": argv}
    stdout = completed.stdout or ""
    return {
        "status": "ok" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "stdout_tail": stdout[-2000:],
        "stderr_tail": (completed.stderr or "")[-2000:],
        "plan_path": stdout.strip().splitlines()[-1] if stdout.strip() and not dry_run else None,
        "argv": argv,
    }


def run_dispatch(
    *,
    token: str,
    strictness: str,
    roots: AgentDecisionRoots | None = None,
    now: datetime | None = None,
    reviewer_dispatcher: Callable[..., dict[str, Any]] | None = None,
    dry_run: bool = False,
    preset_config_path: str | Path | None = None,
) -> CommandResult:
    """``dispatch`` 子命令：scope 定稿 → 严格度终选（读 preset）→ 派发 reviewer → mark_dispatched。"""
    roots = roots or AgentDecisionRoots()
    now_dt = _coerce_now(now)
    reviewer_dispatcher = reviewer_dispatcher or default_reviewer_dispatcher

    if strictness not in STRICTNESS_TIERS:
        return CommandResult(
            command="dispatch", status="error", exit_code=EXIT_USAGE, token=token,
            message=f"未知严格度档 {strictness!r}（可选 {list(STRICTNESS_TIERS)}）。",
        )
    try:
        record, overlay = _load_overlay_for_scope(token, roots=roots, now=now_dt)
    except TokenLoadError as exc:
        return _token_error_result("dispatch", token, exc)

    try:
        preset = load_strictness_preset(strictness, config_path=preset_config_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return CommandResult(
            command="dispatch", status="error", exit_code=EXIT_DISPATCH_FAILED, token=token,
            token_state=record.state, message=f"严格度 preset 读取失败：{exc}",
        )

    # ① scope 定稿。
    effective_primary_files = compute_effective_primary_files(overlay)
    finalize = finalize_scope_contract(overlay, effective_primary_files=effective_primary_files)
    artifacts = overlay.get("prepare_artifacts") if isinstance(overlay.get("prepare_artifacts"), Mapping) else {}

    worktree = str(overlay.get("worktree") or record.payload.get("worktree") or "")
    append_decision_telemetry(
        token=token, event="dispatch_scope_finalized", worktree=worktree,
        session_id=overlay.get("session_id"), now=now_dt, root=roots.telemetry_root,
        detail={"effective_primary_files": effective_primary_files, "finalize": finalize, "strictness": strictness},
    )

    # ② 派发 reviewer。
    dispatch_result = reviewer_dispatcher(
        repo=overlay.get("repo") or worktree,
        review_packet=artifacts.get("review_packet"),
        scope_contract=artifacts.get("scope_contract"),
        session_context=artifacts.get("scope_of_work"),
        run_id=overlay.get("run_id"),
        run_dir=overlay.get("run_dir"),
        strictness=strictness,
        preset=preset,
        dry_run=dry_run,
    )
    if dispatch_result.get("status") not in ("ok", "completed"):
        append_decision_telemetry(
            token=token, event="dispatch_failed", worktree=worktree,
            session_id=overlay.get("session_id"), now=now_dt, root=roots.telemetry_root,
            detail={"strictness": strictness, "dispatch_result": dispatch_result},
        )
        return CommandResult(
            command="dispatch", status="dispatch_failed", exit_code=EXIT_DISPATCH_FAILED, token=token,
            token_state=record.state,
            message=f"reviewer 派发失败：{dispatch_result.get('error') or dispatch_result.get('status')}。token 仍在 acknowledged，可修复后重试。",
            detail={"dispatch_result": dispatch_result, "strictness": strictness},
        )

    # ③ mark_dispatched（终态），记录严格度。
    dispatched = token_store.mark_dispatched(token, strictness=strictness, root=roots.token_root, now=now_dt)
    # 定稿后的严格度 + preset + dispatch 结果也落进 overlay，便于 review/审计回看。
    overlay["dispatched_strictness"] = strictness
    overlay["dispatched_preset"] = dict(preset)
    overlay["dispatch_result"] = dispatch_result
    overlay["scope_finalize"] = finalize
    overlay["updated_at"] = _format_timestamp(now_dt)
    write_scope_adjustment(overlay, token=token, root=roots.scope_adjustment_root)

    append_decision_telemetry(
        token=token, event="dispatched", worktree=worktree, session_id=overlay.get("session_id"),
        now=now_dt, root=roots.telemetry_root,
        detail={"strictness": strictness, "preset": dict(preset), "dispatch_result": dispatch_result},
    )
    return CommandResult(
        command="dispatch", status="ok", exit_code=EXIT_OK, token=token,
        token_state=dispatched.state,
        message=f"已按 {strictness} 档派发 reviewer（primary_files {len(effective_primary_files)}，"
        f"external target {preset.get('external_reviewer_count_target')}）。审查将就地进行。",
        next_steps=["等待 reviewer 结果 → validate → fix → handoff（不变）。"],
        detail={
            "strictness": strictness, "preset": dict(preset),
            "effective_primary_files": effective_primary_files,
            "scope_finalize": finalize,
            "dispatch_plan_path": dispatch_result.get("plan_path"),
            "dispatch_result": dispatch_result,
        },
    )


# ---------------------------------------------------------------------------
# decline：理由持久化到 context 之外（rewind 不可抹）+ mark_declined（指纹入冷却表）
# ---------------------------------------------------------------------------

def _declined_reason_ledger_path(root: str | Path | None) -> Path:
    return declined_reason_ledger_root(root) / "declined-decision-reasons.jsonl"


def run_decline(
    *,
    token: str,
    reason: str,
    roots: AgentDecisionRoots | None = None,
    now: datetime | None = None,
) -> CommandResult:
    """``decline`` 子命令：mark_declined（终态 + 指纹入冷却表）+ 理由落 append-only ledger。

    理由持久化的落点是**磁盘上的 append-only ledger**（``~/.rvf`` 下，路径可注入），刻意在
    transcript / context 之外——rewind 抹不掉磁盘记录（R5/R12：理由必须持久化到 context 之外）。
    这也是 rewind-on-decline 的接入点（R7/R12：未来 rewind 编排会读它，本切片只落盘 + 标注）。
    """
    roots = roots or AgentDecisionRoots()
    now_dt = _coerce_now(now)

    # 先读一份 payload（拿 worktree / fingerprint 供 ledger 记录；mark_declined 后再读也行）。
    lookup = token_store.read_decision_token(token, root=roots.token_root, now=now_dt)
    worktree = str((lookup.payload or {}).get("worktree") or "") if lookup.payload else ""
    session_id = (lookup.payload or {}).get("session_id") if lookup.payload else None

    try:
        declined = token_store.mark_declined(
            token, reason=reason, root=roots.token_root, now=now_dt, cooldown_root=roots.cooldown_root
        )
    except token_store.DecisionTokenError as exc:
        return CommandResult(
            command="decline", status="error", exit_code=EXIT_TOKEN_STATE, token=token,
            token_state=lookup.state, message=f"无法 decline 决策 token：{exc}",
        )

    fingerprint = str(declined.payload.get("candidate_fingerprint") or "")
    # INTEGRATION-POINT(R7/R12 rewind-on-decline)：理由落 append-only 磁盘 ledger，rewind
    # 不可抹；未来 rewind-on-decline 编排（不在本 repo 实现，Linear 跟踪)会读本 ledger
    # 决定「decline 后回退到决策点前的上下文」——本切片只落盘 + 标注。
    ledger_path = _declined_reason_ledger_path(roots.declined_reason_ledger_root)
    _append_jsonl(
        ledger_path,
        {
            "token": token,
            "worktree": worktree,
            "session_id": session_id,
            "candidate_fingerprint": fingerprint,
            "reason": reason,
            "declined_at": _format_timestamp(now_dt),
        },
    )
    append_decision_telemetry(
        token=token, event="declined", worktree=worktree, session_id=session_id,
        now=now_dt, root=roots.telemetry_root, detail={"reason": reason, "candidate_fingerprint": fingerprint},
    )
    return CommandResult(
        command="decline", status="ok", exit_code=EXIT_OK, token=token, token_state=declined.state,
        message="已谢绝本次审查发起；理由已持久化（rewind 不可抹），候选指纹已入 declined 冷却表（指纹未变不再注入）。",
        detail={"declined_reason_ledger": str(ledger_path), "candidate_fingerprint": fingerprint},
    )


# ---------------------------------------------------------------------------
# decline-dispatched-review：谢绝一轮**已派发**的自动 RVF（kanban-followup / flow-1-self-rising）
#
# 与上面的 ``decline`` 是两种不同的 token：``decline`` 收的是 S3 决策 token（Stop hook 在决策点
# 铸的）；本命令收的是**dispatch token**（注入 prompt 里的 ``RVF_DISPATCH=token=<16 hex>``）。
# Kanban task 内的自动 RVF 走的是延续后端，结构上不经过 S3 决策点，历史上因此**根本没有谢绝
# 出口**——注入的是一条纯祈使句，agent 只能照跑。本命令补上那个出口。
# ---------------------------------------------------------------------------

# 锁身份对账判定（磁盘上那把 kanban-followup 锁，是不是「本次要谢绝的这一轮」）。
LOCK_IDENTITY_MATCHES_THIS_DISPATCH = "matches_this_dispatch"
LOCK_IDENTITY_BELONGS_TO_ANOTHER_DISPATCH = "belongs_to_another_dispatch"
LOCK_IDENTITY_UNVERIFIABLE_LEGACY_MARKER = "unverifiable_legacy_marker"

# 清锁 / 清 pending 的实际结果（in-progress 锁与 pending marker 共用同一族取值）。
KANBAN_FOLLOWUP_MARKER_CLEARED = "cleared"
KANBAN_FOLLOWUP_MARKER_ALREADY_ABSENT = "already_absent"
KANBAN_FOLLOWUP_MARKER_PRESERVED_FOR_ANOTHER_DISPATCH = "preserved_for_another_dispatch"
KANBAN_FOLLOWUP_MARKER_CLEAR_FAILED = "clear_failed"
KANBAN_FOLLOWUP_MARKER_SKIPPED_NO_IDENTITY_IN_PREP = "skipped_no_lock_identity_in_prep"


def classify_kanban_followup_lock_identity_against_dispatch(
    marker: Mapping[str, Any] | None,
    *,
    dispatch_token: str,
    run_id: str,
) -> str:
    """判定磁盘上这把 kanban-followup 锁是否就是 ``dispatch_token`` 这一轮派发的锁。

    判据优先序：
      1. marker 的 ``dispatch_token``（Fix A 起写入，是这把锁「对应哪一轮」的权威锚点）；
      2. 缺 1 时回退 marker 的 ``run_id``（**部署滞后**期间由老版 UPS arm 的锁没有
         ``dispatch_token``，但一直有 run_id——若这里直接判不匹配，就会在部署完成前把所有
         谢绝都变成空操作）；
      3. 两者都拿不到 → ``unverifiable_legacy_marker``：无从对账，沿用历史行为（照清），
         但调用方须把这一点如实上报，不能伪装成「已核对」。
    """
    if not isinstance(marker, Mapping):
        return LOCK_IDENTITY_UNVERIFIABLE_LEGACY_MARKER
    marker_dispatch_token = _string_or_empty(marker.get("dispatch_token"))
    if marker_dispatch_token:
        return (
            LOCK_IDENTITY_MATCHES_THIS_DISPATCH
            if marker_dispatch_token == _string_or_empty(dispatch_token)
            else LOCK_IDENTITY_BELONGS_TO_ANOTHER_DISPATCH
        )
    marker_run_id = _string_or_empty(marker.get("run_id"))
    if marker_run_id and _string_or_empty(run_id):
        return (
            LOCK_IDENTITY_MATCHES_THIS_DISPATCH
            if marker_run_id == _string_or_empty(run_id)
            else LOCK_IDENTITY_BELONGS_TO_ANOTHER_DISPATCH
        )
    return LOCK_IDENTITY_UNVERIFIABLE_LEGACY_MARKER


def _clear_kanban_followup_lock_and_pending_for_this_dispatch(
    *,
    task_id: str,
    session_id: str,
    dispatch_token: str,
    run_id: str,
    lock_root: Path | None,
) -> dict[str, Any]:
    """按 dispatch 身份**对账之后**再清锁 + 清 pending，绝不无条件按 task_id 删。

    踩过的坑（本函数存在的全部理由）：原实现只凭「prep 文件可读」就按 ``task_id`` 无条件
    ``clear_marker``，且没给本就内建 token 防误清的 ``clear_pending_marker`` 传 ``token=``。
    不带豁免的 decline 之后同一 scope **可能立刻派发下一轮**；此时若有人重试旧命令（或旧
    token 的调用迟到），就会把**新一轮**的锁与 pending 一并删掉，令当前 RVF 失去互斥与续跑
    状态——正是这条命令要修复的那类「被强制续跑一次没接受的 RVF」的镜像故障。

    对账逐条路径独立做（task 路径与 session 路径可能分属不同轮）：只清判定为「本轮」或
    「无从对账」的那条，判定为别轮的一律保留并如实上报。
    """
    outcome: dict[str, Any] = {
        "removed_lock_paths": [],
        "preserved_lock_paths": [],
        "lock_identity_verdicts": {},
        "removed_pending_paths": [],
    }
    try:
        import kanban_followup_lock  # noqa: PLC0415
    except Exception as exc:
        outcome["lock_outcome"] = KANBAN_FOLLOWUP_MARKER_CLEAR_FAILED
        outcome["pending_outcome"] = KANBAN_FOLLOWUP_MARKER_CLEAR_FAILED
        outcome["lock_clear_error"] = f"{type(exc).__name__}: {exc}"
        return outcome

    inspected_any_lock_marker = False
    any_lock_marker_belongs_to_another_dispatch = False
    try:
        for identity_dimension, dimension_task_id, dimension_session_id in (
            ("task", task_id or None, None),
            ("session", None, session_id or None),
        ):
            if dimension_task_id is None and dimension_session_id is None:
                continue
            marker = kanban_followup_lock.read_marker(
                task_id=dimension_task_id, session_id=dimension_session_id, root=lock_root
            )
            if marker is None:
                continue
            inspected_any_lock_marker = True
            verdict = classify_kanban_followup_lock_identity_against_dispatch(
                marker, dispatch_token=dispatch_token, run_id=run_id
            )
            outcome["lock_identity_verdicts"][identity_dimension] = verdict
            if verdict == LOCK_IDENTITY_BELONGS_TO_ANOTHER_DISPATCH:
                any_lock_marker_belongs_to_another_dispatch = True
                marker_path = marker.get("_marker_path")
                if isinstance(marker_path, str) and marker_path:
                    outcome["preserved_lock_paths"].append(marker_path)
                continue
            outcome["removed_lock_paths"].extend(
                kanban_followup_lock.clear_marker(
                    task_id=dimension_task_id, session_id=dimension_session_id, root=lock_root
                )
            )
    except Exception as exc:
        outcome["lock_outcome"] = KANBAN_FOLLOWUP_MARKER_CLEAR_FAILED
        outcome["lock_clear_error"] = f"{type(exc).__name__}: {exc}"

    if "lock_outcome" not in outcome:
        if any_lock_marker_belongs_to_another_dispatch:
            outcome["lock_outcome"] = KANBAN_FOLLOWUP_MARKER_PRESERVED_FOR_ANOTHER_DISPATCH
        elif outcome["removed_lock_paths"]:
            outcome["lock_outcome"] = KANBAN_FOLLOWUP_MARKER_CLEARED
        elif inspected_any_lock_marker:
            # 读到了本轮的 marker 却一个都没删掉 = unlink 全被 OSError 吞了，不是「本就没有锁」。
            outcome["lock_outcome"] = KANBAN_FOLLOWUP_MARKER_CLEAR_FAILED
        elif not (task_id or session_id):
            outcome["lock_outcome"] = KANBAN_FOLLOWUP_MARKER_SKIPPED_NO_IDENTITY_IN_PREP
        else:
            outcome["lock_outcome"] = KANBAN_FOLLOWUP_MARKER_ALREADY_ABSENT

    # pending 侧：token 防误清 guard 一直就在 clear_pending_marker 里，历史上只是没传 token。
    # 这里先读一次是为了**如实分辨**「本就没有 pending」与「pending 属于另一轮故未清」——
    # 两种情况 clear_pending_marker 都返回空列表，光看返回值分不出来。
    try:
        pending_marker = kanban_followup_lock.read_pending_marker(
            task_id=task_id or None, root=lock_root
        )
        if pending_marker is None:
            outcome["pending_outcome"] = KANBAN_FOLLOWUP_MARKER_ALREADY_ABSENT
        else:
            pending_token = _string_or_empty(pending_marker.get("token"))
            if pending_token and pending_token != _string_or_empty(dispatch_token):
                outcome["pending_outcome"] = KANBAN_FOLLOWUP_MARKER_PRESERVED_FOR_ANOTHER_DISPATCH
                pending_path = pending_marker.get("_marker_path")
                if isinstance(pending_path, str) and pending_path:
                    outcome["preserved_pending_paths"] = [pending_path]
            else:
                removed_pending_paths = kanban_followup_lock.clear_pending_marker(
                    task_id=task_id or None, token=dispatch_token or None, root=lock_root
                )
                outcome["removed_pending_paths"] = list(removed_pending_paths)
                outcome["pending_outcome"] = (
                    KANBAN_FOLLOWUP_MARKER_CLEARED
                    if removed_pending_paths
                    else KANBAN_FOLLOWUP_MARKER_CLEAR_FAILED
                )
    except Exception as exc:
        outcome["pending_outcome"] = KANBAN_FOLLOWUP_MARKER_CLEAR_FAILED
        outcome["pending_clear_error"] = f"{type(exc).__name__}: {exc}"

    return outcome


def run_decline_dispatched_review(
    *,
    token: str,
    reason: str,
    mark_declined_scope_units_review_not_required: bool = False,
    prep_root: str | Path | None = None,
    lock_root: Path | None = None,
    roots: AgentDecisionRoots | None = None,
    now: datetime | None = None,
) -> CommandResult:
    """谢绝一轮已派发的自动 RVF：清锁 + 释放 lease + 理由落盘（+ 可选 diff 级豁免）。

    ``mark_declined_scope_units_review_not_required`` **默认 False**：不带它时谢绝只表示
    「这一轮不审」，本轮 scope 的 unit 退回 ``available``，后续 Stop 仍会重新提议同一批改动。
    带上它才把本轮 scope 声明为「不需审核」——粒度是 diff hunk 级（tracker 的原生粒度），
    该 hunk 内容一旦改动就会产生新 unit、不继承豁免。

    **返回值如实反映实际发生了什么**：豁免 / 释放 lease / 清锁三步各自都可能失败或按设计成为
    no-op（DB 写不动、lease 早已释放、磁盘上的锁属于另一轮…）。历史实现固定返回
    ``status="ok"``，还在带豁免时无条件宣称「已把本轮 scope 声明为不需审核」——写失败时用户
    看到的是谢绝成功而 scope/锁其实仍生效，是**谎报成功**。现在：任一关键步骤未达成即
    ``status="decline_incomplete"`` + ``EXIT_DECLINE_DISPATCHED_REVIEW_INCOMPLETE``，
    ``detail["failed_steps"]`` 逐条说明哪步为何没成，且**请求了豁免却没豁免成功时绝不宣称已
    豁免**。谢绝理由本身仍无条件落 ledger（那是 append-only 的事实记录，与副作用成败无关）。
    """
    roots = roots or AgentDecisionRoots()
    now_dt = _coerce_now(now)

    lookup = rvf_prep_file.read_prep_file(token, root=prep_root)
    payload = lookup.payload if isinstance(lookup.payload, dict) else None
    if payload is None:
        return CommandResult(
            command="decline-dispatched-review", status="error", exit_code=EXIT_TOKEN_STATE,
            token=token, token_state=lookup.status,
            message=f"无法谢绝：dispatch token 对应的 prep 不可读（status={lookup.status}）。",
        )

    rvf_run = payload.get("rvf_run") if isinstance(payload.get("rvf_run"), dict) else {}
    run_id = _string_or_empty(rvf_run.get("run_id"))
    lease_id = _string_or_empty(rvf_run.get("tracker_lease_id"))
    repo = _string_or_empty(payload.get("target_worktree")) or _string_or_empty(
        payload.get("origin_repo")
    )
    task_id = _string_or_empty(payload.get("target_kanban_task_id"))
    session_id = _string_or_empty(payload.get("origin_session_id"))

    detail: dict[str, Any] = {"run_id": run_id, "lease_id": lease_id, "repo": repo}
    # 每一步的实况：``{"step": …, "reason": …}``。非空即「谢绝没有完全生效」，直接决定
    # status / exit_code / message，也一并落 ledger 与遥测（磁盘上可审计，不只在 context 里）。
    failed_steps: list[dict[str, str]] = []
    next_steps: list[str] = []

    # ① 可选豁免：必须在释放 lease **之前**做——释放会把 unit 打回 available，之后再声明就
    #    存在一个「已 available、可被别的 Stop 抢走」的竞态窗口。
    review_not_required_declared = False
    if mark_declined_scope_units_review_not_required:
        if not repo:
            detail["review_not_required_error"] = "no_repo_in_prep_payload"
            failed_steps.append(
                {"step": "review_not_required_waiver", "reason": "no_repo_in_prep_payload"}
            )
        else:
            try:
                from core.session_scope_allocation.reviewable_unit_diff_tracker import (  # noqa: PLC0415
                    declare_units_review_not_required,
                )

                waiver = declare_units_review_not_required(
                    repo=repo,
                    reason=f"agent_declined_dispatched_review: {reason}",
                    lease_id=lease_id or None,
                    run_id=run_id or None,
                    declared_by_run_id=run_id or None,
                )
                detail["review_not_required"] = waiver
                waiver_status = (
                    _string_or_empty(waiver.get("status")) if isinstance(waiver, Mapping) else ""
                )
                # ``noop`` = 一个 unit 都没声明成（目标解析不出 / 全被 tombstone）。它和写失败
                # 对用户是同一件事：**没有豁免**。不许它顺着 status=ok 混过去。
                review_not_required_declared = waiver_status == "declared"
                if not review_not_required_declared:
                    failed_steps.append(
                        {
                            "step": "review_not_required_waiver",
                            "reason": "declare_units_review_not_required_"
                            + (waiver_status or "unexpected_result"),
                        }
                    )
            except Exception as exc:  # 豁免失败不阻断谢绝本身，但绝不可被当成已豁免
                detail["review_not_required_error"] = f"{type(exc).__name__}: {exc}"
                failed_steps.append(
                    {"step": "review_not_required_waiver", "reason": f"{type(exc).__name__}"}
                )
        if not review_not_required_declared:
            next_steps.append(
                "本轮 scope **未**被声明为「不需审核」：修好上面的原因后，带 "
                "--mark-declined-scope-units-review-not-required 重跑本命令。"
            )
    detail["review_not_required_declared"] = review_not_required_declared

    # ② 释放 lease：unit 退回 available（已豁免的那批此刻是 reviewed，release 的
    #    ``review_state='assigned'`` 条件命不中它们，故豁免不会被 release 冲掉）。
    if not lease_id:
        # prep 里压根没记 lease：没有东西要释放，不算失败（不是所有派发都持 tracker lease）。
        detail["lease_release_outcome"] = "skipped_no_lease_id_in_prep"
    elif not repo:
        detail["lease_release_outcome"] = "skipped_no_repo_in_prep"
        failed_steps.append({"step": "lease_release", "reason": "no_repo_in_prep_payload"})
    else:
        try:
            from core.session_scope_allocation.reviewable_unit_diff_tracker import (  # noqa: PLC0415
                lease_release,
            )

            release = lease_release(
                repo=repo, lease_id=lease_id, reason="agent_declined_dispatched_review"
            )
            detail["lease_release"] = release
            released = bool(isinstance(release, Mapping) and release.get("released"))
            # ``missing``（lease_not_found）= 这把 lease 早已释放/不存在：unit 不可能还卡在
            # assigned，对谢绝而言无害，如实记一笔但不算失败。
            detail["lease_release_outcome"] = "released" if released else "already_released_or_missing"
        except Exception as exc:
            detail["lease_release_error"] = f"{type(exc).__name__}: {exc}"
            detail["lease_release_outcome"] = "failed"
            failed_steps.append({"step": "lease_release", "reason": f"{type(exc).__name__}"})

    # ③ 清锁 + 清 pending：不清的话 Stop 读侧会继续拿这把锁催 agent 收尾这一轮。**先按
    #    dispatch 身份对账**，不匹配一律保留（详见被调函数的坑说明）。
    lock_clear = _clear_kanban_followup_lock_and_pending_for_this_dispatch(
        task_id=task_id,
        session_id=session_id,
        dispatch_token=token,
        run_id=run_id,
        lock_root=lock_root,
    )
    detail.update(lock_clear)
    if lock_clear.get("lock_outcome") == KANBAN_FOLLOWUP_MARKER_PRESERVED_FOR_ANOTHER_DISPATCH:
        failed_steps.append(
            {"step": "kanban_followup_lock_clear", "reason": "lock_belongs_to_another_dispatch"}
        )
        next_steps.append(
            "磁盘上的 kanban-followup 锁属于**另一轮** dispatch（token/run 对不上），已原样保留；"
            "若你要谢绝的是当前那一轮，请用当前注入 prompt 里的 RVF_DISPATCH token 重跑。"
        )
    elif lock_clear.get("lock_outcome") == KANBAN_FOLLOWUP_MARKER_CLEAR_FAILED:
        failed_steps.append({"step": "kanban_followup_lock_clear", "reason": "clear_failed"})
        next_steps.append(
            "锁未清干净：Stop 读侧仍会拿这把锁催你收尾这一轮——请人工核查 "
            f"{detail.get('lock_clear_error') or 'kanban-followup 锁目录'}。"
        )
    if lock_clear.get("pending_outcome") == KANBAN_FOLLOWUP_MARKER_PRESERVED_FOR_ANOTHER_DISPATCH:
        failed_steps.append(
            {"step": "kanban_followup_pending_clear", "reason": "pending_belongs_to_another_dispatch"}
        )
    elif lock_clear.get("pending_outcome") == KANBAN_FOLLOWUP_MARKER_CLEAR_FAILED:
        failed_steps.append({"step": "kanban_followup_pending_clear", "reason": "clear_failed"})

    # ④ 理由落 append-only 磁盘 ledger（与 S3 decline 同一份），``token_kind`` 区分两种 token。
    #    刻意分开记「请求了豁免」与「豁免真的成了」——历史实现只记前者且键名叫
    #    ``marked_scope_units_review_not_required``，写失败时这条落盘记录本身就是假的。
    ledger_path = _declined_reason_ledger_path(roots.declined_reason_ledger_root)
    _append_jsonl(
        ledger_path,
        {
            "token": token,
            "token_kind": "dispatch",
            "worktree": repo,
            "session_id": session_id or None,
            "kanban_task_id": task_id or None,
            "run_id": run_id or None,
            "reason": reason,
            "review_not_required_waiver_requested": bool(
                mark_declined_scope_units_review_not_required
            ),
            "review_not_required_waiver_declared": review_not_required_declared,
            "failed_steps": list(failed_steps),
            "declined_at": _format_timestamp(now_dt),
        },
    )
    detail["declined_reason_ledger"] = str(ledger_path)
    append_decision_telemetry(
        token=token, event="dispatched_review_declined", worktree=repo,
        session_id=session_id or None, now=now_dt, root=roots.telemetry_root,
        detail={
            "reason": reason,
            "run_id": run_id or None,
            "review_not_required_waiver_requested": bool(
                mark_declined_scope_units_review_not_required
            ),
            "review_not_required_waiver_declared": review_not_required_declared,
            "lock_outcome": detail.get("lock_outcome"),
            "pending_outcome": detail.get("pending_outcome"),
            "lease_release_outcome": detail.get("lease_release_outcome"),
            "failed_steps": list(failed_steps),
        },
    )

    detail["failed_steps"] = list(failed_steps)
    if failed_steps:
        failure_summary = "；".join(
            f"{item['step']}={item['reason']}" for item in failed_steps
        )
        message = "本次谢绝**未完全生效**：" + failure_summary + "。"
        if mark_declined_scope_units_review_not_required and not review_not_required_declared:
            # RVF-004 的核心：请求了豁免却没豁免成功时，一个字都不许暗示已豁免。
            message += "特别注意：本轮 scope 的 diff **没有**被声明为「不需审核」，请勿按已豁免行事。"
        message += "谢绝理由与上述逐步实况均已落盘（rewind 不可抹）。"
        return CommandResult(
            command="decline-dispatched-review", status="decline_incomplete",
            exit_code=EXIT_DECLINE_DISPATCHED_REVIEW_INCOMPLETE,
            token=token, token_state=lookup.status, message=message,
            next_steps=next_steps, detail=detail,
        )

    if review_not_required_declared:
        message = (
            "已谢绝本轮自动 RVF，并把本轮 scope 的 diff 声明为「不需审核」"
            "（hunk 级：这些 diff 内容一旦改动会重新进入审核范围）。理由已落盘，rewind 不可抹。"
        )
    else:
        message = (
            "已谢绝本轮自动 RVF；本轮 scope 退回可审池，后续 Stop 仍可能重新提议同一批改动。"
            "若确定这批 diff 无需审核，请改带 --mark-declined-scope-units-review-not-required。"
            "理由已落盘，rewind 不可抹。"
        )
    return CommandResult(
        command="decline-dispatched-review", status="ok", exit_code=EXIT_OK,
        token=token, token_state=lookup.status, message=message,
        next_steps=next_steps, detail=detail,
    )


def _string_or_empty(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


# ---------------------------------------------------------------------------
# 错误结果统一构造
# ---------------------------------------------------------------------------

def _token_error_result(command: str, token: str, exc: TokenLoadError) -> CommandResult:
    return CommandResult(
        command=command, status="error", exit_code=EXIT_TOKEN_STATE, token=token,
        token_state=exc.token_state, message=str(exc),
    )


# ---------------------------------------------------------------------------
# argparse 入口
# ---------------------------------------------------------------------------

def _parse_annotate_entry(raw: str) -> tuple[str, str]:
    """解析 ``--entry <path>:<note>``：只在**第一个**冒号处切分（note 里可含冒号）。"""
    if ":" not in raw:
        raise argparse.ArgumentTypeError(f"--entry 须为 <path>:<note> 形式，缺冒号: {raw!r}")
    path, note = raw.split(":", 1)
    if not path.strip():
        raise argparse.ArgumentTypeError(f"--entry 的 path 为空: {raw!r}")
    return path.strip(), note


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rvf-agent",
        description="RVF 主 agent 发起审查的单一入口（S3）：start / scope / dispatch / decline。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="token→acknowledged + 前置校验 + prepare 全套 + 自动 scope 产出")
    p_start.add_argument("--token", required=True, help="注入 prompt 里拿到的决策 token（显式回传）")
    p_start.add_argument("--reason", required=True, help="简析后开启审查的理由")
    p_start.add_argument("--repo", default=None, help="目标 worktree（默认取 token 记录的 worktree）")
    p_start.add_argument("--session-id", default=None, help="覆盖 session id（默认取 token 记录）")
    p_start.add_argument("--transcript-path", default=None, help="主 agent transcript（可选，改善 manifest）")
    p_start.add_argument("--retry", action="store_true", help="prepare 失败后重试（token 仍在 acknowledged）")

    p_scope = sub.add_parser("scope", help="改动自动 scope（annotate / exclude / include），仅 acknowledged 态")
    scope_sub = p_scope.add_subparsers(dest="scope_action", required=True)
    p_ann = scope_sub.add_parser("annotate", help="批量给候选路径加注释")
    p_ann.add_argument("--token", required=True)
    p_ann.add_argument("--entry", action="append", default=[], type=_parse_annotate_entry,
                       help="<path>:<note>，可重复（批量）")
    p_exc = scope_sub.add_parser("exclude", help="把路径移出自动 scope")
    p_exc.add_argument("--token", required=True)
    p_exc.add_argument("paths", nargs="+", help="要排除的路径（可多个）")
    p_exc.add_argument("--reason", required=True, help="排除理由")
    p_inc = scope_sub.add_parser("include", help="把路径追加进自动 scope")
    p_inc.add_argument("--token", required=True)
    p_inc.add_argument("paths", nargs="+", help="要追加的路径（可多个）")
    p_inc.add_argument("--reason", required=True, help="追加理由")

    p_disp = sub.add_parser("dispatch", help="scope 定稿 + 严格度终选 + 派发 reviewer + 终态 dispatched")
    p_disp.add_argument("--token", required=True)
    p_disp.add_argument("--strictness", required=True, choices=list(STRICTNESS_TIERS))
    p_disp.add_argument("--dry-run", action="store_true", help="dispatch_reviewers 走 --dry-run，不真拉起 reviewer")

    p_dec = sub.add_parser("decline", help="谢绝发起：理由持久化 + 指纹入冷却表 + 终态 declined")
    p_dec.add_argument("--token", required=True)
    p_dec.add_argument("--reason", required=True, help="谢绝理由（持久化到 context 之外，rewind 不可抹）")

    p_dec_dispatched = sub.add_parser(
        "decline-dispatched-review",
        help="谢绝一轮**已派发**的自动 RVF（收 dispatch token）：清锁 + 释放 lease + 理由持久化",
    )
    p_dec_dispatched.add_argument(
        "--token", required=True, help="注入 prompt 里的 dispatch token（RVF_DISPATCH=token=…）"
    )
    p_dec_dispatched.add_argument(
        "--reason", required=True, help="谢绝理由（持久化到 context 之外，rewind 不可抹）"
    )
    p_dec_dispatched.add_argument(
        "--mark-declined-scope-units-review-not-required",
        action="store_true",
        help=(
            "额外把本轮 scope 的 diff 声明为「不需审核」，后续 RVF 不再纳入范围"
            "（diff hunk 级；内容改动后会重新进入范围）。**默认不声明**。"
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    if args.command == "start":
        result = run_start(
            token=args.token, reason=args.reason, repo=args.repo,
            session_id=args.session_id, transcript_path=args.transcript_path, retry=args.retry,
        )
    elif args.command == "scope":
        if args.scope_action == "annotate":
            result = run_scope_annotate(token=args.token, entries=list(args.entry))
        elif args.scope_action == "exclude":
            result = run_scope_exclude(token=args.token, paths=list(args.paths), reason=args.reason)
        else:  # include
            result = run_scope_include(token=args.token, paths=list(args.paths), reason=args.reason)
    elif args.command == "dispatch":
        result = run_dispatch(token=args.token, strictness=args.strictness, dry_run=args.dry_run)
    elif args.command == "decline":
        result = run_decline(token=args.token, reason=args.reason)
    elif args.command == "decline-dispatched-review":
        result = run_decline_dispatched_review(
            token=args.token,
            reason=args.reason,
            mark_declined_scope_units_review_not_required=(
                args.mark_declined_scope_units_review_not_required
            ),
        )
    else:  # pragma: no cover - argparse required=True 已挡
        parser.error(f"未知子命令 {args.command!r}")
        return EXIT_USAGE

    stream = sys.stdout if result.exit_code == EXIT_OK else sys.stderr
    print(result.render(), file=stream)
    return result.exit_code


__all__ = [
    "EXIT_OK", "EXIT_USAGE", "EXIT_TOKEN_STATE", "EXIT_START_FAILED", "EXIT_DISPATCH_FAILED",
    "EXIT_BOOTSTRAP_REQUIRED", "EXIT_DECLINE_DISPATCHED_REVIEW_INCOMPLETE",
    "LOCK_IDENTITY_MATCHES_THIS_DISPATCH", "LOCK_IDENTITY_BELONGS_TO_ANOTHER_DISPATCH",
    "LOCK_IDENTITY_UNVERIFIABLE_LEGACY_MARKER",
    "KANBAN_FOLLOWUP_MARKER_CLEARED", "KANBAN_FOLLOWUP_MARKER_ALREADY_ABSENT",
    "KANBAN_FOLLOWUP_MARKER_PRESERVED_FOR_ANOTHER_DISPATCH",
    "KANBAN_FOLLOWUP_MARKER_CLEAR_FAILED",
    "KANBAN_FOLLOWUP_MARKER_SKIPPED_NO_IDENTITY_IN_PREP",
    "classify_kanban_followup_lock_identity_against_dispatch",
    "AgentDecisionRoots", "CommandResult", "TokenLoadError",
    "legacy_codex_rvf_env",
    "scope_adjustment_root", "decision_telemetry_root", "scope_issue_ledger_root",
    "declined_reason_ledger_root", "scope_adjustment_path", "read_scope_adjustment",
    "append_decision_telemetry", "record_scope_exception_and_maybe_file_issue",
    "compute_effective_primary_files", "load_strictness_preset", "finalize_scope_contract",
    "resolve_agent_initiation_backend", "default_launch_preflight", "default_provider_health_requirements",
    "run_start", "run_scope_annotate", "run_scope_exclude", "run_scope_include", "run_dispatch", "run_decline",
    "run_decline_dispatched_review",
    "build_argument_parser", "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
