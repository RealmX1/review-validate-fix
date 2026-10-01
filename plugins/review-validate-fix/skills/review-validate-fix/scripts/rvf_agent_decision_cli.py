#!/usr/bin/env python3
"""RVF 决策 CLI（经 PATH 上的 ``rvf`` 快捷入口调用；RVF overhaul S3：主 agent 发起审查的**单一入口**）。

RVF overhaul 把「要不要审、审什么」的发起决策权从 Stop hook 交给主 agent。Stop hook 降级为
决策点信使：它铸一枚决策 token、把 token 连同候选摘要注入给主 agent；主 agent 简析后经**本
CLI** 显式 ``start`` / ``scope …`` / ``dispatch`` 或 ``decline``。token 一律由主 agent 从注入
prompt 里拿到、显式回传（本 CLI 不枚举、不发现 token —— R18：不存在 ``candidates`` 子命令）。

子命令（实施计划 §四 S3 / §三 agent 侧流程）：

  * ``start --token [--transcript-path] --reason [--retry]``：token → ``acknowledged`` →
    **backend/launch 前置校验 + provider-health 门** → **tracker review scope 分配**（
    ``build_manifest`` 归属播种 + ``allocate_review_scope``，与 Stop hook 旧派发路径同口径；见
    「发起侧 tracker review scope 分配」节） → ``prepare_review_run`` 全套（原 UPS
    ``_run_shared_workflow`` 的重量级准备职能退役至此，本切片**不改 UPS 本身**，只把该职能在 CLI
    里实现） → **自动 scope 产出** 落盘（供 scope 子命令编辑）。任一步失败 →
    ``record_start_failure`` 回退**保持 acknowledged**，支持 ``start --retry``，不吞决策。
    ``--transcript-path`` 省略时回退读决策 token 候选摘要里 Stop/UPS 盖的那份；两者都拿不到时
    scope 口径**显式降级**（回执 message + ``detail.tracker_scope_allocation`` + ledger 各一份）。
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
import rvf_dispatch_prompts  # noqa: E402 — scope-of-work stub 模板与 stub 标记（与 prepare 同源）
import review_reopen_marker  # noqa: E402 — 失败再入（$rvf-reopen）在 allocate 前的消费，与 Stop hook 同源
from core.rvf_shared_runtime_constants.rvf_shared_runtime_constants import (  # noqa: E402
    HANDOFF_FINAL_REPLY_STRUCTURE_INSTRUCTION,
)
from core.rvf_command_line_shortcut_shim_location import (  # noqa: E402
    RVF_COMMAND_LINE_SHORTCUT_ENTRYPOINT_SCRIPT_FILENAME,
    resolve_rvf_command_line_shortcut_invocation,
)


def _rvf() -> str:
    """回执 / next_steps 里渲染的命令前缀：PATH 上的 ``rvf`` > shim 绝对路径 > 入口脚本绝对路径。

    历史上这里硬写 ``rvf-agent``——PATH 上根本没有这个命令，agent 照抄即 command not found。
    """
    return resolve_rvf_command_line_shortcut_invocation(
        fallback_entrypoint_script_path=Path(__file__).resolve().with_name(
            RVF_COMMAND_LINE_SHORTCUT_ENTRYPOINT_SCRIPT_FILENAME
        )
    )


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
# dispatch 撞上「有 reviewer 返回 kind: request」：**待处理终态**，既非故障也非完成态 review。
# 与 EXIT_DISPATCH_FAILED 分开的理由与 dispatch_reviewers 的专用码 3 完全同源：混同成失败会把
# agent 引向「修一修再重跑 dispatch」，而正确处置是先走 Contract Gate 处理那条 request，再用
# `--redispatch-review-request-legs-only` 经同一入口**只重派那条 request 腿**
#（见 REVIEW_REQUEST_REDISPATCH_FLAG）。
# ⚠ 默认的 detached 派发里 dispatch 在 reviewer 跑完之前就返回，所以**这个码实际不会出现**：
# request 终态由 `rvf wait` 的摘要（plan_status=blocked_on_reviewer_request）报出。只有同步返回
# 结果的 dispatcher（测试注入 / 未来的同步模式）才可能走到这里。
EXIT_DISPATCH_BLOCKED_ON_REVIEWER_REQUEST = 8
# dispatch 的 contract gate：scope-of-work 仍是 hook 生成的 stub（未被 agent 覆盖）。**不是故障**，
# 而是「还差一步没做」。与 EXIT_DISPATCH_FAILED 分开的理由：派了也白派——reviewer 的 contract
# gate 会当场把这一轮退回成 `kind: request`，而那是个同轮内转不回 issues 的死结（实测废掉整轮
# codex 评审、白跑约 13 分钟）。在派发**之前**拦住，比派完再退回便宜一个数量级。
EXIT_SCOPE_OF_WORK_STILL_STUB = 9
# start 探到「本会话名下一个未审单元都没有」：这批 diff 就是上一轮 review 的产物。**不是失败**，
# 也不是成功——是一次「没东西可审」的诚实回执。token 保持 acknowledged，agent 可以 decline，也可以
# `--force-full-rescope` 显式要求重审一遍。
EXIT_NO_UNREVIEWED_SCOPE = 10

# ``default_prepare_pipeline`` 在「无未审单元」时的提前返回状态。刻意不复用 ``failed``：
# 这一轮什么都没坏，只是没东西可审；混进 failed 会让 `--retry` 看起来像修复手段。
PREPARE_STATUS_NO_UNREVIEWED_SCOPE = "no_unreviewed_scope"

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

    # 人类可读行的白名单：这几个键必须**不解析 JSON 也看得到**。``execution_handoff`` 的子键
    # 单列在下方——它承载「接受之后拿什么去执行」（INV-3），恰恰是最不该逼 agent 去 grep
    # JSON 才能拿到的一组路径。
    RENDERED_DETAIL_KEYS = (
        "run_id",
        "run_dir",
        "scope_artifact",
        "dispatch_plan_path",
        "dispatch_status_path",
        "dispatch_launch_status",
        "declined_reason_ledger",
    )
    RENDERED_EXECUTION_HANDOFF_KEYS = (
        "target_repo",
        "prep_file",
        "review_env",
        "review_packet",
        "scope_contract",
        "scope_of_work",
        "review_agent_context",
        "session_manifest",
        "worktree_bootstrap",
        "origin_metadata",
        "handoff_path",
        # handoff `## 状态` 的 intent_artifact_status 要照抄它；不渲染出来 agent 就得去翻 JSON。
        "intent_artifact_status_file",
    )

    def render(self) -> str:
        head = f"[rvf {self.command}] {self.status}"
        lines = [head]
        if self.token:
            state = f" ({self.token_state})" if self.token_state else ""
            lines.append(f"- token: {self.token}{state}")
        if self.message:
            lines.append(f"- {self.message}")
        for key in self.RENDERED_DETAIL_KEYS:
            value = self.detail.get(key)
            if value:
                lines.append(f"- {key}: {value}")
        execution_handoff = self.detail.get("execution_handoff")
        if isinstance(execution_handoff, dict):
            lines.append("- execution_handoff:")
            for key in self.RENDERED_EXECUTION_HANDOFF_KEYS:
                value = execution_handoff.get(key)
                if value:
                    lines.append(f"    - {key}: {value}")
            for key in ("execution_instruction", "handoff_contract"):
                value = execution_handoff.get(key)
                if value:
                    lines.append(f"    - {key}: {value}")
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
            f"该操作要求 token 处 acknowledged 态（先跑 {_rvf()} start），当前 state={record.state}。",
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


# ---------------------------------------------------------------------------
# 发起侧 tracker review scope 分配（``start`` 与 Stop hook 旧派发路径**同口径**）
#
# **为什么这一步必须在这里做**：``prepare_run`` 产出的 scope contract 的口径完全由输入决定——
#
#   * ``origin_transcript_path`` 在场 ⇒ prepare 才会用 transcript 生成 session-manifest，contract
#     的 ``scope_mode`` 才是 ``session-owned``；缺它就退化成 ``manual-all-uncommitted``，把**别的
#     会话**的背景 dirty WIP 一并吞进 ``primary_files`` / ``fix_allowlist``（范围污染）。
#   * ``rvf_run.tracker_scope_path`` 在场 ⇒ contract 才有 ``primary_units`` / ``tracker_lease_id`` /
#     ``tracker_scope_hash``。**没有 lease，handoff 就无法把本轮 units 标成 reviewed**：清锁之后
#     同一批未提交 diff 会被后续每一次 Stop 反复重新提议，session-scope / review-highwater
#     那一整套「已审/未审」机制在默认发起路径上被整个绕过。
#
# 历史上这两件事由 Stop hook 的 ``route_reviewable_scope`` 链完成（``refresh_global_diff_tracker``
# → ``build_manifest`` → ``allocate_auto_review_scope`` → ``freeze_cline_kanban_dispatch_artifacts``）。
# P1 把默认发起改成「agent 在决策点就地 ``rvf-agent start``」之后那条链整条不再经过，于是本 CLI
# 里的 prep payload 把三个 tracker 字段写死成 ``None``——旧 kanban-followup 路径产出 ``session-owned``
# + 非空 ``primary_units`` + 真 lease，同一台机器上的 ``rvf-agent start`` 产出
# ``manual-all-uncommitted`` + ``primary_units=0`` + ``lease=None``。本节把那条链**复用既有入口**
# 补回来（``build_manifest`` 做 transcript 归属播种 + ``allocate_review_scope`` 做分配），刻意不另写
# 一份分配逻辑：两处口径一旦分叉，「已审」的裁决权就有两个真相源。
#
# **降级方向一律保守（宁可多审，不可漏审）**：任何一环拿不到（无 session 身份 / 无可读 transcript /
# manifest 失败 / allocator 说空或不可用），都回落到既有的无 tracker 口径（contract 退回
# ``manual-all-uncommitted``，review packet 仍全量覆盖工作树），但**必须留下显式记录**——返回值里
# 的 ``status`` / ``degradation_reason``、run ledger 事件、决策遥测、以及 ``start`` 回执正文各一份。
# 静默回落正是本 issue 之所以能潜伏到「所有自动 RVF 的默认路径」的原因。
# ---------------------------------------------------------------------------

# 分配成功。
AGENTIC_INITIATION_TRACKER_SCOPE_ALLOCATED = "allocated"
# 以下均为**降级**（无 lease、contract 退回 manual-all-uncommitted），按可诊断性分开命名。
AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_NO_SESSION_IDENTITY = "degraded_no_session_identity"
AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_NO_READABLE_TRANSCRIPT = "degraded_no_readable_transcript"
AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_SESSION_MANIFEST_FAILED = "degraded_session_manifest_failed"
AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_TRANSCRIPT_SCHEMA_DRIFT = (
    "degraded_transcript_schema_drift_suspected"
)
AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_ALLOCATOR_EMPTY = "degraded_allocator_reported_empty"
# allocator 报 empty 且**候选单元数为 0**（不是被在途 lease 挡住）：本会话名下所有可审单元都已
# reviewed。历史上这一支与「被 lease 排除」压成同一个降级、静默扩成全量重审，而无 lease 的一轮审完
# 又标不了 reviewed —— 于是同一批 diff 在之后每一次 Stop 都被重新提议。这个状态把它拆出来。
AGENTIC_INITIATION_TRACKER_SCOPE_NO_UNREVIEWED_SCOPE = "no_unreviewed_scope_all_units_already_reviewed"
AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_ALLOCATOR_UNAVAILABLE = "degraded_allocator_unavailable"
AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_ALLOCATOR_ERROR = "degraded_allocator_error"

# 分配出的 lease 记在谁名下（Stop hook 旧路径记的是 ``stop-hook``）。就地发起换一个自解释的
# holder 名，使 tracker events / 冲突诊断一眼看得出这一轮是「agent 在决策点接受后就地开的」。
AGENTIC_INITIATION_TRACKER_SCOPE_REVIEWER_ID = "agentic-initiation-start"

# 就地发起的一轮 RVF 由主 agent 在**当前会话**跑完 review → merge → validate/fix → handoff，墙钟
# 远超 tracker 默认的 600s lease TTL。lease 若在 review 中途过期，units 会被 prune 回 available，
# 下一次 Stop 立刻把同一批改动重新提议——正是本修复要根治的那个循环。
#
# 与 ``kanban_followup_lock.DEFAULT_TTL_SECONDS`` **必须同档**：两者保护的是同一个事件（这一轮
# RVF 还在跑），只是一个挡 Stop 重铸、一个挡 units 被 prune。任一方先到期，另一方就形同虚设。
# 该常量历史上照 1h 标定，正是沿用了那把锁的 1h；本轮实测 p50 = 84.3min、53% 超 60min，两者
# 一并上调到 2h（理由与分布见那边的注释）。
#
# ⚠️ **仍是「无心跳」的一侧**：``kanban_followup_lock`` 这一轮补了心跳续期（``revive_marker``
# + 绝对硬顶），tracker lease 没有对应的 refresh 入口，所以它这个 2h 是**硬上界**而非兜底。
# 超过 2h 的一轮仍会丢 lease。真正对齐要给 tracker 也开一个 heartbeat 入口，属独立一刀。
AGENTIC_INITIATION_TRACKER_SCOPE_LEASE_TTL_SECONDS = 2 * 60 * 60

# 与 ``prepare_review_run`` 的 prep payload 键同名：本仓里「这一轮 run 源自哪份 transcript」一直
# 叫这个名字，决策 token 的候选摘要沿用它，避免同一件事在两层各起一个名。
CANDIDATE_SUMMARY_ORIGIN_TRANSCRIPT_PATH_KEY = "origin_transcript_path"


TRANSCRIPT_PATH_SOURCE_EXPLICIT_FLAG = "explicit_flag"
TRANSCRIPT_PATH_SOURCE_DECISION_TOKEN_CANDIDATE_SUMMARY = "decision_token_candidate_summary"
TRANSCRIPT_PATH_SOURCE_UNRESOLVED = "unresolved"


def resolve_start_transcript_path(
    *,
    explicit_transcript_path: str | None,
    candidate_summary: Any,
) -> tuple[str | None, str]:
    """``start`` 用哪份 transcript：显式 ``--transcript-path`` > 决策 token 候选摘要里盖的那份。

    返回 ``(path_or_None, 来源标签)``；来源标签进遥测，使「这一轮为什么没有 transcript」在事后
    可判读，而不是只看到一个 None。只接受**当前确实存在的文件**：候选摘要是铸 token 那一刻写的，
    到 ``start`` 之间会话可能已被 rewind / transcript 被轮转，指着一个不存在的路径去跑
    ``build_manifest`` 只会换来一次没必要的失败降级。
    """
    if isinstance(explicit_transcript_path, str) and explicit_transcript_path.strip():
        return explicit_transcript_path, TRANSCRIPT_PATH_SOURCE_EXPLICIT_FLAG
    if isinstance(candidate_summary, Mapping):
        stamped = candidate_summary.get(CANDIDATE_SUMMARY_ORIGIN_TRANSCRIPT_PATH_KEY)
        if isinstance(stamped, str) and stamped.strip():
            try:
                exists = Path(stamped).expanduser().is_file()
            except OSError:
                exists = False
            if exists:
                return stamped, TRANSCRIPT_PATH_SOURCE_DECISION_TOKEN_CANDIDATE_SUMMARY
    return None, TRANSCRIPT_PATH_SOURCE_UNRESOLVED


def _degraded_tracker_scope_allocation(
    status: str,
    *,
    reason: str,
    error: str | None = None,
    allocator_status: str | None = None,
    session_manifest_has_owned_paths: bool = False,
) -> dict[str, Any]:
    """降级结果的统一构造器：三个 tracker 字段全 None + 一条人类可读的中文降级理由。

    ``session_manifest_has_owned_paths`` 与 tracker 字段**分开**报：即使分配失败，只要 transcript
    归属出了 owned paths，prepare 仍应吃这份 transcript（拿到 ``session-owned`` 口径），只是没有
    lease。默认 False（保守：拿不准就别把 transcript 递下去，见调用点的硬失败注释）。
    """
    return {
        "status": status,
        "tracker_scope_path": None,
        "tracker_lease_id": None,
        "tracker_scope_hash": None,
        "tracker_unit_count": 0,
        "session_manifest_has_owned_paths": session_manifest_has_owned_paths,
        "degradation_reason": reason,
        "allocator_status": allocator_status,
        "error": error,
    }


def _session_manifest_has_owned_paths(manifest: Any) -> bool:
    """与 ``build_review_packet.session_owned_paths`` 逐字同源的「这份 manifest 有归属吗」判据。

    必须同源：``build_review_packet.validate_session_manifest`` 在归属为空时**直接抛**
    （``session manifest has no owned paths; refusing to build empty Session-Owned scope``）。
    发起侧只要把一份空归属的 transcript 递给 prepare，整个 ``start`` 就从「降级但能跑」变成硬失败
    ——本轮补 transcript 透传时实测踩到过这条（user-only transcript ⇒ prepare status=failed）。
    """
    if not isinstance(manifest, Mapping):
        return False
    for key in ("owned_paths", "owned_dirty_paths", "owned_committed_round_paths"):
        value = manifest.get(key)
        if isinstance(value, (list, tuple)) and any(
            isinstance(item, str) and item for item in value
        ):
            return True
    return False


def _session_unit_review_state_counts(
    repo_path: Path, session_id: str, tracker_dir: Any
) -> dict[str, int]:
    """本 session 名下可审单元的 ``review_state`` 分布，用于给 allocator 的 ``empty`` 定成因。

    优先用 allocator 结果里回报的 ``tracker_dir``：它是**这次分配实际用的**那个 DB，比在这里重新
    解析一遍 log root 更不容易出现两侧不一致。best-effort：拿不到就全 0，调用方按保守分支处理。
    """
    from core.session_scope_allocation.reviewable_unit_diff_tracker import (  # noqa: PLC0415
        summarize_session_unit_review_states,
    )

    try:
        return summarize_session_unit_review_states(
            repo=repo_path,
            session_id=session_id,
            tracker_dir_override=(
                Path(tracker_dir) if isinstance(tracker_dir, str) and tracker_dir else None
            ),
        )
    except Exception:  # noqa: BLE001 - 成因判定永不阻断降级路径
        return {
            "available": 0,
            "assigned": 0,
            "reviewed": 0,
            "total": 0,
            "path_only_reviewed": 0,
        }


def allocate_agentic_initiation_tracker_review_scope(
    *,
    repo: str,
    session_id: str | None,
    transcript_path: str | None,
    run_id: str,
    output_scope_path: Path,
    committed_baseline: str | None = None,
    committed_base_ref: str | None = None,
    lease_ttl_seconds: int | None = AGENTIC_INITIATION_TRACKER_SCOPE_LEASE_TTL_SECONDS,
    force_full_rescope: bool = False,
) -> dict[str, Any]:
    """为「agent 就地接受的这一轮」分配 tracker review scope，复用 Stop hook 那条链的既有入口。

    两步，顺序不可换：

    1. ``build_manifest(repo, transcript, committed_baseline=…, committed_base_ref=…)`` ——
       它会**透传调用** ``reviewable_unit_diff_tracker.register_claims``，把 transcript 归属出来的
       owned paths 写成 ``session_units`` 行。这正是 Stop hook 侧 ``refresh_global_diff_tracker``
       做的唯一一件实事。
    2. ``allocate_review_scope(auto_claim_observed=False)`` —— 归属已由第 1 步播种，此处再
       auto-claim 会把范围撑过 transcript 意图（Stop hook 侧逐字同款理由）。

    返回归一化 dict：``{status, tracker_scope_path, tracker_lease_id, tracker_scope_hash,
    tracker_unit_count, session_manifest_has_owned_paths, degradation_reason, allocator_status,
    error}``。**永不抛**：任何异常都收敛成一条降级结果，让 ``start`` 继续走完（发起被一次 tracker
    故障整个打掉，比多审一遍更糟）。

    ``session_manifest_has_owned_paths`` 是给调用方的第二个开关：它决定这份 transcript 该不该
    递给 prepare（详见 ``_session_manifest_has_owned_paths`` —— 空归属会让 packet 构建硬失败）。
    """
    if not (isinstance(session_id, str) and session_id.strip()):
        return _degraded_tracker_scope_allocation(
            AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_NO_SESSION_IDENTITY,
            reason=(
                "本次 start 解析不出会话身份（session_id 为空），tracker lease 按构造无法绑定；"
                "本轮退回无 lease 的 manual-all-uncommitted 口径。"
            ),
        )
    resolved_transcript = (
        Path(transcript_path).expanduser() if isinstance(transcript_path, str) and transcript_path.strip() else None
    )
    if resolved_transcript is None or not resolved_transcript.is_file():
        # 无 transcript 不只是「少一份 manifest」：``prepare_run`` 在 tracker_scope 在场而
        # session-manifest 缺席时会**直接抛** ValueError（tracker_scope 要 splice 进 manifest.tracker）。
        # 故这里必须在分配之前短路，否则一次好意的分配会把整个 start 变成硬失败。
        return _degraded_tracker_scope_allocation(
            AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_NO_READABLE_TRANSCRIPT,
            reason=(
                "拿不到可读的会话 transcript（既无 --transcript-path，决策 token 的候选摘要里也没有）；"
                "没有 transcript 就生成不了 session-manifest，tracker scope 无处 splice。"
                "本轮退回无 lease 的 manual-all-uncommitted 口径。"
            ),
        )

    # 惰性 import：这两个模块（尤其 tracker）很重，早退路径不该为它们付出 import 成本。
    from core.session_scope_allocation.reviewable_unit_diff_tracker import (  # noqa: PLC0415
        allocate_review_scope,
    )
    from core.session_scope_allocation.session_change_manifest import build_manifest  # noqa: PLC0415

    repo_path = Path(repo).expanduser().resolve()
    try:
        manifest = build_manifest(
            repo_path,
            resolved_transcript.resolve(),
            committed_baseline=committed_baseline,
            committed_base_ref=committed_base_ref,
        )
    except Exception as exc:  # noqa: BLE001 - manifest 失败降级，不阻断已接受的一轮
        return _degraded_tracker_scope_allocation(
            AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_SESSION_MANIFEST_FAILED,
            reason=(
                "从 transcript 构建 session-manifest 失败，本轮没有归属播种可用；"
                "退回无 lease 的 manual-all-uncommitted 口径。"
            ),
            error=f"{type(exc).__name__}: {exc}",
        )
    if isinstance(manifest, dict) and manifest.get("schema_drift_suspected"):
        # transcript 有具体编辑证据、工作树也 dirty，归属却为空 = 解析器与 transcript schema 失配。
        # Stop hook 在这里是 loud-skip；本路径已被 agent 接受、不能 skip，故降级成「全量多审」并
        # 显式点名 drift，绝不让它伪装成一次正常的空归属。
        return _degraded_tracker_scope_allocation(
            AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_TRANSCRIPT_SCHEMA_DRIFT,
            reason=(
                "疑似 transcript schema 漂移（有编辑证据但归属为空），归属播种不可信；"
                "退回无 lease 的 manual-all-uncommitted 口径全量多审，请人工核查解析器。"
            ),
        )

    has_owned_paths = _session_manifest_has_owned_paths(manifest)
    if not has_owned_paths:
        # 归属为空（transcript 里没有本轮的编辑证据，例如纯提问会话，或解析器看不懂这份 rollout）。
        # 既拿不到 tracker scope（候选查询按 session_units 走，必然 empty），也**不能**把这份
        # transcript 递给 prepare —— validate_session_manifest 会因空归属直接抛。原样退回历史口径。
        return _degraded_tracker_scope_allocation(
            AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_ALLOCATOR_EMPTY,
            reason=(
                "transcript 归属为空（本会话在这份 transcript 里没有可识别的文件改动证据）；"
                "既分配不出 tracker scope，也不能据此建 session-owned packet，"
                "本轮退回无 lease 的 manual-all-uncommitted 口径全量多审。"
            ),
        )

    patch_ownership = manifest.get("patch_ownership") if isinstance(manifest, dict) else None
    transcript_max_line_number = (
        patch_ownership.get("transcript_max_line_number")
        if isinstance(patch_ownership, dict)
        and isinstance(patch_ownership.get("transcript_max_line_number"), int)
        else None
    )

    try:
        result = allocate_review_scope(
            repo=repo_path,
            session_id=session_id.strip(),
            run_id=run_id,
            reviewer_id=AGENTIC_INITIATION_TRACKER_SCOPE_REVIEWER_ID,
            output_scope_path=output_scope_path,
            holder_kind="reviewer",
            dry_run=False,
            lease_ttl_seconds=lease_ttl_seconds,
            transcript_max_line_number=transcript_max_line_number,
            # 归属已由上面的 build_manifest → register_claims 播种；auto-claim 会把范围撑过
            # transcript 意图（与 Stop hook 侧同款理由，两处必须一致）。
            auto_claim_observed=False,
            committed_baseline=committed_baseline,
            committed_base_ref=committed_base_ref,
        )
    except Exception as exc:  # noqa: BLE001 - allocator 故障降级，不阻断已接受的一轮
        return _degraded_tracker_scope_allocation(
            AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_ALLOCATOR_ERROR,
            reason=(
                "tracker allocator 抛异常，本轮拿不到 lease（但 transcript 归属可用，"
                "packet 仍按 session-owned 口径构建）。"
            ),
            error=f"{type(exc).__name__}: {exc}",
            session_manifest_has_owned_paths=True,
        )

    status = result.get("status")
    if status == "allocated":
        scope_path = result.get("scope_path")
        if not (isinstance(scope_path, str) and scope_path and Path(scope_path).is_file()):
            # 分配成功但 artifact 没落盘：拿着 lease 却没有能喂给 prepare 的 scope 文件是最坏的
            # 中间态（占着 units 又不进 packet）。立刻释放，按降级处理。
            _release_agentic_initiation_tracker_lease(
                repo=str(repo_path),
                lease_id=result.get("lease_id"),
                reason="agentic_initiation_scope_artifact_missing",
            )
            return _degraded_tracker_scope_allocation(
                AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_ALLOCATOR_ERROR,
                reason=(
                    "tracker allocator 报 allocated 但没落下 tracker-scope.json；已释放该 lease，"
                    "本轮无 lease（packet 仍按 session-owned 口径构建）。"
                ),
                allocator_status=status,
                session_manifest_has_owned_paths=True,
            )
        # unit 数只在 ``result["scope"]["unit_ids"]`` 里（allocated 分支的顶层返回**没有**
        # ``unit_ids`` 键）——与 Stop hook 侧 `tracker_scope_allocated` ledger 事件同一读法。
        allocated_scope = result.get("scope")
        allocated_unit_ids = (
            allocated_scope.get("unit_ids") if isinstance(allocated_scope, Mapping) else None
        )
        return {
            "status": AGENTIC_INITIATION_TRACKER_SCOPE_ALLOCATED,
            "tracker_scope_path": scope_path,
            "tracker_lease_id": result.get("lease_id"),
            "tracker_scope_hash": result.get("scope_hash"),
            "tracker_unit_count": len(allocated_unit_ids or []),
            "session_manifest_has_owned_paths": True,
            "degradation_reason": None,
            "allocator_status": status,
            "error": None,
        }
    if status == "empty":
        # 走到这里说明 transcript 归属**非空**（空归属已在上面短路）。``empty`` 有两个成因，后果
        # 完全不同，必须分开处理——历史上它们被压成同一个降级，正是「RVF 紧跟着 RVF」的发动机：
        #
        #   * 被在途 lease 排除（并发：另一条 reviewer 正拿着这批 unit）→ 多审一遍是**安全**的，
        #     保持既有降级。
        #   * 候选单元数为 0（这批 unit 全部 review_state='reviewed'）→ 这就是上一轮 review 的
        #     产物。旧行为在此静默扩成无 lease 的全量重审；而无 lease ⇒ handoff 标不了 reviewed
        #     ⇒ 同一批 diff 在之后每一次 Stop 又被重新提议，闭环自锁。改为**不进 prepare**，
        #     把「没东西可审」如实回给 agent 去表态。
        candidate_unit_count = result.get("candidate_unit_count", 0) or 0
        leased_excluded_count = result.get("leased_excluded_count", 0) or 0
        # ⚠️ 成因判据**不能**看 ``leased_excluded_count``：候选查询只收
        # ``review_state='available'``，被在途 lease 持有的单元是 ``assigned``，在候选阶段就被滤
        # 掉了、走不到 anti-join，所以那个计数在正常流程下恒为 0。必须直接看本 session 名下单元的
        # review_state 分布。（这条是本次修复自己引入、被并发用例逮住的缺陷，别再改回去。）
        review_state_counts = _session_unit_review_state_counts(
            repo_path, session_id.strip(), result.get("tracker_dir")
        )
        # ⚠️ 第二个必要条件：本 session 名下**不能有已审的 ``path_only`` 单元**。
        # ``path_only`` 是 tracker 在「看不出改了什么」时的兜底身份，``_canonical_hash_path_only``
        # 只哈希路径——不含 mode、不含 change 方向。于是它的 ``reviewed`` 只证明「这个路径被审过
        # 一次」，证明不了「当前这次改动被审过」。实测反例：``100644→100755`` 审掉并提交后，相反
        # 方向的 ``100755→100644`` 推导出同一个 unit_id、命中同一行 reviewed，allocator 照样报
        # empty、计数照样是 reviewed=1——本分支于是把一次**真的没审过**的可执行位回退直接跳掉。
        # 旧的 ``degraded_allocator_reported_empty`` 降级（无 lease 全量多审）在这种形状下反而是对的：
        # 多审一遍只是浪费，跳过则是静默漏审。所以这里只对「身份可信」的单元族启用早退。
        #
        # 刻意**不**改 ``_canonical_hash_path_only`` 去把 mode 编进身份：那会 churn 既有 unit_id、
        # 波及全局去重与 path_only↔同路径他 kind 的 cross-match，而且只补上 mode 这一个可观测维度，
        # 「hunk 锚点消失」「无可观测变更」两支照样看不见内容——治不了「身份自认看不见内容」这个根。
        session_has_reviewed_path_only_units = review_state_counts.get("path_only_reviewed", 0) > 0
        all_units_already_reviewed = (
            review_state_counts["assigned"] == 0
            and review_state_counts["available"] == 0
            and review_state_counts["reviewed"] > 0
            and not session_has_reviewed_path_only_units
        )
        if all_units_already_reviewed and not force_full_rescope:
            return _degraded_tracker_scope_allocation(
                AGENTIC_INITIATION_TRACKER_SCOPE_NO_UNREVIEWED_SCOPE,
                reason=(
                    "tracker 报本会话名下**没有任何未审单元**"
                    f"（已审 {review_state_counts['reviewed']} 个、未审 0 个、被在途 lease 持有 0 个）"
                    "——这批改动的可审单元在此前的 review 中已全部审过。本轮不自动扩成全量重审："
                    "无 lease 的一轮审完也标不了 reviewed，只会让同一批 diff 在下一次 Stop 再被提议一遍。"
                ),
                allocator_status=status,
                session_manifest_has_owned_paths=True,
            )
        return _degraded_tracker_scope_allocation(
            AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_ALLOCATOR_EMPTY,
            reason=(
                "tracker 报本会话名下没有未分配的可审单元"
                f"（候选 {candidate_unit_count} 个、anti-join 排除 {leased_excluded_count} 个；"
                f"review_state 分布 available={review_state_counts['available']} "
                f"assigned={review_state_counts['assigned']} reviewed={review_state_counts['reviewed']}）"
                + (
                    f"；其中 {review_state_counts['path_only_reviewed']} 个已审单元是 path_only "
                    "（身份只含路径、不含 mode 与 change 方向），据此判「全已审」不可信"
                    "——例如 chmod 往返会复用同一个 unit_id，故保守按多审一遍处理"
                    if session_has_reviewed_path_only_units
                    else ""
                )
                + ("；已按 --force-full-rescope 显式要求继续" if force_full_rescope else "")
                + "；本轮无 lease，packet 仍按 session-owned 口径构建。"
            ),
            allocator_status=status,
            session_manifest_has_owned_paths=True,
        )
    return _degraded_tracker_scope_allocation(
        AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_ALLOCATOR_UNAVAILABLE,
        reason=(
            f"tracker 不可用（allocator status={status}，如 tracker 被 env 关掉或仓库形态不受支持）；"
            "本轮无 lease，packet 仍按 session-owned 口径构建。"
        ),
        allocator_status=status if isinstance(status, str) else None,
        session_manifest_has_owned_paths=True,
    )


def _release_agentic_initiation_tracker_lease(
    *, repo: str, lease_id: Any, reason: str
) -> dict[str, Any] | None:
    """把本轮刚分配的 lease 还回去（units 从 ``assigned`` 翻回 ``available``），best-effort。

    调用点只有一个语义：**这一轮没能进 review**（prepare 失败 / 超时 / 撞 bootstrap 等待 / 抛异常）。
    不还的话那批 unit 在整个 TTL 里都是 ``assigned``，而候选查询只收 ``available`` ⇒ 下一次
    ``start --retry`` 会开一个**新 run** 再分配一次，必然拿到 ``empty`` 而静默降级——修复反倒制造了
    一个只在重试路径上发作的新盲区。
    """
    if not (isinstance(lease_id, str) and lease_id):
        return None
    try:
        from core.session_scope_allocation.reviewable_unit_diff_tracker import (  # noqa: PLC0415
            lease_release,
        )

        return lease_release(repo=repo, lease_id=lease_id, reason=reason)
    except Exception as exc:  # noqa: BLE001 - 归还失败只记录，不把失败叠加成第二次失败
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _record_tracker_lease_release_on_unfinished_prepare(
    *,
    worktree: str,
    tracker_scope_allocation: dict[str, Any],
    prepare_status: str,
    ledger: Any,
) -> None:
    """prepare 没跑到 ``completed`` → 归还 lease 并就地把三个 tracker 字段抹回 None + 记 ledger。

    抹回 None 是关键的第二半：返回值里的 ``tracker_scope_allocation`` 会被 ``run_start`` 当作
    「本轮口径」报给 agent，若只还 lease 不改这份记录，回执会声称拿到了一条其实已经不存在的
    lease（谎报成功，本仓 EXIT_DECLINE_DISPATCHED_REVIEW_INCOMPLETE 那条注释记的同一族毛病）。
    """
    lease_id = tracker_scope_allocation.get("tracker_lease_id")
    if not (isinstance(lease_id, str) and lease_id):
        return
    release_result = _release_agentic_initiation_tracker_lease(
        repo=worktree,
        lease_id=lease_id,
        reason=f"agentic_initiation_prepare_{prepare_status}",
    )
    tracker_scope_allocation["status"] = AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_ALLOCATOR_ERROR
    tracker_scope_allocation["degradation_reason"] = (
        f"prepare 未跑到 completed（status={prepare_status}），本轮 lease {lease_id} 已归还，"
        "units 翻回 available 供下一次 start --retry 重新分配。"
    )
    tracker_scope_allocation["tracker_lease_id"] = None
    tracker_scope_allocation["tracker_scope_path"] = None
    tracker_scope_allocation["tracker_scope_hash"] = None
    tracker_scope_allocation["tracker_unit_count"] = 0
    try:
        ledger.event(
            phase="gate",
            event="agentic_initiation_tracker_scope_lease_released",
            status="released",
            level="warn",
            reason_code="agentic_initiation_prepare_unfinished",
            repo=worktree,
            cwd=worktree,
            tracker_lease_id=lease_id,
            prepare_status=prepare_status,
            lease_release_result=release_result,
        )
    except Exception:  # noqa: BLE001 - 记账失败不得掩盖本来要报的 prepare 失败
        pass


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
    force_full_rescope: bool = False,
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

    # tracker review scope 分配：必须排在写 prep payload **之前**——三个 tracker 字段是 prep 的
    # 输入，prepare 只是把它们透传给 scope contract。见本文件上方「发起侧 tracker review scope
    # 分配」整节的动机说明。永不抛：拿不到就是一条显式记录在案的降级，start 照常走完。
    tracker_scope_allocation = allocate_agentic_initiation_tracker_review_scope(
        repo=worktree,
        session_id=session_id,
        transcript_path=transcript_path,
        run_id=ledger.run_id,
        output_scope_path=artifacts_dir / "tracker-scope.json",
        committed_baseline=committed_baseline,
        committed_base_ref=committed_base_ref,
        force_full_rescope=force_full_rescope,
    )
    ledger.event(
        phase="gate",
        event="agentic_initiation_tracker_scope_allocation",
        status=(
            "allocated"
            if tracker_scope_allocation["status"] == AGENTIC_INITIATION_TRACKER_SCOPE_ALLOCATED
            else "degraded"
        ),
        # 降级是 warn 级：它不阻断本轮，但它意味着这一轮审完也无法把 units 标成 reviewed。
        level=(
            None
            if tracker_scope_allocation["status"] == AGENTIC_INITIATION_TRACKER_SCOPE_ALLOCATED
            else "warn"
        ),
        reason_code=tracker_scope_allocation["status"],
        repo=worktree,
        cwd=worktree,
        session_id=session_id,
        tracker_lease_id=tracker_scope_allocation["tracker_lease_id"],
        tracker_scope_hash=tracker_scope_allocation["tracker_scope_hash"],
        tracker_scope_path=tracker_scope_allocation["tracker_scope_path"],
        tracker_unit_count=tracker_scope_allocation["tracker_unit_count"],
        allocator_status=tracker_scope_allocation["allocator_status"],
        degradation_reason=tracker_scope_allocation["degradation_reason"],
        session_manifest_has_owned_paths=tracker_scope_allocation[
            "session_manifest_has_owned_paths"
        ],
        error=tracker_scope_allocation["error"],
    )

    # 「本会话名下没有任何未审单元」→ 到此为止，**不进 prepare**。
    # 继续走下去会派出一次审自己上一轮产物的 review：无 lease、审完标不了 reviewed、下一次 Stop
    # 再提议同一批 diff。run 已经开了（allocator 需要 run_id），就让它作为这次「没东西可审」的
    # 审计记录停在这里；token 由 run_start 保持 acknowledged，agent 可 decline 或 --force-full-rescope。
    if tracker_scope_allocation["status"] == AGENTIC_INITIATION_TRACKER_SCOPE_NO_UNREVIEWED_SCOPE:
        ledger.event(
            phase="gate",
            event="agentic_initiation_no_unreviewed_scope",
            status="skipped",
            reason_code=AGENTIC_INITIATION_TRACKER_SCOPE_NO_UNREVIEWED_SCOPE,
            repo=worktree,
            cwd=worktree,
            session_id=session_id,
            degradation_reason=tracker_scope_allocation["degradation_reason"],
        )
        return {
            "status": PREPARE_STATUS_NO_UNREVIEWED_SCOPE,
            "run_id": ledger.run_id,
            "run_dir": str(ledger.run_dir),
            "artifacts": {},
            "error": None,
            "prep_token": None,
            "bootstrap_gate": None,
            "initiation_run_id": ledger.run_id,
            "initiation_run_dir": str(ledger.run_dir),
            "tracker_scope_allocation": tracker_scope_allocation,
        }

    # transcript 只在**归属非空**时才递给 prepare：``build_review_packet.validate_session_manifest``
    # 对空归属的 manifest 直接抛（refusing to build empty Session-Owned scope），递下去会把一次
    # 本可降级跑完的 start 变成硬失败。归属为空时按历史口径走（无 manifest ⇒
    # manual-all-uncommitted，packet 仍全量覆盖工作树），降级理由已由上面的分配结果记在案。
    prepare_transcript_path = (
        transcript_path if tracker_scope_allocation["session_manifest_has_owned_paths"] else None
    )

    # 本路径的来源是 Stop hook 发起决策点 + agent 显式 start，**不是**用户敲的手动入口。
    # 历史上这里硬写 flow-manual，于是 stub 自称「用户显式触发 /review-validate-fix」，与同一份
    # payload 里的 dispatch_origin=agentic_initiation_start 直接矛盾——reviewer 读到假来源。
    target_flow = rvf_dispatch_prompts.AGENTIC_INITIATION_FLOW
    payload: dict[str, Any] = {
        "origin_session_id": session_id,
        "origin_repo": worktree,
        "origin_cwd": worktree,
        "origin_transcript_path": prepare_transcript_path,
        "target_flow": target_flow,
        "target_worktree": worktree,
        "target_kanban_task_id": None,
        "target_session_id": session_id,
        "dispatch_origin": "agentic_initiation_start",
        # 决策点已经算好的候选路径：prepare 拿它当 scope-of-work 骨架，让 agent 逐条填实质
        # reasoning，而不是对着空白页现编（也不必再算一遍 git 状态）。
        "scope_of_work_candidate_paths": _initial_auto_scope_paths(candidate_summary, None),
        "rvf_run": {
            "run_id": ledger.run_id,
            "run_dir": str(ledger.run_dir),
            "artifacts_dir": str(artifacts_dir),
            "scope_contract_path": str(artifacts_dir / "inputs" / "scope.contract.json"),
            "tracker_scope_path": tracker_scope_allocation["tracker_scope_path"],
            "tracker_lease_id": tracker_scope_allocation["tracker_lease_id"],
            "tracker_scope_hash": tracker_scope_allocation["tracker_scope_hash"],
        },
        "handoff_expectations": {
            "handoff_path": str(artifacts_dir / "handoff.md"),
            "expected_artifacts": ["review-result.json", "merge/merge_dispositions.json", "handoff.md"],
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
    try:
        state = prepare_review_run.prepare_run_from_prep_file(
            record,
            timeout_seconds=timeout_seconds,
            transcript_override=(
                Path(prepare_transcript_path).expanduser() if prepare_transcript_path else None
            ),
        )
    except BaseException:
        # prepare 抛穿（含 KeyboardInterrupt）：本轮进不了 review，刚拿的 lease 必须还回去，
        # 否则那批 unit 在整个 TTL 里都是 assigned，下一次 start --retry 只会拿到 empty。
        _record_tracker_lease_release_on_unfinished_prepare(
            worktree=worktree,
            tracker_scope_allocation=tracker_scope_allocation,
            prepare_status="raised",
            ledger=ledger,
        )
        raise
    gate = rvf_bootstrap_gate.gate_of(state)
    if gate is not None and parent_attempt_run_id and not gate.get("parent_attempt_run_id"):
        gate = {
            **gate,
            "parent_attempt_run_id": parent_attempt_run_id,
            "parent_attempt_run_dir": parent_attempt_run_dir,
        }
    if state.get("status") != "completed":
        # 失败 / 超时 / bootstrap 等待都归这一支。**等待态也要还**：等待期间 agent 会去吸收
        # local base，工作树随之改变；留着一份分配于吸收之前的 scope，恢复时审的就是另一批东西
        # （正是 union_with_prior_scope 那条注释记录的「口径被悄悄换掉」事故）。恢复重放时重新
        # 分配，拿到的才是当时工作树的真实口径。
        _record_tracker_lease_release_on_unfinished_prepare(
            worktree=worktree,
            tracker_scope_allocation=tracker_scope_allocation,
            prepare_status=str(state.get("status")),
            ledger=ledger,
        )
    return {
        "status": state.get("status"),
        "run_id": state.get("run_id"),
        "run_dir": state.get("run_dir"),
        "artifacts": state.get("artifacts") or {},
        "error": state.get("error"),
        "prep_token": record.token,
        # prep 文件的落盘路径：``start`` 的 execution_handoff 要把它交给 agent（历史上这条信息
        # 只经祈使句 prompt 的 ``$RVF_PREP_FILE`` 提示传递，现改为从返回值读）。
        "prep_file": str(record.path),
        "bootstrap_gate": gate,
        # bootstrap 等待路径上 prepare_run 在 start_run **之前**就抛了，故 state 里没有
        # run_id/run_dir；发起侧 ledger 的 run 目录才是该 token 唯一可锚的在途工件。没有它，
        # token 过 900s TTL 后 revive_decision_token 找不到锚点 → 一次大 base 吸收必然把整轮
        # 决策熬死（入口 A 的可恢复性硬缺口）。
        "initiation_run_id": ledger.run_id,
        "initiation_run_dir": str(ledger.run_dir),
        # 本轮 scope 口径的实况（allocated / 各种 degraded_*）。``run_start`` 把它同时写进遥测、
        # 回执 detail 与人类可读正文——降级必须有人看得见，不能只躺在 ledger 里。
        "tracker_scope_allocation": tracker_scope_allocation,
    }


def _current_kanban_task_id_from_env() -> str | None:
    """与 ``stop_review_validate_fix.current_kanban_task_id`` 的 env 段**同源**的 task id。

    CLI 没有 Stop event，只剩 env 这一路；变量名与顺序必须与 Stop 侧逐字一致，否则两边会在
    同一台机器上算出不同的 marker key。
    """
    for name in ("KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _consume_review_reopen_marker_before_allocate(
    *, session_id: str | None, repo: str | None
) -> dict[str, Any]:
    """``start`` 侧的失败再入消费（与 Stop hook 旧派发路径共用同一份实现）。绝不抛。"""
    if not (isinstance(repo, str) and repo.strip()):
        return {"outcome": review_reopen_marker.REVIEW_REOPEN_CONSUMPTION_NO_MARKER}
    try:
        return review_reopen_marker.consume_review_reopen_marker_for_run_scope(
            task_id=_current_kanban_task_id_from_env(),
            session_id=session_id,
            repo=repo,
        )
    except Exception as exc:  # noqa: BLE001 - 失败再入记账永不阻断发起
        return {
            "outcome": review_reopen_marker.REVIEW_REOPEN_CONSUMPTION_NO_MARKER,
            "consumption_error": f"{type(exc).__name__}: {exc}",
        }


def build_execution_handoff(
    *,
    repo: str | None,
    run_id: str | None,
    run_dir: str | None,
    prepare_artifacts: Mapping[str, Any] | None,
    prep_file_path: str | None,
    kanban_task_id: str | None,
) -> dict[str, Any]:
    """``start`` 成功回执里的「接下来怎么执行」块（INV-3 的载体）。

    **它取代的是什么**：历史上 kanban task 内的自动 RVF 靠一条自注入祈使句把 ``RVF_RUN_ID`` /
    ``RVF_TARGET_REPO`` / origin 元数据 / handoff 契约一并塞进 agent 的上下文。那条祈使句现在
    退役了（INV-1/INV-2：唯一的自注入是发起决策点横幅），这些执行所需信息改为**在 agent 接受
    的那一刻从本命令的返回值里读**——既不再占一条 user message，也不再依赖一条可能迟到 49
    分钟、可被 rewind 的异步投递。

    ``origin_metadata`` / ``handoff_path`` 由 ``run_dir`` 派生而不是另存一份：run 目录布局
    （``artifacts/origin.json``、``artifacts/handoff.md``）是 prepare 侧的既有约定，这里只把
    落点算给 agent，避免两处各记一份路径而漂移。
    """
    artifacts = dict(prepare_artifacts) if isinstance(prepare_artifacts, Mapping) else {}
    run_dir_path = Path(run_dir).expanduser() if isinstance(run_dir, str) and run_dir else None
    return {
        "target_repo": repo,
        "kanban_task_id": kanban_task_id,
        "run_id": run_id,
        "run_dir": run_dir,
        "prep_file": prep_file_path,
        # prepare 产出的全套输入工件（缺失的键保持 None，不静默丢字段）。
        "review_env": artifacts.get("review_env"),
        "review_packet": artifacts.get("review_packet"),
        "review_packet_metadata": artifacts.get("review_packet_metadata"),
        "review_agent_context": artifacts.get("review_agent_context"),
        "scope_contract": artifacts.get("scope_contract"),
        "scope_of_work": artifacts.get("scope_of_work"),
        "session_manifest": artifacts.get("session_manifest"),
        "worktree_bootstrap": artifacts.get("worktree_bootstrap"),
        # N5/K4：intent artifact 的消费结局工件。agent 照抄它填 handoff `## 状态` 的
        # `intent_artifact_status`，不要自己判断——「标注」必须是机器事实而非 agent 自觉。
        "intent_artifact_status_file": artifacts.get("intent_artifact_status_file"),
        "origin_metadata": str(run_dir_path / "artifacts" / "origin.json") if run_dir_path else None,
        "handoff_path": str(run_dir_path / "artifacts" / "handoff.md") if run_dir_path else None,
        "handoff_contract": (
            "从准备阶段开始创建并持续维护 run artifact `handoff.md`；完成后最终回复第一行输出 "
            "`RVF_HANDOFF_FILE: <handoff.md 绝对路径>`，"
            f"{HANDOFF_FINAL_REPLY_STRUCTURE_INSTRUCTION}，也不要在正文里重复 handoff 文件内容。"
            "Stop hook 把 `RVF_HANDOFF_FILE` marker 当作完成信号：它同时清掉本轮的在途凭证，"
            "run 结束时发送 OS 系统通知。"
        ),
        "execution_instruction": (
            "就地执行：在**当前会话、当前 worktree** 里调用 `$review-validate-fix` skill，按 "
            "SKILL.md 走 review → merge → validate/fix → handoff。不要新建 Kanban task、"
            "不要 fork 新会话；上面这些路径就是本轮 run 的全部输入，不需要重新 prepare。"
        ),
    }


ACCEPTED_REVIEW_RUN_RECEIPT_ARMED = "armed"
ACCEPTED_REVIEW_RUN_RECEIPT_SKIPPED_NO_SESSION_IDENTITY = "skipped_no_session_identity"
ACCEPTED_REVIEW_RUN_RECEIPT_SKIPPED_REPOSITORY_IDENTITY_UNRESOLVED = (
    "skipped_repository_identity_unresolved"
)
ACCEPTED_REVIEW_RUN_RECEIPT_ARM_FAILED = "arm_failed"


def arm_accepted_review_run_in_flight_receipt(
    *,
    decision_token: str,
    session_id: str | None,
    repo: str | None,
    run_id: str | None,
    run_dir: str | None,
    transcript_path: str | None,
    lock_root: Path | None = None,
) -> dict[str, Any]:
    """``start`` 成功之后落一枚「本会话已接受、这一轮 RVF 在途」的凭证（INV-5 的载体）。

    **为什么必须由 CLI 自己落**：历史上这把在途锁只由 UserPromptSubmit 在「follow-up trigger
    投递落地」那一刻 arm。P1 之后默认路径是 agent 在发起决策点横幅上就地 ``start``——根本没有
    UPS 事件，没人 arm。不补这一枚，接受之后的**每一次** Stop 都会把同一批改动重新征询一遍。

    **刻意只写 session 维度的 key**（``task_id=None``）：读侧（Stop hook）的候选顺序是
    ``task-…`` 优先、``sess-…`` 回退，所以 session-only 的凭证无论读侧有没有解析出 kanban
    task id 都命中得到；反过来若这里按 task id 落、而读侧那一刻恰好没有该 env，就会漏读 ⇒
    重复征询。凭证的语义本来就是「**这个会话**接受了这一轮」，session 维度也更贴切。

    整段 best-effort：任何异常都不该让一次已经成功的 ``start`` 变成失败，故只返回诊断。
    """
    if not (isinstance(session_id, str) and session_id.strip()):
        return {"status": ACCEPTED_REVIEW_RUN_RECEIPT_SKIPPED_NO_SESSION_IDENTITY}
    try:
        import kanban_followup_lock  # noqa: PLC0415 - 与锁模块的耦合限制在本函数内

        marker_path = kanban_followup_lock.write_marker(
            task_id=None,
            session_id=session_id.strip(),
            run_id=str(run_id or ""),
            run_dir=str(run_dir or ""),
            repo=repo,
            cwd=repo,
            decision_token=decision_token,
            delivered_session_id=session_id.strip(),
            delivered_transcript_path=(
                transcript_path
                if isinstance(transcript_path, str) and transcript_path.strip()
                else None
            ),
            state=kanban_followup_lock.STATE_IN_PROGRESS,
            root=lock_root,
        )
    except Exception as exc:  # noqa: BLE001 - 凭证记账永不阻断已成功的 start
        return {"status": ACCEPTED_REVIEW_RUN_RECEIPT_ARM_FAILED, "error": f"{type(exc).__name__}: {exc}"}
    if marker_path is None:
        # marker_paths 返回空 = 仓身份解析不出（见 kanban_followup_lock.marker_paths 的 fail-closed）。
        # 方向是安全的：没有凭证 ⇒ 下一次 Stop 至多多征询一次，绝不静默漏审。
        return {"status": ACCEPTED_REVIEW_RUN_RECEIPT_SKIPPED_REPOSITORY_IDENTITY_UNRESOLVED}
    return {"status": ACCEPTED_REVIEW_RUN_RECEIPT_ARMED, "marker_path": str(marker_path)}


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
    lock_root: Path | None = None,
    force_full_rescope: bool = False,
) -> CommandResult:
    """``start`` 子命令：token→acknowledged → 前置校验 + provider-health 门 → prepare 全套 →
    自动 scope 产出 → arm 在途凭证。任一步失败 → record_start_failure 回退保持 acknowledged，
    可 --retry。

    ``force_full_rescope``：显式覆盖「本会话名下没有未审单元就不开审」这道闸，按历史行为把整份
    未提交 diff 当作本轮范围重审一遍。给的是**知情后仍要重审**的出口（例如工作是经 shell heredoc
    写入的、tracker 归属看不见），不是默认路径。"""
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
    # transcript 回退（本轮 scope 口径的另一半）：``--transcript-path`` 是 agent 手打的可选参数，
    # 而 agent 通常并不知道自己的 transcript 落在哪。铸 token 的 Stop hook / UPS 那一刻是**知道**的
    # （event 里就有），故它们把该路径盖进候选摘要，这里读回来当默认值。没有这一步，默认发起路径
    # 上 transcript 永远是 None ⇒ prepare 生成不了 session-manifest ⇒ contract 退化成
    # manual-all-uncommitted，并且 tracker scope 连分配的前提都不具备。
    transcript_path, transcript_path_source = resolve_start_transcript_path(
        explicit_transcript_path=transcript_path, candidate_summary=candidate_summary
    )
    append_decision_telemetry(
        token=token, event="start_acknowledged", worktree=worktree, session_id=tok_session_id,
        now=now_dt, root=roots.telemetry_root,
        detail={
            "reason": reason,
            "retry": retry,
            "start_attempt_count": record.payload.get("start_attempt_count"),
            "transcript_path_source": transcript_path_source,
        },
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
            next_steps=[f"{_rvf()} start --token {token} --retry --reason <诊断后重试>"],
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
        resume_command = f"{_rvf()} start --token {token} --retry --reason <bootstrap 完成后恢复本轮>"
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
        resume_command = f"{_rvf()} start --token {token} --retry --reason <稍后重试，等在途 prepare 收尾>"
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

    # ④b 失败再入（``$rvf-reopen``）：必须在 prepare→allocate **之前**把上一轮实现 run 的
    #    reviewed units 翻回 available，本轮才拿得到「实现 ∪ fix」全量。历史上这一步只在 Stop
    #    hook 的派发路径里做；P1 把默认发起改成 agent 就地 start 之后，不在这里补一次，
    #    ``$rvf-reopen`` 在默认路径上就整个失效了（marker 永不被消费、范围永不重开）。
    #    刻意放在**接受之后**：谢绝一轮不该顺手把用户武装的重开意图消费掉。
    review_reopen_consumption = _consume_review_reopen_marker_before_allocate(
        session_id=tok_session_id, repo=repo
    )
    if review_reopen_consumption.get("outcome") != review_reopen_marker.REVIEW_REOPEN_CONSUMPTION_NO_MARKER:
        append_decision_telemetry(
            token=token, event="start_review_reopen_consumed", worktree=worktree,
            session_id=tok_session_id, now=now_dt, root=roots.telemetry_root,
            detail=review_reopen_consumption,
        )

    # 只在**显式**要求重审时才把这个 kwarg 递下去：``prepare_pipeline`` 是可注入的，测试与
    # bootstrap 夹具里的 stub 都写死了参数列表。默认路径一个字节都不变，就不会因为多一个关键字
    # 参数把一堆既有注入点变成 TypeError。
    forced_rescope_kwargs = {"force_full_rescope": True} if force_full_rescope else {}
    try:
        prepared = prepare_pipeline(
            token_record=record, repo=repo, session_id=tok_session_id,
            transcript_path=transcript_path, now=now_dt, timeout_seconds=prepare_timeout_seconds,
            resume_run_id=resume_run_id, resume_run_dir=resume_run_dir,
            parent_attempt_run_id=parent_attempt_run_id,
            parent_attempt_run_dir=parent_attempt_run_dir,
            **forced_rescope_kwargs,
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

    # 「本会话名下没有未审单元」：不是失败，是「没东西可审」。必须排在下面的泛化 != completed
    # 分支**之前**，否则会被 _fail 当成一次 prepare 失败——那会把一个正常结论渲染成故障，还会诱导
    # agent 去 --retry 一个重试多少次都一样的状态。
    if prepared.get("status") == PREPARE_STATUS_NO_UNREVIEWED_SCOPE:
        _release_replay_claim()
        allocation = prepared.get("tracker_scope_allocation")
        degradation_reason = (
            allocation.get("degradation_reason") if isinstance(allocation, Mapping) else None
        )
        append_decision_telemetry(
            token=token, event="start_no_unreviewed_scope", worktree=worktree,
            session_id=tok_session_id, now=now_dt, root=roots.telemetry_root,
            detail={
                "retry": retry,
                "run_id": prepared.get("run_id"),
                "degradation_reason": degradation_reason,
            },
        )
        return CommandResult(
            command="start",
            status="no_unreviewed_scope",
            exit_code=EXIT_NO_UNREVIEWED_SCOPE,
            token=token,
            token_state=token_store.STATE_ACKNOWLEDGED,
            message=(
                "这批改动的可审单元在此前的 review 中已全部审过（tracker 候选 0 个，且不是被在途 "
                "lease 挡住）。本轮**没有**自动为你扩成全量重审——那样审完也标不了 reviewed，"
                "只会让同一批 diff 在下一次 Stop 再被提议一遍。"
            ),
            next_steps=[
                f'① 通常应当谢绝本轮：{_rvf()} decline --token {token} --reason "上一轮 RVF 产物，无未审单元"',
                f"② 确知有 tracker 看不见的新工作（例如经 shell heredoc 写入的文件）而要重审："
                f"{_rvf()} start --token {token} --retry --force-full-rescope --reason <重审理由>",
            ],
            detail={
                "phase": "tracker_scope_allocation",
                "prepare_status": prepared.get("status"),
                "run_id": prepared.get("run_id"),
                "run_dir": prepared.get("run_dir"),
                "tracker_scope_allocation": (
                    dict(allocation) if isinstance(allocation, Mapping) else None
                ),
            },
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

    # ⑦ 在途凭证（INV-5）：接受之后的后续 Stop 不得把同一批改动重新征询一遍。就地接受没有
    #    UPS 事件，故必须由本命令自己 arm；``--retry`` 走同一路径，整份覆盖即幂等。
    in_flight_receipt = arm_accepted_review_run_in_flight_receipt(
        decision_token=token,
        session_id=tok_session_id,
        repo=repo,
        run_id=run_id if isinstance(run_id, str) else None,
        run_dir=run_dir if isinstance(run_dir, str) else None,
        transcript_path=transcript_path,
        lock_root=lock_root,
    )

    # 本轮 scope 口径实况。降级时 message / next_steps 也会带上，确保「这一轮审完标不了 reviewed」
    # 这件事对 agent 和用户都是**可见**的，而不是只有事后翻 ledger 才看得出来。
    tracker_scope_allocation = prepared.get("tracker_scope_allocation")
    if not isinstance(tracker_scope_allocation, Mapping):
        tracker_scope_allocation = _degraded_tracker_scope_allocation(
            AGENTIC_INITIATION_TRACKER_SCOPE_DEGRADED_ALLOCATOR_UNAVAILABLE,
            reason="prepare 流水线未回报 tracker scope 口径（自定义 prepare_pipeline？）。",
        )
    tracker_scope_degraded = (
        tracker_scope_allocation.get("status") != AGENTIC_INITIATION_TRACKER_SCOPE_ALLOCATED
    )

    append_decision_telemetry(
        token=token, event="start_prepared", worktree=worktree, session_id=tok_session_id,
        now=now_dt, root=roots.telemetry_root,
        detail={
            "run_id": run_id,
            "run_dir": run_dir,
            "auto_scope_path_count": len(auto_scope_paths),
            "in_flight_receipt": in_flight_receipt,
            "transcript_path_source": transcript_path_source,
            "tracker_scope_allocation": dict(tracker_scope_allocation),
        },
    )

    execution_handoff = build_execution_handoff(
        repo=repo,
        run_id=run_id if isinstance(run_id, str) else None,
        run_dir=run_dir if isinstance(run_dir, str) else None,
        prepare_artifacts=artifacts,
        prep_file_path=(
            prepared.get("prep_file") if isinstance(prepared.get("prep_file"), str) else None
        ),
        kanban_task_id=_current_kanban_task_id_from_env(),
    )

    scope_mode_note = (
        f"审查准备就绪（自动 scope {len(auto_scope_paths)} 路径；tracker scope 已分配，"
        f"lease={tracker_scope_allocation.get('tracker_lease_id')}、"
        f"{tracker_scope_allocation.get('tracker_unit_count')} 个可审单元）。"
        if not tracker_scope_degraded
        else (
            f"审查准备就绪（自动 scope {len(auto_scope_paths)} 路径）。"
            f"⚠ 本轮 scope 口径已降级（{tracker_scope_allocation.get('status')}）："
            f"{tracker_scope_allocation.get('degradation_reason')}"
            " 后果：本轮审完**无法**把这批单元标成 reviewed，同一批改动可能在下一次 Stop 再被提议一次；"
            "且范围可能含其他会话的背景改动，改文件前请以 scope contract 的 fix_allowlist 为准。"
        )
    )
    return CommandResult(
        command="start", status="ok", exit_code=EXIT_OK, token=token,
        token_state=token_store.STATE_ACKNOWLEDGED,
        message=scope_mode_note,
        next_steps=_start_success_ordered_next_steps(
            token=token,
            scope_of_work_path=artifacts.get("scope_of_work"),
            candidate_summary=candidate_summary if isinstance(candidate_summary, Mapping) else None,
            run_dir=run_dir,
        ),
        detail={
            "run_id": run_id, "run_dir": run_dir, "scope_artifact": str(scope_artifact_path),
            "auto_scope_paths": auto_scope_paths, "prepare_artifacts": dict(artifacts),
            # INV-3：执行所需的一切 per-run 信息由本块承载，不再经任何注入正文携带。
            "execution_handoff": execution_handoff,
            "in_flight_receipt": in_flight_receipt,
            "transcript_path": transcript_path,
            "transcript_path_source": transcript_path_source,
            "tracker_scope_allocation": dict(tracker_scope_allocation),
        },
    )


def _suggested_strictness_tier_from_candidate_summary(candidate_summary: Mapping[str, Any] | None) -> str:
    summary = candidate_summary or {}
    for key in ("strictness_reference_unreviewed_estimate", "strictness_reference"):
        reference = summary.get(key)
        if isinstance(reference, Mapping):
            tier = reference.get("suggested_strictness_tier")
            if isinstance(tier, str) and tier in STRICTNESS_TIERS:
                return tier
    return "<light|standard|strict>"


def _host_specific_reviewer_wait_step(run_dir: Any) -> str:
    import rvf_command_line_shortcut_entrypoint  # noqa: PLC0415 - 只为共用同一份 host 分流文案

    return rvf_command_line_shortcut_entrypoint.render_host_specific_reviewer_wait_instruction(
        _rvf(), run_dir=run_dir or None
    )


def _explicit_run_dir_argument_when_current_session_unresolvable(run_dir: Any) -> str:
    """会话 id 解析不出（Kimi 等）时，回执里的 ``rvf`` 执行段命令直接带 ``--run-dir``（否则首跑必 exit 2）。"""
    import rvf_command_line_shortcut_entrypoint  # noqa: PLC0415

    return rvf_command_line_shortcut_entrypoint.render_explicit_run_dir_argument_when_current_session_unresolvable(
        run_dir or None
    )


def _start_success_ordered_next_steps(
    *, token: str, scope_of_work_path: Any, candidate_summary: Mapping[str, Any] | None, run_dir: Any
) -> list[str]:
    """``start`` 成功后按顺序排好、参数已填好的下一步——agent 照抄即可，不必翻 SKILL.md 拼命令。"""
    rvf = _rvf()
    return [
        f"① 用真实 reasoning 覆盖 scope-of-work `{scope_of_work_path}`（本轮改了什么、为什么、审时盯哪里；"
        f"删掉顶部单独成行的 `{rvf_dispatch_prompts.SCOPE_OF_WORK_STUB_MARKER}` 行），"
        f"然后 `{rvf} scope rerender-scope-of-work --token {token}`。",
        f"②（可选）`{rvf} scope exclude|include --token {token} <path>… --reason \"<理由>\"` 微调自动 scope。",
        f"③ `{rvf} dispatch --token {token} --strictness "
        f"{_suggested_strictness_tier_from_candidate_summary(candidate_summary)}`。",
        f"④ 等 reviewer：{_host_specific_reviewer_wait_step(run_dir)}",
        "⑤ 之后在**当前会话**按 `$review-validate-fix` SKILL.md 就地走 merge → validate/fix → handoff"
        "（执行所需路径全在 execution_handoff，不需要重新 prepare）。",
    ]


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
            f"未找到 token {token} 的自动 scope 产出（先跑 {_rvf()} start）。",
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
        next_steps=[f"{_rvf()} dispatch --token {token} --strictness <…>"],
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
        next_steps=[f"{_rvf()} dispatch --token {token} --strictness <…>"],
        detail={
            "operation_type": operation_type,
            "newly_added_paths": newly_added,
            "effective_primary_files": compute_effective_primary_files(overlay),
            "scope_issue": latest_issue,
        },
    )


def run_scope_rerender_scope_of_work(
    *,
    token: str,
    roots: AgentDecisionRoots | None = None,
    now: datetime | None = None,
) -> CommandResult:
    """``scope rerender-scope-of-work``：把 agent 改好的 scope-of-work 重渲进已冻结的 review packet。

    存在的理由：``start`` 在 agent 还没写实质内容时就把 stub **内联冻结**进了 packet，而
    ``prepare_run_from_prep_file`` 在 completed 态幂等直接返回——不提供这条显式入口，agent 就
    只剩「手改冻结的 packet」这一条歪路（2026-08-15 run 实测走的正是那条）。
    """
    roots = roots or AgentDecisionRoots()
    now_dt = _coerce_now(now)
    try:
        record, overlay = _load_overlay_for_scope(token, roots=roots, now=now_dt)
    except TokenLoadError as exc:
        return _token_error_result("scope rerender-scope-of-work", token, exc)

    artifacts = overlay.get("prepare_artifacts") if isinstance(overlay.get("prepare_artifacts"), Mapping) else {}
    artifacts_dir_raw = artifacts.get("artifacts_dir")
    if not (isinstance(artifacts_dir_raw, str) and artifacts_dir_raw.strip()):
        # 老 run（本能力落地前铸的）没有这个字段：从 review_packet 的父目录反推。
        packet_raw = artifacts.get("review_packet")
        artifacts_dir_raw = str(Path(packet_raw).parent) if isinstance(packet_raw, str) and packet_raw else ""
    if not artifacts_dir_raw:
        return CommandResult(
            command="scope rerender-scope-of-work", status="error", exit_code=EXIT_TOKEN_STATE,
            token=token, token_state=record.state,
            message="本轮 overlay 里没有 prepare 工件目录，无法重渲（run 可能还没 prepare 成功）。",
        )

    import prepare_review_run  # noqa: PLC0415 - 重量级，惰性 import

    try:
        result = prepare_review_run.rerender_scope_of_work_into_review_packet(Path(artifacts_dir_raw))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        return CommandResult(
            command="scope rerender-scope-of-work", status="error", exit_code=EXIT_DISPATCH_FAILED,
            token=token, token_state=record.state,
            message=f"scope-of-work 重渲失败：{exc}",
            detail={"artifacts_dir": artifacts_dir_raw},
        )

    still_stub = _scope_of_work_stub_gate(artifacts)
    append_decision_telemetry(
        token=token, event="scope_of_work_rerendered",
        worktree=str(overlay.get("worktree") or ""),
        session_id=overlay.get("session_id"), now=now_dt, root=roots.telemetry_root,
        detail={"round": result.get("round"), "still_stub": bool(still_stub)},
    )
    overlay["scope_of_work_rerender"] = result
    overlay["updated_at"] = _format_timestamp(now_dt)
    write_scope_adjustment(overlay, token=token, root=roots.scope_adjustment_root)

    message = (
        f"scope-of-work 已重渲进 review packet（第 {result.get('round')} 轮，"
        f"旧工件归档在 {result.get('archive_dir')}）。"
    )
    next_steps = [f"{_rvf()} dispatch --token {token} --strictness <…>"]
    if still_stub:
        message += " ⚠️ 但 dispatch 的 contract gate 仍会拒派：" + _scope_of_work_gate_message(still_stub)
        next_steps = [
            *_scope_of_work_gate_next_steps(still_stub, token=token),
            f"处理完再重渲一次，然后 {_rvf()} dispatch --token {token} --strictness <…>",
        ]
    return CommandResult(
        command="scope rerender-scope-of-work",
        status="still_stub" if still_stub else "ok",
        exit_code=EXIT_SCOPE_OF_WORK_STILL_STUB if still_stub else EXIT_OK,
        token=token, token_state=record.state,
        message=message, next_steps=next_steps,
        detail={"rerender": result, "still_stub": still_stub},
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


# ``reviewer_dispatcher`` 返回的 ``status``。刻意与「派发进程 returncode」解耦：
#   * ``ok``                          —— 派发已（同步 dry-run / 异步 detached）就绪；
#   * ``blocked_on_reviewer_request`` —— 有腿返回 ``kind: request``，**待处理终态**，
#                                        既非故障也非完成，不得当失败重跑（见 SKILL.md）；
#   * ``failed``                      —— 真故障。
DISPATCH_OUTCOME_OK = "ok"
DISPATCH_OUTCOME_BLOCKED_ON_REVIEWER_REQUEST = "blocked_on_reviewer_request"
DISPATCH_OUTCOME_FAILED = "failed"

# ``blocked_on_reviewer_request`` 终态下**唯一正确**的重派形态（单一事实源：next_steps 与
# SKILL.md / references/review-merge-policy.md 都必须与它一致，见
# tests/test_reviewer_result_artifact_lifecycle.py 的 LIFE-6 契约测试）。
#
# 为什么不是「带同一 --selection-seed 整盘重跑」：那条指引在真实入口上等于没修 bug。整盘重派
# 会把**健康且已完成**的那条腿也重跑一遍，新一轮结果原地覆写掉旧的（实测一条 cursor 腿跑了三轮
# 2/4/3 条 issue，只留最后一轮）。``--redispatch-review-request-legs-only`` 直接复用上一轮
# reviewer-plan.json，只重跑停在 ``kind: request`` 的腿。
#
# 为什么该模式下**不需要** ``--selection-seed``：seed 的用途是让 ``route()`` 复现同一对
# reviewer；重派模式根本不调 ``route()``（plan 与 reviewer_id 直接复用），传了也不生效。
# ``--selection-seed`` 只在整盘重派时才有意义。
#
# 为什么保持**显式 flag** 而不是让它在该终态下隐式生效：``dispatch_reviewers.py`` 同一入口也
# 服务全新一轮派发，且该 flag 在没有上一轮 plan 时是 fail-closed 抛错的；隐式翻转会让「开新一轮」
# 与「续上一轮」两种意图无法区分。
REVIEW_REQUEST_REDISPATCH_FLAG = "--redispatch-review-request-legs-only"

# detached 派发 launch 的两行 stdout 契约（``dispatch_reviewers.launch_detached_dispatch``）。
DETACHED_DISPATCH_STATUS_PATH_STDOUT_PREFIX = "RVF_DISPATCH_STATUS="
DETACHED_DISPATCH_LAUNCH_RESULT_STDOUT_PREFIX = "RVF_DISPATCH_LAUNCH="


def _parse_detached_dispatch_launch_output(stdout: str) -> dict[str, Any]:
    """按**前缀**解析 detached launch 的两行回执，而不是「取 stdout 最后一行」。

    取最后一行在新输出格式下不可靠：``--execute --detached`` 打印的是
    ``RVF_DISPATCH_STATUS=<path>`` 与 ``RVF_DISPATCH_LAUNCH=<launched|already_running|
    launch_failed>`` 两行，最后一行是后者，把它当 plan 路径会写出一个不存在的路径。
    """
    status_path: str | None = None
    launch_status: str | None = None
    for line in stdout.splitlines():
        text = line.strip()
        if text.startswith(DETACHED_DISPATCH_STATUS_PATH_STDOUT_PREFIX):
            status_path = text[len(DETACHED_DISPATCH_STATUS_PATH_STDOUT_PREFIX):].strip() or None
        elif text.startswith(DETACHED_DISPATCH_LAUNCH_RESULT_STDOUT_PREFIX):
            launch_status = text[len(DETACHED_DISPATCH_LAUNCH_RESULT_STDOUT_PREFIX):].strip() or None
    return {"dispatch_status_path": status_path, "launch_status": launch_status}


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
    """默认 reviewer 派发：以子进程调用既有 ``dispatch_reviewers.py``。

    ``dry_run=True`` 走 ``--dry-run``（同步打印 plan、不真拉起 reviewer）；否则走
    ``--execute --detached``。preset 的 ``external_reviewer_count_target`` 作为
    ``--require-external`` 的意图透传（≥2 时要求真有 external）。

    **为什么必须 ``--detached``**：``dispatch_reviewers.py`` 的模块头写明 detached 的存在理由
    就是「整轮派发不受前台 Bash 工具 600s 上限约束」——一次彻底 review 常常 > 10 分钟。历史实现
    用不带 ``--detached`` 的 ``--execute`` + 无超时 ``subprocess.run`` 同步等整轮跑完；P1 之后
    本函数是**所有**发起的必经之路，那条路在 Claude 里必被砍断。改为立即返回 status.json 路径，
    由 agent 按 SKILL.md「Review」节用 ``rvf wait`` 等终态（等法按 host 分流，不轮询）。

    **``returncode`` 不是判据**：它只说派发进程怎么退出。判「本轮 double-review 是否成立」一律
    看 ``plan_status``（见 SKILL.md「Review」节）。特别地退出码 ``3`` =
    ``blocked_on_reviewer_request``，是**待处理终态**，既非故障也非完成 —— 历史实现把它归到
    ``failed``，agent 多半会盲目重跑 dispatch，而正确处置是先走 Contract Gate 处理那条
    ``kind: request``、再用 ``--redispatch-review-request-legs-only`` 只重派那条腿
    （见 ``REVIEW_REQUEST_REDISPATCH_FLAG``）。
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
    argv += ["--dry-run"] if dry_run else ["--execute", "--detached"]
    if int(preset.get("external_reviewer_count_target") or 0) >= 2:
        argv += ["--require-external"]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    except (OSError, ValueError) as exc:
        return {"status": DISPATCH_OUTCOME_FAILED, "error": f"{type(exc).__name__}: {exc}", "argv": argv}
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    result: dict[str, Any] = {
        "returncode": completed.returncode,
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
        "argv": argv,
    }
    if dry_run:
        # dry-run 是同步的：stdout 最后一行就是 plan 路径（与既有行为一致）。
        result["status"] = (
            DISPATCH_OUTCOME_OK if completed.returncode == 0 else DISPATCH_OUTCOME_FAILED
        )
        result["plan_path"] = stdout.strip().splitlines()[-1] if stdout.strip() else None
        return result

    import dispatch_reviewers  # noqa: PLC0415 - 重量级模块，只为读退出码常量，惰性 import

    result.update(_parse_detached_dispatch_launch_output(stdout))
    if completed.returncode == dispatch_reviewers.DISPATCH_BLOCKED_ON_REVIEWER_REQUEST_EXIT_CODE:
        result["status"] = DISPATCH_OUTCOME_BLOCKED_ON_REVIEWER_REQUEST
    elif completed.returncode == 0:
        result["status"] = DISPATCH_OUTCOME_OK
    else:
        result["status"] = DISPATCH_OUTCOME_FAILED
    return result


