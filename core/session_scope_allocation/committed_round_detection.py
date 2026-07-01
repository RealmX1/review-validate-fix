"""committed-round 窗口下界检测——host-中性核（S10h-b，从引擎 SESSION_ALLOC 簇迁入）。

给定 repo 路径 + task/session 身份，解析「本轮已提交但未审」改动的窗口下界 baseline：
优先 review-highwater（最近一次完成 review 覆盖到的 HEAD，仅当其为当前 HEAD 祖先时采用），
仅当从未完成过 review 时回退 round-baseline 作 bootstrap；并解析 committed-round 的 base
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
    try:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0


def _resolve_committed_round_base_ref(repo_path: Path) -> str | None:
    """committed-round 的 base 分支 ref——把「被 rebase / cherry-pick 压平到本轮第一父链
    的 base 工作」排除出可审窗口（见 ``diff_tracker._base_ref_exclude_revs`` /
    ``_list_committed_round_changed_paths``）。

    返回的是**活的 base 分支引用**（如 ``main``），不是 bootstrap 冻结的 base SHA：
    ``base-branch-sync`` 把 main 顶到 worktree 创建之后才追加的 base 提交（如 monthly-deck）
    重放进第一父链，只有活引用才能覆盖到它们——冻结 SHA 是这些提交的祖先，``^SHA`` 排不掉。

    优先级：① 环境变量 ``CODEX_RVF_COMMITTED_ROUND_BASE_REF`` 显式覆盖（逃生口 / 测试注入）；
    ② 本地 ``main`` 分支（base-branch-sync 默认 base，两起实测过触发用的都是它）；③ None。
    HEAD 在 base 上/之下时绝不排除（否则会减掉会话自己的提交）的发散守卫，统一兜底在
    diff_tracker 侧，故这里只做纯引用选择。base 是非-main 的特性分支栈式 task 是已记录的残留
    （main-only 仍严格优于今天的「rebase 路径完全不排除」）。同一 hook 调用内 detection 与
    allocation 都调本函数、入参只有 repo_path，天然同源（无 ac042be 式不同源风险）。"""
    override = os.environ.get(COMMITTED_ROUND_BASE_REF_ENV, "").strip()
    if override:
        return override
    if _local_branch_exists(repo_path, "main"):
        return "main"
    return None


def _resolve_committed_round_baseline(
    repo_path: Path,
    task_id: str | None,
    session_id: str | None,
) -> str | None:
    """committed-round 窗口下界：优先「最近一次完成 review 覆盖到的 HEAD」高水位
    (``review_highwater_marker``，仅当其为当前 HEAD 祖先时采用），仅当某 task/session
    从未完成过 review（无高水位）时才回退 round-baseline（上一条 prompt 的 HEAD）作 bootstrap。

    这样『在某轮提交、却没在该轮自己的 Stop 里被审』的孤儿提交不会因后续任意 prompt 推进
    round-baseline 而落出窗口——它们留在 ``high-water..HEAD`` 内直到真被审，修掉
    "since last user prompt" 语义下的第三面漏审盲区（见 ``review_highwater_marker`` 模块头）。
    best-effort：高水位读取异常时静默回退 round-baseline。
    """
    try:
        highwater = review_highwater_marker.resolve_review_highwater_head(
            task_id=task_id,
            session_id=session_id,
        )
    except Exception:  # pragma: no cover - 高水位读取永不阻断
        highwater = None
    if highwater and _commit_is_ancestor_of_head(repo_path, highwater):
        return highwater
    return resolve_round_baseline_head(task_id=task_id, session_id=session_id)
