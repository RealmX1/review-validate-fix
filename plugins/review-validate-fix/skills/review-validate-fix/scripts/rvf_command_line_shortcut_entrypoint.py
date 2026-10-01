#!/usr/bin/env python3
"""``rvf``：RVF 执行段的单一快捷入口（部署后经 PATH 上的 shim 调用）。

为什么要它：实测每轮 RVF 执行段中位数 38 个 agent 请求，其中大量是机械停留——手抄 run 目录 /
token / 脚本绝对路径、``--help`` 摸索一个 PATH 上根本不存在的 ``rvf-agent``、逐腿跑
``check_review_result``、``--wait-status`` 反复轮询、fix 循环里 upsert → prepare → start 三连。
本入口把这些机械信息改成**自动推断**，把多步机械调用合成一步：

* ``start / scope / dispatch / decline / decline-dispatched-review``：原样转给
  ``rvf_agent_decision_cli``，省略 ``--token`` 时自动推断（见下）。
* ``wait``：等 reviewer 终态（按 host 分流，见 ``resolve_current_host_reviewer_wait_mode``），
  done 时直接打印合并摘要（每腿 kind / issues / audit_summary / requests，失败腿给 stderr 路径）。
  等待期间登记 awaiting-dispatched-agent marker，让 Stop 不在正常等待里发续跑提示（见
  ``awaiting_dispatched_reviewers_marker_armed_while_waiting``）。
* ``results``：不等待，直接打印同一份摘要。
* ``redispatch-reviewers [--review-request-legs-only]``：对**当前** run 重派（自动带
  ``--rvf-run-dir``，修掉「不 source env 就静默新建一个 run」的缝）。
* ``fix prepare <issue.json>…``：upsert + prepare + start 一步完成，输出可复制的子代理派发参数块
  （含带 ``cd`` / ``--attempt-id`` / ``--run-dir`` 的完整收尾命令），并按 attempt 登记 awaiting marker。
* ``fix stop``：子代理在 attempt worktree 内收尾（原样转给 ``rvf_fix_attempt stop``）。
* ``apply --all-stopped | --attempt-id …``：按创建顺序合回，遇冲突停下交给 agent。
* ``status``：当前 run 的上下文、派发状态、attempt 状态与 handoff 落点。
* ``validate``：``validation_result_cache.py run``，自动补 ``--repo`` / ``--rvf-run-dir``；在 fix attempt
  worktree 里（或 Stop hook 被抑制的子进程里）只验证 cwd 所在 worktree、不写主 run。
* ``land prepare|commit``：原样转给 ``rvf_land.py``；``commit`` 缺 ``--run-dir`` 时取本仓最近一次
  ``land prepare`` 的 run。
* ``handoff``：打印 handoff 落点与契约（机械段预渲染见计划 T1.5，暂缓）。

**当前 run 的推断优先级**（``resolve_current_rvf_run_context``）：显式 ``--run-dir`` / ``--token``
> ``RVF_RUN_DIR`` > cwd 落在某个 fix attempt worktree 里 > 本会话本仓的 in-flight marker
（``~/.rvf/kanban-followup-in-progress/``；session 键优先于 task 键，只认未过期且已接受态）> 本 worktree 最近一个 acknowledged / dispatched
的决策 token 及其 scope overlay（同会话优先；会话 id 未知时只认无会话归属的记录）。Claude Code 的 Bash 不保留 shell 状态，所以
「先 export 再调用」那条路走不通——推断必须由命令自己完成。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — 把 pyroot 上 sys.path，供 core.* / 兄弟脚本 import

from core.host_adapter.current_agent_session_identity_from_environment import (  # noqa: E402
    REVIEWER_WAIT_MODE_BOUNDED_FOREGROUND_POLLING,
    REVIEWER_WAIT_MODE_CLAUDE_BACKGROUND_TASK_COMPLETION_NOTIFICATION,
    REVIEWER_WAIT_MODE_CODEX_SINGLE_LONG_EXEC_SESSION_YIELD,
    current_host_wakes_main_agent_when_background_work_completes,
    resolve_current_agent_session_id_from_environment,
    resolve_current_host_reviewer_wait_mode,
)
from core.rvf_command_line_shortcut_shim_location import (  # noqa: E402
    resolve_rvf_command_line_shortcut_invocation,
)
from core.rvf_shared_runtime_constants.rvf_shared_runtime_constants import (  # noqa: E402
    SUPPRESS_ENV_NAMES,
)

SCRIPTS_DIRECTORY = Path(__file__).resolve().parent

EXIT_OK = 0
EXIT_CURRENT_RUN_CONTEXT_UNRESOLVED = 2
EXIT_FIX_ATTEMPT_APPLY_STOPPED_ON_CONFLICT = 3
EXIT_FIX_PREPARE_FAILED = 4

TOKEN_STATE_MINTED = "minted"
TOKEN_STATE_INJECTED = "injected"
TOKEN_STATE_ACKNOWLEDGED = "acknowledged"
TOKEN_STATE_DISPATCHED = "dispatched"

# 各决策子命令可接受的 token 状态（推断时只在这些状态里挑）。
DECISION_TOKEN_STATES_ACCEPTABLE_FOR_START_OR_DECLINE = frozenset(
    {TOKEN_STATE_MINTED, TOKEN_STATE_INJECTED, TOKEN_STATE_ACKNOWLEDGED}
)
DECISION_TOKEN_STATES_ACCEPTABLE_FOR_SCOPE_OR_DISPATCH = frozenset({TOKEN_STATE_ACKNOWLEDGED})
DECISION_TOKEN_STATES_WITH_ACCEPTED_RUN = frozenset({TOKEN_STATE_ACKNOWLEDGED, TOKEN_STATE_DISPATCHED})

RUN_CONTEXT_SOURCE_EXPLICIT_RUN_DIR = "explicit_run_dir_argument"
RUN_CONTEXT_SOURCE_EXPLICIT_TOKEN = "explicit_decision_token_argument"
RUN_CONTEXT_SOURCE_RVF_RUN_DIR_ENV = "RVF_RUN_DIR_environment_variable"
RUN_CONTEXT_SOURCE_ATTEMPT_WORKTREE_CWD = "cwd_inside_fix_attempt_worktree"
RUN_CONTEXT_SOURCE_IN_FLIGHT_MARKER = "in_flight_accepted_review_run_marker"
RUN_CONTEXT_SOURCE_WORKTREE_LATEST_DECISION_TOKEN = "worktree_latest_accepted_decision_token"

# reviewer 等待时长。detached 派发自带 2700s 总超时 backstop（dispatch_reviewers
# DEFAULT_DISPATCH_TOTAL_TIMEOUT_SECONDS），长等待只需比它多一点余量就一定等到终态。
LONG_REVIEWER_WAIT_MARGIN_SECONDS = 120.0
# 有界前台轮询（Kimi 等无后台通知的 host）：单次 480s < 前台工具 600s 上限。
BOUNDED_FOREGROUND_REVIEWER_WAIT_SECONDS = 480.0
# EXPERIMENT_NODE: Codex 单次长等待是否应比 total-timeout 更短（例如 1200s），以换取中途可见进度；当前取与 Claude 相同的「一次等到底」。
CODEX_SINGLE_LONG_EXEC_SESSION_REVIEWER_WAIT_EXTRA_SECONDS = 0.0
# Claude Code 后台 Bash 的 ``timeout`` 缺省只有 1800000ms（2.1.285 本体 ``_Ke=1800000``，上限
# ``max(7200000, …)``），短于 ``rvf wait`` 在 Claude 下的缺省等待（total-timeout 2700s + 余量），
# 不显式传就会在 reviewer 跑完前被 host 砍掉。取一个盖过缺省等待、又远低于上限的值。
CLAUDE_BACKGROUND_BASH_TIMEOUT_MILLISECONDS_FOR_REVIEWER_WAIT = 3_000_000
# ``rvf wait`` 被 SIGKILL（finally 跑不到）时 awaiting marker 只能靠 TTL 自释放：TTL = 本次最长等待
# + 这段余量（覆盖进程启动、run 推断与摘要渲染）。
REVIEWER_WAIT_AWAITING_MARKER_TTL_MARGIN_SECONDS = 300.0
REVIEWER_WAIT_AWAITING_MARKER_DISPATCHER = "rvf wait（detached reviewer 派发）"
# host 砍后台任务 / 关终端时发的信号：转成 SystemExit，让 finally 清掉 awaiting marker。
REVIEWER_WAIT_TERMINATION_SIGNALS_THAT_MUST_STILL_CLEAR_AWAITING_MARKER = (signal.SIGTERM, signal.SIGHUP)

DISPATCH_STATUS_FILENAME = ".dispatch-thread.status.json"
REVIEWER_PLAN_FILENAME = "reviewer-plan.json"
REVIEW_REQUEST_REDISPATCH_FLAG = "--redispatch-review-request-legs-only"


def _rvf_invocation() -> str:
    return resolve_rvf_command_line_shortcut_invocation(
        fallback_entrypoint_script_path=Path(__file__).resolve()
    )


def render_explicit_run_dir_argument_when_current_session_unresolvable(
    run_dir: str | Path | None,
    *,
    option_name: str = "--run-dir",
    environ: Mapping[str, str] | None = None,
) -> str:
    """回执里给出的下一步命令要不要直接带上 run 目录，返回要拼在子命令后的参数串（或空串）。

    解析不出当前会话 id 时（Kimi 等不注入会话身份的 host），run 推断一律 fail-closed（见
    ``_record_session_attribution_compatible_with_current_session``）：照抄不带 run 目录的命令，
    首跑必 exit 2、再多一轮请求。此时直接把 run 目录写进命令；解析得出时保持短命令。"""
    if run_dir is None or resolve_current_agent_session_id_from_environment(environ):
        return ""
    return f" {option_name} {shlex.quote(str(run_dir))}"


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _same_path(left: str | Path | None, right: str | Path | None) -> bool:
    if not left or not right:
        return False
    return os.path.realpath(str(left)) == os.path.realpath(str(right))


def _git_toplevel(cwd: Path) -> Path | None:
    """按 ``cwd`` 定位仓库根，与 ``rvf_land`` / ``rvf_handoff_intake`` / ``validation_result_cache`` 同口径：
    剥掉调用方 shell 继承来的 ``GIT_DIR`` / ``GIT_WORK_TREE`` 等仓库定位变量，否则这里解析出的仓库根会和
    下游（清洗后）的不一致——land 指针记不上 / 读错仓，attempt 里的 ``rvf validate`` 把 ``--repo`` 指向别处。"""
    from validation_result_cache import clean_git_environment  # noqa: PLC0415

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd), capture_output=True, text=True, check=False, env=clean_git_environment(),
        )
    except (OSError, ValueError):
        return None
    top = completed.stdout.strip()
    return Path(top) if completed.returncode == 0 and top else None


def _load_decision_cli():
    import rvf_agent_decision_cli  # noqa: PLC0415 - 重量级入口，按需加载

    return rvf_agent_decision_cli


def _load_token_store():
    import rvf_decision_token_store  # noqa: PLC0415

    return rvf_decision_token_store


# ---------------------------------------------------------------------------
# 当前 run / 决策 token 推断
# ---------------------------------------------------------------------------

@dataclass
class ResolvedRvfRunContext:
    run_dir: Path | None
    resolution_source: str | None
    decision_token: str | None = None
    scope_adjustment_overlay: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def run_id(self) -> str | None:
        overlay_run_id = (self.scope_adjustment_overlay or {}).get("run_id")
        if isinstance(overlay_run_id, str) and overlay_run_id:
            return overlay_run_id
        if self.run_dir is None:
            return None
        summary = _read_json_object(self.run_dir / "summary.json") or {}
        value = summary.get("run_id")
        return value if isinstance(value, str) and value else self.run_dir.name

    @property
    def repo(self) -> str | None:
        overlay = self.scope_adjustment_overlay or {}
        for key in ("repo", "worktree"):
            value = overlay.get(key)
            if isinstance(value, str) and value:
                return value
        if self.run_dir is None:
            return None
        exported = read_review_env_plain_exports(self.run_dir)
        if exported.get("RVF_REPO"):
            return exported["RVF_REPO"]
        summary = _read_json_object(self.run_dir / "summary.json") or {}
        value = summary.get("repo")
        return value if isinstance(value, str) and value else None

    @property
    def prepare_artifacts(self) -> dict[str, Any]:
        artifacts = (self.scope_adjustment_overlay or {}).get("prepare_artifacts")
        return dict(artifacts) if isinstance(artifacts, dict) else {}

    def artifact_path(self, overlay_key: str, fallback_relative_to_artifacts: str) -> Path | None:
        value = self.prepare_artifacts.get(overlay_key)
        if isinstance(value, str) and value:
            return Path(value)
        if self.run_dir is None:
            return None
        return self.run_dir / "artifacts" / fallback_relative_to_artifacts


def read_review_env_plain_exports(run_dir: Path) -> dict[str, str]:
    """只读 ``review-env.sh`` 里不含变量展开的 ``export K=V`` 行（够取 RVF_REPO / RUN_ID）。"""
    exported: dict[str, str] = {}
    try:
        lines = (run_dir / "artifacts" / "review-env.sh").read_text(encoding="utf-8").splitlines()
    except OSError:
        return exported
    for line in lines:
        text = line.strip()
        if not text.startswith("export ") or "$" in text or "=" not in text:
            continue
        key, _, raw_value = text[len("export "):].partition("=")
        try:
            parts = shlex.split(raw_value)
        except ValueError:
            continue
        if len(parts) == 1:
            exported[key.strip()] = parts[0]
    return exported


def _iter_scope_adjustment_overlays() -> list[dict[str, Any]]:
    root = _load_decision_cli().scope_adjustment_root()
    overlays: list[dict[str, Any]] = []
    try:
        candidates = sorted(root.glob("*.json"))
    except OSError:
        return overlays
    for path in candidates:
        payload = _read_json_object(path)
        if payload is not None and isinstance(payload.get("token"), str):
            overlays.append(payload)
    return overlays


def _decision_token_payload(token: str) -> dict[str, Any] | None:
    store = _load_token_store()
    try:
        path = store.decision_token_path(token)
    except ValueError:
        return None
    return _read_json_object(path)


def _decision_token_state(token: str) -> str | None:
    payload = _decision_token_payload(token) or {}
    state = payload.get("state")
    return state if isinstance(state, str) else None


def _record_session_attribution_compatible_with_current_session(
    record_session_id: Any, *, current_session_id: str | None
) -> bool:
    """决策 token / scope overlay 这条记录能否被**当前会话**的自动推断捡走。

    * 解析得出当前 session id：只收本会话的记录与无会话归属的记录——别的会话铸给它自己的
      token / 它接受的 run，不替它表态、不落到它的 run 上。
    * 解析不出当前 session id（Kimi、或 shell 没注入 ``CLAUDE_CODE_SESSION_ID`` /
      ``CODEX_THREAD_ID``）：**fail-closed**，只收同样无会话归属的记录。此时无法区分「本会话」
      与「同 worktree 上的别的会话」，放行任意会话最新的记录就是替别人 start / decline、把
      wait / apply 绑到别人的 run 上；宁可推断不出、让 agent 显式传 ``--token`` / ``--run-dir``。
      （不直接一刀切拒绝：确实无会话归属的记录不存在「替别人表态」的风险，仍可安全推断。）
    """
    normalized_record_session_id = (
        record_session_id.strip() if isinstance(record_session_id, str) and record_session_id.strip() else None
    )
    if normalized_record_session_id is None:
        return True
    return bool(current_session_id) and normalized_record_session_id == current_session_id


def _pick_newest_preferring_current_session(
    candidates: list[dict[str, Any]], *, session_id: str | None, timestamp_keys: Sequence[str]
) -> dict[str, Any] | None:
    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        same_session = 1 if session_id and item.get("session_id") == session_id else 0
        stamp = ""
        for key in timestamp_keys:
            value = item.get(key)
            if isinstance(value, str) and value:
                stamp = value
                break
        return same_session, stamp

    return max(candidates, key=sort_key) if candidates else None


def infer_decision_token_from_token_store(
    *, worktree: Path, session_id: str | None, acceptable_states: frozenset[str]
) -> dict[str, Any] | None:
    """在决策 token store 里挑本 worktree（同会话优先）最近一枚处于 ``acceptable_states`` 的 token。

    会话身份未知时只收无会话归属的 token（fail-closed，见
    ``_record_session_attribution_compatible_with_current_session``）。
    """
    root = _load_token_store().decision_token_root()
    candidates: list[dict[str, Any]] = []
    try:
        paths = sorted(root.glob("*.json"))
    except OSError:
        return None
    for path in paths:
        payload = _read_json_object(path)
        if payload is None or payload.get("state") not in acceptable_states:
            continue
        if not _same_path(payload.get("worktree"), worktree):
            continue
        if not _record_session_attribution_compatible_with_current_session(
            payload.get("session_id"), current_session_id=session_id
        ):
            # 别的会话铸给它自己的 token（或当前会话身份未知）：不替它表态。
            continue
        candidates.append(payload)
    return _pick_newest_preferring_current_session(
        candidates, session_id=session_id, timestamp_keys=("minted_at", "created_at")
    )


def _read_in_flight_accepted_review_run_marker(*, repo: Path, session_id: str | None) -> dict[str, Any] | None:
    """读「本会话 / 本 kanban task 已接受、仍在途」的 RVF run marker；不合格一律返回 None 落到下一层。

    不能直接照搬 ``kanban_followup_lock.read_marker`` 的结果，有两个缝：

    * **候选顺序反了**：``read_marker`` 先命中 ``task-…`` 键，而 ``rvf start``
      （``arm_accepted_review_run_in_flight_receipt``）只写 ``sess-…`` 键。kanban task 里一枚
      旧的 task 键 marker 会压过本会话刚接受的 run ⇒ wait / apply / dispatch 绑到旧 run。
      故这里**先查本会话 session 键、再查 task 键**，逐键单独读。
    * **没校验合格性**：过期（``marker_status`` 非 active）的锁、以及
      ``delivered_awaiting_agent_decision`` 这类「trigger 已投递但 agent 尚未接受」的态，都不代表
      「本会话接受了这一轮」。只认 active 且 state ∈ 已接受态（in_progress / waiting_bootstrap）。
    """
    try:
        import kanban_followup_lock  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - 推断是便利层，锁模块缺失时退到下一层
        return None
    task_id = _load_decision_cli()._current_kanban_task_id_from_env()
    in_flight_marker_states_with_agent_accepted_run = frozenset(
        {kanban_followup_lock.STATE_IN_PROGRESS, kanban_followup_lock.STATE_WAITING_BOOTSTRAP}
    )
    # (task_id, session_id) 逐键查询：本会话 session 键优先，task 键兜底。
    marker_lookup_keys_in_priority_order: list[tuple[str | None, str | None]] = []
    if session_id:
        marker_lookup_keys_in_priority_order.append((None, session_id))
    if task_id:
        marker_lookup_keys_in_priority_order.append((task_id, None))
    for lookup_task_id, lookup_session_id in marker_lookup_keys_in_priority_order:
        try:
            marker = kanban_followup_lock.read_marker(
                task_id=lookup_task_id, session_id=lookup_session_id, repo=repo
            )
            if marker is None:
                continue
            if kanban_followup_lock.marker_status(marker) != kanban_followup_lock.STATUS_ACTIVE:
                continue
            if kanban_followup_lock.marker_state(marker) not in in_flight_marker_states_with_agent_accepted_run:
                continue
        except Exception:  # noqa: BLE001 - 单键读坏只跳过该键
            continue
        return marker
    return None


def _overlay_for_token(token: str) -> dict[str, Any] | None:
    try:
        return _load_decision_cli().read_scope_adjustment(token)
    except Exception:  # noqa: BLE001
        return None


def _overlay_for_run_dir(run_dir: Path) -> dict[str, Any] | None:
    matches = [
        overlay for overlay in _iter_scope_adjustment_overlays()
        if _same_path(overlay.get("run_dir"), run_dir)
    ]
    return _pick_newest_preferring_current_session(
        matches, session_id=None, timestamp_keys=("updated_at", "created_at")
    )


def _latest_accepted_overlay_for_worktree(*, worktree: Path, session_id: str | None) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for overlay in _iter_scope_adjustment_overlays():
        if not (
            _same_path(overlay.get("worktree"), worktree) or _same_path(overlay.get("repo"), worktree)
        ):
            continue
        if not _record_session_attribution_compatible_with_current_session(
            overlay.get("session_id"), current_session_id=session_id
        ):
            continue
        if _decision_token_state(str(overlay["token"])) not in DECISION_TOKEN_STATES_WITH_ACCEPTED_RUN:
            continue
        matches.append(overlay)
    return _pick_newest_preferring_current_session(
        matches, session_id=session_id, timestamp_keys=("updated_at", "created_at")
    )


def resolve_current_rvf_run_context(
    *,
    explicit_run_dir: str | None = None,
    explicit_token: str | None = None,
    cwd: Path | None = None,
    environ: dict[str, str] | None = None,
) -> ResolvedRvfRunContext:
    environment = environ if environ is not None else dict(os.environ)
    working_directory = Path(cwd) if cwd is not None else Path.cwd()

    if explicit_run_dir:
        run_dir = Path(explicit_run_dir).expanduser().resolve()
        overlay = _overlay_for_run_dir(run_dir)
        return ResolvedRvfRunContext(
            run_dir=run_dir, resolution_source=RUN_CONTEXT_SOURCE_EXPLICIT_RUN_DIR,
            decision_token=(overlay or {}).get("token"), scope_adjustment_overlay=overlay,
        )
    if explicit_token:
        overlay = _overlay_for_token(explicit_token)
        run_dir_value = (overlay or {}).get("run_dir")
        return ResolvedRvfRunContext(
            run_dir=Path(run_dir_value) if isinstance(run_dir_value, str) and run_dir_value else None,
            resolution_source=RUN_CONTEXT_SOURCE_EXPLICIT_TOKEN,
            decision_token=explicit_token, scope_adjustment_overlay=overlay,
        )
    env_run_dir = (environment.get("RVF_RUN_DIR") or "").strip()
    if env_run_dir:
        run_dir = Path(env_run_dir).expanduser().resolve()
        overlay = _overlay_for_run_dir(run_dir)
        return ResolvedRvfRunContext(
            run_dir=run_dir, resolution_source=RUN_CONTEXT_SOURCE_RVF_RUN_DIR_ENV,
            decision_token=(overlay or {}).get("token"), scope_adjustment_overlay=overlay,
        )

    import rvf_fix_attempt  # noqa: PLC0415

    from_attempt = rvf_fix_attempt.infer_run_dir_and_attempt_id_from_attempt_worktree_path(working_directory)
    if from_attempt is not None:
        run_dir = from_attempt[0]
        overlay = _overlay_for_run_dir(run_dir)
        return ResolvedRvfRunContext(
            run_dir=run_dir, resolution_source=RUN_CONTEXT_SOURCE_ATTEMPT_WORKTREE_CWD,
            decision_token=(overlay or {}).get("token"), scope_adjustment_overlay=overlay,
        )

    worktree = _git_toplevel(working_directory)
    session_id = resolve_current_agent_session_id_from_environment(environment)
    if worktree is None:
        return ResolvedRvfRunContext(
            run_dir=None, resolution_source=None,
            notes=[f"cwd 不在 git worktree 内：{working_directory}"],
        )

    marker = _read_in_flight_accepted_review_run_marker(repo=worktree, session_id=session_id)
    marker_run_dir = (marker or {}).get("run_dir")
    if isinstance(marker_run_dir, str) and marker_run_dir and Path(marker_run_dir).is_dir():
        marker_token = (marker or {}).get("decision_token")
        overlay = (
            _overlay_for_token(marker_token) if isinstance(marker_token, str) and marker_token
            else _overlay_for_run_dir(Path(marker_run_dir))
        )
        return ResolvedRvfRunContext(
            run_dir=Path(marker_run_dir), resolution_source=RUN_CONTEXT_SOURCE_IN_FLIGHT_MARKER,
            decision_token=marker_token if isinstance(marker_token, str) else (overlay or {}).get("token"),
            scope_adjustment_overlay=overlay,
        )

    overlay = _latest_accepted_overlay_for_worktree(worktree=worktree, session_id=session_id)
    if overlay is not None and isinstance(overlay.get("run_dir"), str) and overlay["run_dir"]:
        return ResolvedRvfRunContext(
            run_dir=Path(overlay["run_dir"]),
            resolution_source=RUN_CONTEXT_SOURCE_WORKTREE_LATEST_DECISION_TOKEN,
            decision_token=str(overlay["token"]), scope_adjustment_overlay=overlay,
        )
    return ResolvedRvfRunContext(
        run_dir=None, resolution_source=None,
        notes=[
            f"在 worktree {worktree}（session={session_id or '未知'}）下找不到已接受的 RVF run："
            "没有 RVF_RUN_DIR、不在 fix attempt worktree 里、没有 in-flight marker、"
            "也没有 acknowledged / dispatched 的决策 token"
            + ("" if session_id else "（会话 id 未知时只认无会话归属的决策 token，防止落到别的会话的 run）")
            + "。显式传 --run-dir 或 --token。"
        ],
    )


def _require_run_context(explicit_run_dir: str | None, explicit_token: str | None = None) -> ResolvedRvfRunContext:
    context = resolve_current_rvf_run_context(explicit_run_dir=explicit_run_dir, explicit_token=explicit_token)
    if context.run_dir is None:
        detail = "；".join(context.notes) or "无法确定当前 RVF run。"
        raise CurrentRunContextUnresolved(detail)
    return context


class CurrentRunContextUnresolved(Exception):
    pass


# ---------------------------------------------------------------------------
# 决策子命令：原样转给 rvf_agent_decision_cli，缺 --token 时推断
# ---------------------------------------------------------------------------

def _argv_names_token(argv: Sequence[str]) -> bool:
    return any(item == "--token" or item.startswith("--token=") for item in argv)


def _argv_asks_for_help(argv: Sequence[str]) -> bool:
    return any(item in ("-h", "--help") for item in argv)


def infer_decision_token_for_subcommand(subcommand: str) -> tuple[str | None, str]:
    """返回 ``(token, 说明)``。start / decline 从 token store 挑；scope / dispatch 从当前 run 取。"""
    worktree = _git_toplevel(Path.cwd())
    session_id = resolve_current_agent_session_id_from_environment()
    if subcommand in ("start", "decline"):
        if worktree is None:
            return None, "cwd 不在 git worktree 内，无法按 worktree 推断决策 token"
        payload = infer_decision_token_from_token_store(
            worktree=worktree, session_id=session_id,
            acceptable_states=DECISION_TOKEN_STATES_ACCEPTABLE_FOR_START_OR_DECLINE,
        )
        if payload is None:
            session_unknown_hint = (
                "" if session_id else
                "（未解析出当前会话 id：为免替同 worktree 上别的会话表态，只推断无会话归属的 token）"
            )
            return None, (
                f"worktree {worktree} 下没有待表态的决策 token（minted / injected / acknowledged）"
                f"{session_unknown_hint}；照抄横幅里的 RVF_DECISION_TOKEN= 传 --token"
            )
        return str(payload["token"]), f"推断自决策 token store（state={payload.get('state')}）"
    context = resolve_current_rvf_run_context()
    token = context.decision_token
    if not token:
        return None, "；".join(context.notes) or "找不到当前 run 的决策 token；显式传 --token"
    state = _decision_token_state(token)
    if state not in DECISION_TOKEN_STATES_ACCEPTABLE_FOR_SCOPE_OR_DISPATCH:
        return None, f"当前 run 的决策 token {token} 处于 {state}，{subcommand} 需要 acknowledged 态"
    return token, f"推断自 {context.resolution_source}"


def run_decision_subcommand(subcommand: str, rest: list[str]) -> int:
    cli = _load_decision_cli()
    forwarded = [subcommand, *rest]
    if subcommand == "scope" and rest and not rest[0].startswith("-"):
        action, action_rest = rest[0], rest[1:]
        needs_token = not _argv_names_token(action_rest) and not _argv_asks_for_help(action_rest)
        if needs_token:
            token, how = infer_decision_token_for_subcommand("scope")
            if token is None:
                print(f"[rvf scope] 无法推断 token：{how}", file=sys.stderr)
                return EXIT_CURRENT_RUN_CONTEXT_UNRESOLVED
            print(f"[rvf] token {token}（{how}）")
            forwarded = ["scope", action, "--token", token, *action_rest]
    elif (
        subcommand in ("start", "dispatch", "decline")
        and not _argv_names_token(rest)
        and not _argv_asks_for_help(rest)
    ):
        token, how = infer_decision_token_for_subcommand(subcommand)
        if token is None:
            print(f"[rvf {subcommand}] 无法推断 token：{how}", file=sys.stderr)
            return EXIT_CURRENT_RUN_CONTEXT_UNRESOLVED
        print(f"[rvf] token {token}（{how}）")
        forwarded = [subcommand, "--token", token, *rest]
    return int(cli.main(forwarded))


# ---------------------------------------------------------------------------
# wait / results：等 reviewer 终态并打印合并摘要
# ---------------------------------------------------------------------------

def _dispatch_status_path_for(context: ResolvedRvfRunContext) -> Path | None:
    dispatch_result = (context.scope_adjustment_overlay or {}).get("dispatch_result")
    if isinstance(dispatch_result, dict):
        value = dispatch_result.get("dispatch_status_path")
        if isinstance(value, str) and value:
            return Path(value)
    if context.run_dir is None:
        return None
    return context.run_dir / "artifacts" / "reviewers" / DISPATCH_STATUS_FILENAME


def _reviewers_directory_for(context: ResolvedRvfRunContext) -> Path | None:
    status_path = _dispatch_status_path_for(context)
    return status_path.parent if status_path is not None else None


def _newest_file_matching(directory: Path, pattern: str) -> Path | None:
    try:
        candidates = [path for path in directory.glob(pattern) if path.is_file()]
    except OSError:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def summarize_reviewer_legs_from_canonical_results(
    *, reviewers_directory: Path, scope_contract: Path | None
) -> dict[str, Any]:
    """读 ``reviewer-plan.json`` + 每腿 **canonical** ``review-result.json``（不碰
    ``superseded-review-result-artifacts/``），用与 Contract Gate 相同的 ``check_review_result.classify``
    判定每腿。runner 已逐腿跑过同一校验，这里再跑一次只是为了读到**当前** canonical 内容。"""
    import check_review_result  # noqa: PLC0415

    plan_path = reviewers_directory / REVIEWER_PLAN_FILENAME
    plan = _read_json_object(plan_path)
    if plan is None:
        return {"plan_path": str(plan_path), "plan_status": "unknown", "legs": [], "plan_missing": True}
    legs: list[dict[str, Any]] = []
    for reviewer in plan.get("reviewers") or []:
        if not isinstance(reviewer, dict):
            continue
        reviewer_id = str(reviewer.get("reviewer_id") or "")
        reviewer_directory = reviewers_directory / reviewer_id
        result_path_value = reviewer.get("review_result_path")
        result_path = Path(result_path_value) if result_path_value else reviewer_directory / "review-result.json"
        leg: dict[str, Any] = {
            "reviewer_id": reviewer_id,
            "harness_id": reviewer.get("harness_id"),
            "label": reviewer.get("label"),
            "returncode": reviewer.get("returncode"),
            "review_result_path": str(result_path),
        }
        if result_path.is_file():
            classified = check_review_result.classify(result_path, scope_contract=scope_contract)
            leg.update(
                {
                    "kind": classified.get("kind"),
                    "valid": bool(classified.get("valid")),
                    "issues": classified.get("issues") or [],
                    "requests": classified.get("requests") or [],
                    "audit_summary": classified.get("audit_summary"),
                    "errors": classified.get("errors") or [],
                }
            )
        else:
            leg.update({"kind": None, "valid": False, "issues": [], "requests": [], "errors": []})
        if leg["returncode"] not in (0, None) or not leg["valid"]:
            stderr_path = _newest_file_matching(reviewer_directory, "reviewer.stderr*.txt")
            leg["stderr_path"] = str(stderr_path) if stderr_path else None
        legs.append(leg)
    effective = plan.get("effective_reviewer_count")
    summary: dict[str, Any] = {
        "plan_path": str(plan_path),
        "plan_status": plan.get("status") or "unknown",
        "plan_reason": plan.get("reason"),
        "effective_reviewer_count": effective if isinstance(effective, int) else None,
        "reviewer_count": len(legs),
        "fallbacks": plan.get("fallbacks") or [],
        "review_request_pending_reviewer_ids": plan.get("review_request_pending_reviewer_ids") or [],
        "needs_last_resort_fallback": bool(plan.get("needs_last_resort_fallback")),
        "legs": legs,
    }
    # --json 与文本摘要共用同一份判定事实。
    established, not_established_reasons = evaluate_dual_review_establishment_from_canonical_legs(summary)
    summary["dual_review_established_from_canonical_legs"] = established
    summary["dual_review_not_established_reasons"] = not_established_reasons
    return summary


REVIEWER_LEG_KINDS_ELIGIBLE_FOR_ESTABLISHED_DUAL_REVIEW = frozenset({"issues", "no_issues"})


def evaluate_dual_review_establishment_from_canonical_legs(summary: dict[str, Any]) -> tuple[bool, list[str]]:
    """判定双评审是否成立，返回 ``(是否成立, 不成立原因列表)``。

    ``reviewer-plan.json`` 缓存的 status / effective_reviewer_count 只是 runner 当时的结论；canonical
    ``review-result.json`` 事后缺失或损坏时它不会更新。因此「成立」必须**同时**满足：plan completed、
    有效腿数与腿数一致且 ≥2、且**当前**每腿 canonical 结果都 valid 且 kind ∈ {issues, no_issues}。
    任一不满足即 fail-close 为不成立。returncode 不参与判定。"""
    reasons: list[str] = []
    status = summary.get("plan_status")
    if status != "completed":
        reasons.append(f"plan_status={status}（不是 completed）")
    legs = [leg for leg in summary.get("legs") or [] if isinstance(leg, dict)]
    effective = summary.get("effective_reviewer_count")
    if effective is None or effective != summary.get("reviewer_count"):
        reasons.append(f"plan 记录的有效腿={effective if effective is not None else '?'} 与腿数 {summary.get('reviewer_count')} 不一致")
    if len(legs) < 2:
        reasons.append(f"canonical 腿数={len(legs)}（少于 2）")
    for leg in legs:
        kind = leg.get("kind")
        if not leg.get("valid") or kind not in REVIEWER_LEG_KINDS_ELIGIBLE_FOR_ESTABLISHED_DUAL_REVIEW:
            reasons.append(
                f"腿 {leg.get('reviewer_id')} 当前 canonical review-result 不是合法的 issues / no_issues"
                f"（valid={bool(leg.get('valid'))}, kind={kind}）"
            )
    return (not reasons, reasons)


def _render_issue_line(index: int, issue: dict[str, Any]) -> str:
    return (
        f"      {index}. [{issue.get('severity')}/{issue.get('kind')}] "
        f"{issue.get('path')}:{issue.get('line')} — {issue.get('message')}"
    )


def render_reviewer_results_summary(
    summary: dict[str, Any], *, dispatch_state: dict[str, Any] | None, run_dir: str | Path | None = None
) -> str:
    invocation = _rvf_invocation()
    run_dir_argument = render_explicit_run_dir_argument_when_current_session_unresolvable(run_dir)
    lines: list[str] = []
    effective = summary.get("effective_reviewer_count")
    head = (
        f"[rvf results] plan_status={summary.get('plan_status')} "
        f"有效腿={effective if effective is not None else '?'}/{summary.get('reviewer_count')}"
    )
    if dispatch_state:
        head += (
            f" launch_status={dispatch_state.get('launch_status')} "
            f"returncode={dispatch_state.get('returncode')}"
        )
    lines.append(head)
    lines.append(f"- plan: {summary.get('plan_path')}")
    if summary.get("plan_reason"):
        lines.append(f"- plan_reason: {summary.get('plan_reason')}")
    fallbacks = summary.get("fallbacks") or []
    if fallbacks:
        for item in fallbacks:
            if isinstance(item, dict):
                lines.append(
                    f"- fallback: slot{item.get('slot')} {item.get('from')}→{item.get('to')}"
                    f"（{item.get('reason')}）"
                )
    for leg in summary.get("legs") or []:
        title = f"- 腿 {leg.get('reviewer_id')}（harness={leg.get('harness_id')}, rc={leg.get('returncode')}）"
        kind = leg.get("kind")
        if kind == "issues":
            issues = leg.get("issues") or []
            lines.append(f"{title}: kind=issues，{len(issues)} 条")
            lines.extend(_render_issue_line(i, issue) for i, issue in enumerate(issues, 1) if isinstance(issue, dict))
        elif kind == "no_issues":
            lines.append(f"{title}: kind=no_issues")
            lines.append(f"      audit_summary: {leg.get('audit_summary')}")
        elif kind == "request":
            requests = leg.get("requests") or []
            lines.append(f"{title}: kind=request，{len(requests)} 条（待处理终态，不得进 merge table）")
            for request in requests:
                if isinstance(request, dict):
                    detail = {k: v for k, v in request.items() if k != "type"}
                    lines.append(f"      - [{request.get('type')}] {json.dumps(detail, ensure_ascii=False)}")
        else:
            errors = "; ".join(str(e) for e in (leg.get("errors") or [])) or "无 review-result.json"
            lines.append(f"{title}: 无合法结果（{errors}）")
        if leg.get("stderr_path"):
            lines.append(f"      stderr: {leg.get('stderr_path')}")
    status = summary.get("plan_status")
    # 不信任 summary 里缓存的判定字段，按当前 canonical 腿重新判定。
    established, not_established_reasons = evaluate_dual_review_establishment_from_canonical_legs(summary)
    if established:
        any_issues = any(leg.get("kind") == "issues" for leg in summary.get("legs") or [])
        lines.append(
            "- 判定：双评审成立。"
            + (
                f"按 references/review-merge-policy.md 合并去重后，把 canonical issue 写成 JSON，"
                f"`{invocation} fix prepare <issue.json>…{run_dir_argument}` 一步建好 attempt 并拿到子代理派发参数。"
                if any_issues
                else "两腿均 no_issues → clean path，直接写 handoff。"
            )
        )
        if any_issues:
            lines.append(
                "- 合并处置是 fix prepare 的前置闸：每条 reviewer issue（上面的编号就是处置里的 index，从 1 起）"
                "都要记 adopted / merged_into / dropped（dropped 带 reason_code），经 "
                f"`{invocation} merge write`（决定 JSON 走 stdin）写成 artifacts/merge/merge_dispositions.json；"
                "没写或没覆盖到的 issue_id，fix prepare 会拒绝。全部 dropped 也要写，然后走 clean path。"
            )
    elif status == "blocked_on_reviewer_request":
        lines.append(
            "- 判定：有腿返回 kind: request（待处理终态）。按 Contract Gate 满足或驳回后，"
            f"`{invocation} redispatch-reviewers --review-request-legs-only{run_dir_argument}` 只重派 request 腿"
            "（健康腿结果保留、不重跑）。"
        )
    elif summary.get("needs_last_resort_fallback"):
        lines.append(
            "- 判定：0 个 external reviewer 可用，按 "
            "references/zero-external-reviewer-last-resort-in-harness-fallback.md 兜底。"
        )
    else:
        lines.extend(f"- 不成立原因：{reason}" for reason in not_established_reasons)
        lines.append(
            "- 判定：双评审**不成立**（看 plan_status 与每腿当前 canonical 结果，不看 returncode）。读上面的 stderr 排障，"
            f"处置后 `{invocation} redispatch-reviewers{run_dir_argument}` 对同一 run 重派。"
        )
    return "\n".join(lines)


def render_host_specific_reviewer_wait_instruction(
    invocation: str, wait_mode: str | None = None, *, run_dir: str | Path | None = None
) -> str:
    """「怎么等 reviewer」的 host 分流文案（start / dispatch 回执与 ``rvf wait`` 共用这一份）。"""
    mode = wait_mode or resolve_current_host_reviewer_wait_mode()
    wait_command = f"{invocation} wait{render_explicit_run_dir_argument_when_current_session_unresolvable(run_dir)}"
    if mode == REVIEWER_WAIT_MODE_CLAUDE_BACKGROUND_TASK_COMPLETION_NOTIFICATION:
        return (
            f"用 Bash `run_in_background: true`、`timeout: {CLAUDE_BACKGROUND_BASH_TIMEOUT_MILLISECONDS_FOR_REVIEWER_WAIT}` "
            f"跑一次 `{wait_command}`（不传 timeout 会被 host 按缺省 30 分钟砍掉），然后结束本回合等完成通知"
            "（不要轮询、不要 sleep；等待期间 Stop 不会发续跑提示；通知到达后读它的输出，里面就是合并摘要）。"
        )
    if mode == REVIEWER_WAIT_MODE_CODEX_SINGLE_LONG_EXEC_SESSION_YIELD:
        return (
            f"在 exec 会话里跑一次 `{wait_command}`（它一直阻塞到终态）；拿到 session 后用 write_stdin "
            "空轮询、yield_time_ms 取 300000（Codex 空轮询上限），直到进程退出。"
        )
    return f"`{wait_command}`（单次最多阻塞 {BOUNDED_FOREGROUND_REVIEWER_WAIT_SECONDS:g}s，打印 running 就再调一次）。"


def _host_specific_rerun_wait_instruction(wait_mode: str, run_dir: str | Path | None) -> str:
    return "仍在运行：" + render_host_specific_reviewer_wait_instruction(_rvf_invocation(), wait_mode, run_dir=run_dir)


def default_reviewer_wait_seconds_for_host(wait_mode: str) -> float:
    import dispatch_reviewers  # noqa: PLC0415

    long_wait = dispatch_reviewers.DEFAULT_DISPATCH_TOTAL_TIMEOUT_SECONDS + LONG_REVIEWER_WAIT_MARGIN_SECONDS
    if wait_mode == REVIEWER_WAIT_MODE_CLAUDE_BACKGROUND_TASK_COMPLETION_NOTIFICATION:
        return long_wait
    if wait_mode == REVIEWER_WAIT_MODE_CODEX_SINGLE_LONG_EXEC_SESSION_YIELD:
        return long_wait + CODEX_SINGLE_LONG_EXEC_SESSION_REVIEWER_WAIT_EXTRA_SECONDS
    return BOUNDED_FOREGROUND_REVIEWER_WAIT_SECONDS


def _scope_contract_for(context: ResolvedRvfRunContext) -> Path | None:
    path = context.artifact_path("scope_contract", "inputs/scope.contract.json")
    return path if path is not None and path.is_file() else None


def _raise_system_exit_so_finally_clears_awaiting_marker(signum: int, frame: object) -> None:
    del frame
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def awaiting_dispatched_reviewers_marker_armed_while_waiting(
    *, max_wait_seconds: float, environ: Mapping[str, str] | None = None
) -> Iterator[Path | None]:
    """``rvf wait`` 阻塞期间登记一条 awaiting-dispatched-agent marker，退出时（含异常与被信号终止）清掉。

    Stop 侧的读取方 ``awaiting_dispatched_agent_decision`` 早已存在、排在 in-progress 续跑提示之前，
    只是生产派发路径从不写 marker ⇒ agent 在 Claude 下按协议后台跑 ``rvf wait`` 并结束回合时，Stop
    照发续跑提示把它拉起来（实测 run beb3c652：reengage 2 次 ≈ 4 个请求），正常等待还会把续跑预算
    耗光、真卡死时反而没有提示。这里补上写入方，Stop 侧不加任何新的放行判据。

    只在「host 会在后台进程结束时唤起主 agent」时登记（见
    ``current_host_wakes_main_agent_when_background_work_completes``）：否则静默 = 把 agent 晾住。
    marker 按进程区分（id 带 pid），并发的两个 wait 各清各的。SIGKILL 时 finally 跑不到，由
    TTL = 最长等待 + 余量兜底。登记失败只降级回「会收到续跑提示」，不影响等待本身。

    登记只依赖环境变量、刻意发生在 run 推断**之前**：推断要加载 tracker / decision CLI（冷启动 1s+），
    而 agent 在后台启动 ``rvf wait`` 后紧接着就结束回合，Stop 读 marker 的时刻不能早于登记。"""
    session_id = resolve_current_agent_session_id_from_environment(environ)
    if (
        max_wait_seconds <= 0
        or not session_id
        or not current_host_wakes_main_agent_when_background_work_completes(environ)
    ):
        yield None
        return
    import rvf_awaiting_dispatched_agent_marker  # noqa: PLC0415

    dispatched_agent_id = f"rvf-wait-for-detached-reviewers-pid-{os.getpid()}"
    # 先装信号处理、再登记：反过来的话，两步之间到达的 SIGTERM 会按默认动作直接杀进程、留下 marker。
    previous_signal_handlers: dict[int, Any] = {}
    for termination_signal in REVIEWER_WAIT_TERMINATION_SIGNALS_THAT_MUST_STILL_CLEAR_AWAITING_MARKER:
        try:
            previous_signal_handlers[termination_signal] = signal.signal(
                termination_signal, _raise_system_exit_so_finally_clears_awaiting_marker
            )
        except (OSError, ValueError):  # 非主线程等场景装不上：只剩 TTL 兜底
            continue
    marker_path: Path | None = None
    try:
        try:
            marker_path = rvf_awaiting_dispatched_agent_marker.arm_awaiting_dispatched_agent(
                main_session_id=session_id,
                dispatched_agent_id=dispatched_agent_id,
                dispatcher=REVIEWER_WAIT_AWAITING_MARKER_DISPATCHER,
                description=f"rvf wait（pid {os.getpid()}）等当前 run 的 reviewer 终态",
                repo=None,
                cwd=os.getcwd(),
                ttl_seconds_override=max_wait_seconds + REVIEWER_WAIT_AWAITING_MARKER_TTL_MARGIN_SECONDS,
            )
        except OSError as exc:
            print(f"[rvf wait] 登记 awaiting marker 失败（等待照常进行，只是 Stop 可能照发续跑提示）：{exc}", file=sys.stderr)
        yield marker_path
    finally:
        for termination_signal, previous_handler in previous_signal_handlers.items():
            signal.signal(termination_signal, previous_handler)
        rvf_awaiting_dispatched_agent_marker.clear_awaiting_dispatched_agent(
            main_session_id=session_id, dispatched_agent_id=dispatched_agent_id
        )


def run_results(args: argparse.Namespace, *, wait: bool) -> int:
    wait_mode = resolve_current_host_reviewer_wait_mode()
    max_wait = args.max_wait if wait and args.max_wait is not None else (
        default_reviewer_wait_seconds_for_host(wait_mode) if wait else 0.0
    )
    with awaiting_dispatched_reviewers_marker_armed_while_waiting(max_wait_seconds=max_wait) as awaiting_marker_path:
        return _run_results_with_optional_wait(
            args, wait=wait, wait_mode=wait_mode, max_wait=max_wait, awaiting_marker_path=awaiting_marker_path
        )


def _run_results_with_optional_wait(
    args: argparse.Namespace, *, wait: bool, wait_mode: str, max_wait: float, awaiting_marker_path: Path | None
) -> int:
    context = _require_run_context(args.run_dir, getattr(args, "token", None))
    status_path = _dispatch_status_path_for(context)
    reviewers_directory = _reviewers_directory_for(context)
    assert status_path is not None and reviewers_directory is not None
    dispatch_state: dict[str, Any] | None = None
    if status_path.is_file():
        import dispatch_reviewers  # noqa: PLC0415

        if wait:
            print(
                f"[rvf wait] run={context.run_id} 等待 reviewer 终态（最长 {max_wait:g}s，wait_mode={wait_mode}"
                + ("，已登记 awaiting marker：等待期间 Stop 不发续跑提示" if awaiting_marker_path else "")
                + "）…",
                flush=True,
            )
        dispatch_state = dispatch_reviewers.wait_for_dispatch_status(status_path, max_wait=max_wait)
        if dispatch_state.get("state") != "done":
            if args.json:
                print(json.dumps({"state": "running", "dispatch": dispatch_state}, ensure_ascii=False))
            else:
                print(f"RVF_DISPATCH_STATE=running launch_status={dispatch_state.get('launch_status')}")
                print(f"- {_host_specific_rerun_wait_instruction(wait_mode, context.run_dir)}")
            return EXIT_OK
    elif not (reviewers_directory / REVIEWER_PLAN_FILENAME).is_file():
        print(
            f"[rvf {'wait' if wait else 'results'}] run {context.run_id} 尚未派发 reviewer"
            f"（{status_path} 与 reviewer-plan.json 都不存在）。先 `{_rvf_invocation()} dispatch`。",
            file=sys.stderr,
        )
        return EXIT_CURRENT_RUN_CONTEXT_UNRESOLVED
    summary = summarize_reviewer_legs_from_canonical_results(
        reviewers_directory=reviewers_directory, scope_contract=_scope_contract_for(context)
    )
    if args.json:
        print(json.dumps({"state": "done", "dispatch": dispatch_state, "summary": summary}, ensure_ascii=False))
    else:
        print(render_reviewer_results_summary(summary, dispatch_state=dispatch_state, run_dir=context.run_dir))
    return EXIT_OK


# ---------------------------------------------------------------------------
# redispatch-reviewers：对当前 run 重派（自动带 --rvf-run-dir）
# ---------------------------------------------------------------------------

def build_redispatch_reviewers_argv(
    context: ResolvedRvfRunContext, *, review_request_legs_only: bool, dry_run: bool,
    selection_seed: int | None,
) -> list[str]:
    argv = [sys.executable, str(SCRIPTS_DIRECTORY / "dispatch_reviewers.py")]
    if context.repo:
        argv += ["--repo", context.repo]
    for flag, overlay_key, fallback in (
        ("--review-packet", "review_packet", "review-packet.md"),
        ("--scope-contract", "scope_contract", "inputs/scope.contract.json"),
        ("--session-context", "scope_of_work", "scope-of-work.md"),
    ):
        path = context.artifact_path(overlay_key, fallback)
        if path is not None and path.is_file():
            argv += [flag, str(path)]
    if context.run_id:
        argv += ["--rvf-run-id", context.run_id]
    argv += ["--rvf-run-dir", str(context.run_dir)]
    argv += ["--dry-run"] if dry_run else ["--execute", "--detached"]
    preset = (context.scope_adjustment_overlay or {}).get("dispatched_preset")
    if isinstance(preset, dict) and int(preset.get("external_reviewer_count_target") or 0) >= 2:
        argv.append("--require-external")
    if review_request_legs_only:
        argv.append(REVIEW_REQUEST_REDISPATCH_FLAG)
    if selection_seed is not None:
        argv += ["--selection-seed", str(selection_seed)]
    return argv


def run_redispatch_reviewers(args: argparse.Namespace) -> int:
    context = _require_run_context(args.run_dir)
    argv = build_redispatch_reviewers_argv(
        context, review_request_legs_only=args.review_request_legs_only, dry_run=args.dry_run,
        selection_seed=args.selection_seed,
    )
    print(f"[rvf redispatch-reviewers] run={context.run_id}（{context.resolution_source}）")
    completed = subprocess.run(argv, check=False)
    if completed.returncode == 0 and not args.dry_run:
        print(
            "- 已派发。等待："
            + render_host_specific_reviewer_wait_instruction(_rvf_invocation(), run_dir=context.run_dir)
        )
    return completed.returncode


# ---------------------------------------------------------------------------
# fix prepare / fix stop / apply
# ---------------------------------------------------------------------------

def _scope_hash_for(scope_contract: Path | None) -> str | None:
    contract = _read_json_object(scope_contract) if scope_contract else None
    if not contract:
        return None
    for key in ("scope_hash",):
        value = contract.get(key)
        if isinstance(value, str) and value:
            return value
    return None


FIX_STOP_STATUS_PLACEHOLDER = "<fixed|false_positive|elevated|failed>"


def render_fix_attempt_subagent_stop_command(*, invocation: str, attempt_worktree: str, attempt_id: str, run_dir: str) -> str:
    """子代理收尾命令的完整形态。

    不能靠 cwd 反推 attempt：Claude 子代理每次 Bash 调用都会重置 cwd（2.1.285 本体：「Agent threads
    always have their cwd reset between bash calls」），``rvf fix stop`` 单独跑时 cwd 已不在 attempt
    worktree 里。故一行里自带 ``cd`` 与显式 ``--attempt-id`` / ``--run-dir``，照抄即对。"""
    return (
        f"cd {shlex.quote(attempt_worktree)} && {invocation} fix stop --attempt-id {shlex.quote(attempt_id)} "
        f"--run-dir {shlex.quote(run_dir)} --status {FIX_STOP_STATUS_PLACEHOLDER}"
    )


def _arm_fix_attempt_subagent_awaiting_marker_if_host_wakes_main_agent(
    rvf_fix_attempt: Any, *, run_dir: Path, attempt_id: str
) -> str | None:
    """主会话派后台子代理去修、结束回合等完成通知时，别让 Stop 在这段等待里发续跑提示（H3）。

    只在 host 会在子代理结束时唤起主 agent 时登记；返回 marker 路径（未登记为 None）。登记失败
    只降级回「会收到续跑提示」，绝不让 prepare 失败。"""
    main_agent_session_id = resolve_current_agent_session_id_from_environment()
    if not main_agent_session_id or not current_host_wakes_main_agent_when_background_work_completes():
        return None
    try:
        marker_path = rvf_fix_attempt.arm_fix_attempt_subagent_awaiting_marker(
            run_dir=run_dir, attempt_id=attempt_id, main_agent_session_id=main_agent_session_id
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[rvf fix prepare] attempt {attempt_id} 登记 awaiting marker 失败（不影响派发）：{exc}", file=sys.stderr)
        return None
    return str(marker_path) if marker_path is not None else None


def run_fix_prepare(args: argparse.Namespace) -> int:
    import rvf_fix_attempt  # noqa: PLC0415
    import rvf_fix_issue  # noqa: PLC0415

    context = _require_run_context(args.run_dir)
    run_dir = context.run_dir
    assert run_dir is not None
    repo_value = context.repo
    if not repo_value:
        raise CurrentRunContextUnresolved(f"run {context.run_id} 解析不出目标 repo；显式传 --run-dir 的同时检查 review-env.sh")
    repo = Path(repo_value).expanduser().resolve()
    scope_contract = _scope_contract_for(context)
    scope_hash = _scope_hash_for(scope_contract)
    invocation = _rvf_invocation()
    prepared: list[dict[str, Any]] = []
    failure: str | None = None
    for raw_issue_file in args.issue_files:
        issue_file = Path(raw_issue_file).expanduser().resolve()
        try:
            upserted = rvf_fix_issue.upsert_canonical_issue(run_dir=run_dir, repo=repo, issue_file=issue_file)
            attempt = rvf_fix_attempt.prepare_fix_attempt(
                run_dir=run_dir, repo=repo, issue_id=str(upserted["issue_id"]),
            )
            rvf_fix_attempt.mark_fix_attempt_started(
                run_dir=run_dir, attempt_id=attempt["attempt_id"], repo_override=str(repo),
            )
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            failure = f"{issue_file}: {exc}"
            break
        prepared.append(
            {
                "issue_id": upserted["issue_id"],
                "issue_file": upserted.get("issue_artifact_path"),
                "attempt_id": attempt["attempt_id"],
                "attempt_worktree": attempt["worktree_path"],
                "run_dir": str(run_dir),
                "repo": str(repo),
                "scope_contract": str(scope_contract) if scope_contract else None,
                "scope_hash": scope_hash,
                "fix_allowlist": attempt.get("fix_allowlist") or [],
                "boundary_paths": attempt.get("boundary_paths") or [],
                "fix_stop_command": render_fix_attempt_subagent_stop_command(
                    invocation=invocation, attempt_worktree=str(attempt["worktree_path"]),
                    attempt_id=str(attempt["attempt_id"]), run_dir=str(run_dir),
                ),
                "awaiting_dispatched_agent_marker_path": None,
            }
        )
    # 整批都建好才登记 awaiting marker：中途失败时主会话拿到的是 exit 4、通常不会派发子代理，先前各条若已登记，
    # 残留 marker 会把本会话的 Stop 静默到 TTL、续跑提示发不出。失败批次一条都不登记——即使主会话仍派发了
    # 已建好的 attempt，也只降级回「会收到续跑提示」。
    if failure is None:
        for item in prepared:
            item["awaiting_dispatched_agent_marker_path"] = (
                _arm_fix_attempt_subagent_awaiting_marker_if_host_wakes_main_agent(
                    rvf_fix_attempt, run_dir=run_dir, attempt_id=item["attempt_id"]
                )
            )
    if args.json:
        print(json.dumps({"prepared": prepared, "failure": failure}, ensure_ascii=False))
    else:
        print(
            f"[rvf fix prepare] {len(prepared)}/{len(args.issue_files)} 条 issue 已建好 attempt"
            "（upsert + prepare + start 一步完成，子代理不需要再调 start）"
        )
        for item in prepared:
            print(f"--- 子代理派发参数（issue {item['issue_id']}）---")
            print(f"attempt_id: {item['attempt_id']}")
            print(f"attempt_worktree: {item['attempt_worktree']}   ← 只在这里改文件；每条命令都用绝对路径或先 cd 到这里")
            print(f"issue_file: {item['issue_file']}")
            print(f"run_dir: {item['run_dir']}")
            print(f"repo: {item['repo']}")
            print(f"scope_contract: {item['scope_contract']}")
            print(f"scope_hash: {item['scope_hash']}")
            print(f"fix_allowlist: {', '.join(item['fix_allowlist']) or '（空）'}")
            print(f"收尾（整行照抄，--status 四选一）: `{item['fix_stop_command']}`")
        if prepared:
            print(
                f"子代理全部结束后：`{invocation} apply --all-stopped"
                f"{render_explicit_run_dir_argument_when_current_session_unresolvable(run_dir)}`"
            )
            if any(item["awaiting_dispatched_agent_marker_path"] for item in prepared):
                print("（已按 attempt 登记 awaiting marker：子代理在后台跑、你结束回合等完成通知期间，Stop 不发续跑提示）")
    if failure is not None:
        print(f"[rvf fix prepare] 失败，已停在这一条：{failure}", file=sys.stderr)
        return EXIT_FIX_PREPARE_FAILED
    return EXIT_OK


def run_merge(rest: list[str]) -> int:
    """``rvf merge write|check``：转给 ``rvf_merge_dispositions``，缺 ``--run-dir`` 时补当前 run。"""
    import rvf_merge_dispositions  # noqa: PLC0415

    if not rest or _argv_asks_for_help(rest):
        return int(rvf_merge_dispositions.main(rest or ["--help"]))
    explicit_run_dir = _explicit_option_value_in_argv(rest, "--run-dir")
    injected: list[str] = []
    if explicit_run_dir is None:
        injected = ["--run-dir", str(_require_run_context(None).run_dir)]
    return int(rvf_merge_dispositions.main([rest[0], *injected, *rest[1:]]))


def run_fix_stop(rest: list[str]) -> int:
    import rvf_fix_attempt  # noqa: PLC0415

    return int(rvf_fix_attempt.main(["stop", *rest]))


def _attempt_records(run_dir: Path) -> list[dict[str, Any]]:
    root = run_dir / "artifacts" / "fix-attempts"
    records: list[dict[str, Any]] = []
    try:
        directories = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return records
    for directory in directories:
        attempt = _read_json_object(directory / "attempt.json")
        if attempt is None:
            continue
        created_at = attempt.get("created_at")
        records.append({**attempt, "_created_at_sort_key": created_at if isinstance(created_at, str) else ""})
    records.sort(key=lambda item: item["_created_at_sort_key"])
    return records


FIX_ATTEMPT_STATUSES_READY_TO_APPLY = frozenset({"fixed"})
FIX_ATTEMPT_STATUSES_STOPPED_WITHOUT_PATCH = frozenset({"false_positive", "elevated", "failed"})
FIX_ATTEMPT_STATUSES_NOT_YET_STOPPED = frozenset({"prepared", "started"})
# apply 时写回 attempt.json 的冲突终态：该 attempt 的 patch **未**合回主 worktree。
FIX_ATTEMPT_STATUSES_APPLY_CONFLICT_NOT_MERGED = frozenset({"merge_conflict", "scope_expansion_conflict"})


def find_unresolved_apply_conflict_attempts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回仍未解决的合回冲突 attempt：状态是冲突终态，且同一 issue 之后没有更晚创建的 attempt 取代它。

    冲突终态不会自己消失——``--all-stopped`` 只重选 ``fixed``，若不在每次 apply 里单独报出，
    第二次运行时冲突那条会从输出里静默消失、剩余 fixed 合完后以 0 退出，被误读为「全部合回」。
    ``records`` 须已按创建顺序排序（见 ``_attempt_records``）。"""
    latest_attempt_id_by_issue_id: dict[Any, str] = {}
    for record in records:
        latest_attempt_id_by_issue_id[record.get("issue_id")] = record["attempt_id"]
    return [
        record for record in records
        if record.get("status") in FIX_ATTEMPT_STATUSES_APPLY_CONFLICT_NOT_MERGED
        and latest_attempt_id_by_issue_id.get(record.get("issue_id")) == record["attempt_id"]
    ]