REVIEW_PACKET_SESSION_CONTEXT_HEADING = "## Session Context"

SCOPE_OF_WORK_GATE_REASON_STILL_STUB = "scope_of_work_still_stub"
SCOPE_OF_WORK_GATE_REASON_UNREADABLE = "scope_of_work_unreadable"
SCOPE_OF_WORK_GATE_REASON_EMPTY = "scope_of_work_empty"
SCOPE_OF_WORK_GATE_REASON_PACKET_UNREADABLE = "review_packet_unreadable"
SCOPE_OF_WORK_GATE_REASON_PACKET_MISSING_SESSION_CONTEXT = "review_packet_missing_session_context"
SCOPE_OF_WORK_GATE_REASON_PACKET_STALE = "review_packet_session_context_is_stale"


def _packet_inlined_session_context_matches(packet_text: str, scope_of_work_body: str) -> bool:
    """冻结 packet 的 ``## Session Context`` 是否**正好**是这份 scope-of-work 正文。

    锚定比对而不是全文 substring：packet 里还内联着整个 diff 与全部 untracked 文件，本仓自审时
    那些内容本身就含 stub 标记字面量，全文匹配会当场假红/假绿。
    """
    heading = REVIEW_PACKET_SESSION_CONTEXT_HEADING
    for anchor in (f"\n{heading}\n\n", f"{heading}\n\n"):
        index = packet_text.find(anchor)
        if index < 0:
            continue
        return packet_text[index + len(anchor) :].startswith(scope_of_work_body)
    return False


