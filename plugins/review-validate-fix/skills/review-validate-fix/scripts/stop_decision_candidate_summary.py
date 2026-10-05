#!/usr/bin/env python3
"""Stop hook 侧**轻量**候选摘要生成器（S2 / R1 / R18 / R25）。

RVF overhaul 把「要不要审、审什么」的发起决策权交给主 agent。Stop hook 降级为决策点信使：
它在**推送**给 agent 的注入 prompt 里带一份「候选摘要」（R18：push 不 pull，不存在
``candidates`` 子命令），主 agent 据此简析后 ``start`` / ``decline``。

本模块生成那份摘要，遵循以下铁律：

* **轻量**（R1 / R18）：只用 git + tracker sqlite 缓存，**绝不**做重量级 transcript 解析
  （不调 ``build_manifest`` / ``ownership_baseline``）。ownership 走 tracker 缓存的 best-effort
  读取，读不到就标 ``unattributed``（默认纳入）。
* **候选全集程序化构造**（R1）：本 worktree「自上次 review 以来」的 committed ∪ uncommitted
  改动，先按审查范围排除前缀（``review_scope_excluded_path_prefixes``：内置工具托管目录如
  ``.serena/`` ∪ ``.review-validate-fix-ignore``）过滤，再按 ownership 过滤。agent 不枚举全集。
* **必含 committed-but-unreviewed**（committed-round 漏审盲区家族教训）：committed 分支的**窗口
  种子**用 committed-round 的 SHA 同源 resolver（种子 = review 高水位 / round-baseline），因此
  ``种子..工作树`` 里「已提交但未审」的孤儿提交先进枚举窗口，再由 review_state 过滤掉其中已审的。
* **真相源分期**（S2/S6 决策，防 S2/S6 双源撕裂）—— **S6 已落地（方案 A，本 commit）**：
  * **窗口种子（bootstrap）** 仍复用现有 SHA 同源 resolver
    （``committed_round_detection._resolve_committed_round_baseline`` 家族 + ``review_highwater_marker``），
    **不删码**（R9 登记簿：SHA marker 家族 reroute 不删码，观察期后用户确认才物理删除）。
  * **「已审/未审」裁决权** 改由 tracker unit ``review_state`` 承担：committed 枚举后经
    ``reviewable_unit_diff_tracker.select_committed_paths_reviewed_by_session``（owner 经 sessions 表
    session_units 归属）过滤掉已审 committed 路径。detection / allocation / summary 三处同一 commit
    原子切换（allocation 的单元选择本就 ``review_state IN ('available','assigned')``，天然同源）。
    ``lower_bound_source`` 字段口径随之从窗口种子 SHA（``review_highwater``/``round_baseline``）改为
    「已审裁决权威」（``tracker_review_state``/``none``）；窗口种子来源另置
    ``committed_round_window_seed_source`` 字段透明化。
  * **R9 终局物理删除 SHA marker 前，须先把 reviewed_head 持久化进 tracker（方案 B 写侧）**，届时
    marker 才能卸下「窗口种子」角色——见 ``select_committed_paths_reviewed_by_session`` 函数头。
  * 这些 resolver / 过滤器现住在**中性核**（``committed_round_detection`` S10h-b 迁 core、
    ``reviewable_unit_diff_tracker`` 本就在 core），与 Stop hook 引擎解耦，故**直接 top-level import**，
    不涉 S4 环。
* **先过 workspace 守卫**：Cline Kanban task 的 Stop event，若 cwd/git-root 与该 task 的执行
  worktree 不一致，直接短路（不审 unrelated dirty repo）。默认走 Stop hook 引擎模块
  ``stop_review_validate_fix`` 的 ``kanban_task_workspace_guard_payload``（**惰性 import** 破 S4
  环——该守卫仍在引擎侧），非 kanban 场景该守卫立即返回 None，
  开销可忽略。

## 候选集指纹（R25）—— 本模块是唯一权威

指纹用于 declined 冷却（R25：``decline 后指纹未变不再注入``）与注入幂等去重键的一部分。
算法在本模块实现并作为**唯一权威**；S1 token store 只把它当**不透明字符串**存取，绝不自行解析
或重算。序列化格式（``compute_candidate_fingerprint``，版本
``CANDIDATE_FINGERPRINT_ALGORITHM_VERSION``）：

  1. 取**纳入候选集**（未被排除前缀或 ownership 过滤）的每个路径及其**当前内容摘要**
     ``content_sha256``（工作树文件字节的 sha256 十六进制；文件不存在/已删除记字面量
     ``"absent"``）。
  2. 按路径的 **UTF-8 字节序升序**排序（路径唯一，无需去重歧义）。
  3. 序列化：``DOMAIN_TAG`` + ``b"\\x1e"``（记录分隔符）+ 依次拼接每条
     ``path.encode("utf-8") + b"\\x00" + content_hash.encode("ascii") + b"\\x00"``。
     NUL（``\\x00``）在路径、十六进制摘要、``"absent"`` 中都**不可能**出现，故拼接**分隔符安全**、
     无歧义。
  4. 对整段字节取 ``sha256``，返回 ``"sha256:" + hexdigest``。

空候选集 → 仅 ``DOMAIN_TAG`` + 记录分隔符的 sha256（稳定值）。指纹**只看内容**（路径集 + 各
文件内容摘要），**不含** committed/uncommitted 状态：内容相同时「dirty → committed」这种纯提交
状态迁移不改变指纹（与 tracker 的 canonical-hash 去重哲学一致——相同可审内容即同一次审查）。

## 工作范围指纹（declined 会话级 snooze 用）—— 与内容指纹**刻意分开**

内容指纹逐字节敏感：工作还在进行时，两次 Stop 之间几乎总有字节改动，于是「指纹未变不再注入」的
declined 冷却在「边做边停」的会话里形同虚设（实测同一会话 8 天内重复谢绝 12 次）。因此另算一枚
**粗粒度**的 ``candidate_scope_fingerprint``（``compute_candidate_scope_fingerprint``，版本
``CANDIDATE_SCOPE_FINGERPRINT_ALGORITHM_VERSION``）：只看「纳入候选集的路径集合 +
committed_round_baseline + 工作树 HEAD」，**不看**文件内容。它连同判定 snooze 解除所需的原始量
（HEAD、路径集合、未审行数估计）一起放进摘要的 ``candidate_scope_identity_for_declined_session_snooze``
子字典，由 token store 的闸 2b 消费。内容指纹的算法与语义保持不变。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — 把 pyroot 上 sys.path，供 core.* import

import review_strictness_reference_calculator as strictness_calculator  # noqa: E402
from review_scope_excluded_path_prefixes import (  # noqa: E402
    BUILTIN_DEVELOPER_TOOL_MANAGED_PATH_PREFIXES_EXCLUDED_FROM_REVIEW_SCOPE,
    load_review_scope_excluded_path_prefixes,
    path_matches_review_scope_excluded_path_prefix,
)
from core.session_scope_allocation import review_highwater_marker  # noqa: E402
from core.session_scope_allocation.committed_round_detection import (  # noqa: E402 — committed-round 窗口下界检测「中性核」（S10h-b 已迁 core，内部消费 core 的 round_baseline/review_highwater marker）；直接 top-level import，无需惰性、无 S4 环
    _resolve_committed_round_base_ref,
    _resolve_committed_round_baseline,
)
from core.session_scope_allocation.session_change_manifest import dirty_paths  # noqa: E402
from core.session_scope_allocation.layered_session_write_attribution_for_unattributed_dirty_paths import (  # noqa: E402
    WEAK_SESSION_WRITE_ATTRIBUTION_EVIDENCE_LABELS,
)
from core.session_scope_allocation.reviewable_unit_diff_tracker import (  # noqa: E402
    SQLITE_FILENAME,
    _disabled as _tracker_disabled,
    _list_committed_round_changed_paths,
    _worktree_key,
    git_common_dir,
    repo_key,
    select_committed_paths_reviewed_by_session,
    select_dirty_path_reviewed_change_by_session,
    tracker_dir,
)
from core.run_ledger.run_ledger import log_root  # noqa: E402


SUMMARY_SCHEMA_VERSION = 1
CANDIDATE_FINGERPRINT_ALGORITHM_VERSION = 1
CANDIDATE_FINGERPRINT_DOMAIN_TAG = b"rvf-agentic-initiation-candidate-fingerprint-v1"
CANDIDATE_FINGERPRINT_ABSENT_SENTINEL = "absent"
CANDIDATE_SCOPE_FINGERPRINT_ALGORITHM_VERSION = 1
CANDIDATE_SCOPE_FINGERPRINT_DOMAIN_TAG = b"rvf-agentic-initiation-candidate-scope-fingerprint-v1"

# change_state 取值。
CHANGE_STATE_COMMITTED = "committed"
CHANGE_STATE_UNCOMMITTED = "uncommitted"
CHANGE_STATE_COMMITTED_AND_UNCOMMITTED = "committed_and_uncommitted"

# ownership 取值（best-effort tracker 缓存读取）。
OWNERSHIP_SESSION_OWNED = "session_owned"
OWNERSHIP_OTHER_SESSION_OWNED = "other_session_owned"
OWNERSHIP_UNATTRIBUTED = "unattributed"

# ``candidate_set_exclusion_reason``：路径为什么没进候选集（纳入时为 None）。被排除的路径仍留在
# ``candidates`` 清单里（``included_in_candidate_set=False``），便于在 run-logs 里追溯。
# 审查范围排除前缀先于 ownership 判定：命中前缀的路径无论归谁都不该审。
CANDIDATE_SET_EXCLUSION_REASON_REVIEW_SCOPE_EXCLUDED_PATH_PREFIX = "review_scope_excluded_path_prefix"
CANDIDATE_SET_EXCLUSION_REASON_OTHER_SESSION_OWNED = "other_session_owned"

# ``lower_bound_source``（R9 真相源迁移后=「已审/未审」**裁决权威**，非窗口种子 SHA 来源）：
# review_state 是 committed 候选「是否已审」的真相源。有 committed 窗口种子时=tracker_review_state，
# 无种子时=none。口径切换与 detection/allocation 同一 commit 原子完成（S6）。
LOWER_BOUND_SOURCE_TRACKER_REVIEW_STATE = "tracker_review_state"
LOWER_BOUND_SOURCE_NONE = "none"

# ``committed_round_window_seed_source``：committed-round git 窗口下界**种子**（bootstrap）来源。
# R9 观察期内，窗口种子仍由 SHA marker 家族（review 高水位 / round-baseline）提供（见模块 docstring
# 「真相源分期」），review_state 只接管「已审」裁决、不接管窗口种子。R9 终局物理删除 marker 前须先把
# reviewed_head 持久化进 tracker（方案 B 写侧），marker 才能卸下窗口种子角色。
COMMITTED_ROUND_WINDOW_SEED_REVIEW_HIGHWATER = "review_highwater"
COMMITTED_ROUND_WINDOW_SEED_ROUND_BASELINE = "round_baseline"
COMMITTED_ROUND_WINDOW_SEED_NONE = "none"

# ``unreviewed_line_estimate.estimate_basis``：「已审行扣除」这一步的**可信度自陈**。横幅按它决定
# 是渲染「其中约 N 行已审」还是诚实地说「本轮拿不到可信的已审估算，下列规模为全量」。
#
# ⚠️ 这里没有、也不该有一个「静默归零」的取值：扣不掉就是扣不掉，宁可把规模报大（多审一次），
# 也不能让一个失效的扣减看起来像成功扣减——那正是第二代假绿的长相。
ESTIMATE_BASIS_HUNK_FILTERED = "hunk_filtered"                    # 已审行全部对上并扣除
ESTIMATE_BASIS_PARTIAL_HUNK_FILTERED = "partial_hunk_filtered"    # 部分对上（工作树在 review 后又动过）
ESTIMATE_BASIS_NO_REVIEWED_UNITS = "no_reviewed_units"            # tracker 有观测，但本会话名下无已审单元
ESTIMATE_BASIS_NO_TRACKER_OBSERVATION = "no_tracker_observation"  # tracker 无观测（禁用 / 无 DB / 异常）
ESTIMATE_BASIS_STALE_TRACKER_OBSERVATION = "stale_tracker_observation"  # 声称有已审行，却一行都对不上
ESTIMATE_BASIS_WHOLE_PATH_REVIEWED = "whole_path_reviewed"        # 整路径身份单元已审（新增/删除/二进制）
# 整路径身份单元已审，**但**本路径的 delta 跨过了 committed 层 → 扣减覆盖面（仅 dirty）窄于被扣对象
# （baseline→工作树的 committed+dirty 合并 delta），整份回退全量。详见 ``_estimate_unreviewed_lines_for_path``。
ESTIMATE_BASIS_WHOLE_PATH_REVIEWED_SPANS_UNREVIEWED_COMMITTED = (
    "whole_path_reviewed_spans_unreviewed_committed_delta"
)

# 聚合优先级：越靠前越「值得报警」。任一路径 stale → 整份摘要报 stale（诚实告警优先于好看的数字）。
_ESTIMATE_BASIS_AGGREGATION_ORDER = (
    ESTIMATE_BASIS_STALE_TRACKER_OBSERVATION,
    ESTIMATE_BASIS_WHOLE_PATH_REVIEWED_SPANS_UNREVIEWED_COMMITTED,
    ESTIMATE_BASIS_PARTIAL_HUNK_FILTERED,
    ESTIMATE_BASIS_HUNK_FILTERED,
    ESTIMATE_BASIS_WHOLE_PATH_REVIEWED,
    ESTIMATE_BASIS_NO_REVIEWED_UNITS,
    ESTIMATE_BASIS_NO_TRACKER_OBSERVATION,
)

# 判定「已审扣减是否真的生效」的 basis 集合——横幅只在命中这些时才渲染「其中约 N 行已审」。
ESTIMATE_BASES_WITH_EFFECTIVE_DEDUCTION = frozenset(
    {
        ESTIMATE_BASIS_HUNK_FILTERED,
        ESTIMATE_BASIS_PARTIAL_HUNK_FILTERED,
        ESTIMATE_BASIS_WHOLE_PATH_REVIEWED,
    }
)


# ---------------------------------------------------------------------------
# 依赖复用：
#   * committed-round 的 SHA 同源下界 resolver 现住在**中性核** committed_round_detection
#     （S10h-b 迁 core），与 Stop hook 引擎解耦 → 直接 top-level import，无 S4 环。
#   * workspace 守卫 / kanban-task-id / session-id 提取仍在 Stop hook 引擎模块
#     ``stop_review_validate_fix``（S4 会让它反向 import 本模块）→ 保留**惰性** import 破环。
# 全部可被测试注入替换。
# ---------------------------------------------------------------------------

def _load_stop_hook_module():
    """惰性加载 Stop hook 引擎模块（``stop_review_validate_fix``，S10j-2 由
    ``codex_stop_review_validate_fix`` 改名而来）。放在调用点内、不在模块顶层，是为了：S4 会让
    Stop hook 顶层 ``import stop_decision_candidate_summary``；若本模块顶层反向 import 它则成环。
    惰性 import 在**运行时**求值，此时 Stop hook 已完成加载，取到的是已就绪模块。仅 workspace
    守卫 / id 提取走此路径——committed-round 下界 resolver 已迁中性核、直接 import。"""
    import stop_review_validate_fix as stop_hook  # noqa: PLC0415

    return stop_hook


def _default_task_id_resolver(event: Mapping[str, Any]) -> str | None:
    return _load_stop_hook_module().current_kanban_task_id(dict(event))


def _default_session_id_resolver(event: Mapping[str, Any]) -> str | None:
    return _load_stop_hook_module().session_hook_id_from_event(dict(event))


def _default_committed_baseline_resolver(
    repo_path: Path, task_id: str | None, session_id: str | None
) -> str | None:
    """委托给 committed-round 的**同源** resolver（review 高水位优先、round-baseline 兜底），
    该组合逻辑现住在中性核 ``committed_round_detection``，与 Stop hook detection/allocation 端调
    的是**同一个**函数。不重实现，避免第二真相源（S2 决策）。"""
    return _resolve_committed_round_baseline(repo_path, task_id, session_id)


def _default_committed_base_ref_resolver(repo_path: Path) -> str | None:
    return _resolve_committed_round_base_ref(repo_path)


def _default_reviewed_committed_paths_resolver(
    repo_path: Path, session_id: str | None, candidate_committed_paths: set[str], log_root_override: Path | None
) -> set[str]:
    """# OBSERVATION-WINDOW(R9) —— 委托给中性核 tracker 的 review_state「已审」过滤器。

    返回 ``candidate_committed_paths`` 中**已被本 session 审过**的路径子集（其 committed 单元全
    ``review_state='reviewed'`` 且本 session 拥有），供摘要从 committed 候选里剔除。与 Stop hook
    detection 端 ``maybe_route_committed_round_scope`` 调**同一个** tracker 过滤器，保证 detection /
    allocation / summary 三处「已审」裁决同源（S6 原子切换）。best-effort：任何异常退化为空集。

    R9 终局物理删除 SHA marker 家族前，须先把 reviewed_head 持久化进 tracker（方案 B 写侧），
    marker 才能卸下窗口种子角色——见 ``select_committed_paths_reviewed_by_session`` 函数头。"""
    try:
        return select_committed_paths_reviewed_by_session(
            repo=repo_path,
            session_id=session_id,
            candidate_paths=candidate_committed_paths,
            log_root_override=log_root_override,
        )
    except Exception:  # pragma: no cover - 过滤器永不阻断摘要
        return set()


def _default_dirty_reviewed_change_resolver(
    repo_path: Path,
    session_id: str | None,
    candidate_paths: Sequence[str],
    log_root_override: Path | None,
) -> Mapping[str, Any]:
    """委托给 tracker 的 dirty 侧「已审」只读观测器（committed 侧过滤器的对偶）。

    与 committed 侧的关键区别：committed 侧那位**裁决剔除**，本 resolver 只提供「哪些行已审」的
    观测，**绝不影响候选集**。dirty 没有 commit 那种不可变锚点，按它剔除路径就是凭空开漏审面。
    best-effort：任何异常退化为空 mapping（调用方按「全量都算未审」渲染，即今日行为）。"""
    try:
        return select_dirty_path_reviewed_change_by_session(
            repo=repo_path,
            session_id=session_id,
            candidate_paths=candidate_paths,
            log_root_override=log_root_override,
        )
    except Exception:  # pragma: no cover - 观测器永不阻断摘要
        return {}


def _default_workspace_guard(
    *, event: Mapping[str, Any], cwd: str | None, ledger: Any
) -> dict[str, Any] | None:
    """默认 workspace 守卫：委托 ``kanban_task_workspace_guard_payload``。ledger 缺失（例如
    诊断 CLI / 单测未构造 ledger）时跳过守卫（返回 None），因为该守卫需要 ledger 记事件；生产
    路径（Stop hook）一定持有 ledger。"""
    if ledger is None:
        return None
    return _load_stop_hook_module().kanban_task_workspace_guard_payload(
        event=dict(event), cwd=cwd, ledger=ledger
    )


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateFileChange:
    """单个候选文件在摘要里的完整标注。"""

    path: str
    change_state: str
    ownership: str
    included_in_candidate_set: bool
    content_sha256: str
    candidate_set_exclusion_reason: str | None = None
    added_lines: tuple[str, ...] = field(default=())
    deleted_lines: tuple[str, ...] = field(default=())
    # 「扣掉上一轮 review 已审那部分」之后剩下的行。默认与全量相同 —— 没有可信的 tracker 观测
    # 时，全量即未审，这是本估算的 fail-open 方向（宁可报大，不可报小）。
    unreviewed_added_lines: tuple[str, ...] = field(default=())
    unreviewed_deleted_lines: tuple[str, ...] = field(default=())
    reviewed_line_estimate_basis: str = ESTIMATE_BASIS_NO_TRACKER_OBSERVATION

    def to_public_dict(self) -> dict[str, Any]:
        # 注入 prompt 只需要清单元数据，不需要逐行原文（避免注入体积爆炸）；行文本仅供内部
        # 喂给严格度计算器与 bootstrap-size。
        return {
            "path": self.path,
            "change_state": self.change_state,
            "ownership": self.ownership,
            "included_in_candidate_set": self.included_in_candidate_set,
            "candidate_set_exclusion_reason": self.candidate_set_exclusion_reason,
            "content_sha256": self.content_sha256,
            "nonblank_added_lines": strictness_calculator.count_nonblank_lines(self.added_lines)[0],
            "nonblank_deleted_lines": strictness_calculator.count_nonblank_lines(self.deleted_lines)[0],
            "unreviewed_nonblank_added_lines": strictness_calculator.count_nonblank_lines(
                self.unreviewed_added_lines
            )[0],
            "unreviewed_nonblank_deleted_lines": strictness_calculator.count_nonblank_lines(
                self.unreviewed_deleted_lines
            )[0],
            "reviewed_line_estimate_basis": self.reviewed_line_estimate_basis,
        }


# ---------------------------------------------------------------------------
# git 原语（best-effort：任何失败退化为空，绝不抛出阻断 Stop hook）
# ---------------------------------------------------------------------------

def _run_git(repo_path: Path, args: list[str]) -> str | None:
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
    return result.stdout


def _run_git_bytes(repo_path: Path, args: list[str]) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _untracked_paths(repo_path: Path) -> set[str]:
    out = _run_git_bytes(
        repo_path, ["ls-files", "--others", "--exclude-standard", "-z"]
    )
    if out is None:
        return set()
    return {item.decode("utf-8", "surrogateescape") for item in out.split(b"\0") if item}


def _parse_unified_diff_added_deleted(diff_text: str) -> tuple[list[str], list[str]]:
    """从 ``git diff`` 统一 diff 文本抽出增/删行的**裸文本**（去掉前导 ``+``/``-``）。

    跳过 hunk 头（``@@``）、file 头（``+++``/``---``/``diff``/``index``/``rename``/``new``/
    ``deleted``/``Binary`` 等），只留真正的内容增删行。
    """
    added: list[str] = []
    deleted: list[str] = []
    for line in diff_text.splitlines():
        if not line:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            deleted.append(line[1:])
        # 其余（' ' 上下文行、'@@'、'diff'、'index'、'\\ No newline' 等）忽略。
    return added, deleted


def _added_deleted_for_path(
    repo_path: Path, diff_base: str | None, path: str, is_untracked: bool
) -> tuple[list[str], list[str]]:
    """单路径的增/删行文本。untracked 文件整份内容记为新增；否则解析 ``git diff <base> -- path``
    （base 为 None 时用 HEAD）。工作树版本，含已提交 + 未提交的合并 delta。"""
    if is_untracked:
        file_path = repo_path / path
        try:
            content = file_path.read_text(encoding="utf-8", errors="surrogateescape")
        except (OSError, UnicodeError):
            return [], []
        lines = content.splitlines()
        return lines, []
    base = diff_base if diff_base else "HEAD"
    diff_text = _run_git(
        repo_path, ["diff", "--no-color", "--no-renames", base, "--", path]
    )
    if diff_text is None:
        return [], []
    return _parse_unified_diff_added_deleted(diff_text)


def _path_delta_spans_committed_layer(
    repo_path: Path, diff_base: str | None, path: str
) -> bool:
    """本路径的 ``diff_base``→工作树 delta 是否**跨过了 committed 层**（即 base..HEAD 之间该路径有改动）。

    存在的理由：``_added_deleted_for_path`` 在有 committed-round 下界时用 ``diff_base`` 而不是 ``HEAD``，
    拿到的是 committed + dirty 的**合并** delta；而 dirty 侧的整路径已审观测
    （``select_dirty_path_reviewed_change_by_session``，``observed_state='dirty'``）只覆盖 dirty 那一半。
    两者覆盖面不等时，整路径扣减会把未审的已提交内容一并算成已审。

    ``diff_base`` 为空（无 committed 窗口，delta 就是纯 dirty）→ False，行为与既往完全一致；
    ``diff_base`` 等于 HEAD（本轮没提交过东西）→ git diff 为空 → False，同样不受影响。
    git 读取失败（返回 None）→ **无法核验即判为跨层**，回退全量，与本模块「扣不掉即算未审」同向。
    """
    if not diff_base:
        return False
    out = _run_git(
        repo_path, ["diff", "--name-only", "--no-renames", diff_base, "HEAD", "--", path]
    )
    if out is None:
        return True
    return bool(out.strip())


def _subtract_line_multiset(
    total_lines: Sequence[str], reviewed_lines: Sequence[str]
) -> tuple[list[str], int]:
    """从 ``total_lines`` 里按**多重集**减去 ``reviewed_lines``，返回 (剩余行, 实际扣掉的条数)。

    为什么是多重集减法而不是「总数 - 已审数」：后者分不清「同一批行」与「恰好一样多的新行」，
    工作树在 review 之后再改一次就会把新工作也减没。多重集减法只减真正逐字相同的行，减不掉的行
    自动留在未审桶里——**扣不掉即未审**，是本估算唯一的失败方向。

    返回的扣除条数是调用方做陈旧性自校验的依据：声称有已审行、却一条都扣不掉，说明 tracker 观测
    与当前工作树已经对不上，此时必须整份回退全量而不是接受一个失效的扣减。"""
    if not reviewed_lines:
        return list(total_lines), 0
    remaining = Counter(reviewed_lines)
    kept: list[str] = []
    removed = 0
    for line in total_lines:
        if remaining.get(line, 0) > 0:
            remaining[line] -= 1
            removed += 1
            continue
        kept.append(line)
    return kept, removed


def _estimate_unreviewed_lines_for_path(
    added: Sequence[str],
    deleted: Sequence[str],
    observation: Any,
    *,
    delta_spans_committed_layer: bool = False,
) -> tuple[list[str], list[str], str]:
    """单路径的「未审行」估算：(未审增行, 未审删行, estimate_basis)。

    ``observation`` 是 tracker 的 ``DirtyPathReviewedChangeObservation``（或 None=无观测）。
    整路径身份单元（新增 / 删除 / 二进制 / path_only）没有 hunk 锚点，只能整条判定；tracked_hunk
    才做行级多重集扣减。任何拿不准的情形一律回退全量。

    ``delta_spans_committed_layer``：传进来的 ``added``/``deleted`` 是否是 baseline→工作树的
    **committed + dirty 合并** delta（见 ``_path_delta_spans_committed_layer``）。整路径扣减的证据只
    来自 dirty 侧观测（``observed_state='dirty'``），覆盖面窄于合并 delta——此时若照旧整份归零，未审
    的已提交内容会被算成已审，横幅进而渲染出「未审量为 0 …… 通常应当 decline」，这是实打实的漏审诱导。
    故跨层时整份回退全量，并换一个如实标注的 basis（与 ``stale_tracker_observation`` 同一保守方向）。

    刻意**不**做「按层拆分后分别扣减」：整路径身份单元（chmod/path_only、删除、二进制）的 dirty 那
    一半本来就没有可扣的行文本，分层的实际收益是 0，却要为每条路径多付一次 git diff，并引入「两层
    之和 ≠ 合并 delta」（commit 里加、dirty 里删的行会在合并 delta 抵消、分层却各算一次）的语义债。
    """
    if observation is None:
        return list(added), list(deleted), ESTIMATE_BASIS_NO_TRACKER_OBSERVATION
    if getattr(observation, "whole_path_identity_reviewed", False):
        if delta_spans_committed_layer:
            return (
                list(added),
                list(deleted),
                ESTIMATE_BASIS_WHOLE_PATH_REVIEWED_SPANS_UNREVIEWED_COMMITTED,
            )
        return [], [], ESTIMATE_BASIS_WHOLE_PATH_REVIEWED
    reviewed_added = list(getattr(observation, "reviewed_added_lines", ()) or ())
    reviewed_deleted = list(getattr(observation, "reviewed_deleted_lines", ()) or ())
    claimed = len(reviewed_added) + len(reviewed_deleted)
    if claimed == 0:
        return list(added), list(deleted), ESTIMATE_BASIS_NO_REVIEWED_UNITS
    kept_added, removed_added = _subtract_line_multiset(added, reviewed_added)
    kept_deleted, removed_deleted = _subtract_line_multiset(deleted, reviewed_deleted)
    removed = removed_added + removed_deleted
    if removed == 0:
        # 声称审过、却一行都对不上 —— tracker 观测已陈旧（或口径漂移）。整份回退全量并如实标注，
        # 绝不把一个失效的扣减渲染成「已审 0 行」那种看起来正常的数字。
        return list(added), list(deleted), ESTIMATE_BASIS_STALE_TRACKER_OBSERVATION
    basis = (
        ESTIMATE_BASIS_HUNK_FILTERED
        if removed >= claimed
        else ESTIMATE_BASIS_PARTIAL_HUNK_FILTERED
    )
    return kept_added, kept_deleted, basis


def _aggregate_estimate_basis(per_path_bases: Sequence[str]) -> str:
    """把逐路径的 basis 合成整份摘要的一个自陈值。

    优先级见 ``_ESTIMATE_BASIS_AGGREGATION_ORDER``：**越值得报警的越优先**。任一路径 stale，整份
    就报 stale——横幅据此改口说「本轮拿不到可信的已审估算」。反过来做（让多数派或最乐观值胜出）会
    把一份含失效扣减的估算包装成正常数字，正是要避免的那种假绿。空候选集 → 无观测。"""
    if not per_path_bases:
        return ESTIMATE_BASIS_NO_TRACKER_OBSERVATION
    present = set(per_path_bases)
    for basis in _ESTIMATE_BASIS_AGGREGATION_ORDER:
        if basis in present:
            return basis
    return ESTIMATE_BASIS_NO_TRACKER_OBSERVATION


def _content_sha256(repo_path: Path, path: str) -> str:
    """候选路径当前工作树内容的 sha256 十六进制；文件不存在 → ``"absent"``（指纹哨兵）。"""
    file_path = repo_path / path
    try:
        data = file_path.read_bytes()
    except (OSError, IsADirectoryError):
        return CANDIDATE_FINGERPRINT_ABSENT_SENTINEL
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# ownership：best-effort tracker sqlite 缓存读取（不做 transcript 解析）
# ---------------------------------------------------------------------------

def _tracker_db_path(repo_path: Path, log_root_override: Path | None) -> Path | None:
    """定位本 repo 的 tracker sqlite。用 diff_tracker 的**公开**路径原语拼装，只读、不建目录、
    不写。tracker 被禁用 / 非 git 仓 / DB 不存在 → None。"""
    if _tracker_disabled():
        return None
    common_dir = git_common_dir(repo_path)
    if common_dir is None:
        return None
    key = repo_key(common_dir)
    base = log_root_override if log_root_override is not None else log_root()
    db_path = tracker_dir(base, key) / SQLITE_FILENAME
    return db_path if db_path.exists() else None


def _read_tracker_ownership(
    repo_path: Path, session_id: str | None, log_root_override: Path | None
) -> dict[str, str]:
    """返回 ``path -> ownership 标签``，仅覆盖 tracker 缓存**已登记**的路径；未登记路径由调用方
    默认标 ``unattributed``。

    best-effort：读不到 / 无 session_id / 任何异常 → 返回 {}（全部落 unattributed，默认纳入）。
    今日单 agent/worktree 语境下，绝大多数改动尚未被 heavy refresh 登记，故普遍 unattributed
    —— 这正确且安全（不误排除自己的工作）。ownership 作为 filter 的真正价值在未来 Orca 同
    worktree 多 agent 并发（Q4=a）。判定规则：路径的 owned/takeover 单元里含**当前 session** →
    session_owned；只含**其他 session** → other_session_owned（被过滤出候选）；否则 unattributed。

    其他 session 只凭**弱证据**（时间窗：文件改动时刻恰好落在它的工具调用期间）登记的单元不算数：
    弱证据够让那个会话把文件拿去审，不够把文件从本会话的横幅里拿掉（08ac4 报告 §5.5）。
    """
    db_path = _tracker_db_path(repo_path, log_root_override)
    if db_path is None:
        return {}
    worktree = _worktree_key(str(repo_path.resolve()))
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT u.path AS path, su.session_id AS owner_session_id, su.evidence AS owner_evidence
              FROM units u
              LEFT JOIN session_units su
                     ON su.unit_id = u.unit_id
                    AND su.assignment_kind IN ('owned', 'takeover')
             WHERE u.worktree_key = ?
               AND u.is_tombstoned = 0
               AND u.observed_state IN ('dirty', 'committed')
            """,
            (worktree,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        if conn is not None:
            conn.close()

    owners_by_path: dict[str, set[str | None]] = {}
    for row in rows:
        owner = row["owner_session_id"]
        if owner and owner != session_id and row["owner_evidence"] in WEAK_SESSION_WRITE_ATTRIBUTION_EVIDENCE_LABELS:
            owner = None
        owners_by_path.setdefault(row["path"], set()).add(owner)

    labels: dict[str, str] = {}
    for path, owners in owners_by_path.items():
        real_owners = {owner for owner in owners if owner}
        if not real_owners:
            labels[path] = OWNERSHIP_UNATTRIBUTED
        elif session_id and session_id in real_owners:
            labels[path] = OWNERSHIP_SESSION_OWNED
        else:
            labels[path] = OWNERSHIP_OTHER_SESSION_OWNED
    return labels


# ---------------------------------------------------------------------------
# 指纹（本模块唯一权威）
# ---------------------------------------------------------------------------

def compute_candidate_fingerprint(entries: list[tuple[str, str]]) -> str:
    """候选集指纹。见模块 docstring「候选集指纹」节的完整格式定义。

    ``entries``：``(path, content_sha256_or_absent)`` 列表（通常来自**纳入候选集**的文件）。
    输出 ``"sha256:" + hexdigest``。空列表返回稳定的空态指纹。
    """
    ordered = sorted(entries, key=lambda item: item[0].encode("utf-8", "surrogateescape"))
    hasher = hashlib.sha256()
    hasher.update(CANDIDATE_FINGERPRINT_DOMAIN_TAG)
    hasher.update(b"\x1e")
    for path, content_hash in ordered:
        hasher.update(path.encode("utf-8", "surrogateescape"))
        hasher.update(b"\x00")
        hasher.update(str(content_hash).encode("ascii", "surrogateescape"))
        hasher.update(b"\x00")
    return "sha256:" + hasher.hexdigest()


def compute_candidate_scope_fingerprint(
    included_candidate_paths: Sequence[str],
    *,
    committed_round_baseline: str | None,
    worktree_head_commit_sha: str | None,
) -> str:
    """工作范围指纹（粗粒度，**不看内容**）：``sorted(纳入路径) + committed_round_baseline + HEAD``。

    序列化：``DOMAIN_TAG`` + ``\\x1e`` + ``baseline`` + ``\\x00`` + ``HEAD`` + ``\\x00`` + 依次每条
    ``path + \\x00``（路径按 UTF-8 字节序升序、去重）。缺失的 baseline / HEAD 记空串——NUL 分隔使
    「空」与任何真实 SHA 不可混淆。输出 ``"sha256:" + hexdigest``。
    """
    ordered = sorted(
        set(included_candidate_paths), key=lambda item: item.encode("utf-8", "surrogateescape")
    )
    hasher = hashlib.sha256()
    hasher.update(CANDIDATE_SCOPE_FINGERPRINT_DOMAIN_TAG)
    hasher.update(b"\x1e")
    hasher.update((committed_round_baseline or "").encode("ascii", "surrogateescape"))
    hasher.update(b"\x00")
    hasher.update((worktree_head_commit_sha or "").encode("ascii", "surrogateescape"))
    hasher.update(b"\x00")
    for path in ordered:
        hasher.update(path.encode("utf-8", "surrogateescape"))
        hasher.update(b"\x00")
    return "sha256:" + hasher.hexdigest()


def _candidate_scope_identity_for_declined_session_snooze(
    *,
    included_candidate_paths: Sequence[str],
    committed_round_baseline: str | None,
    worktree_head_commit_sha: str | None,
    unreviewed_nonblank_changed_line_estimate: int,
) -> dict[str, Any]:
    """闸 2b（declined 会话级 snooze）判定所需的全部原始量，打包成一个子字典。

    token store 只读这个子字典，不去翻 ``candidates`` 列表的形状——摘要 schema 的其余部分可以自由
    演进而不牵动 snooze 判定。"""
    ordered_paths = sorted(
        set(included_candidate_paths), key=lambda item: item.encode("utf-8", "surrogateescape")
    )
    return {
        "candidate_scope_fingerprint": compute_candidate_scope_fingerprint(
            ordered_paths,
            committed_round_baseline=committed_round_baseline,
            worktree_head_commit_sha=worktree_head_commit_sha,
        ),
        "candidate_scope_fingerprint_algorithm_version": CANDIDATE_SCOPE_FINGERPRINT_ALGORITHM_VERSION,
        "worktree_head_commit_sha": worktree_head_commit_sha,
        "committed_round_baseline": committed_round_baseline,
        "included_candidate_paths": ordered_paths,
        "unreviewed_nonblank_changed_line_estimate": int(unreviewed_nonblank_changed_line_estimate),
    }


def _worktree_head_commit_sha(repo_path: Path) -> str | None:
    """工作树 HEAD 的 commit SHA（空仓 / 非 git / 失败 → None，best-effort）。"""
    out = _run_git(repo_path, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
    head = (out or "").strip()
    return head or None


# ---------------------------------------------------------------------------
# committed-round 窗口种子来源标注（R9 后：种子=bootstrap，非「已审」裁决权威）
# ---------------------------------------------------------------------------

def _classify_committed_round_window_seed_source(
    baseline: str | None,
    task_id: str | None,
    session_id: str | None,
    repo_path: Path | str | None,
    log_root_override: Path | None,
) -> str:
    """标注 committed-round git 窗口下界**种子**来自 review 高水位还是 round-baseline（仅摘要透明度，
    不影响候选枚举）。R9 后种子仅作 bootstrap；「已审/未审」裁决权威改由 ``review_state`` 承担
    （见 ``lower_bound_source`` 字段），二者分工见模块 docstring「真相源分期」。"""
    if not baseline:
        return COMMITTED_ROUND_WINDOW_SEED_NONE
    try:
        highwater = review_highwater_marker.resolve_review_highwater_head(
            task_id=task_id, session_id=session_id, repo=repo_path, root=log_root_override
        )
    except Exception:  # pragma: no cover - 标注读取永不阻断
        highwater = None
    if highwater and highwater == baseline:
        return COMMITTED_ROUND_WINDOW_SEED_REVIEW_HIGHWATER
    return COMMITTED_ROUND_WINDOW_SEED_ROUND_BASELINE


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def build_candidate_summary(
    *,
    event: Mapping[str, Any],
    cwd: str | None,
    repo: str | None,
    ledger: Any = None,
    log_root_override: Path | None = None,
    workspace_guard: Callable[..., dict[str, Any] | None] | None = None,
    task_id_resolver: Callable[[Mapping[str, Any]], str | None] | None = None,
    session_id_resolver: Callable[[Mapping[str, Any]], str | None] | None = None,
    committed_baseline_resolver: Callable[[Path, str | None, str | None], str | None] | None = None,
    committed_base_ref_resolver: Callable[[Path], str | None] | None = None,
    reviewed_committed_paths_resolver: Callable[[Path, str | None, set[str], Path | None], set[str]] | None = None,
    dirty_reviewed_change_resolver: Callable[[Path, str | None, Sequence[str], Path | None], Mapping[str, Any]] | None = None,
    strictness_rule_parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 Stop hook 注入用的候选摘要（结构见返回 dict / 模块 docstring）。

    所有外部依赖都可注入（默认复用中性核 committed_round_detection 的同源下界种子 resolver +
    tracker 的 review_state「已审」过滤器 + Stop hook 引擎的 workspace 守卫 / id 提取）。
    best-effort：git / tracker 读取失败一律退化，绝不抛异常阻断 Stop hook。
    """
    workspace_guard = workspace_guard or _default_workspace_guard
    task_id_resolver = task_id_resolver or _default_task_id_resolver
    session_id_resolver = session_id_resolver or _default_session_id_resolver
    committed_baseline_resolver = committed_baseline_resolver or _default_committed_baseline_resolver
    committed_base_ref_resolver = committed_base_ref_resolver or _default_committed_base_ref_resolver
    reviewed_committed_paths_resolver = (
        reviewed_committed_paths_resolver or _default_reviewed_committed_paths_resolver
    )
    dirty_reviewed_change_resolver = (
        dirty_reviewed_change_resolver or _default_dirty_reviewed_change_resolver
    )

    # ① 先过 workspace 守卫（Kanban task worktree 一致性）。命中 → 短路，空候选。
    guard_payload = workspace_guard(event=event, cwd=cwd, ledger=ledger)
    if guard_payload is not None:
        return _empty_summary(
            repo=repo,
            workspace_guard_blocked=True,
            workspace_guard_payload=guard_payload,
        )

    if not repo:
        return _empty_summary(repo=repo)
    repo_path = Path(repo).expanduser().resolve()

    task_id = task_id_resolver(event)
    session_id = session_id_resolver(event)

    # ② 窗口种子（bootstrap）：committed-round 的 SHA 同源 resolver（高水位 / round-baseline）。
    # R9 后种子仅作 committed 枚举窗口的下界；「已审/未审」裁决权改由 review_state 承担（③.5）。
    try:
        baseline = committed_baseline_resolver(repo_path, task_id, session_id)
    except Exception:  # pragma: no cover - resolver 永不阻断
        baseline = None
    try:
        base_ref = committed_base_ref_resolver(repo_path)
    except Exception:  # pragma: no cover
        base_ref = None
    committed_round_window_seed_source = _classify_committed_round_window_seed_source(
        baseline, task_id, session_id, repo_path, log_root_override
    )

    # ③ 枚举：committed（种子..工作树，含 committed-but-unreviewed）∪ uncommitted（dirty）。
    committed_paths: list[str] = []
    if baseline:
        try:
            committed_paths = _list_committed_round_changed_paths(repo_path, baseline, base_ref)
        except Exception:  # pragma: no cover
            committed_paths = []
    try:
        uncommitted_paths = dirty_paths(repo_path)
    except Exception:  # pragma: no cover
        uncommitted_paths = []

    committed_set = set(committed_paths)
    uncommitted_set = set(uncommitted_paths)

    # ③.5 OBSERVATION-WINDOW(R9): review_state「已审」真相源过滤 —— 把已被本 session 审过的
    # committed 路径（其 committed 单元全 reviewed 且本 session 拥有）从 committed_set 剔除，不再
    # 作为「已提交待审」候选注入。仅剔除 committed 维度：若同路径仍有未提交 dirty 改动，经
    # uncommitted_set 保留（re-dirtied 未审工作不被误剔）。与 detection 端调同一 tracker 过滤器，
    # 三处「已审」裁决同源。lower_bound_source 字段口径随之从窗口种子 SHA 切到 review_state 权威。
    reviewed_committed_paths = reviewed_committed_paths_resolver(
        repo_path, session_id, committed_set, log_root_override
    )
    review_state_filtered_committed_paths = sorted(committed_set & reviewed_committed_paths)
    committed_set = committed_set - reviewed_committed_paths
    # R9 后 lower_bound_source = 「已审/未审」裁决权威：有 committed 窗口种子即 review_state 主导。
    lower_bound_source = (
        LOWER_BOUND_SOURCE_TRACKER_REVIEW_STATE if baseline else LOWER_BOUND_SOURCE_NONE
    )

    all_paths = sorted(committed_set | uncommitted_set)
    review_scope_excluded_path_prefixes = _load_review_scope_excluded_path_prefixes_best_effort(repo_path)
    untracked = _untracked_paths(repo_path)
    ownership_labels = _read_tracker_ownership(repo_path, session_id, log_root_override)
    diff_base = baseline if baseline else None

    # ③.6 dirty 侧「已审」只读观测（committed 侧 ③.5 的对偶，但**只影响规模数字，不影响候选集**）。
    # 只喂未提交路径：纯 committed 路径的已审剔除已在 ③.5 按路径做完，剩下的 committed 工作按定义
    # 就是未审，整份计入未审桶即可。
    try:
        dirty_reviewed_observations = dirty_reviewed_change_resolver(
            repo_path, session_id, sorted(uncommitted_set), log_root_override
        )
    except Exception:  # pragma: no cover - 观测永不阻断摘要
        dirty_reviewed_observations = {}

    candidates: list[CandidateFileChange] = []
    for path in all_paths:
        in_committed = path in committed_set
        in_uncommitted = path in uncommitted_set
        if in_committed and in_uncommitted:
            change_state = CHANGE_STATE_COMMITTED_AND_UNCOMMITTED
        elif in_committed:
            change_state = CHANGE_STATE_COMMITTED
        else:
            change_state = CHANGE_STATE_UNCOMMITTED
        ownership = ownership_labels.get(path, OWNERSHIP_UNATTRIBUTED)
        # 两道 filter，先前缀后归属：
        # * 审查范围排除前缀（内置工具托管目录 ∪ .review-validate-fix-ignore）：工具自动生成的文件
        #   不是任何人的工作，只有它们变脏时不该铸决策点；
        # * ownership（Q4=a）：仅**明确归属他 session** 的路径被排除；unattributed（今日单 agent
        #   常态）默认纳入。
        if path_matches_review_scope_excluded_path_prefix(path, review_scope_excluded_path_prefixes):
            candidate_set_exclusion_reason = CANDIDATE_SET_EXCLUSION_REASON_REVIEW_SCOPE_EXCLUDED_PATH_PREFIX
        elif ownership == OWNERSHIP_OTHER_SESSION_OWNED:
            candidate_set_exclusion_reason = CANDIDATE_SET_EXCLUSION_REASON_OTHER_SESSION_OWNED
        else:
            candidate_set_exclusion_reason = None
        included = candidate_set_exclusion_reason is None
        added, deleted = _added_deleted_for_path(
            repo_path, diff_base, path, path in untracked
        )
        path_observation = dirty_reviewed_observations.get(path)
        # 整路径扣减即将生效时，先核验被扣的 delta 是不是纯 dirty：有 committed-round 下界时
        # ``_added_deleted_for_path`` 给的是 baseline→工作树的合并 delta，而整路径已审证据只覆盖
        # dirty 一半。仅在「真要扣」时才多付这一次 git diff（罕见），其余路径零开销。
        delta_spans_committed_layer = bool(
            path_observation is not None
            and getattr(path_observation, "whole_path_identity_reviewed", False)
            and _path_delta_spans_committed_layer(repo_path, diff_base, path)
        )
        unreviewed_added, unreviewed_deleted, estimate_basis = _estimate_unreviewed_lines_for_path(
            added,
            deleted,
            path_observation,
            delta_spans_committed_layer=delta_spans_committed_layer,
        )
        candidates.append(
            CandidateFileChange(
                path=path,
                change_state=change_state,
                ownership=ownership,
                included_in_candidate_set=included,
                candidate_set_exclusion_reason=candidate_set_exclusion_reason,
                content_sha256=_content_sha256(repo_path, path),
                added_lines=tuple(added),
                deleted_lines=tuple(deleted),
                unreviewed_added_lines=tuple(unreviewed_added),
                unreviewed_deleted_lines=tuple(unreviewed_deleted),
                reviewed_line_estimate_basis=estimate_basis,
            )
        )

    included_candidates = [c for c in candidates if c.included_in_candidate_set]
    exclusion_reason_counts = Counter(
        c.candidate_set_exclusion_reason for c in candidates if not c.included_in_candidate_set
    )

    # ④ 严格度参考档（R13）：仅对纳入候选集的文件计算。
    strictness_reference = strictness_calculator.compute_strictness_reference(
        [
            strictness_calculator.FileLineChange(
                path=c.path, added_lines=c.added_lines, deleted_lines=c.deleted_lines
            )
            for c in included_candidates
        ],
        rule_parameters=strictness_rule_parameters,
    )

    # ⑤ bootstrap-size（R25：并入决策摘要）：纳入候选集的规模度量。
    total_nonblank_added = strictness_reference["totals"]["nonblank_added_lines"]
    total_nonblank_deleted = strictness_reference["totals"]["nonblank_deleted_lines"]
    total_working_tree_bytes = 0
    for c in included_candidates:
        file_path = repo_path / c.path
        try:
            total_working_tree_bytes += file_path.stat().st_size
        except OSError:
            continue
    bootstrap_size = {
        "candidate_path_count": len(included_candidates),
        "total_nonblank_added_lines": total_nonblank_added,
        "total_nonblank_deleted_lines": total_nonblank_deleted,
        "total_working_tree_bytes": total_working_tree_bytes,
    }

    # ⑤.5 未审口径的规模与严格度参考档（复用同一个计算器，只是喂「扣掉已审之后」的行）。
    # 既有 bootstrap_size / strictness_reference 原样保留 —— 它们是**全量**口径，下游断言不动。
    strictness_reference_unreviewed_estimate = strictness_calculator.compute_strictness_reference(
        [
            strictness_calculator.FileLineChange(
                path=c.path,
                added_lines=c.unreviewed_added_lines,
                deleted_lines=c.unreviewed_deleted_lines,
            )
            for c in included_candidates
        ],
        rule_parameters=strictness_rule_parameters,
    )
    aggregated_estimate_basis = _aggregate_estimate_basis(
        [c.reviewed_line_estimate_basis for c in included_candidates]
    )
    unreviewed_line_estimate = {
        "nonblank_added_lines": strictness_reference_unreviewed_estimate["totals"][
            "nonblank_added_lines"
        ],
        "nonblank_deleted_lines": strictness_reference_unreviewed_estimate["totals"][
            "nonblank_deleted_lines"
        ],
        "estimate_basis": aggregated_estimate_basis,
        "hunk_filtered_path_count": sum(
            1
            for c in included_candidates
            if c.reviewed_line_estimate_basis in ESTIMATE_BASES_WITH_EFFECTIVE_DEDUCTION
        ),
        "fallback_path_count": sum(
            1
            for c in included_candidates
            if c.reviewed_line_estimate_basis
            in (
                ESTIMATE_BASIS_STALE_TRACKER_OBSERVATION,
                ESTIMATE_BASIS_NO_TRACKER_OBSERVATION,
                ESTIMATE_BASIS_WHOLE_PATH_REVIEWED_SPANS_UNREVIEWED_COMMITTED,
            )
        ),
        "stale_observation_path_count": sum(
            1
            for c in included_candidates
            if c.reviewed_line_estimate_basis == ESTIMATE_BASIS_STALE_TRACKER_OBSERVATION
        ),
    }

    # ⑥ 指纹（本模块唯一权威）：纳入候选集的 (path, content_sha256)。
    fingerprint = compute_candidate_fingerprint(
        [(c.path, c.content_sha256) for c in included_candidates]
    )

    # ⑥.5 工作范围身份（闸 2b 用，粗粒度、不看内容；见模块 docstring「工作范围指纹」）。
    candidate_scope_identity = _candidate_scope_identity_for_declined_session_snooze(
        included_candidate_paths=[c.path for c in included_candidates],
        committed_round_baseline=baseline,
        worktree_head_commit_sha=_worktree_head_commit_sha(repo_path),
        unreviewed_nonblank_changed_line_estimate=(
            int(unreviewed_line_estimate["nonblank_added_lines"])
            + int(unreviewed_line_estimate["nonblank_deleted_lines"])
        ),
    )

    has_committed_but_unreviewed = any(
        c.change_state in (CHANGE_STATE_COMMITTED, CHANGE_STATE_COMMITTED_AND_UNCOMMITTED)
        for c in included_candidates
    )

    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "workspace_guard_blocked": False,
        "workspace_guard_payload": None,
        "repo": str(repo_path),
        "kanban_task_id": task_id,
        "session_id": session_id,
        "committed_round_baseline": baseline,
        "committed_round_base_ref": base_ref,
        "lower_bound_source": lower_bound_source,
        "committed_round_window_seed_source": committed_round_window_seed_source,
        "review_state_filtered_committed_path_count": len(review_state_filtered_committed_paths),
        "candidates": [c.to_public_dict() for c in candidates],
        "candidate_path_count": len(included_candidates),
        "filtered_out_other_session_path_count": exclusion_reason_counts[
            CANDIDATE_SET_EXCLUSION_REASON_OTHER_SESSION_OWNED
        ],
        "filtered_out_review_scope_excluded_path_prefix_count": exclusion_reason_counts[
            CANDIDATE_SET_EXCLUSION_REASON_REVIEW_SCOPE_EXCLUDED_PATH_PREFIX
        ],
        "review_scope_excluded_path_prefixes": review_scope_excluded_path_prefixes,
        "has_committed_but_unreviewed": has_committed_but_unreviewed,
        "bootstrap_size": bootstrap_size,
        "unreviewed_line_estimate": unreviewed_line_estimate,
        "candidate_fingerprint": fingerprint,
        "candidate_fingerprint_algorithm_version": CANDIDATE_FINGERPRINT_ALGORITHM_VERSION,
        "candidate_scope_fingerprint": candidate_scope_identity["candidate_scope_fingerprint"],
        "candidate_scope_identity_for_declined_session_snooze": candidate_scope_identity,
        "strictness_reference": strictness_reference,
        "strictness_reference_unreviewed_estimate": strictness_reference_unreviewed_estimate,
    }


def _load_review_scope_excluded_path_prefixes_best_effort(repo_path: Path) -> list[str]:
    """读仓库的审查范围排除前缀；ignore 文件读不了（权限、编码）时退回只用内置前缀，绝不阻断 Stop。"""
    try:
        return load_review_scope_excluded_path_prefixes(repo_path, [])
    except (OSError, UnicodeDecodeError):
        return sorted(BUILTIN_DEVELOPER_TOOL_MANAGED_PATH_PREFIXES_EXCLUDED_FROM_REVIEW_SCOPE)


def _empty_summary(
    *,
    repo: str | None,
    workspace_guard_blocked: bool = False,
    workspace_guard_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """无候选（无 repo / workspace 守卫命中）时的稳定空摘要。指纹为稳定空态值，便于
    declined 冷却对账（空候选一律同一指纹）。"""
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "workspace_guard_blocked": workspace_guard_blocked,
        "workspace_guard_payload": workspace_guard_payload,
        "repo": repo,
        "kanban_task_id": None,
        "session_id": None,
        "committed_round_baseline": None,
        "committed_round_base_ref": None,
        "lower_bound_source": LOWER_BOUND_SOURCE_NONE,
        "committed_round_window_seed_source": COMMITTED_ROUND_WINDOW_SEED_NONE,
        "review_state_filtered_committed_path_count": 0,
        "candidates": [],
        "candidate_path_count": 0,
        "filtered_out_other_session_path_count": 0,
        "filtered_out_review_scope_excluded_path_prefix_count": 0,
        "review_scope_excluded_path_prefixes": [],
        "has_committed_but_unreviewed": False,
        "bootstrap_size": {
            "candidate_path_count": 0,
            "total_nonblank_added_lines": 0,
            "total_nonblank_deleted_lines": 0,
            "total_working_tree_bytes": 0,
        },
        "unreviewed_line_estimate": {
            "nonblank_added_lines": 0,
            "nonblank_deleted_lines": 0,
            "estimate_basis": ESTIMATE_BASIS_NO_TRACKER_OBSERVATION,
            "hunk_filtered_path_count": 0,
            "fallback_path_count": 0,
            "stale_observation_path_count": 0,
        },
        "candidate_fingerprint": compute_candidate_fingerprint([]),
        "candidate_fingerprint_algorithm_version": CANDIDATE_FINGERPRINT_ALGORITHM_VERSION,
        "candidate_scope_fingerprint": compute_candidate_scope_fingerprint(
            [], committed_round_baseline=None, worktree_head_commit_sha=None
        ),
        # 空摘要不参与 snooze 判定（无候选根本到不了铸造），故身份子字典记 None。
        "candidate_scope_identity_for_declined_session_snooze": None,
        "strictness_reference": strictness_calculator.compute_strictness_reference([]),
        "strictness_reference_unreviewed_estimate": (
            strictness_calculator.compute_strictness_reference([])
        ),
    }


def _main(argv: list[str] | None = None) -> int:
    """诊断 / 测试用 CLI（**非** R18 的 pull 通道）：给定 repo 打印候选摘要 JSON。生产路径是
    Stop hook 侧 push 生成（``build_candidate_summary``），不铸 token、不驱动决策。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="目标 git 仓库路径")
    parser.add_argument("--cwd", default=None, help="Stop event 的 cwd（默认同 --repo）")
    parser.add_argument("--session-id", default=None, help="覆盖 session id（诊断用）")
    parser.add_argument("--task-id", default=None, help="覆盖 kanban task id（诊断用）")
    args = parser.parse_args(argv)

    event: dict[str, Any] = {"cwd": args.cwd or args.repo}
    summary = build_candidate_summary(
        event=event,
        cwd=args.cwd or args.repo,
        repo=args.repo,
        ledger=None,
        task_id_resolver=(lambda _e: args.task_id) if args.task_id else (lambda _e: None),
        session_id_resolver=(lambda _e: args.session_id) if args.session_id else (lambda _e: None),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "SUMMARY_SCHEMA_VERSION",
    "CANDIDATE_FINGERPRINT_ALGORITHM_VERSION",
    "CANDIDATE_SCOPE_FINGERPRINT_ALGORITHM_VERSION",
    "CHANGE_STATE_COMMITTED",
    "CHANGE_STATE_UNCOMMITTED",
    "CHANGE_STATE_COMMITTED_AND_UNCOMMITTED",
    "OWNERSHIP_SESSION_OWNED",
    "OWNERSHIP_OTHER_SESSION_OWNED",
    "OWNERSHIP_UNATTRIBUTED",
    "LOWER_BOUND_SOURCE_TRACKER_REVIEW_STATE",
    "LOWER_BOUND_SOURCE_NONE",
    "COMMITTED_ROUND_WINDOW_SEED_REVIEW_HIGHWATER",
    "COMMITTED_ROUND_WINDOW_SEED_ROUND_BASELINE",
    "COMMITTED_ROUND_WINDOW_SEED_NONE",
    "CandidateFileChange",
    "compute_candidate_fingerprint",
    "compute_candidate_scope_fingerprint",
    "build_candidate_summary",
]


if __name__ == "__main__":
    raise SystemExit(_main())