def run_apply(args: argparse.Namespace) -> int:
    import rvf_fix_attempt  # noqa: PLC0415

    context = _require_run_context(args.run_dir)
    run_dir = context.run_dir
    assert run_dir is not None
    records = _attempt_records(run_dir)
    if args.attempt_id:
        wanted = list(dict.fromkeys(args.attempt_id))
        by_id = {record["attempt_id"]: record for record in records}
        missing = [attempt_id for attempt_id in wanted if attempt_id not in by_id]
        if missing:
            print(f"[rvf apply] 找不到 attempt：{', '.join(missing)}", file=sys.stderr)
            return EXIT_CURRENT_RUN_CONTEXT_UNRESOLVED
        selected = [by_id[attempt_id] for attempt_id in wanted]
    else:
        selected = [r for r in records if r.get("status") in FIX_ATTEMPT_STATUSES_READY_TO_APPLY]
    not_ready = [r for r in records if r.get("status") in FIX_ATTEMPT_STATUSES_NOT_YET_STOPPED]
    outcomes: list[dict[str, Any]] = []
    stopped_on_conflict = False
    for record in selected:
        try:
            outcome = rvf_fix_attempt.apply_fix_attempt(
                run_dir=run_dir, attempt_id=record["attempt_id"], target_repo_override=context.repo,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            outcome = {"attempt_id": record["attempt_id"], "status": "apply_error", "stderr": str(exc)}
        outcome["issue_id"] = record.get("issue_id")
        outcomes.append(outcome)
        if outcome["status"] != rvf_fix_attempt.FIX_ATTEMPT_APPLY_STATUS_APPLIED:
            stopped_on_conflict = True
            break
    remaining = [r["attempt_id"] for r in selected[len(outcomes):]]
    records_after_apply = _attempt_records(run_dir)
    # apply = 主会话消费子代理结果的时刻，清掉不再有人在跑的 attempt 的 awaiting marker（正常已在子代理
    # stop 时清过，这里兜住 stop 时清除失败、子代理没调 stop 就死掉等情形）：
    # * ``--all-stopped``：协议规定「全部子代理结束后」才跑它 ⇒ 本 run 的 attempt 一律清，包括没 stop 的
    #   （否则死掉的子代理留下的 marker 会在 TTL 内静默本会话此后每一次 Stop，连新工作的决策点也发不出）；
    # * ``--attempt-id``：可能边收结果边合回 ⇒ 只清已 stop 的，仍在跑的不动。
    for record in records_after_apply:
        if args.all_stopped or record.get("status") not in FIX_ATTEMPT_STATUSES_NOT_YET_STOPPED:
            rvf_fix_attempt.clear_fix_attempt_subagent_awaiting_marker(record)
    # 合回后重读 attempt.json：本次新写的冲突与此前遗留的冲突一并算进「未解决」。
    unresolved_apply_conflicts = [
        {"attempt_id": r["attempt_id"], "issue_id": r.get("issue_id"), "status": r.get("status")}
        for r in find_unresolved_apply_conflict_attempts(records_after_apply)
    ]
    if args.json:
        print(json.dumps({"outcomes": outcomes, "remaining_not_applied": remaining,
                          "attempts_not_stopped": [r["attempt_id"] for r in not_ready],
                          "unresolved_apply_conflict_attempts": unresolved_apply_conflicts}, ensure_ascii=False))
    else:
        print(f"[rvf apply] run={context.run_id} 合回 {sum(1 for o in outcomes if o['status'] == 'applied')}/{len(selected)}")
        for outcome in outcomes:
            line = f"- {outcome['attempt_id']}（issue {outcome.get('issue_id')}）: {outcome['status']}"
            if outcome["status"] != "applied" and outcome.get("stderr"):
                line += f" — {str(outcome['stderr']).strip()[:500]}"
            print(line)
        for record in records:
            if record.get("status") in FIX_ATTEMPT_STATUSES_STOPPED_WITHOUT_PATCH:
                print(f"- {record['attempt_id']}（issue {record.get('issue_id')}）: {record.get('status')}（无 patch，不合回）")
        if not_ready:
            print("- ⚠ 尚未 stop 的 attempt：" + ", ".join(r["attempt_id"] for r in not_ready))
        if stopped_on_conflict:
            print(
                "- 已停在冲突处：这需要你判断。merge_conflict 记为该 attempt 未合并，不得手工搬运后伪装成已归因 patch；"
                f"其余未合回：{', '.join(remaining) or '无'}"
            )
        if unresolved_apply_conflicts:
            print(
                "- ⚠ 未解决的合回冲突（patch 未合回主 worktree；同一 issue 之后也没有新 attempt 取代）："
                + ", ".join(
                    f"{item['attempt_id']}（issue {item['issue_id']}，{item['status']}）"
                    for item in unresolved_apply_conflicts
                )
            )
    # --all-stopped 宣称「全部合回」，只要还有未解决冲突就不得以 0 退出；
    # --attempt-id 的退出码只反映所点名的 attempt，其它冲突仍在上面照常报出。
    if stopped_on_conflict or (args.all_stopped and unresolved_apply_conflicts):
        return EXIT_FIX_ATTEMPT_APPLY_STOPPED_ON_CONFLICT
    return EXIT_OK


# ---------------------------------------------------------------------------
# status / handoff / validate
# ---------------------------------------------------------------------------

def run_status(args: argparse.Namespace) -> int:
    context = _require_run_context(args.run_dir, args.token)
    handoff_path = context.run_dir / "artifacts" / "handoff.md" if context.run_dir else None
    intent_status = context.artifact_path(
        "intent_artifact_status_file", "base-task-intent-reconciliation-status.json"
    )
    status_path = _dispatch_status_path_for(context)
    dispatch_state: dict[str, Any] | None = None
    if status_path is not None and status_path.is_file():
        import dispatch_reviewers  # noqa: PLC0415

        dispatch_state = dispatch_reviewers.wait_for_dispatch_status(status_path, max_wait=0.0)
    attempts = [
        {"attempt_id": r["attempt_id"], "issue_id": r.get("issue_id"), "status": r.get("status")}
        for r in (_attempt_records(context.run_dir) if context.run_dir else [])
    ]
    token_state = _decision_token_state(context.decision_token) if context.decision_token else None
    payload = {
        "run_id": context.run_id,
        "run_dir": str(context.run_dir),
        "repo": context.repo,
        "resolution_source": context.resolution_source,
        "decision_token": context.decision_token,
        "decision_token_state": token_state,
        "dispatch": dispatch_state,
        "fix_attempts": attempts,
        "handoff_path": str(handoff_path) if handoff_path else None,
        "intent_artifact_status_file": str(intent_status) if intent_status else None,
        "validation_records": str(context.run_dir / "artifacts" / "validation-records.json") if context.run_dir else None,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return EXIT_OK
    print(f"[rvf status] run={payload['run_id']}（{payload['resolution_source']}）")
    for key in ("run_dir", "repo", "decision_token", "decision_token_state", "handoff_path",
                "intent_artifact_status_file", "validation_records"):
        if payload.get(key):
            print(f"- {key}: {payload[key]}")
    if dispatch_state:
        print(
            f"- dispatch: state={dispatch_state.get('state')} plan_status={dispatch_state.get('plan_status')} "
            f"有效腿={dispatch_state.get('effective_reviewer_count')}/{dispatch_state.get('reviewer_count')}"
        )
    for attempt in attempts:
        print(f"- attempt {attempt['attempt_id']}（issue {attempt['issue_id']}）: {attempt['status']}")
    return EXIT_OK


def run_handoff(args: argparse.Namespace) -> int:
    context = _require_run_context(args.run_dir)
    handoff_path = context.run_dir / "artifacts" / "handoff.md" if context.run_dir else None
    intent_status = context.artifact_path(
        "intent_artifact_status_file", "base-task-intent-reconciliation-status.json"
    )
    print(f"[rvf handoff] run={context.run_id}")
    print(f"- handoff_path: {handoff_path}")
    print(f"- intent_artifact_status_file: {intent_status}（照抄其 status 填 `## 状态` 的 intent_artifact_status）")
    print("- 模板：references/handoff-template.md；完成后最终回复第一行输出 `RVF_HANDOFF_FILE: <handoff_path>`。")
    return EXIT_OK


def _argv_before_double_dash(argv: Sequence[str]) -> list[str]:
    return list(argv[: argv.index("--")]) if "--" in argv else list(argv)


def _explicit_option_value_in_argv(argv: Sequence[str], option_name: str) -> str | None:
    """取 ``--opt value`` 或 ``--opt=value`` 形式的显式值；未出现返回 ``None``（出现但缺值返回 ``""``）。"""
    for index, item in enumerate(argv):
        if item == option_name:
            return argv[index + 1] if index + 1 < len(argv) else ""
        if item.startswith(option_name + "="):
            return item[len(option_name) + 1:]
    return None


# 与 Stop hook 的 ``is_truthy`` 同一口径。
SUPPRESS_ENVIRONMENT_VALUES_TREATED_AS_SET = frozenset({"1", "true", "yes", "y", "on"})
# 调用方 shell 继承来的 run 目录变量（reviewer 子进程被显式注入、Codex task shell 会 export），值正指向主 run。
# ``validation_result_cache.py`` 自身已只认显式 ``--rvf-run-dir``，但被验证的命令及其下游仍会原样继承它；
# 不写主 run 的那一支从子进程环境里剥掉它，不能只靠「不注入 --rvf-run-dir」。
RVF_RUN_DIRECTORY_ENVIRONMENT_VARIABLE_STRIPPED_WHEN_VALIDATION_STAYS_OFF_MAIN_RUN = "RVF_RUN_DIR"


def validation_must_stay_off_main_rvf_run_reason(
    cwd: Path, environ: Mapping[str, str] | None = None
) -> str | None:
    """``rvf validate`` 这次调用是否**不能**写进主 run 的验证记录；能写返回 None，不能写返回原因。

    主 run 的验证记录是 land 的零执行复用与重放事实源，只该收主会话对主 worktree 的最终验证。
    * cwd 在 fix attempt worktree 里：fixer 验证的是自己的 attempt。若照常按 run 推断，``--repo``
      会取主 worktree（验错对象），记录还写进主 run、land 时被重放。
    * Stop hook 被抑制的子进程（reviewer / fixer / analyze 线程）：它们的命令只是探索性证据。"""
    from rvf_fix_attempt import infer_run_dir_and_attempt_id_from_attempt_worktree_path  # noqa: PLC0415

    if infer_run_dir_and_attempt_id_from_attempt_worktree_path(cwd) is not None:
        return "cwd 在 fix attempt worktree 内"
    environment = environ if environ is not None else os.environ
    for name in SUPPRESS_ENV_NAMES:
        if (environment.get(name) or "").strip().lower() in SUPPRESS_ENVIRONMENT_VALUES_TREATED_AS_SET:
            return f"{name} 已置位（Stop hook 被抑制的子进程）"
    return None


def run_validate(rest: list[str]) -> int:
    """转给 ``validation_result_cache.py run``，缺 ``--repo`` / ``--rvf-run-dir`` 时从当前 run 补上。

    显式参数优先：``--help`` 直接透传；显式 ``--rvf-run-dir`` 用作 run context 解析的依据（只用来补
    ``--repo``）；``--repo`` 与 ``--rvf-run-dir`` 都显式时完全不做推断——干净 shell 里也不会 exit 2。
    ``validation_must_stay_off_main_rvf_run_reason`` 成立时不推断 run：``--repo`` 取 cwd 所在
    worktree、不注入 ``--rvf-run-dir``，并从子进程环境剥掉继承来的 ``RVF_RUN_DIR``。"""
    validation_result_cache_argv_prefix = [
        sys.executable, str(SCRIPTS_DIRECTORY / "validation_result_cache.py"), "run",
    ]
    head = _argv_before_double_dash(rest)
    if _argv_asks_for_help(head):
        return subprocess.run([*validation_result_cache_argv_prefix, *rest], check=False).returncode
    explicit_repo = _explicit_option_value_in_argv(head, "--repo")
    explicit_rvf_run_dir = _explicit_option_value_in_argv(head, "--rvf-run-dir")
    injected: list[str] = []
    validation_result_cache_subprocess_environment: dict[str, str] | None = None  # None = 原样继承
    off_main_run_reason = validation_must_stay_off_main_rvf_run_reason(Path.cwd())
    if off_main_run_reason is not None and explicit_rvf_run_dir is None:
        cwd_worktree = _git_toplevel(Path.cwd())
        if explicit_repo is None and cwd_worktree is not None:
            injected += ["--repo", str(cwd_worktree)]
        validation_result_cache_subprocess_environment = {
            name: value for name, value in os.environ.items()
            if name != RVF_RUN_DIRECTORY_ENVIRONMENT_VARIABLE_STRIPPED_WHEN_VALIDATION_STAYS_OFF_MAIN_RUN
        }
        print(
            f"[rvf validate] {off_main_run_reason}：只验证 cwd 所在 worktree"
            f"（{cwd_worktree or Path.cwd()}），不写主 run 的验证记录（land 不会重放它）。",
            file=sys.stderr,
        )
    elif explicit_repo is None or explicit_rvf_run_dir is None:
        context = _require_run_context(explicit_rvf_run_dir or None)
        if explicit_repo is None and context.repo:
            injected += ["--repo", context.repo]
        if explicit_rvf_run_dir is None:
            injected += ["--rvf-run-dir", str(context.run_dir)]
    argv = [*validation_result_cache_argv_prefix, *injected, *rest]
    return subprocess.run(argv, env=validation_result_cache_subprocess_environment, check=False).returncode


# ---------------------------------------------------------------------------
# land：原样转给 rvf_land.py（prepare / commit），commit 缺 --run-dir 时取本仓最近一次 prepare
# ---------------------------------------------------------------------------

RVF_LAND_SUBCOMMANDS_WITH_RUN_DIRECTORY_BOOKKEEPING = ("prepare", "commit")
# 本仓最近一次 ``rvf land prepare`` 落 plan 的 run 目录，按仓库根 realpath 的 hash 分文件。
RVF_LAND_MOST_RECENT_PREPARE_RUN_DIRECTORY_POINTER_DIRNAME = (
    "rvf-land-most-recent-prepare-run-directory-pointer-by-repository"
)


def rvf_land_most_recent_prepare_run_directory_pointer_path(repo_root: Path) -> Path:
    repository_key = hashlib.sha256(os.fsencode(os.path.realpath(repo_root))).hexdigest()[:32]
    return (
        _load_token_store()._rvf_home()
        / RVF_LAND_MOST_RECENT_PREPARE_RUN_DIRECTORY_POINTER_DIRNAME
        / f"{repository_key}.json"
    )


def _run_directory_reported_by_rvf_land_prepare(stdout: str) -> str | None:
    """从 prepare 的输出里取 run 目录：``--format json`` 读 ``run_dir`` 键，文本形态读 ``run_dir: …`` 行。"""
    try:
        payload = json.loads(stdout)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        value = payload.get("run_dir")
        return value if isinstance(value, str) and value else None
    for line in stdout.splitlines():
        if line.startswith("run_dir: "):
            return line[len("run_dir: "):].strip() or None
    return None


def record_rvf_land_prepare_run_directory_if_plan_written(
    *, repo_root: Path, prepare_stdout: str, plan_not_older_than_epoch_seconds: float
) -> Path | None:
    """prepare 这一次真落了 plan（plan 文件在本次调用开始之后写成、且属于本仓）才记指针。

    prepare 在硬门槛前半段失败时不落 plan；此时不能让指针指向更早那次 prepare 的 run。"""
    import rvf_land  # noqa: PLC0415

    run_dir_text = _run_directory_reported_by_rvf_land_prepare(prepare_stdout)
    if run_dir_text is None:
        return None
    run_dir = Path(run_dir_text).expanduser().resolve()
    plan_path = rvf_land.prepare_plan_artifact_path(run_dir)
    try:
        plan_written_at = plan_path.stat().st_mtime
    except OSError:
        return None
    plan = rvf_land.read_json_object_or_none(plan_path) or {}
    if plan_written_at < plan_not_older_than_epoch_seconds or not _same_path(plan.get("repo_root"), repo_root):
        return None
    pointer_path = rvf_land_most_recent_prepare_run_directory_pointer_path(repo_root)
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = pointer_path.with_suffix(f".{os.getpid()}.tmp")
    temporary_path.write_text(
        json.dumps(
            {"repo_root": str(repo_root), "run_dir": str(run_dir), "prepare_plan_path": str(plan_path),
             "recorded_at": datetime.now(timezone.utc).isoformat()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(temporary_path, pointer_path)
    return pointer_path


def read_rvf_land_most_recent_prepare_run_directory(repo_root: Path) -> Path | None:
    pointer = _read_json_object(rvf_land_most_recent_prepare_run_directory_pointer_path(repo_root)) or {}
    run_dir = pointer.get("run_dir")
    if not isinstance(run_dir, str) or not run_dir or not _same_path(pointer.get("repo_root"), repo_root):
        return None
    return Path(run_dir)


def run_land(rest: list[str]) -> int:
    """``rvf land prepare|commit …`` 原样转给 ``rvf_land.py``（land 逻辑全在那边，这里只做入口）。

    省掉的是两处机械信息：脚本在部署 payload 里的绝对路径，以及 commit 的 ``--run-dir``——prepare 已从
    handoff 推断出 run 并落了 plan，commit 缺省就用本仓最近一次 prepare 的 run（plan 过期与否仍由
    ``rvf_land.py commit`` 自己核验）。"""
    rvf_land_argv_prefix = [sys.executable, str(SCRIPTS_DIRECTORY / "rvf_land.py")]
    if not rest or rest[0] not in RVF_LAND_SUBCOMMANDS_WITH_RUN_DIRECTORY_BOOKKEEPING or _argv_asks_for_help(rest[1:]):
        return subprocess.run([*rvf_land_argv_prefix, *rest], check=False).returncode
    action, action_rest = rest[0], rest[1:]
    explicit_repo = _explicit_option_value_in_argv(action_rest, "--repo")
    repo_root = _git_toplevel(Path(explicit_repo).expanduser() if explicit_repo else Path.cwd())
    if action == "prepare":
        prepare_started_at = time.time()
        completed = subprocess.run(
            [*rvf_land_argv_prefix, "prepare", *action_rest], stdout=subprocess.PIPE, text=True, check=False
        )
        sys.stdout.write(completed.stdout)
        sys.stdout.flush()
        if repo_root is not None:
            try:
                record_rvf_land_prepare_run_directory_if_plan_written(
                    repo_root=repo_root, prepare_stdout=completed.stdout,
                    plan_not_older_than_epoch_seconds=prepare_started_at - 1.0,
                )
            except OSError as exc:
                print(f"[rvf land prepare] 记录 run 目录失败（commit 时显式传 --run-dir 即可）：{exc}", file=sys.stderr)
        return completed.returncode
    if _explicit_option_value_in_argv(action_rest, "--run-dir") is None:
        run_dir = read_rvf_land_most_recent_prepare_run_directory(repo_root) if repo_root is not None else None
        if run_dir is None:
            print(
                f"[rvf land commit] 找不到本仓（{repo_root or Path.cwd()}）最近一次 `rvf land prepare` 落下的 run；"
                "先跑 prepare，或显式传 --run-dir。",
                file=sys.stderr,
            )
            return EXIT_CURRENT_RUN_CONTEXT_UNRESOLVED
        print(f"[rvf land commit] run_dir={run_dir}（取自本仓最近一次 rvf land prepare）", file=sys.stderr)
        action_rest = ["--run-dir", str(run_dir), *action_rest]
    return subprocess.run([*rvf_land_argv_prefix, "commit", *action_rest], check=False).returncode


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

DECISION_PASSTHROUGH_SUBCOMMANDS = ("start", "scope", "dispatch", "decline", "decline-dispatched-review")
RAW_PASSTHROUGH_SUBCOMMANDS = ("validate", "land")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rvf",
        description=(
            "RVF 执行段快捷入口。token / run 目录全部可推断。决策：start / scope / dispatch / decline / "
            "decline-dispatched-review（参数同 rvf_agent_decision_cli，--token 可省）；执行：wait / results / "
            "redispatch-reviewers / merge write|check / fix prepare|stop / apply / status / validate / handoff。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in DECISION_PASSTHROUGH_SUBCOMMANDS:
        sub.add_parser(name, add_help=False, help=f"转给 rvf_agent_decision_cli {name}" + ("（--token 可省）" if name != "decline-dispatched-review" else ""))

    for name, help_text in (
        ("wait", "等 reviewer 终态并打印合并摘要（按 host 选默认等待时长）"),
        ("results", "不等待，直接打印 reviewer 合并摘要"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--run-dir", default=None)
        p.add_argument("--token", default=None)
        p.add_argument("--json", action="store_true")
        if name == "wait":
            p.add_argument("--max-wait", type=float, default=None, help="覆盖按 host 推导的默认等待秒数")

    p_redispatch = sub.add_parser("redispatch-reviewers", help="对当前 run 重派 reviewer（自动带 --rvf-run-dir）")
    p_redispatch.add_argument("--run-dir", default=None)
    p_redispatch.add_argument(
        "--review-request-legs-only", action="store_true",
        help=f"只重派停在 kind: request 的腿（= dispatch_reviewers {REVIEW_REQUEST_REDISPATCH_FLAG}）",
    )
    p_redispatch.add_argument("--dry-run", action="store_true")
    p_redispatch.add_argument("--selection-seed", type=int, default=None)

    p_fix = sub.add_parser("fix", help="validate/fix attempt：prepare（主会话）/ stop（子代理）")
    fix_sub = p_fix.add_subparsers(dest="fix_action", required=True)
    p_fix_prepare = fix_sub.add_parser("prepare", help="upsert + prepare + start，一步建好 attempt")
    p_fix_prepare.add_argument("issue_files", nargs="+", help="canonical issue JSON（可多个）")
    p_fix_prepare.add_argument("--run-dir", default=None)
    p_fix_prepare.add_argument("--json", action="store_true")
    fix_sub.add_parser("stop", add_help=False, help="转给 rvf_fix_attempt stop（照抄 fix prepare 给的整行命令：cd + --attempt-id + --run-dir）")

    sub.add_parser(
        "merge", add_help=False,
        help="合并阶段处置：merge write（决定 JSON 走 stdin）/ merge check（自动补 --run-dir）",
    )

    p_apply = sub.add_parser("apply", help="把已 stop 的 fixed attempt 合回主 worktree")
    group = p_apply.add_mutually_exclusive_group(required=True)
    group.add_argument("--all-stopped", action="store_true", help="按创建顺序合回全部 fixed attempt，遇冲突停下")
    group.add_argument("--attempt-id", action="append", default=None)
    p_apply.add_argument("--run-dir", default=None)
    p_apply.add_argument("--json", action="store_true")

    p_status = sub.add_parser("status", help="当前 run 的上下文、派发与 attempt 状态")
    p_status.add_argument("--run-dir", default=None)
    p_status.add_argument("--token", default=None)
    p_status.add_argument("--json", action="store_true")

    p_handoff = sub.add_parser("handoff", help="handoff 落点与契约")
    p_handoff.add_argument("--run-dir", default=None)

    sub.add_parser(
        "validate", add_help=False,
        help="validation_result_cache.py run（自动补 --repo / --rvf-run-dir）：rvf validate --name <n> -- <argv…>",
    )
    sub.add_parser(
        "land", add_help=False,
        help="rvf_land.py prepare|commit 透传（commit 缺 --run-dir 时取本仓最近一次 prepare 的 run）",
    )
    return parser


RAW_PASSTHROUGH_SUBCOMMAND_RUNNERS: dict[str, Callable[[list[str]], int]] = {
    "validate": run_validate,
    "land": run_land,
}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_argument_parser()
    if arguments and arguments[0] in DECISION_PASSTHROUGH_SUBCOMMANDS:
        return run_decision_subcommand(arguments[0], arguments[1:])
    if arguments and arguments[0] in RAW_PASSTHROUGH_SUBCOMMANDS:
        runner = RAW_PASSTHROUGH_SUBCOMMAND_RUNNERS[arguments[0]]
        try:
            return runner(arguments[1:])
        except CurrentRunContextUnresolved as exc:
            print(f"[rvf {arguments[0]}] {exc}", file=sys.stderr)
            return EXIT_CURRENT_RUN_CONTEXT_UNRESOLVED
    if len(arguments) >= 2 and arguments[0] == "fix" and arguments[1] == "stop":
        return run_fix_stop(arguments[2:])
    if arguments and arguments[0] == "merge":
        try:
            return run_merge(arguments[1:])
        except CurrentRunContextUnresolved as exc:
            print(f"[rvf merge] {exc}", file=sys.stderr)
            return EXIT_CURRENT_RUN_CONTEXT_UNRESOLVED
    args = parser.parse_args(arguments)
    try:
        if args.command == "wait":
            return run_results(args, wait=True)
        if args.command == "results":
            return run_results(args, wait=False)
        if args.command == "redispatch-reviewers":
            return run_redispatch_reviewers(args)
        if args.command == "fix":
            return run_fix_prepare(args)
        if args.command == "apply":
            return run_apply(args)
        if args.command == "status":
            return run_status(args)
        if args.command == "handoff":
            return run_handoff(args)
    except CurrentRunContextUnresolved as exc:
        print(f"[rvf {args.command}] {exc}", file=sys.stderr)
        return EXIT_CURRENT_RUN_CONTEXT_UNRESOLVED
    parser.error(f"未知子命令 {args.command!r}")
    return EXIT_CURRENT_RUN_CONTEXT_UNRESOLVED


if __name__ == "__main__":
    raise SystemExit(main())