def _scope_of_work_stub_gate(artifacts: Mapping[str, Any]) -> dict[str, Any] | None:
    """本轮 scope-of-work 还不能拿去派 reviewer → 返回 gate detail；可以派 → ``None``。

    判据是 **reviewer 真正会读到的那份正文**，即冻结进 review packet 的 ``## Session Context``，
    而不是可变的盘上文件。只看盘上文件拦不住这条 gate 本来要拦的失败模式：agent 覆盖了盘上的
    scope-of-work、删掉了 stub 标记，但**没跑 rerender** 就 dispatch —— gate 放行，reviewer 读到的
    packet 里冻的仍是 stub，整轮空审（2026-08-15 run 实测浪费约 13 分钟）。

    「路径已声明但读不到」与「压根没声明路径」分开处置：

    * 没声明 → 放行。没有 scope-of-work 的合法路径（manual-all-uncommitted）本来就该派得出去，
      这条 gate 不替 prepare 兜底「工件缺失」。
    * 声明了却读不到 / 内容为空 → **拒派**。声明过就意味着这一轮的契约里有这份正文；此时读不到
      只可能是它被删了或坏了，而放行等于把「删掉文件」变成绕过 gate 的最省事办法。
    """
    raw_path = artifacts.get("scope_of_work")
    if not (isinstance(raw_path, str) and raw_path.strip()):
        return None
    path = Path(raw_path).expanduser()
    detail: dict[str, Any] = {
        "scope_of_work": str(path),
        "stub_marker": rvf_dispatch_prompts.SCOPE_OF_WORK_STUB_MARKER,
    }
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {**detail, "reason": SCOPE_OF_WORK_GATE_REASON_UNREADABLE, "error": f"{type(exc).__name__}: {exc}"}
    detail["scope_of_work_bytes"] = len(text.encode("utf-8"))
    body = text.strip()
    if not body:
        return {**detail, "reason": SCOPE_OF_WORK_GATE_REASON_EMPTY}
    if rvf_dispatch_prompts.scope_of_work_is_untouched_stub(text):
        return {**detail, "reason": SCOPE_OF_WORK_GATE_REASON_STILL_STUB}

    raw_packet = artifacts.get("review_packet")
    if not (isinstance(raw_packet, str) and raw_packet.strip()):
        # 本轮没声明 packet（本能力落地前铸的老 run）：无从核验冻结正文，不在这里假装能核验。
        return None
    packet_path = Path(raw_packet).expanduser()
    detail["review_packet"] = str(packet_path)
    try:
        packet_text = packet_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {**detail, "reason": SCOPE_OF_WORK_GATE_REASON_PACKET_UNREADABLE, "error": f"{type(exc).__name__}: {exc}"}
    if REVIEW_PACKET_SESSION_CONTEXT_HEADING not in packet_text:
        return {**detail, "reason": SCOPE_OF_WORK_GATE_REASON_PACKET_MISSING_SESSION_CONTEXT}
    if not _packet_inlined_session_context_matches(packet_text, body):
        return {**detail, "reason": SCOPE_OF_WORK_GATE_REASON_PACKET_STALE}
    return None


