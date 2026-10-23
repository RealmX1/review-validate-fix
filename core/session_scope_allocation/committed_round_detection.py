"""committed-round 窗口下界检测——host-中性核（S10h-b，从引擎 SESSION_ALLOC 簇迁入）。

给定 repo 路径 + task/session 身份，解析「本轮已提交但未审」改动的窗口下界 baseline：
优先 review-highwater（最近一次完成 review 覆盖到的 HEAD，仅当其为当前 HEAD 祖先时采用），
仅当从未完成过 review 时走 bootstrap（Kanban task 内取 round-baseline 与 task 分支点中更早者，
其余取 round-baseline）；并解析 committed-round 的 base
分支 ref（把被 rebase/cherry-pick 压平到第一父链的 base 工作排除出可审窗口）。

子进程仅 spawn ``git``（host-中性版本控制原语，非 host agent 二进制），满足 ``core/``
host-free 不变量（R-1 精炼守卫）。高水位 / round-baseline 经已迁 core 的
``review_highwater_marker`` / ``round_baseline_marker`` 读取。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from core.session_scope_allocation import review_highwater_marker
from core.session_scope_allocation.round_baseline_marker import resolve_round_baseline_head


def _commit_is_ancestor_of_head(repo_path: Path, commit: str) -> bool:
    """``commit`` 是否为当前 HEAD 的祖先（含等于 HEAD）。best-effort：任何失败返回 False。

    high-water 仅在通过本判定时才被 committed-round 采纳为窗口下界——分支被 reset/rebase
    到 high-water 之前时，旧 high-water 不再是 HEAD 祖先，``baseline..HEAD`` 会退化，故视为
    失效、回退 round-baseline。
    """
    if not commit:
        return False
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0


COMMITTED_ROUND_BASE_REF_ENV = "CODEX_RVF_COMMITTED_ROUND_BASE_REF"


def _local_branch_exists(repo_path: Path, branch: str) -> bool:
    """``refs/heads/<branch>`` 是否存在（best-effort，任何失败返回 False）。"""
    local_ref = branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"
    try:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", local_ref],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0


def resolve_local_review_base_branch_ref(
    repo_path: Path,
    *,
    explicit_local_base_ref: str | None = None,
    committed_base_ref: str | None = None,
) -> str | None:
    """候选登记与 prepare 共用本地 base 选择；远端默认分支只用于选择已有本地分支。"""
    if explicit_local_base_ref:
        return explicit_local_base_ref.strip()
    if committed_base_ref:
        candidate = committed_base_ref.strip().lstrip("^")
        if _local_branch_exists(repo_path, candidate):
            return candidate
    override = os.environ.get(COMMITTED_ROUND_BASE_REF_ENV, "").strip()
    if override:
        return override
    origin_head = _git_single_line_output(repo_path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    prefix = "refs/remotes/origin/"
    if origin_head and origin_head.startswith(prefix):
        candidate = origin_head[len(prefix):]
        if candidate and _local_branch_exists(repo_path, candidate):
            return candidate
    if _local_branch_exists(repo_path, "main"):
        return "main"
    return None


def _resolve_committed_round_base_ref(repo_path: Path) -> str | None:
    """解析 committed-round 的活 base ref；开审后由 receipt 固定其 OID。"""
    return resolve_local_review_base_branch_ref(repo_path)


def _git_single_line_output(repo_path: Path, *args: str) -> str | None:
    """跑一条只读 git 命令，成功且输出非空时返回去掉首尾空白的输出；任何失败返回 None。"""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def _first_commit_is_ancestor_of_second(repo_path: Path, first: str, second: str) -> bool:
    """``first`` 是否为 ``second`` 的祖先（含相等）。best-effort：任何失败返回 False。"""
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", first, second],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0


def frozen_committed_round_base_exclusion_ref(
    repo_path: Path, base_ref: str | None, fixed_base_tip: str | None,
) -> str | None:
    """固定 base 排除范围；直接在 base 分支工作时保留本会话自己的提交。"""
    if not fixed_base_tip:
        return None
    head_ref = _git_single_line_output(repo_path, "symbolic-ref", "--quiet", "HEAD")
    base_branch_ref = (
        _git_single_line_output(repo_path, "rev-parse", "--symbolic-full-name", base_ref)
        if base_ref else None
    )
    if head_ref and head_ref == base_branch_ref:
        return None
    return fixed_base_tip


def _resolve_kanban_task_branch_point_from_committed_round_base_ref(repo_path: Path) -> str | None:
    """Kanban task 分支从 base 分支分出的那一点：``merge-base(<base ref>, HEAD)``。

    base ref 与 committed-round 的 ``^base`` 排除同源（``_resolve_committed_round_base_ref``）；解不出
    （仓里没有 ``main`` 且未设覆盖变量）或 git 失败 → None，调用方退回今天的 round-baseline。
    """
    base_ref = _resolve_committed_round_base_ref(repo_path)
    if not base_ref:
        return None
    return _git_single_line_output(repo_path, "merge-base", base_ref, "HEAD")


def _resolve_committed_round_baseline(
    repo_path: Path,
    task_id: str | None,
    session_id: str | None,
) -> str | None:
    """committed-round 窗口下界：优先「最近一次完成 review 覆盖到的 HEAD」高水位
    (``review_highwater_marker``，仅当其为当前 HEAD 祖先时采用）；某 task/session 从未完成过
    review（无高水位）时走 bootstrap。

    这样『在某轮提交、却没在该轮自己的 Stop 里被审』的孤儿提交不会因后续任意 prompt 推进
    round-baseline 而落出窗口——它们留在 ``high-water..HEAD`` 内直到真被审，修掉
    "since last user prompt" 语义下的第三面漏审盲区（见 ``review_highwater_marker`` 模块头）。

    **bootstrap（无高水位）**：
      * Kanban task 上下文（``task_id`` 解析得出）：取 round-baseline（上一条 prompt 的 HEAD）与
        task 分支点 ``merge-base(<base ref>, HEAD)`` 中**更早**的那个。round-baseline 会被每条 prompt
        顶到当前 HEAD，task 首轮从未审过时，被顶过去的已提交工作就永久落出窗口（实例：会话 893079be、
        worktree 01457，35 个提交、约 1.57 万行非测试代码从未经过 RVF——被谢绝 / 延后、或唯一看见它们的
        Stop 被熔断挡住，随后一条 prompt 就把它们推出了窗口）。分支点是稳定的：在第一次 review 完成
        之前，task 分支上的提交都留在窗口里。base 工作的排除仍由 ``^<base ref>`` 负责，故不会把 base
        上的提交算成本 task 的。
      * 其它上下文：round-baseline（不变）。

    EXPERIMENT_NODE: 分支点 bootstrap 只在 Kanban task 上下文启用——task 分支每个 task 独立、分支点之后的提交都属于本 task；非 task 会话可能停在长期特性分支上，旧提交在别的会话里审过但合并 diff 的 canonical hash 对不上，会被整条重派。备选是对所有会话启用，或改用「task 创建时记录的起点」代替 merge-base。

    best-effort：高水位 / 分支点读取异常时静默回退 round-baseline。
    """
    try:
        highwater = review_highwater_marker.resolve_review_highwater_head(
            task_id=task_id,
            session_id=session_id,
            repo=repo_path,
        )
    except Exception:  # pragma: no cover - 高水位读取永不阻断
        highwater = None
    if highwater and _commit_is_ancestor_of_head(repo_path, highwater):
        return highwater
    round_baseline = resolve_round_baseline_head(
        task_id=task_id, session_id=session_id, repo=repo_path
    )
    if not task_id:
        return round_baseline
    kanban_task_branch_point = _resolve_kanban_task_branch_point_from_committed_round_base_ref(
        repo_path
    )
    if not kanban_task_branch_point:
        return round_baseline
    if not round_baseline:
        return kanban_task_branch_point
    # 只在分支点确实更早（是 round-baseline 的祖先）时替换；两者无祖先关系（round-baseline 来自被
    # 改写过的历史）时保持今天的行为。
    if _first_commit_is_ancestor_of_second(repo_path, kanban_task_branch_point, round_baseline):
        return kanban_task_branch_point
    return round_baseline
