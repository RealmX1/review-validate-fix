#!/usr/bin/env python3
"""RVF pre-review local-base bootstrap 的**可恢复等待状态机**（gate 子记录 + reconcile 判定）。

`prepare_review_run.pre_review_base_sync_state` 早已能检出「opt-in 仓库的 task worktree 还没吸收
它的 local base」并让 `prepare_run` 在 `start_run` **之前** 抛 `BootstrapRequiredBeforeRvf`
（故该路径不建 run、不分配 lease、不派 reviewer）。但这个信号历史上只是**一次普通 prepare 失败**：
等待期没有持久状态、没有可恢复归属、也没有给 agent 的确定性 self-injection 指令，于是
（a）decision-token 入口的 token 无 run 锚点、900s TTL 一到就死；（b）kanban follow-up 的
in-progress 锁只有一个笼统 `in_progress` 态，1h TTL 过期后读侧直接 clear + 回落常规 gate → 重铸
token / 重开 run（历史卡 32cad 的 bug 家族）。

本模块把该信号收敛成一枚**嵌入既有 store 的 gate 子记录**（入口 A 存进 decision-token payload、
入口 B 存进 kanban-followup 锁 payload），并提供两入口共用的单一深函数 `reconcile_bootstrap_gate`。
**刻意不引入第二套独立状态存储、也不新增 Stop shield**——等待期的存活/复活复用既有机制
（`revive_decision_token` / followup 锁的 re-engage nudge / `revive_prep_file`），receipt 重验证
复用现成的 fail-closed 全套 `prepare_review_run.validated_bootstrap_receipt`。

状态（`phase`，与领域语义一一对应）::

    preparing                                        # 瞬态，prepare 正在跑，不落 gate
    waiting_for_local_base_bootstrap                 # 等 agent 调同步 skill 吸收 local base
    waiting_for_local_base_bootstrap_conflict_resolution
                                                     # 吸收撞冲突，等在 task worktree 内解决
    bootstrap_interrupted_retryable                  # 等待期 agent 退出 / host 重启，可恢复
    reviewing                                        # prepare 真正成功，gate 退场、回落常规 RVF

**职责边界**：Git 吸收 / WIP stash / 冲突暂存 / hooks 禁用 / 短锁 / 边界校验一律归外部
`sync-local-base-into-task-worktree-only`（my-ai-setup）所有，RVF 不重实现、只**读它的 receipt**。

ponytail 收敛注记：gate 目前是「嵌进两个既有 store 的子记录」，因为等待态的 arm/clear 全都发生在
已持有该 store 的进程内（入口 A = rvf-agent CLI 自身；入口 B = UPS arm 锁的同一进程 + Stop 读侧）。
若将来 bootstrap 等待下沉到 orchestration 层、需要被**非 RVF** 的 dispatcher arm/clear，再把它迁成
独立 marker 文件族（形状照抄 `rvf_awaiting_dispatched_agent_marker.py`）。在那之前多一套 marker
只是多一处要对账的真相源。
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import


BOOTSTRAP_GATE_SCHEMA_VERSION = 1

# --- phase（5 态，见模块 docstring） ---------------------------------------
PHASE_PREPARING = "preparing"
PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP = "waiting_for_local_base_bootstrap"
PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP_CONFLICT_RESOLUTION = (
    "waiting_for_local_base_bootstrap_conflict_resolution"
)
PHASE_BOOTSTRAP_INTERRUPTED_RETRYABLE = "bootstrap_interrupted_retryable"
PHASE_REVIEWING = "reviewing"

WAITING_PHASES = frozenset(
    {
        PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP,
        PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP_CONFLICT_RESOLUTION,
        PHASE_BOOTSTRAP_INTERRUPTED_RETRYABLE,
    }
)

# --- reconcile 结论 ---------------------------------------------------------
OUTCOME_RECEIPT_VALID = "receipt_valid"
OUTCOME_STILL_WAITING = "still_waiting"
OUTCOME_CONFLICT = "conflict"
OUTCOME_TAMPERED = "tampered"
OUTCOME_ARTIFACT_LOST = "artifact_lost"

# 「不 resume、也不 clear」的结论：证据不可信 / 冲突未解，必须由 agent 处理后再来。
HOLD_OUTCOMES = frozenset({OUTCOME_STILL_WAITING, OUTCOME_CONFLICT, OUTCOME_TAMPERED})

# --- `replay_prepare_for_gate` 的 status（调用方必须**分类**处理，见下方函数 docstring） ------
REPLAY_STATUS_COMPLETED = "completed"
REPLAY_STATUS_TIMEOUT = "timeout"
REPLAY_STATUS_ARTIFACT_LOST = "artifact_lost"

# --- gate 归属 --------------------------------------------------------------
BOOTSTRAP_ORIGIN_DECISION_TOKEN = "decision_token"
BOOTSTRAP_ORIGIN_KANBAN_FOLLOWUP = "kanban_followup"

# prepare 的 shared_workflow_state 上用于区分「等 bootstrap」与「真失败」的独立 status。
WAITING_STATUS = "waiting_local_base_bootstrap"

# 调用同步 skill 时约定的「吸收后由谁负责审查」契约 env **名**（不落值，见 gate 白名单）。
# 该 env 属外部 my-ai-setup 的 sync skill，本仓无任何写侧实现——跨仓缺口见 handoff。
SYNC_SKILL_POST_ABSORPTION_REVIEW_OWNER_ENV = "BASE_BRANCH_SYNC_POST_ABSORPTION_REVIEW_OWNER"
SYNC_SKILL_POST_ABSORPTION_REVIEW_OWNER_VALUE = "RVF"

# receipt 的 canonical 落点（相对 task 的 absolute-git-dir）与 schema 版本。
BOOTSTRAP_RECEIPT_RELATIVE_PATH = (
    "base-branch-sync/local-base-into-task-worktree-bootstrap-receipt.json"
)
BOOTSTRAP_RECEIPT_SCHEMA_NAME = "local-base-into-task-worktree-bootstrap-receipt"
BOOTSTRAP_RECEIPT_SCHEMA_VERSION = 1

# receipt 里表征「吸收干净收尾」的状态字段；任一不符 = 冲突未解 / 收尾未完成（非篡改）。
_RECEIPT_CLEAN_STATUS_VALUES: dict[str, frozenset[str]] = {
    "boundary_status": frozenset({"VERIFIED"}),
    "lock_status": frozenset({"RELEASED"}),
    "target_repository_hooks_status": frozenset({"DISABLED"}),
    "wip_status": frozenset({"NOT_REQUIRED_TASK_WORKTREE_CLEAN", "RESTORED_AND_DROPPED"}),
}

# gate 白名单：`build_bootstrap_gate` **只**产出这些键。杜绝把整个 env / token payload spread
# 进 gate（contract #14：不泄露 credential / personal routing override / env 值）。
BOOTSTRAP_GATE_FIELDS = frozenset(
    {
        "gate_version",
        "phase",
        "armed_at",
        "required_sync_skill",
        "local_base_ref",
        "expected_local_base_oid",
        "task_head_at_detection",
        "git_common_directory_realpath",
        "task_worktree_realpath",
        "bootstrap_receipt_path",
        "bootstrap_receipt_digest",
        "bootstrap_receipt_schema_version",
        "rvf_run_id",
        "rvf_run_dir",
        "parent_attempt_run_id",
        "parent_attempt_run_dir",
        "origin",
    }
)
BOOTSTRAP_GATE_ORIGIN_FIELDS = frozenset(
    {"kind", "decision_token", "prep_token", "kanban_task_id", "session_id"}
)


class BootstrapGateError(ValueError):
    """gate 构造 / 判定的用法错误（非等待态诊断）。"""


@dataclass(frozen=True)
class BootstrapGateReconciliation:
    """`reconcile_bootstrap_gate` 的结论。``outcome`` 决定动作，``phase`` 决定对外状态名。"""

    outcome: str
    phase: str
    reason: str
    diagnostic: str
    receipt_path: str | None = None
    receipt_digest: str | None = None
    receipt_payload: dict[str, Any] | None = None
    resume_run_id: str | None = None
    resume_run_dir: str | None = None
    conflict_paths: tuple[str, ...] = field(default_factory=tuple)
    # 本次判定实际观测到的 local base tip，以及它是否已与 gate 记录的
    # ``expected_local_base_oid`` 不同（等待期内 base 前移是常规事件，见
    # `_reconcile_absorbed_receipt`）。写侧据此**就地重新快照** base 身份。
    observed_local_base_oid: str | None = None
    local_base_identity_changed: bool = False

    @property
    def resumable(self) -> bool:
        return self.outcome == OUTCOME_RECEIPT_VALID

    @property
    def holds_gate(self) -> bool:
        """True = 既不 resume 也不 clear，gate 原样留着等 agent 处理。"""
        return self.outcome in HOLD_OUTCOMES

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "outcome": self.outcome,
            "phase": self.phase,
            "reason": self.reason,
            "diagnostic": self.diagnostic,
        }
        if self.receipt_path:
            payload["bootstrap_receipt_path"] = self.receipt_path
        if self.receipt_digest:
            payload["bootstrap_receipt_digest"] = self.receipt_digest
        if self.resume_run_id:
            payload["resume_run_id"] = self.resume_run_id
        if self.resume_run_dir:
            payload["resume_run_dir"] = self.resume_run_dir
        if self.conflict_paths:
            payload["conflict_paths"] = list(self.conflict_paths)
        if self.local_base_identity_changed:
            payload["local_base_identity_changed"] = True
            payload["observed_local_base_oid"] = self.observed_local_base_oid
        return payload


# ---------------------------------------------------------------------------
# 惰性依赖（`prepare_review_run` 反过来 import 本模块，故只能在函数内 import）
# ---------------------------------------------------------------------------

def _prepare_review_run() -> Any:
    import prepare_review_run  # noqa: PLC0415

    return prepare_review_run


def _iso(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return stamp.isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _git_text(repo: Path, args: list[str]) -> str | None:
    """跑一条 git 命令并返回 stripped stdout；失败一律 None（gate 构造绝不因 git 抖动抛出）。

    复用 `prepare_review_run.git_repository_location_clean_environment`：调用方进程可能带着
    被污染的 ``GIT_DIR`` / ``GIT_WORK_TREE``（既有 bootstrap 用例正是用 sacrificial repo 验这一点），
    必须与 prepare 走同一套清洗，否则会读到另一个仓库的身份。
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            env=_prepare_review_run().git_repository_location_clean_environment(),
        )
    except Exception:  # noqa: BLE001 — 构造期绝不抛
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def expected_bootstrap_receipt_path(repo: Path | str) -> str | None:
    """task worktree 的 canonical receipt 落点（``<absolute-git-dir>/base-branch-sync/…``）。"""
    absolute_git_dir = _git_text(Path(repo), ["rev-parse", "--absolute-git-dir"])
    if absolute_git_dir is None:
        return None
    return str(Path(absolute_git_dir) / BOOTSTRAP_RECEIPT_RELATIVE_PATH)