_SCOPE_OF_WORK_GATE_WHY_IT_MATTERS = (
    "派了也白派：reviewer 的 contract gate 会当场把这一轮退回成 `kind: request`，"
    "而 request 在同一 run 内转不回 issues。"
)


def _scope_of_work_gate_message(detail: Mapping[str, Any]) -> str:
    """把 gate 的 reason 翻成「哪一步没做」，而不是一律报「仍是 stub」。"""
    reason = detail.get("reason")
    scope_of_work = detail.get("scope_of_work")
    if reason == SCOPE_OF_WORK_GATE_REASON_UNREADABLE:
        head = (
            f"本轮契约声明了 scope-of-work `{scope_of_work}`，但它读不到"
            f"（{detail.get('error')}），拒绝派发。"
        )
    elif reason == SCOPE_OF_WORK_GATE_REASON_EMPTY:
        head = f"scope-of-work `{scope_of_work}` 是空的，拒绝派发。"
    elif reason == SCOPE_OF_WORK_GATE_REASON_PACKET_UNREADABLE:
        head = (
            f"冻结的 review packet `{detail.get('review_packet')}` 读不到"
            f"（{detail.get('error')}），无法确认 reviewer 会读到哪份 scope-of-work，拒绝派发。"
        )
    elif reason == SCOPE_OF_WORK_GATE_REASON_PACKET_MISSING_SESSION_CONTEXT:
        head = (
            f"冻结的 review packet 里没有 `{REVIEW_PACKET_SESSION_CONTEXT_HEADING}` 段，"
            "reviewer 拿不到本轮 scope-of-work，拒绝派发。"
        )
    elif reason == SCOPE_OF_WORK_GATE_REASON_PACKET_STALE:
        head = (
            "盘上的 scope-of-work 已经写好了，但 review packet 里冻的仍是**旧正文**"
            "（多半是 stub —— packet 在 start 时就内联冻结了它，改盘上的文件不会进 packet），拒绝派发。"
        )
    else:
        head = "scope-of-work 仍是 hook 生成的 stub（未被真实 reasoning 内容覆盖），拒绝派发。"
    return head + _SCOPE_OF_WORK_GATE_WHY_IT_MATTERS