def _git_common_directory_realpath(repo: Path) -> str | None:
    value = _git_text(repo, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    if value is None:
        return None
    try:
        return str(Path(value).resolve())
    except OSError:
        return None


def _task_worktree_realpath(repo: Path) -> str | None:
    value = _git_text(repo, ["rev-parse", "--show-toplevel"])
    if value is None:
        return None
    try:
        return str(Path(value).resolve())
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 构造
# ---------------------------------------------------------------------------

def build_bootstrap_gate(
    repo: Path | str,
    state: Mapping[str, Any],
    *,
    origin_kind: str,
    origin_decision_token: str | None = None,
    origin_prep_token: str | None = None,
    origin_kanban_task_id: str | None = None,
    origin_session_id: str | None = None,
    rvf_run_id: str | None = None,
    rvf_run_dir: str | Path | None = None,
    parent_attempt_run_id: str | None = None,
    parent_attempt_run_dir: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """按白名单构造一枚 waiting gate。``state`` = `pre_review_base_sync_state` 的返回 dict。

    只写 `BOOTSTRAP_GATE_FIELDS` 里的键，**绝不** spread 调用方的 env / token payload
    （contract #14）。git 身份字段取不到时留 None——`reconcile_bootstrap_gate` 的 G0 会据此
    判 `artifact_lost` 并给出显式诊断，而不是让 prepare 在失败路径上再抛一次。
    """
    if origin_kind not in (BOOTSTRAP_ORIGIN_DECISION_TOKEN, BOOTSTRAP_ORIGIN_KANBAN_FOLLOWUP):
        raise BootstrapGateError(f"未知 bootstrap gate origin: {origin_kind!r}")
    repo_path = Path(repo)
    origin: dict[str, Any] = {
        "kind": origin_kind,
        "decision_token": _text(origin_decision_token),
        "prep_token": _text(origin_prep_token),
        "kanban_task_id": _text(origin_kanban_task_id),
        "session_id": _text(origin_session_id),
    }
    return {
        "gate_version": BOOTSTRAP_GATE_SCHEMA_VERSION,
        "phase": PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP,
        "armed_at": _iso(now),
        "required_sync_skill": _text(state.get("required_skill")),
        "local_base_ref": _text(state.get("local_base_ref")),
        "expected_local_base_oid": _text(state.get("local_base_tip")),
        "task_head_at_detection": _text(state.get("task_head")),
        "git_common_directory_realpath": _git_common_directory_realpath(repo_path),
        "task_worktree_realpath": _task_worktree_realpath(repo_path),
        "bootstrap_receipt_path": expected_bootstrap_receipt_path(repo_path),
        "bootstrap_receipt_digest": None,
        "bootstrap_receipt_schema_version": BOOTSTRAP_RECEIPT_SCHEMA_VERSION,
        "rvf_run_id": _text(rvf_run_id),
        "rvf_run_dir": str(rvf_run_dir) if rvf_run_dir else None,
        "parent_attempt_run_id": _text(parent_attempt_run_id),
        "parent_attempt_run_dir": str(parent_attempt_run_dir) if parent_attempt_run_dir else None,
        "origin": origin,
    }


def attach_origin_decision_token(gate: Mapping[str, Any], token: str | None) -> dict[str, Any]:
    """把 decision token 补进 gate 的 origin（prepare 侧不知道它，由 rvf-agent CLI 补盖）。"""
    updated = dict(gate)
    origin = dict(updated.get("origin") or {})
    origin["kind"] = origin.get("kind") or BOOTSTRAP_ORIGIN_DECISION_TOKEN
    origin["decision_token"] = _text(token)
    updated["origin"] = {
        name: value for name, value in origin.items() if name in BOOTSTRAP_GATE_ORIGIN_FIELDS
    }
    return updated


def gate_with_verified_receipt(
    gate: Mapping[str, Any],
    reconciliation: BootstrapGateReconciliation,
) -> dict[str, Any]:
    """把一次 reconcile **校验通过**的 receipt 路径/digest（及重新快照的 base 身份）盖回 gate。

    `build_bootstrap_gate` 造 gate 时 receipt 还不存在，故 ``bootstrap_receipt_digest`` 恒为
    ``None``。若没有任何写侧在「receipt 首次校验通过、但本轮仍要继续等」时把它落盘，
    `_reconcile_absorbed_receipt` 的 ``bootstrap_receipt_digest_changed`` 判据在真实运行路径上
    **永远不可达**：后续每一轮重试都只做身份校验，任何「被替换但仍能通过身份校验」的 receipt
    都不会被判成篡改。两条入口（A = decision-token payload、B = kanban 锁 marker）凡是
    **继续持有等待态**的路径都必须调本函数再落盘。

    ``expected_local_base_oid`` 同理：等待期内 local base 前移（别的 worktree 往本地 base 落提交、
    `base-branch-sync` fast-forward）是**常规事件**，gate 记的那枚 oid 会就此过期。receipt 已按
    **当前** base 重新通过全套 fail-closed 校验时，这里把 base 身份就地重新快照，让 gate 记录与
    本轮实际依据保持一致（判定本身不依赖它，见 `_reconcile_absorbed_receipt`）。

    非 ``receipt_valid`` 结论 / 拿不到 digest 时原样返回：绝不把未经校验的证据写进 gate。
    """
    if reconciliation.outcome != OUTCOME_RECEIPT_VALID:
        return dict(gate)
    digest = _text(reconciliation.receipt_digest)
    if digest is None:
        return dict(gate)
    updated = dict(gate)
    updated["bootstrap_receipt_digest"] = digest
    receipt_path = _text(reconciliation.receipt_path)
    if receipt_path is not None:
        updated["bootstrap_receipt_path"] = receipt_path
    refreshed_base_oid = _text(reconciliation.observed_local_base_oid)
    if reconciliation.local_base_identity_changed and refreshed_base_oid is not None:
        updated["expected_local_base_oid"] = refreshed_base_oid
    return updated


def gate_of(container: Any) -> dict[str, Any] | None:
    """从任意 store payload（token payload / 锁 marker / shared_workflow_state）里取出 gate。"""
    if not isinstance(container, Mapping):
        return None
    gate = container.get("bootstrap_gate")
    if isinstance(gate, Mapping) and gate.get("gate_version") == BOOTSTRAP_GATE_SCHEMA_VERSION:
        return dict(gate)
    return None


def is_waiting_gate(gate: Any) -> bool:
    return isinstance(gate, Mapping) and gate.get("phase") in WAITING_PHASES


# ---------------------------------------------------------------------------
# 判定：单一深函数，两入口共用
# ---------------------------------------------------------------------------

def _lost(reason: str, diagnostic: str) -> BootstrapGateReconciliation:
    return BootstrapGateReconciliation(
        outcome=OUTCOME_ARTIFACT_LOST,
        phase=PHASE_BOOTSTRAP_INTERRUPTED_RETRYABLE,
        reason=reason,
        diagnostic=diagnostic,
    )


def _tampered(reason: str, diagnostic: str) -> BootstrapGateReconciliation:
    return BootstrapGateReconciliation(
        outcome=OUTCOME_TAMPERED,
        phase=PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP,
        reason=reason,
        diagnostic=diagnostic,
    )


def _conflict(
    reason: str, diagnostic: str, *, conflict_paths: tuple[str, ...] = ()
) -> BootstrapGateReconciliation:
    return BootstrapGateReconciliation(
        outcome=OUTCOME_CONFLICT,
        phase=PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP_CONFLICT_RESOLUTION,
        reason=reason,
        diagnostic=diagnostic,
        conflict_paths=conflict_paths,
    )


def _unresolved_absorption_conflict_paths(repo: Path) -> tuple[str, ...]:
    """task worktree 里是否卡着一次未解决的合并/rebase（= 冲突待解，而非「还没开始吸收」）。

    三条独立信号任一命中即算：``MERGE_HEAD`` 在场、``rebase-merge``/``rebase-apply`` 目录在场、
    索引里有 unmerged 条目。返回冲突路径（拿不到路径但确有冲突时返回 ``("<unknown>",)``，
    绝不因为「取不到路径」把冲突降级成普通等待）。
    """
    unmerged = _git_text(repo, ["diff", "--name-only", "--diff-filter=U"])
    paths = tuple(sorted({line for line in (unmerged or "").splitlines() if line.strip()}))
    if paths:
        return paths
    if _git_text(repo, ["rev-parse", "--verify", "--quiet", "MERGE_HEAD"]):
        return ("<unknown>",)
    git_dir = _git_text(repo, ["rev-parse", "--absolute-git-dir"])
    if git_dir:
        base = Path(git_dir)
        if (base / "rebase-merge").is_dir() or (base / "rebase-apply").is_dir():
            return ("<unknown>",)
    return ()


def _receipt_status_conflict(payload: Mapping[str, Any]) -> str | None:
    for name, allowed in _RECEIPT_CLEAN_STATUS_VALUES.items():
        value = payload.get(name)
        if value not in allowed:
            return f"{name}={value!r}"
    return None


# gate 记录的 git object id（`task_head_at_detection`）的形状；短 oid 也接受，但必须是纯 hex。
_GIT_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{7,64}$")

# 「id 与 dir 必须成对」的 run 锚点：只记一半 = 血缘不完整，无从判断该恢复到哪个 run。
_RUN_ANCHOR_FIELD_PAIRS = (
    ("rvf_run_id", "rvf_run_dir"),
    ("parent_attempt_run_id", "parent_attempt_run_dir"),
)


def _origin_attribution_problem(origin: Any) -> str | None:
    """gate 的 origin 归属自证：kind 必须是已知入口，字段必须在白名单内且是非空字符串 / None。

    等待态的恢复动作完全由 origin 决定（入口 A 拿 decision token 回 token store、入口 B 拿
    kanban task / session 回 followup 锁）。一枚 kind 伪造、或夹带白名单外字段的 origin 会让
    恢复落到**错误的归属**上，故这里 fail-closed：不认识就不复用。
    """
    if not isinstance(origin, Mapping):
        return "origin 不是对象"
    kind = origin.get("kind")
    if kind not in (BOOTSTRAP_ORIGIN_DECISION_TOKEN, BOOTSTRAP_ORIGIN_KANBAN_FOLLOWUP):
        return f"origin.kind={kind!r} 不是已知入口"
    smuggled = sorted(name for name in origin if name not in BOOTSTRAP_GATE_ORIGIN_FIELDS)
    if smuggled:
        return "origin 夹带白名单外字段（" + ", ".join(smuggled) + "）"
    for name, value in origin.items():
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            return f"origin.{name} 不是非空字符串（{value!r}）"
    return None


def _run_lineage_incomplete(gate: Mapping[str, Any]) -> str | None:
    """run / parent-attempt 锚点必须成对出现（两个都记或都不记）。"""
    for id_field, dir_field in _RUN_ANCHOR_FIELD_PAIRS:
        run_id = _text(gate.get(id_field))
        run_dir = _text(gate.get(dir_field))
        if (run_id is None) != (run_dir is None):
            return f"{id_field} / {dir_field} 只记了一半"
    return None


def _recorded_run_identity(run_dir: Path) -> str | None:
    """run 目录自己记的 run_id（``summary.json``），拿不到一律 None（不因此判罪）。"""
    try:
        payload = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, Mapping):
        return _text(payload.get("run_id"))
    return None


def _run_lineage_incoherent(gate: Mapping[str, Any]) -> str | None:
    """run / parent-attempt 血缘自洽：不能自己当自己的 parent，也不能与 run 目录自述身份冲突。

    第三条判据（run 目录里 ``summary.json`` 自述的 run_id）是**有则必校**：等待态的 run 目录
    未必已经落过 summary（入口 B 的 run 是派发侧建的），拿不到就跳过，绝不因缺证据判罪。
    """
    run_id = _text(gate.get("rvf_run_id"))
    run_dir = _text(gate.get("rvf_run_dir"))
    parent_run_id = _text(gate.get("parent_attempt_run_id"))
    parent_run_dir = _text(gate.get("parent_attempt_run_dir"))
    if run_id is not None and parent_run_id is not None and run_id == parent_run_id:
        return f"parent_attempt_run_id 与 rvf_run_id 同为 {run_id}（run 不能是自己的 parent attempt）"
    if run_dir is not None and parent_run_dir is not None and Path(run_dir) == Path(parent_run_dir):
        return f"parent attempt 与本轮共用同一个 run 目录 {run_dir}"
    if run_id is not None and run_dir is not None:
        recorded = _recorded_run_identity(Path(run_dir))
        if recorded is not None and recorded != run_id:
            return (
                f"run 目录 {run_dir} 自述的 run_id 是 {recorded}，与 gate 记录的 {run_id} 不符"
            )
    return None


def _task_head_at_detection_problem(repo: Path, task_head: str) -> tuple[str, str] | None:
    """检测时刻的 task HEAD 必须是合法 oid，且在**当前** task worktree 里仍可解析成 commit。

    返回 ``(kind, diagnostic)``：``invalid`` = 形状就不是 oid（伪造）；``unresolvable`` =
    形状合法但这个仓库里没有该提交（gate 记的是另一段历史 / 该提交已消失）。
    """
    if not _GIT_OBJECT_ID_RE.fullmatch(task_head):
        return ("invalid", f"task_head_at_detection={task_head!r} 不是合法的 git object id")
    if _git_text(repo, ["rev-parse", "--verify", "--quiet", f"{task_head}^{{commit}}"]) is None:
        return (
            "unresolvable",
            f"gate 记录的检测时刻 task HEAD {task_head} 在当前 task worktree 里解析不到",
        )
    return None


def _run_artifacts_lost(gate: Mapping[str, Any]) -> BootstrapGateReconciliation | None:
    """gate 锚定的 run 目录还在吗？不在 = 在途工件已丢失，本轮无从恢复（安全重来）。

    两条路径都必须查：仍需 bootstrap 的分支（G3）**以及** base 已吸收的分支（G2）。只查前者
    会让「receipt 有效但 run 目录已被清掉」照样判 `receipt_valid`，调用方随后按一个不存在的
    run 身份恢复。
    """
    run_dir = _text(gate.get("rvf_run_dir"))
    if run_dir is not None and not Path(run_dir).is_dir():
        return _lost(
            "rvf_run_artifacts_missing",
            f"gate 锚定的 RVF run 目录已消失：{run_dir}（等待期工件被清理）。",
        )
    return None


def reconcile_bootstrap_gate(
    gate: Mapping[str, Any],
    *,
    store_expired: bool = False,
) -> BootstrapGateReconciliation:
    """按 fail-closed 顺序 G0→G3 重新判定一枚 waiting gate。**只读，绝不改任何 store。**

    调用方据 ``outcome`` 决定动作、据 ``phase`` 对外报状态：``receipt_valid`` 才可重放 prepare；
    ``still_waiting``/``conflict``/``tampered`` 一律**既不 resume 也不 clear**（`holds_gate`）；
    ``artifact_lost`` 给显式诊断后安全重来。``store_expired`` 由调用方按各自 store 的 TTL 传入
    （入口 A = decision token 过 TTL，入口 B = followup 锁 STALE），仅影响等待态的 phase 命名
    （`bootstrap_interrupted_retryable`），不改变结论。
    """
    prepare = _prepare_review_run()

    # --- G0：gate 自证 ------------------------------------------------------
    # 自证覆盖等待态恢复所依赖的**全部**身份：同步契约（skill/base）、git 位置、归属
    # （origin = 该回哪个 store）、run 血缘（该恢复成哪个 run）、检测时刻的 task HEAD。
    # 少校验任何一项，一枚被伪造/被截断的 gate 都可能走到 `receipt_valid` 并按错误身份恢复。
    required_skill = _text(gate.get("required_sync_skill"))
    local_base_ref = _text(gate.get("local_base_ref"))
    expected_base_oid = _text(gate.get("expected_local_base_oid"))
    recorded_common_dir = _text(gate.get("git_common_directory_realpath"))
    recorded_worktree = _text(gate.get("task_worktree_realpath"))
    recorded_task_head = _text(gate.get("task_head_at_detection"))
    recorded_origin = gate.get("origin")
    missing = [
        name
        for name, value in (
            ("required_sync_skill", required_skill),
            ("local_base_ref", local_base_ref),
            ("expected_local_base_oid", expected_base_oid),
            ("git_common_directory_realpath", recorded_common_dir),
            ("task_worktree_realpath", recorded_worktree),
            ("task_head_at_detection", recorded_task_head),
            ("origin", recorded_origin if isinstance(recorded_origin, Mapping) else None),
        )
        if value is None
    ]
    if missing:
        return _lost(
            "gate_self_identity_incomplete",
            "bootstrap gate 自身身份字段缺失（" + ", ".join(missing) + "），无法安全恢复。",
        )

    origin_problem = _origin_attribution_problem(recorded_origin)
    if origin_problem is not None:
        return _tampered(
            "bootstrap_gate_origin_untrusted",
            f"bootstrap gate 的归属（origin）不可信：{origin_problem}；拒绝按该 gate 恢复。",
        )
    lineage_incomplete = _run_lineage_incomplete(gate)
    if lineage_incomplete is not None:
        return _lost(
            "gate_run_lineage_incomplete",
            f"bootstrap gate 的 run 血缘不完整（{lineage_incomplete}），无法安全恢复。",
        )
    lineage_incoherent = _run_lineage_incoherent(gate)
    if lineage_incoherent is not None:
        return _tampered(
            "bootstrap_gate_run_lineage_incoherent",
            f"bootstrap gate 的 run 血缘自相矛盾：{lineage_incoherent}；拒绝按该 gate 恢复。",
        )

    repo = Path(recorded_worktree)
    if not repo.is_dir():
        return _lost(
            "task_worktree_missing",
            f"gate 记录的 task worktree 已不存在：{recorded_worktree}。",
        )
    actual_worktree = _task_worktree_realpath(repo)
    actual_common_dir = _git_common_directory_realpath(repo)
    if actual_worktree is None or actual_common_dir is None:
        return _lost(
            "task_worktree_not_a_git_worktree",
            f"gate 记录的路径已不是可用的 git worktree：{recorded_worktree}。",
        )
    if actual_worktree != recorded_worktree or actual_common_dir != recorded_common_dir:
        return _tampered(
            "task_worktree_identity_changed",
            "task worktree / git common-dir 身份与 gate 记录不符"
            f"（worktree={actual_worktree}，common-dir={actual_common_dir}），拒绝复用该 gate。",
        )
    task_head_problem = _task_head_at_detection_problem(repo, recorded_task_head)
    if task_head_problem is not None:
        kind, diagnostic = task_head_problem
        if kind == "invalid":
            return _tampered(
                "task_head_at_detection_invalid",
                f"{diagnostic}；拒绝按该 gate 恢复。",
            )
        # 形状合法但解析不到：等待态所基于的那段历史已不在，安全重来（而非硬等一枚废 gate）。
        return _lost("task_head_at_detection_unresolvable", f"{diagnostic}。")

    # --- G1：base 重查 ------------------------------------------------------
    try:
        state = prepare.pre_review_base_sync_state(repo, local_base_ref)
    except Exception as exc:  # noqa: BLE001 — 判定绝不把异常抛给 Stop / CLI
        return _lost(
            "local_base_unreadable",
            f"重查 local base `{local_base_ref}` 失败（{type(exc).__name__}: {exc}）。",
        )
    if state is None:
        return _lost(
            "pre_review_base_sync_opt_in_withdrawn",
            f"local base `{local_base_ref}` 已不再声明 pre-review 同步 marker，等待态失去依据。",
        )

    if state["status"] == "absorbed":
        # base 身份**不在这里**判罪：等待期内 local base 前移是常规事件（别的 worktree 往本地
        # base 落提交、`base-branch-sync` fast-forward），而 `expected_local_base_oid` 只在
        # `build_bootstrap_gate` 写过一次。曾经在此 fail-closed 判 `tampered`（∈ HOLD_OUTCOMES）
        # 会让这一轮**永久**卡死：agent 照 next_steps 重跑同步 skill、吸收新 tip、留下完全合法的
        # receipt，判定仍恒等于「base 身份变了」。真正的证据校验交给 `_reconcile_absorbed_receipt`
        # ——它用**当前** state 跑全套 `validated_bootstrap_receipt`，而后者要求
        # ``absorbed_base_tip == state["local_base_tip"]``，即 receipt 必须是对**当前** base 的
        # 一次真实吸收；一份旧 epoch 的 receipt 在那里照样 fail-closed。
        return _reconcile_absorbed_receipt(
            prepare, repo, gate, state, expected_base_oid=expected_base_oid
        )

    # --- G3：仍需 bootstrap → 在途工件 + 冲突信号 ---------------------------
    conflict_paths = _unresolved_absorption_conflict_paths(repo)
    if conflict_paths:
        return _conflict(
            "local_base_absorption_conflicted",
            f"task worktree 里有一次未解决的 local base 吸收（冲突路径 {len(conflict_paths)} 条）；"
            "请在 task worktree 内解决冲突并让同步 skill 收尾后再继续。",
            conflict_paths=conflict_paths,
        )

    artifacts_lost = _run_artifacts_lost(gate)
    if artifacts_lost is not None:
        return artifacts_lost
    run_dir = _text(gate.get("rvf_run_dir"))
    return BootstrapGateReconciliation(
        outcome=OUTCOME_STILL_WAITING,
        phase=(
            PHASE_BOOTSTRAP_INTERRUPTED_RETRYABLE
            if store_expired
            else PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP
        ),
        reason=(
            "bootstrap_interrupted_retryable" if store_expired else "bootstrap_still_required"
        ),
        diagnostic=(
            f"task worktree 仍未吸收 local base `{local_base_ref}`"
            # 报**当前**观测到的 tip，不是 gate armed 那一刻的旧 tip：等待期内 base 可能已前移，
            # 拿旧 oid 指路会让 agent 去吸收一枚早就不是 base 尖端的提交。
            f"（当前 tip {_text(state.get('local_base_tip')) or '<unknown>'}）；"
            f"请先跑 `{required_skill}`。"
        ),
        resume_run_id=_text(gate.get("rvf_run_id")),
        resume_run_dir=run_dir,
        observed_local_base_oid=_text(state.get("local_base_tip")),
        local_base_identity_changed=_text(state.get("local_base_tip")) != expected_base_oid,
    )


def _reconcile_absorbed_receipt(
    prepare: Any,
    repo: Path,
    gate: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    expected_base_oid: str | None = None,
) -> BootstrapGateReconciliation:
    """G2：base 已吸收 → 在途工件仍在、receipt 在场、身份自洽、且表明吸收干净收尾。

    工件存在性必须在这里**再查一次**：G3 的同名检查在 base 已吸收时根本到不了，少了它
    「receipt 有效但 run 目录已被清掉」会照样判 `receipt_valid`，调用方随后按一个已经不存在
    的 run 恢复（入口 A 甚至会用它当 `resume_run_id/dir` 去 `start_run`）。

    ``expected_base_oid`` = gate 记录的 base 身份。它与**当前**观测到的 tip 不同（等待期内
    base 前移）时，本轮属于一个新的 base epoch：证据仍走全套 fail-closed 校验（
    `validated_bootstrap_receipt` 要求 ``absorbed_base_tip`` 等于当前 tip、且 task HEAD 由
    receipt 记录的各 tip 派生，故旧 epoch 的 receipt 通不过），但 gate 上钉的
    ``bootstrap_receipt_digest`` 属于**已被取代的那个 epoch**，不能再用它判「证据被替换」——
    否则「base 前移 + 重跑同步 skill 产出新 receipt」这条常规恢复路径会被恒判 tampered 而锁死。
    """
    artifacts_lost = _run_artifacts_lost(gate)
    if artifacts_lost is not None:
        return artifacts_lost
    expected_path = expected_bootstrap_receipt_path(repo)
    if expected_path is None:
        return _lost(
            "bootstrap_receipt_path_unresolvable",
            "无法解析 task worktree 的 absolute-git-dir，取不到 canonical receipt 路径。",
        )
    recorded_path = _text(gate.get("bootstrap_receipt_path"))
    if recorded_path is not None and Path(recorded_path) != Path(expected_path):
        return _tampered(
            "bootstrap_receipt_path_changed",
            f"gate 记录的 receipt 路径 {recorded_path} 与当前 canonical 路径 {expected_path} 不符。",
        )

    receipt_path = Path(expected_path)
    sidecar = Path(f"{expected_path}.sha256")
    if not receipt_path.exists() and not sidecar.exists():
        conflict_paths = _unresolved_absorption_conflict_paths(repo)
        return _conflict(
            "bootstrap_receipt_absent_after_absorption",
            "local base 看起来已被吸收，但 task worktree 里没有同步 skill 留下的 bootstrap "
            "receipt（手工 merge 不留证据无法进审）；请重跑同步 skill 产出可信 receipt。",
            conflict_paths=conflict_paths,
        )

    # 状态字段先读：`validated_bootstrap_receipt` 把身份与状态归并成同一条 ValueError，
    # 单看消息无法区分「篡改」与「冲突未收尾」，故先按原始 payload 判状态、再交给全套校验。
    try:
        raw_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _tampered(
            "bootstrap_receipt_unreadable",
            f"bootstrap receipt 不可读或不是合法 JSON（{type(exc).__name__}: {exc}）。",
        )
    if not isinstance(raw_payload, dict):
        return _tampered(
            "bootstrap_receipt_not_an_object",
            "bootstrap receipt 顶层必须是 JSON 对象。",
        )
    status_conflict = _receipt_status_conflict(raw_payload)
    if status_conflict is not None:
        return _conflict(
            "bootstrap_receipt_not_cleanly_finished",
            f"bootstrap receipt 显示吸收未干净收尾（{status_conflict}）；"
            "请在 task worktree 内解决冲突、让同步 skill 正常收尾后再继续。",
        )

    try:
        receipt_payload = prepare.validated_bootstrap_receipt(repo, state, receipt_path)
        _receipt_value, receipt_digest = prepare.canonical_json_artifact_identity(receipt_path)
    except Exception as exc:  # noqa: BLE001 — 任何校验失败 = 证据不可信，fail-closed
        return _tampered(
            "bootstrap_receipt_identity_invalid",
            f"bootstrap receipt 未通过 fail-closed 校验（{type(exc).__name__}: {exc}）。",
        )

    observed_base_oid = _text(state.get("local_base_tip"))
    base_identity_changed = observed_base_oid != expected_base_oid
    recorded_digest = _text(gate.get("bootstrap_receipt_digest"))
    if (
        recorded_digest is not None
        and recorded_digest != receipt_digest
        and not base_identity_changed
    ):
        return _tampered(
            "bootstrap_receipt_digest_changed",
            "bootstrap receipt 的 digest 与 gate 记录不符，拒绝按旧 gate 进审。",
        )
    diagnostic = "local base 已吸收且 bootstrap receipt 校验通过，可恢复本轮 prepare。"
    if base_identity_changed:
        diagnostic += (
            f"（等待期内 local base 已从 {expected_base_oid} 变为 {observed_base_oid}，"
            "receipt 已按当前 base 重新通过全套校验，gate 的 base 身份就地重新快照。）"
        )
    return BootstrapGateReconciliation(
        outcome=OUTCOME_RECEIPT_VALID,
        phase=PHASE_REVIEWING,
        reason="bootstrap_receipt_valid",
        diagnostic=diagnostic,
        receipt_path=str(receipt_path),
        receipt_digest=receipt_digest,
        receipt_payload=receipt_payload,
        resume_run_id=_text(gate.get("rvf_run_id")),
        resume_run_dir=_text(gate.get("rvf_run_dir")),
        observed_local_base_oid=observed_base_oid,
        local_base_identity_changed=base_identity_changed,
    )


# ---------------------------------------------------------------------------
# 给 agent 的 self-injection checkpoint（模型可见，非 user-only systemMessage）
# ---------------------------------------------------------------------------

def bootstrap_waiting_agent_steps(
    gate: Mapping[str, Any],
    reconciliation: BootstrapGateReconciliation | None = None,
) -> list[str]:
    """等待期给主 agent 的确定性步骤清单（调同步 skill → 解冲突 → 取/验 receipt → 恢复 prepare）。

    这是任务要求的「模型可见 self-injection checkpoint」的**单一文案来源**：两入口共用，
    也让同步 skill 契约 env 名只写在这一处。
    """
    required_skill = _text(gate.get("required_sync_skill")) or "<sync skill>"
    local_base_ref = _text(gate.get("local_base_ref")) or "<local base>"
    receipt_path = _text(gate.get("bootstrap_receipt_path")) or "<git-dir>/" + BOOTSTRAP_RECEIPT_RELATIVE_PATH
    steps = [
        f"① 调用 `{required_skill}` 把 local base `{local_base_ref}` 单向吸收进本 task worktree"
        f"（传 `{SYNC_SKILL_POST_ABSORPTION_REVIEW_OWNER_ENV}="
        f"{SYNC_SKILL_POST_ABSORPTION_REVIEW_OWNER_VALUE}`，声明吸收后的审查归 RVF）。",
        "② 若吸收撞冲突，**在本 task worktree 内**解决完再让该 skill 收尾——RVF 不代做 Git 吸收。",
        f"③ 确认它产出了 bootstrap receipt：`{receipt_path}`（含 `.sha256` sidecar）。",
    ]
    if reconciliation is not None and reconciliation.conflict_paths:
        shown = ", ".join(reconciliation.conflict_paths[:8])
        suffix = " …" if len(reconciliation.conflict_paths) > 8 else ""
        steps.insert(1, f"⚠ 当前未解决的冲突路径：{shown}{suffix}")
    return steps


def bootstrap_replay_pending_reengage_reason(
    gate: Mapping[str, Any],
    reconciliation: BootstrapGateReconciliation,
    *,
    resume_hint: str,
) -> str:
    """receipt 已验证通过、只差本轮 prepare 重放收尾时，给主 agent 的**模型可见**定向指令。

    **刻意不复用** `bootstrap_waiting_reengage_reason`：那条文案让 agent 去跑同步 skill，而此刻
    local base 早已吸收、receipt 也已通过校验并把 digest 记进 gate。再跑一次同步 skill 会产出
    **另一份** receipt，下一轮 reconcile 会按 digest 判 `tampered` 并把这一轮锁死——所以这里必须
    反过来明确要求「别再动它」。
    """
    receipt_path = _text(gate.get("bootstrap_receipt_path")) or "<bootstrap receipt>"
    return "\n".join(
        [
            "本轮 RVF 的 local-base bootstrap **已经完成**（receipt 校验通过），只差 prepare 收尾。",
            reconciliation.diagnostic,
            f"① **不要**再跑一次同步 skill、也不要改动 `{receipt_path}`：那会产出另一份 receipt，"
            "下一轮会按 digest 判为证据被替换而拒绝进审。",
            "② 若吸收过程留了未提交的冲突残留，只在 task worktree 内收拾干净即可。",
            resume_hint,
            "在 prepare 收尾前**不要**输出 handoff、也不要另开一轮 RVF。",
        ]
    )


def bootstrap_waiting_reengage_reason(
    gate: Mapping[str, Any],
    reconciliation: BootstrapGateReconciliation,
    *,
    resume_hint: str,
) -> str:
    """把等待/冲突/篡改结论渲染成一段**模型可见**的定向指令（不是通用「去收尾 handoff」提示）。"""
    steps = bootstrap_waiting_agent_steps(gate, reconciliation)
    head = {
        OUTCOME_STILL_WAITING: "本轮 RVF 正卡在 pre-review local-base bootstrap，**尚未开始 review**。",
        OUTCOME_CONFLICT: "本轮 RVF 的 local-base 吸收有未解决的冲突，**尚未开始 review**。",
        OUTCOME_TAMPERED: "本轮 RVF 的 bootstrap 证据不可信，拒绝进入 review。",
        OUTCOME_ARTIFACT_LOST: "本轮 RVF 的 bootstrap 等待工件已丢失。",
    }.get(reconciliation.outcome, "本轮 RVF 正卡在 pre-review local-base bootstrap。")
    lines = [head, reconciliation.diagnostic, *steps, resume_hint]
    lines.append("在 bootstrap 完成前**不要**输出 handoff、也不要另开一轮 RVF。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 恢复：按 gate 记录的 prep token 重放**同一个** run 的 prepare
# ---------------------------------------------------------------------------

# 子进程重放的强杀余量：内层预算（`timeout_seconds`）到点后，子进程会写结果并 `os._exit`
# 立刻退出；这段余量只覆盖「连内层超时都没能让它退出」的卡死（例如卡在 import / 主线程的
# 阻塞系统调用里），到点由父进程按进程组 SIGKILL 收掉。故对外的**硬**上限 =
# ``timeout_seconds + REPLAY_SUBPROCESS_KILL_GRACE_SECONDS``。
REPLAY_SUBPROCESS_KILL_GRACE_SECONDS = 10.0

# 子进程程序：只做「读 prep → 跑 prepare → 回传状态 → 立刻硬退出」。
#
# 为什么必须是**子进程**：`prepare_run_from_prep_file` 的 `ThreadPoolExecutor` worker 是非守护
# 线程，`concurrent.futures.thread` 注册的 `_python_exit` 会在解释器退出时 join 它。因此
# `future.result(timeout=…)` 只能让**函数**按时返回，**进程**仍会挂到 worker 自己跑完为止——
# 同进程调用它的 Stop hook 会被 120s 预算杀掉，自动恢复决定永远送不出去。把重放隔离进子进程，
# 时间上限才真正可强制；也**不必**改动 `prepare_run_from_prep_file` 本身（UPS / manual / 入口 A
# 都复用它，改共享 executor 语义的 blast radius 明显更大）。
_REPLAY_CHILD_PROGRAM = r"""
import json
import os
import sys

scripts_dir, prep_token, timeout_text, result_path = sys.argv[1:5]
sys.path.insert(0, scripts_dir)


def _emit(payload):
    try:
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, default=str)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        # 结果已落盘 -> 立刻硬退出。走正常 return 会让解释器在退出时 join 那个仍在跑的
        # prepare worker（非守护线程），本进程就白白继承了父进程原来的那个卡死。
        sys.stderr.flush()
        os._exit(0)


try:
    import rvf_prep_file
    import prepare_review_run

    lookup = rvf_prep_file.read_prep_file(prep_token)
    if lookup.payload is None:
        _emit({"status": "artifact_lost", "error": "prep 文件不可用（status=%s）。" % lookup.status})
    record = rvf_prep_file.PrepFileRecord(
        token=lookup.token, path=lookup.path, payload=dict(lookup.payload)
    )
    _emit(
        prepare_review_run.prepare_run_from_prep_file(
            record, timeout_seconds=float(timeout_text)
        )
    )
except BaseException as exc:  # 子进程绝不把异常留给父进程去猜
    _emit({"status": "failed", "error": "%s: %s" % (type(exc).__name__, exc)})
"""


def _kill_replay_child(process: subprocess.Popen) -> None:
    """硬杀重放子进程（连同它 fork 出去的 git 等后代），绝不抛。"""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:  # pragma: no cover — RVF 只在 POSIX host 上跑
            process.kill()
    except Exception:  # noqa: BLE001
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        process.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _stderr_tail(path: Path, limit: int = 400) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[-limit:]


def _replay_prepare_in_subprocess(prep_token: str, *, timeout_seconds: float) -> dict[str, Any]:
    """在**可强制终止**的子进程里跑一次 prepare 重放；返回子进程回传的 state dict。

    刻意不接管道：子进程的 stdout/stderr 一律走 DEVNULL / 临时文件。若接了 `capture_output`，
    强杀后 `communicate()` 还要等到**孙进程**（prepare 起的 git）也放掉管道写端才返回——那正好
    把「硬上限」又变回一次不可控的等待。
    """
    budget = max(float(timeout_seconds), 0.0)
    hard_deadline = budget + REPLAY_SUBPROCESS_KILL_GRACE_SECONDS
    scripts_dir = str(Path(__file__).resolve().parent)
    with tempfile.TemporaryDirectory(prefix="rvf-bootstrap-replay-") as workspace:
        result_path = Path(workspace) / "replay-result.json"
        stderr_path = Path(workspace) / "replay-stderr.log"
        try:
            with stderr_path.open("wb") as stderr_sink:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        _REPLAY_CHILD_PROGRAM,
                        scripts_dir,
                        prep_token,
                        f"{budget}",
                        str(result_path),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_sink,
                    start_new_session=os.name == "posix",
                )
                try:
                    process.wait(timeout=hard_deadline)
                except subprocess.TimeoutExpired:
                    _kill_replay_child(process)
                    return {
                        "status": REPLAY_STATUS_TIMEOUT,
                        "error": (
                            f"prepare 重放子进程超过 {hard_deadline:.0f}s 硬上限"
                            f"（预算 {budget:.0f}s + 强杀余量 "
                            f"{REPLAY_SUBPROCESS_KILL_GRACE_SECONDS:.0f}s），已被强制终止。"
                        ),
                    }
        except Exception as exc:  # noqa: BLE001 — 起不来子进程也只是一次可重试的失败
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}

        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            tail = _stderr_tail(stderr_path)
            return {
                "status": "failed",
                "error": (
                    f"prepare 重放子进程未回传结果（returncode={process.returncode}，"
                    f"{type(exc).__name__}: {exc}）{'：' + tail if tail else ''}"
                ),
            }
        if not isinstance(payload, dict):
            return {"status": "failed", "error": "prepare 重放子进程回传的结果不是 JSON 对象。"}
        return payload


def replay_prepare_for_gate(
    gate: Mapping[str, Any],
    *,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """receipt 有效后重放本轮 prepare：复活 gate 记录的 prep 文件并复用**同一 run_id/run_dir**。

    这是 contract「恢复不得产生第二个 run / 第二条 lease / 第二次 reviewer」的落点：prep 的
    ``rvf_run`` 里已经钉死了 run_id/run_dir，`prepare_run_from_prep_file` 会原样透传给
    `prepare_run`。任何失败都以 dict 形式返回（``status`` 非 ``completed``），**绝不抛**——
    调用方（Stop 读侧）不能因为一次重放失败被卡住。

    **时间上限是可强制的**：重放跑在一个独立子进程里，到 ``timeout_seconds +
    REPLAY_SUBPROCESS_KILL_GRACE_SECONDS`` 会被按进程组 SIGKILL 收掉（见
    `_replay_prepare_in_subprocess`）。同进程直调只能让**函数**按时返回、**进程**仍被非守护
    worker 的 join 拖住，Stop hook 会因此超出 120s 预算被杀、恢复决定永远送不出去。

    ``status`` 是**分类信号，不是一个二元的成功/失败位**，调用方必须分开处理：

    - ``completed``：prepare 已就绪，gate 退场。
    - ``artifact_lost``：prep 文件已不可用（TTL 只有 300s，远短于一次 bootstrap 等待，且
      `default_prepare_pipeline` / UPS 都会 `rvf_prep_file.sweep_stale()` 把它删掉）。此时
      本轮**无从恢复也无在途工作可撞**，应立刻走「显式诊断 + 安全重来」而不是继续空等到
      re-engage / revive 预算耗尽。
    - ``timeout``：本次重放**已被终止、且不留在途的 prepare 写者**。内层预算到点时子进程把结果
      写完就 `os._exit`（那个仍在跑的非守护 prepare worker 线程随进程一起消失），外层硬上限到点
      则按**进程组** SIGKILL、连同它 fork 出去的 git 等后代一并收掉。故调用方应**释放**自己为这次
      重放抢下的占位，并允许下一次调用**立刻重试**同一个 run 的 prepare——不要把它当成「重放仍在
      途」去做持久去重、白挡满一整个替换窗口（那条旧立论来自同进程直调时代「shutdown(wait=False)
      后后台 worker 仍在写同一个 run 目录」，重放子进程化之后已不成立）。并发去重由起 replay
      **之前**那次原子 claim 负责，与超时后是否保留占位无关；重试本身也安全：prep 的
      shared_workflow_state 被推进为 ``timeout`` 不会挡住下一次重放，而若它已被别人推进为
      ``completed``，重放会幂等短路直接成功。
    - 其余（``failed`` / 又撞回 ``waiting_local_base_bootstrap``）：可重试等待。
    """
    import rvf_prep_file  # noqa: PLC0415

    prep_token = None
    origin = gate.get("origin")
    if isinstance(origin, Mapping):
        prep_token = _text(origin.get("prep_token"))
    if prep_token is None:
        return {"status": "artifact_lost", "error": "bootstrap gate 未记录 prep token，无法重放。"}

    try:
        lookup = rvf_prep_file.read_prep_file(prep_token)
        if lookup.status == "expired" and lookup.payload is not None:
            record = rvf_prep_file.revive_prep_file(lookup)
        elif lookup.status == "valid" and lookup.payload is not None:
            record = rvf_prep_file.PrepFileRecord(
                token=lookup.token, path=lookup.path, payload=dict(lookup.payload)
            )
        else:
            return {
                "status": "artifact_lost",
                "error": f"bootstrap gate 的 prep 文件不可用（status={lookup.status}）。",
            }
    except Exception as exc:  # noqa: BLE001
        return {"status": "artifact_lost", "error": f"{type(exc).__name__}: {exc}"}

    # prep 的查找 / 复活留在本进程（纯文件 IO，无阻塞风险）；重放本身隔离进子进程，
    # 这样「慢或卡死的 prepare」只能吃掉子进程的寿命，吃不掉调用方（Stop hook）的预算。
    try:
        return _replay_prepare_in_subprocess(record.token, timeout_seconds=timeout_seconds)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "BOOTSTRAP_GATE_SCHEMA_VERSION",
    "BOOTSTRAP_GATE_FIELDS",
    "BOOTSTRAP_GATE_ORIGIN_FIELDS",
    "BOOTSTRAP_ORIGIN_DECISION_TOKEN",
    "BOOTSTRAP_ORIGIN_KANBAN_FOLLOWUP",
    "BOOTSTRAP_RECEIPT_RELATIVE_PATH",
    "BOOTSTRAP_RECEIPT_SCHEMA_NAME",
    "BOOTSTRAP_RECEIPT_SCHEMA_VERSION",
    "BootstrapGateError",
    "BootstrapGateReconciliation",
    "HOLD_OUTCOMES",
    "OUTCOME_ARTIFACT_LOST",
    "OUTCOME_CONFLICT",
    "OUTCOME_RECEIPT_VALID",
    "OUTCOME_STILL_WAITING",
    "OUTCOME_TAMPERED",
    "PHASE_BOOTSTRAP_INTERRUPTED_RETRYABLE",
    "PHASE_PREPARING",
    "PHASE_REVIEWING",
    "PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP",
    "PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP_CONFLICT_RESOLUTION",
    "REPLAY_STATUS_ARTIFACT_LOST",
    "REPLAY_STATUS_COMPLETED",
    "REPLAY_STATUS_TIMEOUT",
    "REPLAY_SUBPROCESS_KILL_GRACE_SECONDS",
    "SYNC_SKILL_POST_ABSORPTION_REVIEW_OWNER_ENV",
    "SYNC_SKILL_POST_ABSORPTION_REVIEW_OWNER_VALUE",
    "WAITING_PHASES",
    "WAITING_STATUS",
    "attach_origin_decision_token",
    "bootstrap_replay_pending_reengage_reason",
    "bootstrap_waiting_agent_steps",
    "bootstrap_waiting_reengage_reason",
    "build_bootstrap_gate",
    "expected_bootstrap_receipt_path",
    "gate_of",
    "gate_with_verified_receipt",
    "is_waiting_gate",
    "reconcile_bootstrap_gate",
    "replay_prepare_for_gate",
]