def _scope_of_work_gate_next_steps(detail: Mapping[str, Any], *, token: str) -> list[str]:
    """每个 reason 给出**它自己**的下一步；rerender 这一步几乎每条路径都要走，所以统一附在末尾。"""
    reason = detail.get("reason")
    scope_of_work = detail.get("scope_of_work")
    rerender_step = (
        f"跑 `{_rvf()} scope rerender-scope-of-work --token {token}` 把新正文重渲进 "
        "review packet——packet 在 start 时就冻结了旧正文，只改盘上的文件不会进 packet。"
    )
    if reason == SCOPE_OF_WORK_GATE_REASON_UNREADABLE:
        first = (
            f"先把 `{scope_of_work}` 恢复出来（它是本轮契约声明过的冻结输入，不能靠删掉它来放行），"
            "再写清本轮改了什么、为什么、审的时候该盯哪里。"
        )
    elif reason == SCOPE_OF_WORK_GATE_REASON_EMPTY:
        first = f"往 `{scope_of_work}` 里写清本轮改了什么、为什么、审的时候该盯哪里。"
    elif reason == SCOPE_OF_WORK_GATE_REASON_PACKET_UNREADABLE:
        first = (
            f"先恢复 `{detail.get('review_packet')}`（本轮的冻结 reviewer 输入）；"
            "确实不可恢复时重跑 prepare 而不是绕过这条 gate。"
        )
    elif reason in (
        SCOPE_OF_WORK_GATE_REASON_PACKET_MISSING_SESSION_CONTEXT,
        SCOPE_OF_WORK_GATE_REASON_PACKET_STALE,
    ):
        first = None  # 正文已经写好了，缺的就是重渲这一步。
    else:
        first = (
            f"编辑 `{scope_of_work}`：写清本轮改了什么、为什么、审的时候该盯哪里，"
            f"并删掉顶部单独成行的 `{rvf_dispatch_prompts.SCOPE_OF_WORK_STUB_MARKER}` 标记行。"
        )
    steps = [step for step in (first, rerender_step) if step]
    steps.append(f"然后重跑 `{_rvf()} dispatch --token {token} --strictness <…>`。")
    return steps


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

    # ①a contract gate：reviewer 会读到的那份 scope-of-work 还不能审就**拒派**。
    stub_gate = _scope_of_work_stub_gate(artifacts)
    if stub_gate is not None:
        append_decision_telemetry(
            token=token, event="dispatch_blocked_on_scope_of_work_stub", worktree=worktree,
            session_id=overlay.get("session_id"), now=now_dt, root=roots.telemetry_root,
            detail=stub_gate,
        )
        return CommandResult(
            command="dispatch",
            status="scope_of_work_still_stub",
            exit_code=EXIT_SCOPE_OF_WORK_STILL_STUB,
            token=token,
            token_state=record.state,
            message=_scope_of_work_gate_message(stub_gate),
            next_steps=_scope_of_work_gate_next_steps(stub_gate, token=token),
            detail=stub_gate,
        )
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
    # ②a ``blocked_on_reviewer_request`` 是**待处理终态**，既不是故障也不是完成态 review。
    #    绝不能报成 dispatch_failed —— 那会把 agent 引向「修一修再重跑 dispatch」，而正确处置
    #    是先按 references/review-merge-policy.md 的 Contract Gate 满足/驳回那条 ``kind:
    #    request``，再用 ``REVIEW_REQUEST_REDISPATCH_FLAG`` 经同一入口只重派那条 request 腿
    #    （整盘重派会把健康腿的结果一并覆写掉）。
    if dispatch_result.get("status") == DISPATCH_OUTCOME_BLOCKED_ON_REVIEWER_REQUEST:
        append_decision_telemetry(
            token=token, event="dispatch_blocked_on_reviewer_request", worktree=worktree,
            session_id=overlay.get("session_id"), now=now_dt, root=roots.telemetry_root,
            detail={"strictness": strictness, "dispatch_result": dispatch_result},
        )
        return CommandResult(
            command="dispatch",
            status=DISPATCH_OUTCOME_BLOCKED_ON_REVIEWER_REQUEST,
            exit_code=EXIT_DISPATCH_BLOCKED_ON_REVIEWER_REQUEST,
            token=token,
            token_state=record.state,
            message=(
                "有 reviewer 返回 `kind: request`（待处理终态，不是故障、也不是完成态 review）。"
                "先按 references/review-merge-policy.md 的 Contract Gate 满足或驳回该 request，"
                "再对**同一** run 重派；request 不得进入 merge table。"
            ),
            next_steps=[
                f"`{_rvf()} results{_explicit_run_dir_argument_when_current_session_unresolvable(overlay.get('run_dir'))}`"
                " 打印各腿摘要（含 request 正文），处置那条 request。",
                f"处置完对**同一 run** 跑 `{_rvf()} redispatch-reviewers --review-request-legs-only"
                f"{_explicit_run_dir_argument_when_current_session_unresolvable(overlay.get('run_dir'))}`"
                f"（= dispatch_reviewers `--execute --detached {REVIEW_REQUEST_REDISPATCH_FLAG}`，自动带当前"
                " run 目录）：它复用上一轮 plan、只重派停在 `kind: request` 的腿，健康且已完成的腿保留结果、"
                "不重跑也不被覆写。该模式不调用 route()，因此**不需要** `--selection-seed`"
                "（reviewer 身份直接来自上一轮 plan）。",
            ],
            detail={"dispatch_result": dispatch_result, "strictness": strictness},
        )

    if dispatch_result.get("status") not in (DISPATCH_OUTCOME_OK, "completed"):
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
    dispatch_status_path = dispatch_result.get("dispatch_status_path")
    next_steps = (
        [
            # detached：整轮派发跑在 tmux 线程里、不受前台 Bash 600s 上限约束，故这里只拿到
            # status.json 的落点；等待方式按 host 分流（见 rvf_command_line_shortcut_entrypoint）。
            f"等 reviewer 终态：{_host_specific_reviewer_wait_step(overlay.get('run_dir'))}",
            "`wait` 到 done 时直接打印合并摘要（每腿 kind / issues / audit_summary / requests，"
            "失败腿给 stderr 路径）与判定——**只看 `plan_status`，不看 `returncode`**；"
            "不要再逐腿跑 check_review_result、也不要再去读 reviewer 目录。",
            f"成立后：按摘要 merge → `{_rvf()} fix prepare <issue.json>…"
            f"{_explicit_run_dir_argument_when_current_session_unresolvable(overlay.get('run_dir'))}` → 子代理 → "
            f"`{_rvf()} apply --all-stopped"
            f"{_explicit_run_dir_argument_when_current_session_unresolvable(overlay.get('run_dir'))}` → handoff。",
        ]
        if dispatch_status_path
        else ["等待 reviewer 结果 → validate → fix → handoff（不变）。"]
    )
    return CommandResult(
        command="dispatch", status="ok", exit_code=EXIT_OK, token=token,
        token_state=dispatched.state,
        message=f"已按 {strictness} 档派发 reviewer（primary_files {len(effective_primary_files)}，"
        f"external target {preset.get('external_reviewer_count_target')}）。审查将就地进行。",
        next_steps=next_steps,
        detail={
            "strictness": strictness, "preset": dict(preset),
            "effective_primary_files": effective_primary_files,
            "scope_finalize": finalize,
            "dispatch_plan_path": dispatch_result.get("plan_path"),
            "dispatch_status_path": dispatch_status_path,
            "dispatch_launch_status": dispatch_result.get("launch_status"),
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
    """``decline`` 子命令：mark_declined（终态 + 指纹入冷却表 + 会话级 snooze）+ 理由落 append-only ledger。

    回执如实报告会话级 snooze 是否落盘（读回磁盘，而非假设 mark_declined 一定写成）：落盘了就告诉
    agent「本会话内这片工作范围不会再弹横幅，直到提交 / 范围明显扩大」；没落盘（无
    session_id、候选摘要缺工作范围身份、I/O 失败）就只承诺内容指纹冷却。

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
    declined_session_scope_snooze_recorded = (
        token_store.read_declined_session_scope_snooze(
            worktree=str(declined.payload.get("worktree") or ""),
            session_id=str(declined.payload.get("session_id") or ""),
            cooldown_root=roots.cooldown_root,
        )
        is not None
    )
    message = "已谢绝本次审查发起；理由已持久化（rewind 不可抹），候选指纹已入 declined 冷却表（指纹未变不再注入）"
    message += (
        "；本会话已 snooze 这片工作范围——直到提交或范围明显扩大（开发者发来新 prompt 后以那一刻为新基线），自动 Stop 不再为它弹横幅。"
        if declined_session_scope_snooze_recorded
        else "；会话级 snooze 未落盘（缺会话身份或工作范围快照），之后内容一变仍可能再次征询。"
    )
    return CommandResult(
        command="decline", status="ok", exit_code=EXIT_OK, token=token, token_state=declined.state,
        message=message,
        detail={
            "declined_reason_ledger": str(ledger_path),
            "candidate_fingerprint": fingerprint,
            "declined_session_scope_snooze_recorded": declined_session_scope_snooze_recorded,
        },
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
    repo: str,
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
                task_id=dimension_task_id,
                session_id=dimension_session_id,
                repo=repo or None,
                root=lock_root,
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
                    task_id=dimension_task_id,
                    session_id=dimension_session_id,
                    repo=repo or None,
                    root=lock_root,
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
        repo=repo,
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
        prog="rvf",
        description="RVF 主 agent 发起审查的单一入口（S3）：start / scope / dispatch / decline。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="token→acknowledged + 前置校验 + prepare 全套 + 自动 scope 产出")
    p_start.add_argument("--token", required=True, help="注入 prompt 里拿到的决策 token（显式回传）")
    p_start.add_argument("--reason", required=True, help="简析后开启审查的理由")
    p_start.add_argument("--repo", default=None, help="目标 worktree（默认取 token 记录的 worktree）")
    p_start.add_argument("--session-id", default=None, help="覆盖 session id（默认取 token 记录）")
    p_start.add_argument(
        "--transcript-path",
        default=None,
        help=(
            "主 agent transcript（可选；省略时回退读决策 token 候选摘要里 Stop/UPS 盖的那份）。"
            "它决定本轮 scope 口径：拿得到 ⇒ session-owned + tracker lease + primary_units；"
            "拿不到 ⇒ 退回 manual-all-uncommitted 且审完标不了 reviewed"
        ),
    )
    p_start.add_argument("--retry", action="store_true", help="prepare 失败后重试（token 仍在 acknowledged）")
    p_start.add_argument(
        "--force-full-rescope",
        action="store_true",
        help=(
            "本会话名下没有未审单元时仍强行开审（把整份未提交 diff 当本轮范围重审一遍）。"
            "默认不这么做：那一轮拿不到 lease，审完标不了 reviewed，同一批 diff 下次 Stop 会再被提议。"
            "只在确知有 tracker 归属看不见的新工作（例如经 shell heredoc 写入的文件）时用"
        ),
    )

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
    p_rerender = scope_sub.add_parser(
        "rerender-scope-of-work",
        help=(
            "把改好的 scope-of-work 重渲进已冻结的 review packet"
            "（packet 在 start 时就内联冻结了 stub，只改盘上的文件不会进 packet）"
        ),
    )
    p_rerender.add_argument("--token", required=True)

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
            force_full_rescope=args.force_full_rescope,
        )
    elif args.command == "scope":
        if args.scope_action == "annotate":
            result = run_scope_annotate(token=args.token, entries=list(args.entry))
        elif args.scope_action == "exclude":
            result = run_scope_exclude(token=args.token, paths=list(args.paths), reason=args.reason)
        elif args.scope_action == "rerender-scope-of-work":
            result = run_scope_rerender_scope_of_work(token=args.token)
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
