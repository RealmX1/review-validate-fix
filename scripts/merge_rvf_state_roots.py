#!/usr/bin/env python3
"""把 legacy 的 ``~/.claude/rvf`` 状态根并入 canonical 的 RVF 状态根。

背景：Claude host 的 hook shim 曾用一行 ``env.setdefault`` 把
``CODEX_RVF_LOG_ROOT`` 钉在 ``~/.claude/rvf``，于是「走 Claude hook 的一切」与
「不走 hook 的一切（shell 里跑的 prepare / seal / reviewer / rvf-agent）」各写一
棵状态树，而**没有任何东西比较过它们**。后果是双向的：

- **过触发**：``/rvf-land`` 封窗把 review highwater 写进 canonical 根，Stop hook
  读 legacy 根的旧水位，于是刚 land 的那个 commit 每次都被重新圈成待审范围。
- **漏审**：反方向的 task 上，legacy 根的水位更旧，canonical 根的水位更新；
  取新水位会把中间那些**从未被审**的 committed 单元挡在窗口之外。

因此这个工具的核心不是「把两边的行拼起来」，而是**在每一处冲突上选择可证明
安全的一侧**，并把每一条决策写进对账表。判据见 ``docs`` 与本文件各 policy 函数
的 docstring；简述：

- highwater **禁止按时间戳取较新**。只有当合并后的 unit 表能证明「窗口内没有
  未审的 committed 单元」时才允许前移，否则退到祖先 / ``merge-base``。
- ``review_state`` 的 ``assigned`` **不参与偏序**——它是「被某把 lease 占着」的
  引用态，不是「更已审」。active lease 不迁移，故 ``assigned`` 一律降回
  ``available``，否则会变成永久 assigned 且无解。
- ``is_tombstoned`` **禁止取 OR**。tombstone 是某个观察时刻的陈旧性断言，不是
  单调事实；它与 ``observed_state`` / ``tombstoned_at`` / ``tombstone_reason``
  是一个**原子四元组**，整组按 ``last_observed_at`` 更新的一侧取用。
- 表清单**从 DDL 反射**，任何未在合并策略表里显式登记的表直接 fail-closed，
  而不是静默漏掉。

子命令：

``report``   只读。产出对账表，什么都不写。**先跑这个，看过再谈迁移。**
``merge``    在 staging 目标里合并（绝不原地改 canonical 根）。
``verify``   校验 staging 结果的 FK / 状态 invariant / 路径引用。
``switch``   原子切换 canonical 根，并把 legacy 根改名为只读归档。

``merge`` / ``switch`` 有前置闸：若 legacy 根在最近若干分钟内仍有 mtime 变化，
直接 fail-closed 并说明「部署版仍在写 legacy 根」——这正是原计划顺序会当场制造
第三次分裂的地方。

canonical 根本身也是一道闸：默认走与运行时同一个裁决点
``resolved_rvf_runtime_log_root()``（``CODEX_RVF_LOG_ROOT`` → ``CODEX_RVF_STATE_DIR``
→ user-local-data 的 ``run-logs``），因为那才是状态真正被读写的位置；但只要它与
legacy 根重叠就 fail-closed，要求显式 ``--canonical-root``。理由见
``overlapping_state_root_rejection_message``：本工具最常被运行的那些进程（hook
后代、reviewer / fix 子进程）继承的正是仍指向 legacy 根的那份环境。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from core.rvf_user_local_data_paths import (  # noqa: E402
    resolved_rvf_runtime_log_root,
    resolved_rvf_user_local_data_paths,
)


LEGACY_CLAUDE_SCOPED_STATE_ROOT = Path.home() / ".claude" / "rvf"

# 只用于给对账表标注「canonical 根是被哪个旋钮决定的」。**路径本身一律由
# ``resolved_rvf_runtime_log_root()`` 裁决**，这里绝不参与路径计算——否则就又多
# 出一个各写各的解析点，正是本工具在收拾的那类分裂。
RUNTIME_LOG_ROOT_OVERRIDE_ENVIRONMENT_VARIABLE_NAMES_FOR_PROVENANCE_LABELLING = (
    "CODEX_RVF_LOG_ROOT",
    "CODEX_RVF_STATE_DIR",
)

# legacy 根仍在被写 ⇒ 部署版还没换掉，这时候合并必然当场制造第三次分裂。
LEGACY_ROOT_QUIESCENCE_MINIMUM_IDLE_SECONDS = 600

TRACKER_REPOSITORIES_SUBDIRECTORY = Path("diff-tracker") / "repos"
TRACKER_DATABASE_FILE_NAME = "tracker.sqlite3"
TRACKER_REPOSITORY_METADATA_FILE_NAME = "meta.json"

REVIEW_HIGHWATER_SUBDIRECTORY = "review-highwater"
ROUND_BASELINE_PENDING_SUBDIRECTORY = "round-baseline-pending"
RUNS_SUBDIRECTORY = "runs"

# lease 状态里哪些算「已终结、可安全迁移」。active / paused 是活的：迁过去要么
# 是幽灵 lease（系统已 quiesce），要么旧 reviewer 还持着 legacy 根的环境继续
# heartbeat（系统没 quiesce）。两种都不能默认迁。
TERMINAL_LEASE_STATES = frozenset({"completed", "stale-released", "failed-released"})
UNDRAINED_LEASE_STATES = frozenset({"active", "paused"})

UNIT_LIVE_REVIEW_STATES = frozenset({"available", "assigned"})

# highwater 冲突策略。两个取值都会**完整计算并落账**窗口内未审的 committed 单元，
# 区别只在拿这份证据做什么。
#
# ``advance-to-descendant``（默认，由 owner 拍板）：分歧时一律取后代那一侧。
#   窗口内若仍有未审的 committed 单元，它们会被推出观察窗口；下一次
#   ``_supersede_absent_units_in_txn`` 会把它们改写成 ``superseded``，而候选查询
#   与并集查询都过滤 ``observed_state IN ('dirty','committed')`` ⇒ 自动路径上取不
#   回来。为此本策略会把每一个被写掉的 unit_id 落进迁移账本，留一条手工回溯的路。
#
# ``advance-only-when-provably-closed``：仅当窗口内查不到未审的 committed 单元时
#   才前移，否则守在可证明安全的下界。代价是这些窗口会在对应 task 被恢复时重新
#   进入待审范围（重复审），好处是不产生不可逆的排除。
HIGHWATER_CONFLICT_POLICY_ADVANCE_TO_DESCENDANT = "advance-to-descendant"
HIGHWATER_CONFLICT_POLICY_ADVANCE_ONLY_WHEN_PROVABLY_CLOSED = "advance-only-when-provably-closed"
DEFAULT_HIGHWATER_CONFLICT_POLICY = HIGHWATER_CONFLICT_POLICY_ADVANCE_TO_DESCENDANT


# ---------------------------------------------------------------------------
# 合并策略登记表：每张表都必须在这里显式登记，否则 fail-closed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableMergeStrategy:
    """一张 tracker 表的合并策略。

    ``primary_key_columns``  行身份。
    ``recency_column``       冲突时用来判断「哪一侧更新」的列（None = 不比较，
                             直接以 canonical 侧为准并计一次冲突）。
    ``identity_columns``     必须两侧一致的身份列；不一致即 fail-closed 隔离，
                             绝不静默决胜（例如 worktree_path、branch refname）。
    ``earliest_columns``     归并时取两侧较早值的列（创建时刻类）。
    ``latest_columns``       归并时取两侧较晚值的列（最后可见时刻类）。
    ``custom_policy``        非通用策略的表名标记，由专门的函数处理。
    """

    table_name: str
    primary_key_columns: tuple[str, ...]
    recency_column: str | None = None
    identity_columns: tuple[str, ...] = ()
    earliest_columns: tuple[str, ...] = ()
    latest_columns: tuple[str, ...] = ()
    custom_policy: str | None = None
    follows_parent_table: str | None = None


TRACKER_TABLE_MERGE_STRATEGIES: dict[str, TableMergeStrategy] = {
    strategy.table_name: strategy
    for strategy in (
        TableMergeStrategy(
            table_name="units",
            primary_key_columns=("unit_id",),
            recency_column="last_observed_at",
            custom_policy="units",
        ),
        TableMergeStrategy(
            table_name="leases",
            primary_key_columns=("lease_id",),
            recency_column="last_activity_at",
            custom_policy="leases",
        ),
        TableMergeStrategy(
            table_name="lease_units",
            primary_key_columns=("lease_id", "unit_id"),
            follows_parent_table="leases",
        ),
        TableMergeStrategy(
            table_name="lease_participants",
            primary_key_columns=("lease_id", "reviewer_id", "run_id"),
            recency_column="last_activity_at",
            follows_parent_table="leases",
        ),
        TableMergeStrategy(
            table_name="sessions",
            primary_key_columns=("session_id",),
            recency_column="last_seen_at",
            identity_columns=("current_worktree_key", "parent_session_id"),
            earliest_columns=("created_at",),
            latest_columns=("last_seen_at",),
        ),
        TableMergeStrategy(
            table_name="worktrees",
            primary_key_columns=("worktree_key",),
            recency_column="last_seen_at",
            identity_columns=("worktree_path",),
            earliest_columns=("first_seen_at",),
            latest_columns=("last_seen_at",),
        ),
        TableMergeStrategy(
            table_name="branches",
            primary_key_columns=("branch_key",),
            recency_column="last_seen_at",
            identity_columns=("refname",),
            earliest_columns=("first_seen_at",),
            latest_columns=("last_seen_at",),
        ),
        TableMergeStrategy(
            table_name="session_units",
            primary_key_columns=("session_id", "unit_id"),
            recency_column="last_seen_at",
        ),
        TableMergeStrategy(
            table_name="edit_claims",
            primary_key_columns=("claim_id",),
            recency_column="last_seen_at",
        ),
        TableMergeStrategy(
            table_name="edit_claim_units",
            primary_key_columns=("claim_id", "unit_id"),
            follows_parent_table="edit_claims",
        ),
        TableMergeStrategy(
            table_name="rvf_issues",
            primary_key_columns=("issue_key",),
            recency_column="updated_at",
            custom_policy="path_rewrite",
        ),
        TableMergeStrategy(
            table_name="rvf_fix_attempts",
            primary_key_columns=("attempt_id",),
            recency_column="updated_at",
            custom_policy="path_rewrite",
        ),
        TableMergeStrategy(
            table_name="rvf_fix_patch_events",
            primary_key_columns=("patch_event_id",),
            recency_column="created_at",
        ),
        TableMergeStrategy(
            table_name="rvf_issue_patch_links",
            primary_key_columns=("issue_key", "attempt_id", "patch_event_id"),
            recency_column="created_at",
        ),
        TableMergeStrategy(
            table_name="manual_rvf_runs",
            primary_key_columns=("session_id", "run_id"),
            recency_column="completed_at",
        ),
        TableMergeStrategy(
            table_name="tombstones",
            primary_key_columns=("tombstone_id",),
            custom_policy="tombstones_append_only",
        ),
        TableMergeStrategy(
            table_name="meta",
            primary_key_columns=("key",),
            custom_policy="meta",
        ),
    )
}

# 路径前缀重写涉及的列。runs 目录一移，这些绝对路径全部变成死路；其中
# ``rvf_fix_attempts`` 的两列还被 fix 复现流程直接读。
ABSOLUTE_PATH_COLUMNS_BY_TABLE = {
    "rvf_issues": ("artifact_path",),
    "rvf_fix_attempts": ("baseline_overlay_path", "fix_patch_path"),
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def read_json_document(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def most_recent_modification_time(root: Path, *, sample_limit: int = 4000) -> float:
    """root 子树里最新的 mtime。用于「legacy 根是否还在被写」的前置闸。"""

    newest = 0.0
    inspected = 0
    for path in root.rglob("*"):
        try:
            newest = max(newest, path.lstat().st_mtime)
        except OSError:
            continue
        inspected += 1
        if inspected >= sample_limit:
            break
    return newest


def open_sqlite_readonly(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def table_names_in_database(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def fetch_table_rows_keyed_by_primary_key(
    connection: sqlite3.Connection,
    strategy: TableMergeStrategy,
) -> dict[tuple[Any, ...], dict[str, Any]]:
    rows = connection.execute(f"SELECT * FROM {strategy.table_name}").fetchall()
    keyed: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        record = dict(row)
        key = tuple(record[column] for column in strategy.primary_key_columns)
        keyed[key] = record
    return keyed


def text_is_later(left: Any, right: Any) -> bool:
    """ISO8601 文本时间比较；缺失一律视为更旧。"""

    if not isinstance(left, str) or not left:
        return False
    if not isinstance(right, str) or not right:
        return True
    return left > right


def run_git(repository_directory: Path, *arguments: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_directory), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - 对账工具，任何 git 故障都只降级
        return 1, f"{type(exc).__name__}: {exc}"
    return completed.returncode, (completed.stdout or completed.stderr or "").strip()


# ---------------------------------------------------------------------------
# tracker 仓库配对
# ---------------------------------------------------------------------------


@dataclass
class TrackerRepositoryPair:
    repository_key: str
    legacy_directory: Path | None
    canonical_directory: Path | None
    legacy_metadata: dict[str, Any] | None
    canonical_metadata: dict[str, Any] | None

    @property
    def present_in_both_roots(self) -> bool:
        return self.legacy_directory is not None and self.canonical_directory is not None

    @property
    def git_common_directory(self) -> str | None:
        for metadata in (self.legacy_metadata, self.canonical_metadata):
            if metadata and isinstance(metadata.get("git_common_dir"), str):
                return metadata["git_common_dir"]
        return None

    @property
    def git_common_directory_matches(self) -> bool:
        """两侧的 repo 身份是否一致。

        刻意比 ``git_common_dir`` 而不是 ``repo``：``repo`` 只是「最先创建这条
        记录的那个 worktree」，同一个仓在两根里可能由不同 worktree 首次创建
        （实测 ``my-ai-setup`` 在 legacy 根记的是 cline worktree、在 canonical 根
        记的是主 checkout）。``git_common_dir`` 才是 repo_key 的派生源。
        """

        if not self.present_in_both_roots:
            return True
        legacy = (self.legacy_metadata or {}).get("git_common_dir")
        canonical = (self.canonical_metadata or {}).get("git_common_dir")
        if legacy is None or canonical is None:
            return False
        return legacy == canonical

    @property
    def repository_working_directory(self) -> Path | None:
        """能跑 git 的目录：优先 git_common_dir 的父目录，其次记录里的 repo。"""

        common_directory = self.git_common_directory
        if common_directory:
            candidate = Path(common_directory)
            if candidate.name == ".git" and candidate.parent.is_dir():
                return candidate.parent
            if candidate.is_dir():
                return candidate
        for metadata in (self.canonical_metadata, self.legacy_metadata):
            if metadata and isinstance(metadata.get("repo"), str):
                candidate = Path(metadata["repo"])
                if candidate.is_dir():
                    return candidate
        return None


def discover_tracker_repository_pairs(
    legacy_root: Path,
    canonical_root: Path,
) -> list[TrackerRepositoryPair]:
    legacy_repositories_root = legacy_root / TRACKER_REPOSITORIES_SUBDIRECTORY
    canonical_repositories_root = canonical_root / TRACKER_REPOSITORIES_SUBDIRECTORY

    def directories_by_key(root: Path) -> dict[str, Path]:
        if not root.is_dir():
            return {}
        return {
            child.name: child
            for child in sorted(root.iterdir())
            if child.is_dir() and (child / TRACKER_DATABASE_FILE_NAME).is_file()
        }

    legacy_directories = directories_by_key(legacy_repositories_root)
    canonical_directories = directories_by_key(canonical_repositories_root)

    pairs: list[TrackerRepositoryPair] = []
    for repository_key in sorted(set(legacy_directories) | set(canonical_directories)):
        legacy_directory = legacy_directories.get(repository_key)
        canonical_directory = canonical_directories.get(repository_key)
        pairs.append(
            TrackerRepositoryPair(
                repository_key=repository_key,
                legacy_directory=legacy_directory,
                canonical_directory=canonical_directory,
                legacy_metadata=(
                    read_json_document(legacy_directory / TRACKER_REPOSITORY_METADATA_FILE_NAME)
                    if legacy_directory
                    else None
                ),
                canonical_metadata=(
                    read_json_document(
                        canonical_directory / TRACKER_REPOSITORY_METADATA_FILE_NAME
                    )
                    if canonical_directory
                    else None
                ),
            )
        )
    return pairs


# ---------------------------------------------------------------------------
# units 合并策略
# ---------------------------------------------------------------------------


@dataclass
class UnitMergeOutcome:
    merged_row: dict[str, Any]
    decision_classes: list[str] = field(default_factory=list)


def merge_unit_rows(
    legacy_row: dict[str, Any] | None,
    canonical_row: dict[str, Any] | None,
) -> UnitMergeOutcome:
    """合并同一个 ``unit_id`` 在两根里的两条记录。

    三条硬规则（每一条都由实测反例逼出来）：

    1. ``observed_state`` / ``is_tombstoned`` / ``tombstoned_at`` /
       ``tombstone_reason`` 是**原子四元组**，整组按 ``last_observed_at`` 更新的
       一侧取用。取 OR 会把「另一根在更早时刻认为它陈旧」的断言永久固化——
       实测抽样 12 条冲突行全部是 legacy 侧观察更新、canonical 侧更旧却说
       tombstoned，OR 之后连当时仍 dirty 的活改动一起被永久排除。
    2. ``assigned`` **不参与偏序**。它只表示「被某把 lease 占着」，而 active
       lease 不迁移，故一律降回 ``available``；否则合并库里会出现永久 assigned
       且无 ``lease_units`` 行可反查复位的死单元（实测两根 80 个重叠 unit 里有
       22 个正是这种）。
    3. ``reviewed`` 只在「对侧不是 available」或「对侧的 available 更旧、即它
       根本没看见过这次评审」时保留。对侧 available 且更新 ⇒ 可能是显式
       reopen，取 ``available`` 重审。**宁可重复审，不可漏审。**
    """

    decisions: list[str] = []
    if legacy_row is None and canonical_row is None:
        raise ValueError("merge_unit_rows 至少需要一侧存在")
    if legacy_row is None:
        merged = dict(canonical_row or {})
        decisions.append("canonical_only")
    elif canonical_row is None:
        merged = dict(legacy_row)
        decisions.append("legacy_only")
    else:
        legacy_is_newer = text_is_later(
            legacy_row.get("last_observed_at"), canonical_row.get("last_observed_at")
        )
        newer_row = legacy_row if legacy_is_newer else canonical_row
        older_row = canonical_row if legacy_is_newer else legacy_row
        merged = dict(newer_row)
        decisions.append("newer_observation_side_" + ("legacy" if legacy_is_newer else "canonical"))

        # 规则 1：陈旧性四元组整组取用（已随 newer_row 带入），只在两侧确实
        # 分歧时记一次冲突，便于对账时看见数量。
        for column in ("observed_state", "is_tombstoned"):
            if legacy_row.get(column) != canonical_row.get(column):
                decisions.append(f"conflict_{column}")

        merged["first_observed_at"] = min(
            [
                value
                for value in (
                    legacy_row.get("first_observed_at"),
                    canonical_row.get("first_observed_at"),
                )
                if isinstance(value, str) and value
            ]
            or [merged.get("first_observed_at")]
        )

        # 规则 3：review_state 的保守合并。
        merged["review_state"] = conservative_review_state(
            newer_row=newer_row,
            older_row=older_row,
            decisions=decisions,
        )

        # 显式豁免（review_not_required_*）无论在哪一侧都保留。
        for column in (
            "review_not_required_declared_at",
            "review_not_required_declared_reason",
            "review_not_required_declared_by_run_id",
        ):
            if not merged.get(column):
                for candidate_row in (newer_row, older_row):
                    if candidate_row.get(column):
                        merged[column] = candidate_row[column]
                        decisions.append("preserved_explicit_waiver")
                        break

    # 规则 2：assigned 一律降回 available（active lease 不迁移）。
    if merged.get("review_state") == "assigned":
        merged["review_state"] = "available"
        decisions.append("demoted_assigned_to_available_because_lease_not_migrated")

    return UnitMergeOutcome(merged_row=merged, decision_classes=decisions)


def conservative_review_state(
    *,
    newer_row: Mapping[str, Any],
    older_row: Mapping[str, Any],
    decisions: list[str],
) -> str:
    newer_state = newer_row.get("review_state")
    older_state = older_row.get("review_state")

    if newer_state == older_state:
        return str(newer_state)

    # 显式豁免过的单元视同已审，不再重开。
    if newer_row.get("review_not_required_declared_at") or older_row.get(
        "review_not_required_declared_at"
    ):
        decisions.append("kept_reviewed_due_to_explicit_waiver")
        return "reviewed"

    states = {newer_state, older_state}
    if states == {"reviewed", "available"}:
        if newer_state == "available":
            # 更新的观察说它 available ⇒ 可能是显式 reopen，重审。
            decisions.append("reopened_to_available_newer_side_says_available")
            return "available"
        # 更新的观察说它 reviewed，对侧的 available 更旧 ⇒ 那一侧只是没看见
        # 这次评审，不是 reopen。
        decisions.append("kept_reviewed_stale_side_never_saw_the_review")
        return "reviewed"

    if "assigned" in states:
        # assigned 不参与偏序，落到下游统一降级。
        decisions.append("assigned_does_not_participate_in_ordering")
        return str(newer_state if newer_state != "assigned" else older_state)

    decisions.append("fell_back_to_available_for_unclassified_state_conflict")
    return "available"


# ---------------------------------------------------------------------------
# highwater 决策
# ---------------------------------------------------------------------------


# marker 契约里 ``reviewed_head`` 是 git 写出的 40 位小写 hex 全 sha（``stop_finalize``
# 与 ``rvf_land_seal`` 两条写入路径都来自 ``git rev-parse``）。这里刻意**不**接受
# 缩写 sha、不接受大写、不接受 ref 名：缩写 sha 会被写回 marker 而破坏契约，且它
# 的唯一性随仓库增长而失效；ref 名根本不是水位（它会随分支移动）。
REVIEW_HIGHWATER_FULL_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

# 一侧 marker 的可用性分类。只有 ``ABSENT`` 与 ``USABLE`` 允许继续裁决，其余三种
# 一律 fail-closed（见 ``choose_review_highwater`` 里的判据注释）。
MARKER_SIDE_ABSENT = "marker_file_absent_on_this_side"
MARKER_SIDE_USABLE = "reviewed_head_is_a_full_40_hex_commit_sha"
MARKER_SIDE_FILE_UNREADABLE = "marker_file_present_but_not_parseable_as_a_json_object"
MARKER_SIDE_REVIEWED_HEAD_FIELD_ABSENT = "marker_present_but_reviewed_head_field_absent"
MARKER_SIDE_REVIEWED_HEAD_NOT_A_FULL_COMMIT_SHA = (
    "marker_present_but_reviewed_head_is_not_a_full_40_hex_commit_sha"
)

# 这三种分类表示「这一侧确实声称有水位，但我们读不出它到底是哪个 commit」。
MARKER_SIDE_CLASSIFICATIONS_REQUIRING_FAIL_CLOSED_REFUSAL = frozenset(
    {
        MARKER_SIDE_FILE_UNREADABLE,
        MARKER_SIDE_REVIEWED_HEAD_FIELD_ABSENT,
        MARKER_SIDE_REVIEWED_HEAD_NOT_A_FULL_COMMIT_SHA,
    }
)


def classify_review_highwater_marker_side(
    marker: Mapping[str, Any] | None,
    *,
    marker_file_present_but_unreadable: bool,
) -> tuple[str, str | None]:
    """判定一侧 marker 是否给出了可用的 ``reviewed_head``。

    返回 ``(分类, 可用的 reviewed_head 或 None)``。刻意把「文件根本不存在」与
    「文件在、但读不出水位」分成两类：前者是合法的迁移形态（该 task 只在一根里
    出现过），后者是**损坏**，两者绝不能混为一谈。``read_json_document`` 只保证
    「解析成功且是 dict」，把解析失败静默降级成 None，因此调用方必须另外把
    「文件存在但解析失败」这一位传进来，否则损坏会伪装成缺席。
    """

    if marker_file_present_but_unreadable:
        return MARKER_SIDE_FILE_UNREADABLE, None
    if marker is None:
        return MARKER_SIDE_ABSENT, None
    if "reviewed_head" not in marker:
        return MARKER_SIDE_REVIEWED_HEAD_FIELD_ABSENT, None
    reviewed_head = marker["reviewed_head"]
    if isinstance(reviewed_head, str) and REVIEW_HIGHWATER_FULL_COMMIT_SHA_PATTERN.match(
        reviewed_head
    ):
        return MARKER_SIDE_USABLE, reviewed_head
    return MARKER_SIDE_REVIEWED_HEAD_NOT_A_FULL_COMMIT_SHA, None


@dataclass
class ReviewHighwaterReconciliation:
    task_marker_name: str
    legacy_reviewed_head: str | None
    canonical_reviewed_head: str | None
    legacy_repository: str | None
    canonical_repository: str | None
    ancestry_relation: str
    chosen_reviewed_head: str | None
    chosen_side: str
    commit_count_in_window: int | None
    unreviewed_committed_unit_count_in_window: int | None
    conflict_classes: list[str] = field(default_factory=list)
    # 因水位前移而被推出观察窗口的 unit_id。这些单元会在下一次 refresh 被改写成
    # ``superseded``，自动路径上再也进不了候选；落账是唯一留下的回溯路径。
    written_off_unit_ids: list[str] = field(default_factory=list)


def ancestry_relation_between(
    repository_directory: Path | None,
    left_commit: str | None,
    right_commit: str | None,
) -> str:
    if not left_commit or not right_commit:
        return "single_side_only"
    if left_commit == right_commit:
        return "identical"
    if repository_directory is None:
        return "repository_unavailable"
    left_exists, _ = run_git(repository_directory, "cat-file", "-e", f"{left_commit}^{{commit}}")
    right_exists, _ = run_git(repository_directory, "cat-file", "-e", f"{right_commit}^{{commit}}")
    if left_exists != 0 or right_exists != 0:
        return "commit_missing_from_repository"
    left_is_ancestor, _ = run_git(
        repository_directory, "merge-base", "--is-ancestor", left_commit, right_commit
    )
    if left_is_ancestor == 0:
        return "left_is_ancestor_of_right"
    right_is_ancestor, _ = run_git(
        repository_directory, "merge-base", "--is-ancestor", right_commit, left_commit
    )
    if right_is_ancestor == 0:
        return "right_is_ancestor_of_left"
    return "diverged"


def changed_paths_between(
    repository_directory: Path,
    older_commit: str,
    newer_commit: str,
) -> set[str]:
    """窗口 ``older..newer`` 内被改动的路径集合。

    这个集合只有一个用途：与 units 表求交，算出「窗口内还有多少未审的
    committed 单元」。所以它必须与 ``units.path`` 处在**同一个路径宇宙**、且逐
    字节可比 —— 交不上就等于静默丢单元，而丢单元只会让 ``unreviewed_count``
    偏小，于是 safe 策略误以为「窗口已清空」而前移 highwater、默认策略的
    ``written_off_unit_ids`` 也漏记。两个 flag 各堵一个实测缺口：

    * ``--no-renames``：units 的生产侧（``reviewable_unit_diff_tracker`` 的三处
      枚举）一律用 ``--no-renames``，因此一次 rename 在 units 表里是**两条**
      记录 —— 旧路径上的 ``deleted_file`` 与新路径上的 add（该文件注释写明
      first-class rename unit 要等未来用 ``-M`` 的那一刀）。而 git 自 2.9 起
      ``diff.renames`` 默认开启，``--name-only`` 对 rename **只吐新路径**，旧
      路径上那条 committed+available 的删除单元就永远交不上。这里显式关掉
      rename 检测与生产侧对齐，顺带也不再受用户 ``diff.renames`` 配置摆布。
      刻意不用 ``--name-status -M`` 再把 R 的两侧都收进来：那样在用户配了
      ``diff.renames=copy`` 时会把 copy 的**源**路径也算进窗口，而 copy 源根本
      没被改动，属于反方向的污染。
    * ``-z``：``core.quotePath`` 默认开启，非 ASCII 路径会被吐成
      ``"\\344\\270\\255..."`` 这种加引号的转义形式，与 units 表里存的原始路径
      永不相等（实测 6 个真实 tracker 库共 348 条非 ASCII 路径会因此全部交不
      上）。``-z`` 输出原始字节并以 NUL 分隔，生产侧同样用 ``-z``。
    """

    exit_code, output = run_git(
        repository_directory,
        "diff",
        "--no-renames",
        "--name-only",
        "-z",
        f"{older_commit}..{newer_commit}",
    )
    if exit_code != 0:
        return set()
    return {entry for entry in output.split("\0") if entry}


def unit_paths_relevant_to_window_intersection(unit: Mapping[str, Any]) -> set[str]:
    """一个 unit 拿来与窗口路径集合求交时，应当比对的全部路径。

    ``units.old_path`` 目前**恒为 NULL**：生产侧每个 unit spec 都写死
    ``old_path=None``，实测 25 个 tracker 库 0 条非空，schema 注释也写明它是留
    给「未来用 ``-M`` 做 first-class rename unit」那一刀的预留列。这里仍然把它
    纳入比对，是为了让那一刀落地时本函数不必再改：届时 rename 会变成「单条
    unit + ``old_path``」，若只认 ``path``，就会在窗口只覆盖旧路径的情形下漏掉
    它。多认一个路径只会让 ``unreviewed_count`` **偏大**，而偏大是安全方向
    （safe 策略更不敢前移、``written_off_unit_ids`` 记得更全）；偏小才会静默丢
    单元。
    """

    candidate_paths: set[str] = set()
    for column_name in ("path", "old_path"):
        value = unit.get(column_name)
        if isinstance(value, str) and value:
            candidate_paths.add(value)
    return candidate_paths


def commit_count_between(
    repository_directory: Path,
    older_commit: str,
    newer_commit: str,
) -> int | None:
    exit_code, output = run_git(
        repository_directory, "rev-list", "--count", f"{older_commit}..{newer_commit}"
    )
    if exit_code != 0:
        return None
    try:
        return int(output.strip())
    except ValueError:
        return None


def choose_review_highwater(
    *,
    task_marker_name: str,
    legacy_marker: Mapping[str, Any] | None,
    canonical_marker: Mapping[str, Any] | None,
    legacy_marker_file_present_but_unreadable: bool = False,
    canonical_marker_file_present_but_unreadable: bool = False,
    merged_units_for_marker_worktree: Sequence[Mapping[str, Any]] | None,
    conflict_policy: str = DEFAULT_HIGHWATER_CONFLICT_POLICY,
) -> ReviewHighwaterReconciliation:
    """裁决一个 task 的 review highwater。

    **祖先关系单独不足以裁决。** 两个真实用例方向恰好相反，而且形态完全一样
    （都是「legacy 更旧、canonical 更新且是其后代」）：

    - 过触发用例（``/rvf-land`` 封窗后）：窗口内的 commit 已经被审/被豁免，
      不前移就会每次 Stop 都重新圈出同一批范围。
    - 漏审用例：窗口内躺着 9 个 ``committed`` + ``available`` 的单元，前移会把
      它们挡在窗口外；而下一次 refresh 的「缺席即 superseded」会把它们改写成
      ``superseded``，候选查询与并集查询双双排除 ⇒ 自动路径上取不回来。

    所以本函数**无论采用哪种策略都会完整计算**「窗口内还有多少未审的 committed
    单元」，并把它们的 unit_id 落进 ``written_off_unit_ids``。策略只决定拿这份
    证据做什么，见 ``HIGHWATER_CONFLICT_POLICY_*``。因此本函数必须在 units
    合并之后调用。

    归因按 **worktree 身份**而非路径。highwater marker 是 per-task 的，而每个
    Kanban task 有自己的 worktree，``units.worktree_key`` 又是内容派生、跨根
    一致（实测 ``ac08e`` 在两根都是 ``284c53c98d80``），所以 worktree 是这里最
    准的键。早先试过的「路径落在窗口 diff 里」太粗：像
    ``stop_review_validate_fix.py`` 这种几乎每个 commit 都碰的文件，会把别的
    task、别的月份的陈旧单元全都算进窗口。

    在 worktree 之上再与窗口 diff 求交，作为第二重收窄。

    **入口先验证 marker 本身，读不出水位就 fail-closed。** 判据与本工具其余部分
    一致（未登记的表直接拒绝、repo 身份不一致直接拒绝、canonical 根与 legacy 根
    重叠直接拒绝）：迁移写回的是**新的真相源**，一个猜出来的水位会被后续每一次
    Stop 当成事实。「读不出来」不等于「没有」——把损坏 marker 当缺席处理，就会
    静默采用对侧的水位；若对侧恰好更新，前移会把窗口内未审的 committed 单元推出
    观察窗口，下一次 refresh 把它们改写成 ``superseded``，自动路径上再也取不
    回来。宁可在对账表上留一条要人看的拒绝，也不要产生一个不可逆的排除。
    """

    legacy_marker_classification, legacy_head = classify_review_highwater_marker_side(
        legacy_marker,
        marker_file_present_but_unreadable=legacy_marker_file_present_but_unreadable,
    )
    canonical_marker_classification, canonical_head = classify_review_highwater_marker_side(
        canonical_marker,
        marker_file_present_but_unreadable=canonical_marker_file_present_but_unreadable,
    )
    legacy_repository = (legacy_marker or {}).get("repo")
    canonical_repository = (canonical_marker or {}).get("repo")
    conflicts: list[str] = []

    # 任何一侧「声称有水位却读不出水位」⇒ 拒绝裁决这个 task，交人工。
    # 两侧同时缺席也拒绝：调用方只会为「至少一侧存在」的 marker 名调用本函数，
    # 真出现两侧皆无就说明扫目录与裁决之间对不上，同样不该猜。
    for side_name, classification in (
        ("legacy", legacy_marker_classification),
        ("canonical", canonical_marker_classification),
    ):
        if classification in MARKER_SIDE_CLASSIFICATIONS_REQUIRING_FAIL_CLOSED_REFUSAL:
            conflicts.append(f"{side_name}_{classification}")
    if legacy_marker_classification == canonical_marker_classification == MARKER_SIDE_ABSENT:
        conflicts.append("no_readable_review_highwater_marker_on_either_side")
    if conflicts:
        return ReviewHighwaterReconciliation(
            task_marker_name=task_marker_name,
            legacy_reviewed_head=legacy_head,
            canonical_reviewed_head=canonical_head,
            legacy_repository=legacy_repository if isinstance(legacy_repository, str) else None,
            canonical_repository=(
                canonical_repository if isinstance(canonical_repository, str) else None
            ),
            ancestry_relation="review_highwater_marker_unreadable",
            chosen_reviewed_head=None,
            chosen_side="refused",
            commit_count_in_window=None,
            unreviewed_committed_unit_count_in_window=None,
            conflict_classes=conflicts,
        )

    repository_directory: Path | None = None
    for candidate in (canonical_repository, legacy_repository):
        if isinstance(candidate, str) and Path(candidate).is_dir():
            repository_directory = Path(candidate)
            break

    if (
        isinstance(legacy_repository, str)
        and isinstance(canonical_repository, str)
        and legacy_repository != canonical_repository
    ):
        # marker key 只有 task id，两个不同的仓可能撞同一个 key（实测确有 1 例）。
        # 只有当两个路径属于同一个 git common dir 时才算同一个仓。
        legacy_common = git_common_directory_of(Path(legacy_repository))
        canonical_common = git_common_directory_of(Path(canonical_repository))
        if legacy_common is None or canonical_common is None or legacy_common != canonical_common:
            conflicts.append("repository_identity_mismatch_refusing_to_merge_marker")
            return ReviewHighwaterReconciliation(
                task_marker_name=task_marker_name,
                legacy_reviewed_head=legacy_head,
                canonical_reviewed_head=canonical_head,
                legacy_repository=legacy_repository,
                canonical_repository=canonical_repository,
                ancestry_relation="repository_identity_mismatch",
                chosen_reviewed_head=None,
                chosen_side="refused",
                commit_count_in_window=None,
                unreviewed_committed_unit_count_in_window=None,
                conflict_classes=conflicts,
            )

    if legacy_head and not canonical_head:
        return ReviewHighwaterReconciliation(
            task_marker_name, legacy_head, canonical_head, legacy_repository,
            canonical_repository, "single_side_only", legacy_head, "legacy", None, None, conflicts,
        )
    if canonical_head and not legacy_head:
        return ReviewHighwaterReconciliation(
            task_marker_name, legacy_head, canonical_head, legacy_repository,
            canonical_repository, "single_side_only", canonical_head, "canonical", None, None,
            conflicts,
        )

    relation = ancestry_relation_between(repository_directory, legacy_head, canonical_head)

    if relation == "identical":
        return ReviewHighwaterReconciliation(
            task_marker_name, legacy_head, canonical_head, legacy_repository,
            canonical_repository, relation, legacy_head, "identical", 0, 0, conflicts,
        )

    if relation in {"repository_unavailable", "commit_missing_from_repository"}:
        conflicts.append("ancestry_unverifiable_falling_back_to_manual_review")
        return ReviewHighwaterReconciliation(
            task_marker_name, legacy_head, canonical_head, legacy_repository,
            canonical_repository, relation, None, "refused", None, None, conflicts,
        )

    # 入口的 marker 校验保证了走到这里时两侧都是合法全 sha，因此
    # ``ancestry_relation_between`` 不可能再返回 ``single_side_only``；而
    # ``repository_unavailable`` 已在上面提前返回，故此处 repo 目录必然存在。
    assert repository_directory is not None

    if relation == "diverged":
        exit_code, merge_base = run_git(
            repository_directory, "merge-base", str(legacy_head), str(canonical_head)
        )
        conflicts.append("diverged_heads_fell_back_to_merge_base")
        # merge-base 也可能失败（无共同祖先）或吐出非 sha；同样不许把它当水位写回。
        chosen = merge_base.strip() if exit_code == 0 else None
        if chosen is not None and not REVIEW_HIGHWATER_FULL_COMMIT_SHA_PATTERN.match(chosen):
            conflicts.append("merge_base_output_is_not_a_full_40_hex_commit_sha")
            chosen = None
        if chosen is None:
            conflicts.append("no_provable_common_ancestor_refusing_to_choose_highwater")
        return ReviewHighwaterReconciliation(
            task_marker_name, legacy_head, canonical_head, legacy_repository,
            canonical_repository, relation, chosen, "merge_base" if chosen else "refused",
            None, None, conflicts,
        )

    if relation == "left_is_ancestor_of_right":
        older_commit, newer_commit = str(legacy_head), str(canonical_head)
        newer_side = "canonical"
    else:
        older_commit, newer_commit = str(canonical_head), str(legacy_head)
        newer_side = "legacy"

    commit_count = commit_count_between(repository_directory, older_commit, newer_commit)
    changed_paths = changed_paths_between(repository_directory, older_commit, newer_commit)

    unreviewed_unit_ids: list[str] = []
    unreviewed_count: int | None = None
    if merged_units_for_marker_worktree is not None:
        for unit in merged_units_for_marker_worktree:
            if (
                unit.get("observed_state") == "committed"
                and unit.get("review_state") in UNIT_LIVE_REVIEW_STATES
                and not unit.get("is_tombstoned")
                # ``not changed_paths`` 是刻意保留的 fail-open：diff 跑失败
                # （run_git 非零）或窗口确实为空时都会得到空集，此时**不做**这层
                # 收窄，宁可把整个 worktree 的候选都算进来 —— 偏大是安全方向。
                and (
                    not changed_paths
                    or bool(unit_paths_relevant_to_window_intersection(unit) & changed_paths)
                )
            ):
                unit_identifier = unit.get("unit_id")
                if isinstance(unit_identifier, str):
                    unreviewed_unit_ids.append(unit_identifier)
        unreviewed_count = len(unreviewed_unit_ids)
    else:
        conflicts.append("no_tracker_units_resolved_for_marker_worktree")

    written_off_unit_ids: list[str] = []
    if unreviewed_count == 0:
        chosen, chosen_side = newer_commit, newer_side
    elif conflict_policy == HIGHWATER_CONFLICT_POLICY_ADVANCE_TO_DESCENDANT:
        # owner 的裁决：分歧时一律取后代。窗口内未审的 committed 单元会被推出
        # 观察窗口 —— 证据照算、照落账，但不改变选择。
        chosen, chosen_side = newer_commit, newer_side
        # 排序落账：unreviewed_unit_ids 的顺序继承自 units 合并时的
        # ``set(legacy) | set(canonical)`` 迭代序，而 str 的 hash 每进程加盐，
        # 于是同样的两根跑两次会得到顺序不同、成员相同的账本，diff 不掉。
        # 这是**迁移账本**（被推出观察窗口的单元只剩这一条回溯路径），必须可比。
        written_off_unit_ids = sorted(unreviewed_unit_ids)
        conflicts.append(
            "advanced_past_unreviewed_committed_units"
            if unreviewed_count
            else "advanced_without_tracker_unit_evidence"
        )
    elif unreviewed_count is None:
        chosen, chosen_side = older_commit, "safe_lower_bound"
    else:
        chosen, chosen_side = older_commit, "safe_lower_bound"
        conflicts.append("window_still_holds_unreviewed_committed_units")

    return ReviewHighwaterReconciliation(
        task_marker_name=task_marker_name,
        legacy_reviewed_head=legacy_head,
        canonical_reviewed_head=canonical_head,
        legacy_repository=legacy_repository,
        canonical_repository=canonical_repository,
        ancestry_relation=relation,
        chosen_reviewed_head=chosen,
        chosen_side=chosen_side,
        commit_count_in_window=commit_count,
        unreviewed_committed_unit_count_in_window=unreviewed_count,
        conflict_classes=conflicts,
        written_off_unit_ids=written_off_unit_ids,
    )


def git_common_directory_of(worktree_path: Path) -> str | None:
    if not worktree_path.is_dir():
        return None
    exit_code, output = run_git(worktree_path, "rev-parse", "--git-common-dir")
    if exit_code != 0:
        return None
    candidate = Path(output.strip())
    if not candidate.is_absolute():
        candidate = (worktree_path / candidate).resolve()
    return str(candidate)


# ---------------------------------------------------------------------------
# 对账报告
# ---------------------------------------------------------------------------


@dataclass
class TrackerRepositoryReconciliation:
    repository_key: str
    presence: str
    git_common_directory_matches: bool
    unregistered_tables: list[str] = field(default_factory=list)
    row_counts_by_table: dict[str, dict[str, int]] = field(default_factory=dict)
    unit_decision_class_counts: dict[str, int] = field(default_factory=dict)
    undrained_lease_count: int = 0
    orphan_assigned_unit_count: int = 0
    legacy_root_absolute_path_reference_counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def count_legacy_root_absolute_path_references(
    connection: sqlite3.Connection,
    legacy_root: Path,
    available_tables: set[str],
) -> dict[str, int]:
    """统计还有多少行把绝对路径指向 legacy 根。

    runs 目录一移，这些引用全部变成死路。其中
    ``rvf_fix_attempts.baseline_overlay_path`` / ``fix_patch_path`` 会被 fix 复现
    流程直接读，``rvf_issues.artifact_path`` 会被 handoff / analyze 读——
    「历史 run 失效可接受」这个说法在这里不成立。
    """

    legacy_root_prefix = f"{legacy_root}%"
    counts: dict[str, int] = {}
    for table_name, columns in ABSOLUTE_PATH_COLUMNS_BY_TABLE.items():
        if table_name not in available_tables:
            continue
        for column in columns:
            count = connection.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE {column} LIKE ?",
                (legacy_root_prefix,),
            ).fetchone()[0]
            if count:
                counts[f"{table_name}.{column}"] = counts.get(f"{table_name}.{column}", 0) + count
    return counts


def reconcile_tracker_repository(
    pair: TrackerRepositoryPair,
    *,
    legacy_root: Path,
) -> tuple[TrackerRepositoryReconciliation, dict[str, list[dict[str, Any]]]]:
    """只读地算出一个 repo 的合并结果。

    返回对账摘要，以及**按 worktree 路径分组的合并后 unit**——highwater 决策要
    的就是这个（marker 是 per-task 的，task 与 worktree 一一对应）。
    """

    presence = (
        "both"
        if pair.present_in_both_roots
        else ("legacy_only" if pair.legacy_directory else "canonical_only")
    )
    reconciliation = TrackerRepositoryReconciliation(
        repository_key=pair.repository_key,
        presence=presence,
        git_common_directory_matches=pair.git_common_directory_matches,
    )
    merged_units_by_worktree_path: dict[str, list[dict[str, Any]]] = {}

    if not pair.git_common_directory_matches:
        reconciliation.notes.append(
            "两根同名 repo_key 的 git_common_dir 不一致，拒绝合并该 repo"
        )
        return reconciliation, merged_units_by_worktree_path

    connections: dict[str, sqlite3.Connection] = {}
    try:
        if pair.legacy_directory is not None:
            connections["legacy"] = open_sqlite_readonly(
                pair.legacy_directory / TRACKER_DATABASE_FILE_NAME
            )
        if pair.canonical_directory is not None:
            connections["canonical"] = open_sqlite_readonly(
                pair.canonical_directory / TRACKER_DATABASE_FILE_NAME
            )

        # 表清单从 DDL 反射；未登记的表 fail-closed，不静默漏掉。
        observed_tables: set[str] = set()
        for connection in connections.values():
            observed_tables |= table_names_in_database(connection)
        reconciliation.unregistered_tables = sorted(
            observed_tables - set(TRACKER_TABLE_MERGE_STRATEGIES)
        )

        for table_name in sorted(observed_tables & set(TRACKER_TABLE_MERGE_STRATEGIES)):
            counts: dict[str, int] = {}
            for side, connection in connections.items():
                if table_name not in table_names_in_database(connection):
                    counts[side] = 0
                    continue
                counts[side] = connection.execute(
                    f"SELECT COUNT(*) FROM {table_name}"
                ).fetchone()[0]
            reconciliation.row_counts_by_table[table_name] = counts

        # worktree_key → worktree_path（内容派生的 key，两根一致，可直接并集）
        worktree_path_by_key: dict[str, str] = {}
        for connection in connections.values():
            if "worktrees" not in table_names_in_database(connection):
                continue
            for row in connection.execute(
                "SELECT worktree_key, worktree_path FROM worktrees"
            ).fetchall():
                worktree_path_by_key.setdefault(row["worktree_key"], row["worktree_path"])

        # units 合并（真正的决策发生在这里）
        units_strategy = TRACKER_TABLE_MERGE_STRATEGIES["units"]
        legacy_units = (
            fetch_table_rows_keyed_by_primary_key(connections["legacy"], units_strategy)
            if "legacy" in connections
            else {}
        )
        canonical_units = (
            fetch_table_rows_keyed_by_primary_key(connections["canonical"], units_strategy)
            if "canonical" in connections
            else {}
        )
        for unit_key in set(legacy_units) | set(canonical_units):
            outcome = merge_unit_rows(legacy_units.get(unit_key), canonical_units.get(unit_key))
            for decision in outcome.decision_classes:
                reconciliation.unit_decision_class_counts[decision] = (
                    reconciliation.unit_decision_class_counts.get(decision, 0) + 1
                )
            worktree_path = worktree_path_by_key.get(outcome.merged_row.get("worktree_key", ""))
            if worktree_path:
                merged_units_by_worktree_path.setdefault(worktree_path, []).append(
                    outcome.merged_row
                )

        # 未 drain 的 lease
        for side, connection in connections.items():
            if "leases" not in table_names_in_database(connection):
                continue
            placeholders = ",".join("?" for _ in UNDRAINED_LEASE_STATES)
            count = connection.execute(
                f"SELECT COUNT(*) FROM leases WHERE state IN ({placeholders})",
                tuple(sorted(UNDRAINED_LEASE_STATES)),
            ).fetchone()[0]
            reconciliation.undrained_lease_count += count

        # 已经存在的孤儿 assigned（F7 invariant 3 的当前违规数）
        for side, connection in connections.items():
            tables = table_names_in_database(connection)
            if "units" not in tables or "lease_units" not in tables:
                continue
            count = connection.execute(
                "SELECT COUNT(*) FROM units u WHERE u.review_state='assigned' "
                "AND NOT EXISTS (SELECT 1 FROM lease_units lu WHERE lu.unit_id=u.unit_id)"
            ).fetchone()[0]
            reconciliation.orphan_assigned_unit_count += count

        # 指向 legacy 根的绝对路径引用（runs 一移就全变死路）
        for connection in connections.values():
            for reference, count in count_legacy_root_absolute_path_references(
                connection, legacy_root, table_names_in_database(connection)
            ).items():
                reconciliation.legacy_root_absolute_path_reference_counts[reference] = (
                    reconciliation.legacy_root_absolute_path_reference_counts.get(reference, 0)
                    + count
                )
    finally:
        for connection in connections.values():
            connection.close()

    return reconciliation, merged_units_by_worktree_path


@dataclass(frozen=True)
class MarkerDirectoryScan:
    """一个 marker 目录的扫描结果。

    刻意把「解析失败的文件」单独带出来而不是就地丢掉：丢掉之后，一个损坏的
    marker 与一个根本不存在的 marker 在下游完全不可区分，于是损坏会伪装成缺席，
    对账表上连一行都不留（实测：两侧文件都损坏时，该 task 会整个从对账表里消
    失）。迁移必须能看见自己读不懂的东西。
    """

    parsed_documents_by_marker_name: dict[str, dict[str, Any]]
    marker_names_whose_file_could_not_be_parsed: set[str]


def collect_marker_documents(root: Path, subdirectory: str) -> MarkerDirectoryScan:
    directory = root / subdirectory
    if not directory.is_dir():
        return MarkerDirectoryScan({}, set())
    documents: dict[str, dict[str, Any]] = {}
    unparseable_marker_names: set[str] = set()
    for path in sorted(directory.glob("*.json")):
        payload = read_json_document(path)
        if payload is None:
            unparseable_marker_names.add(path.stem)
            continue
        documents[path.stem] = payload
    return MarkerDirectoryScan(documents, unparseable_marker_names)


def build_reconciliation_report(
    legacy_root: Path,
    canonical_root: Path,
    *,
    include_repositories_without_working_directory: bool,
    conflict_policy: str = DEFAULT_HIGHWATER_CONFLICT_POLICY,
    canonical_root_provenance_description: str | None = None,
) -> dict[str, Any]:
    pairs = discover_tracker_repository_pairs(legacy_root, canonical_root)

    repository_reconciliations: list[TrackerRepositoryReconciliation] = []
    merged_units_by_worktree_path: dict[str, list[dict[str, Any]]] = {}
    skipped_repository_keys: list[str] = []

    for pair in pairs:
        working_directory = pair.repository_working_directory
        if working_directory is None and not include_repositories_without_working_directory:
            # 绝大多数是测试残留（临时目录早已消失）。默认过滤，否则对账表会被
            # 几百个噪声条目淹没；用 --include-orphan-repositories 可以看全量。
            skipped_repository_keys.append(pair.repository_key)
            continue
        reconciliation, units_by_worktree_path = reconcile_tracker_repository(
            pair, legacy_root=legacy_root
        )
        repository_reconciliations.append(reconciliation)
        for worktree_path, units in units_by_worktree_path.items():
            merged_units_by_worktree_path.setdefault(worktree_path, []).extend(units)

    legacy_highwater = collect_marker_documents(legacy_root, REVIEW_HIGHWATER_SUBDIRECTORY)
    canonical_highwater = collect_marker_documents(canonical_root, REVIEW_HIGHWATER_SUBDIRECTORY)

    highwater_reconciliations: list[ReviewHighwaterReconciliation] = []
    # 解析失败的 marker 名也要进这个并集，否则「两侧文件都损坏」的 task 会整个
    # 从对账表里消失——迁移悄悄丢掉一个 task 的水位，比留一行拒绝危险得多。
    for marker_name in sorted(
        set(legacy_highwater.parsed_documents_by_marker_name)
        | set(canonical_highwater.parsed_documents_by_marker_name)
        | legacy_highwater.marker_names_whose_file_could_not_be_parsed
        | canonical_highwater.marker_names_whose_file_could_not_be_parsed
    ):
        legacy_marker = legacy_highwater.parsed_documents_by_marker_name.get(marker_name)
        canonical_marker = canonical_highwater.parsed_documents_by_marker_name.get(marker_name)
        marker_worktree_path = (canonical_marker or legacy_marker or {}).get("repo")
        units_for_worktree = (
            merged_units_by_worktree_path.get(marker_worktree_path)
            if isinstance(marker_worktree_path, str)
            else None
        )
        highwater_reconciliations.append(
            choose_review_highwater(
                task_marker_name=marker_name,
                legacy_marker=legacy_marker,
                canonical_marker=canonical_marker,
                legacy_marker_file_present_but_unreadable=(
                    marker_name in legacy_highwater.marker_names_whose_file_could_not_be_parsed
                ),
                canonical_marker_file_present_but_unreadable=(
                    marker_name
                    in canonical_highwater.marker_names_whose_file_could_not_be_parsed
                ),
                merged_units_for_marker_worktree=units_for_worktree,
                conflict_policy=conflict_policy,
            )
        )

    legacy_round_baseline = collect_marker_documents(
        legacy_root, ROUND_BASELINE_PENDING_SUBDIRECTORY
    )
    canonical_round_baseline = collect_marker_documents(
        canonical_root, ROUND_BASELINE_PENDING_SUBDIRECTORY
    )
    legacy_round_baseline_documents = legacy_round_baseline.parsed_documents_by_marker_name
    canonical_round_baseline_documents = canonical_round_baseline.parsed_documents_by_marker_name
    round_baseline_identity_conflicts = [
        {
            "marker": marker_name,
            "legacy_repository": legacy_round_baseline_documents[marker_name].get("repo"),
            "canonical_repository": canonical_round_baseline_documents[marker_name].get("repo"),
        }
        for marker_name in sorted(
            set(legacy_round_baseline_documents) & set(canonical_round_baseline_documents)
        )
        if legacy_round_baseline_documents[marker_name].get("repo")
        != canonical_round_baseline_documents[marker_name].get("repo")
    ]

    # 解析失败的 marker 文件逐个报出来（不止 highwater）。round-baseline 这一侧
    # 目前只参与「仓身份冲突」这一项只读比较、不写回任何水位，所以这里只报不拒；
    # 但它同样不该被静默跳过，否则「比过了」与「没读到」在报告上无从区分。
    unparseable_marker_files_by_root_and_subdirectory = {
        f"{side}:{subdirectory}": sorted(scan.marker_names_whose_file_could_not_be_parsed)
        for side, subdirectory, scan in (
            ("legacy", REVIEW_HIGHWATER_SUBDIRECTORY, legacy_highwater),
            ("canonical", REVIEW_HIGHWATER_SUBDIRECTORY, canonical_highwater),
            ("legacy", ROUND_BASELINE_PENDING_SUBDIRECTORY, legacy_round_baseline),
            ("canonical", ROUND_BASELINE_PENDING_SUBDIRECTORY, canonical_round_baseline),
        )
        if scan.marker_names_whose_file_could_not_be_parsed
    }

    legacy_runs = run_identifiers_in_root(legacy_root)
    canonical_runs = run_identifiers_in_root(canonical_root)

    return {
        "generated_at": utc_now_text(),
        "highwater_conflict_policy": conflict_policy,
        "legacy_root": str(legacy_root),
        "canonical_root": str(canonical_root),
        # 落账「canonical 根是被哪个旋钮定下来的」：一份存档的报告若不带这条，
        # 事后无法分辨它审的是默认安装根还是某个环境变量指过去的根。
        "canonical_root_provenance": canonical_root_provenance_description,
        "legacy_root_seconds_since_last_write": (
            None
            if not legacy_root.is_dir()
            else round(datetime.now(timezone.utc).timestamp() - most_recent_modification_time(legacy_root))
        ),
        "tracker_repositories": [
            {
                "repository_key": item.repository_key,
                "presence": item.presence,
                "git_common_directory_matches": item.git_common_directory_matches,
                "unregistered_tables": item.unregistered_tables,
                "row_counts_by_table": item.row_counts_by_table,
                "unit_decision_class_counts": item.unit_decision_class_counts,
                "undrained_lease_count": item.undrained_lease_count,
                "orphan_assigned_unit_count": item.orphan_assigned_unit_count,
                "legacy_root_absolute_path_reference_counts": (
                    item.legacy_root_absolute_path_reference_counts
                ),
                "notes": item.notes,
            }
            for item in repository_reconciliations
        ],
        "skipped_repository_key_count": len(skipped_repository_keys),
        "review_highwater": [
            {
                "task_marker": item.task_marker_name,
                "legacy_reviewed_head": item.legacy_reviewed_head,
                "canonical_reviewed_head": item.canonical_reviewed_head,
                "ancestry_relation": item.ancestry_relation,
                "chosen_reviewed_head": item.chosen_reviewed_head,
                "chosen_side": item.chosen_side,
                "commit_count_in_window": item.commit_count_in_window,
                "unreviewed_committed_unit_count_in_window": (
                    item.unreviewed_committed_unit_count_in_window
                ),
                "conflict_classes": item.conflict_classes,
                "written_off_unit_ids": item.written_off_unit_ids,
            }
            for item in highwater_reconciliations
        ],
        "round_baseline_repository_identity_conflicts": round_baseline_identity_conflicts,
        "unparseable_marker_files_by_root_and_subdirectory": (
            unparseable_marker_files_by_root_and_subdirectory
        ),
        "runs": {
            "legacy_count": len(legacy_runs),
            "canonical_count": len(canonical_runs),
            "colliding_run_identifiers": sorted(legacy_runs & canonical_runs),
        },
    }


def run_identifiers_in_root(root: Path) -> set[str]:
    runs_directory = root / RUNS_SUBDIRECTORY
    if not runs_directory.is_dir():
        return set()
    return {child.name for child in runs_directory.iterdir() if child.is_dir()}


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------


def render_reconciliation_report_text(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append("RVF 状态根合并 · 对账表")
    lines.append(f"  生成时刻      : {report['generated_at']}")
    lines.append(f"  legacy 根     : {report['legacy_root']}")
    canonical_root_provenance = report.get("canonical_root_provenance")
    lines.append(
        f"  canonical 根  : {report['canonical_root']}"
        + (f"（来源：{canonical_root_provenance}）" if canonical_root_provenance else "")
    )
    lines.append(f"  水位冲突策略  : {report['highwater_conflict_policy']}")
    idle_seconds = report.get("legacy_root_seconds_since_last_write")
    if idle_seconds is not None:
        quiescence = (
            "已静默" if idle_seconds >= LEGACY_ROOT_QUIESCENCE_MINIMUM_IDLE_SECONDS else "⚠️ 仍在被写"
        )
        lines.append(f"  legacy 根写入 : {idle_seconds}s 前（{quiescence}）")
    lines.append("")

    lines.append(f"── tracker 仓库（已过滤 {report['skipped_repository_key_count']} 个无本地路径的残留）")
    for item in report["tracker_repositories"]:
        units = item["row_counts_by_table"].get("units", {})
        leases = item["row_counts_by_table"].get("leases", {})
        lines.append(
            f"  {item['repository_key']:<48} {item['presence']:<15}"
            f" units L/C={units.get('legacy', 0)}/{units.get('canonical', 0)}"
            f" leases L/C={leases.get('legacy', 0)}/{leases.get('canonical', 0)}"
        )
        if item["unregistered_tables"]:
            lines.append(f"      ⛔ 未登记的表（fail-closed）: {item['unregistered_tables']}")
        if not item["git_common_directory_matches"]:
            lines.append("      ⛔ git_common_dir 不一致，拒绝合并")
        if item["undrained_lease_count"]:
            lines.append(f"      ⚠️ 未 drain 的 lease: {item['undrained_lease_count']}")
        if item["orphan_assigned_unit_count"]:
            lines.append(
                f"      ⚠️ 孤儿 assigned unit（无 lease_units 行）: "
                f"{item['orphan_assigned_unit_count']}"
            )
        for reference, count in sorted(
            item["legacy_root_absolute_path_reference_counts"].items()
        ):
            lines.append(f"      · 需重写的 legacy 绝对路径 {reference}: {count}")
        for decision, count in sorted(item["unit_decision_class_counts"].items()):
            if decision.startswith(("conflict_", "reopened_", "demoted_", "kept_", "preserved_")):
                lines.append(f"      · {decision}: {count}")
    lines.append("")

    lines.append("── review highwater（禁止按时间戳取较新）")
    # 祖先关系列宽按最长的取值给（``review_highwater_marker_unreadable`` 34 字符），
    # 否则拒绝行会把整行挤歪，而拒绝行恰恰是最需要被看清的那几行。
    header = (
        f"  {'task':<16}{'legacy':<10}{'canonical':<10}{'祖先关系':<36}"
        f"{'选定':<10}{'窗口commit':>10}{'窗口未审unit':>14}"
    )
    lines.append(header)
    for item in report["review_highwater"]:
        legacy_head = (item["legacy_reviewed_head"] or "-")[:8]
        canonical_head = (item["canonical_reviewed_head"] or "-")[:8]
        chosen = (item["chosen_reviewed_head"] or "-")[:8]
        commit_count = item["commit_count_in_window"]
        unreviewed = item["unreviewed_committed_unit_count_in_window"]
        lines.append(
            f"  {item['task_marker']:<16}{legacy_head:<10}{canonical_head:<10}"
            f"{item['ancestry_relation']:<36}{chosen:<10}"
            f"{'-' if commit_count is None else commit_count:>10}"
            f"{'-' if unreviewed is None else unreviewed:>14}"
        )
        for conflict in item["conflict_classes"]:
            lines.append(f"      ⚠️ {conflict}")
    refused_markers = [
        item for item in report["review_highwater"] if item["chosen_side"] == "refused"
    ]
    if refused_markers:
        # 65 行的表里一条 ⚠️ 很容易被读漏，所以再汇总一次：这些 task 迁移时没有
        # 可写回的水位，必须人工裁决，不能当成「已对账」。
        lines.append(
            f"  ⛔ 拒绝裁决（无可证明的水位，需人工处理）: {len(refused_markers)} 个 task"
            f" — {', '.join(item['task_marker'] for item in refused_markers)}"
        )
    lines.append("")

    unparseable_marker_files = report.get("unparseable_marker_files_by_root_and_subdirectory", {})
    if unparseable_marker_files:
        lines.append("── ⛔ 解析失败的 marker 文件（既不是缺席也不是可用，一律不得静默跳过）")
        for location, marker_names in sorted(unparseable_marker_files.items()):
            lines.append(f"  {location}: {len(marker_names)} 个 — {', '.join(marker_names)}")
        lines.append("")

    conflicts = report["round_baseline_repository_identity_conflicts"]
    lines.append(f"── round-baseline marker 仓身份冲突: {len(conflicts)}")
    for conflict in conflicts:
        lines.append(f"  ⛔ {conflict['marker']}")
        lines.append(f"      legacy    : {conflict['legacy_repository']}")
        lines.append(f"      canonical : {conflict['canonical_repository']}")
    lines.append("")

    written_off = [
        item for item in report["review_highwater"] if item.get("written_off_unit_ids")
    ]
    if written_off:
        total_written_off = sum(len(item["written_off_unit_ids"]) for item in written_off)
        lines.append(
            f"── ⚠️ 因水位前移被推出观察窗口的 committed 单元: "
            f"{total_written_off} 个，分布在 {len(written_off)} 个 task"
        )
        lines.append(
            "   这些单元下一次 refresh 会被改写成 superseded，自动路径上取不回来；"
            "unit_id 已逐个落进迁移账本。"
        )
        for item in written_off:
            lines.append(
                f"  · {item['task_marker']:<14} {len(item['written_off_unit_ids']):>4} 个"
                f"   {(item['legacy_reviewed_head'] or '-')[:8]} → "
                f"{(item['chosen_reviewed_head'] or '-')[:8]}"
            )
        lines.append("")

    runs = report["runs"]
    lines.append(
        f"── runs 目录: legacy {runs['legacy_count']} / canonical {runs['canonical_count']}"
        f"，run id 撞名 {len(runs['colliding_run_identifiers'])}"
    )
    for run_identifier in runs["colliding_run_identifiers"][:20]:
        lines.append(f"  ⛔ {run_identifier}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# canonical 根的裁决与重叠闸
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalStateRootResolution:
    canonical_state_root: Path
    resolution_provenance_description: str


def resolve_canonical_state_root_with_provenance(
    explicit_canonical_root_argument: Path | None,
    *,
    environment: Mapping[str, str] | None = None,
    user_local_data_run_logs_root: Path | None = None,
) -> CanonicalStateRootResolution:
    """裁决对账用的 canonical 根，并记录它是被哪个旋钮决定的。

    为什么不能只看 user-local-data 的默认值：``resolved_rvf_runtime_log_root()``
    才是**运行时真正读写状态的那个根**（``run_ledger.log_root()`` 就是它，
    tracker 的 ``diff-tracker/repos/...`` 全部挂在它下面）。用户若用
    ``CODEX_RVF_LOG_ROOT`` / ``CODEX_RVF_STATE_DIR`` 把状态搬到自定义安装根，
    只读 user-local-data 默认值就会去审一个空目录，把「真 canonical 根里已有的
    状态」整个漏掉，同时把 legacy 侧的每一行都误判成「canonical 缺失、需导入」。

    ``provenance`` 只是给对账表留的审计痕迹：路径永远由上面那个统一裁决点给出，
    这里的环境变量扫描不参与任何路径计算。
    """

    effective_environment = os.environ if environment is None else environment
    if explicit_canonical_root_argument is not None:
        return CanonicalStateRootResolution(
            canonical_state_root=explicit_canonical_root_argument.expanduser().resolve(),
            resolution_provenance_description="--canonical-root 显式指定",
        )

    resolved_root = resolved_rvf_runtime_log_root(environment=effective_environment)
    resolved_root = resolved_root.expanduser().resolve()
    default_run_logs_root = (
        user_local_data_run_logs_root
        if user_local_data_run_logs_root is not None
        else resolved_rvf_user_local_data_paths(environment=effective_environment).run_logs_root
    ).expanduser().resolve()

    for environment_variable_name in (
        RUNTIME_LOG_ROOT_OVERRIDE_ENVIRONMENT_VARIABLE_NAMES_FOR_PROVENANCE_LABELLING
    ):
        configured_value = effective_environment.get(environment_variable_name, "").strip()
        if configured_value and Path(configured_value).expanduser().resolve() == resolved_root:
            return CanonicalStateRootResolution(
                canonical_state_root=resolved_root,
                resolution_provenance_description=f"环境变量 {environment_variable_name}",
            )
    if resolved_root == default_run_logs_root:
        return CanonicalStateRootResolution(
            canonical_state_root=resolved_root,
            resolution_provenance_description="user-local-data 默认 run-logs 根",
        )
    # 裁决点将来若新增覆盖来源，这里宁可承认「不认识」，也不谎称是默认根。
    return CanonicalStateRootResolution(
        canonical_state_root=resolved_root,
        resolution_provenance_description="runtime log root 裁决点（未识别的覆盖来源）",
    )


def overlapping_state_root_rejection_message(
    legacy_root: Path,
    canonical_root_resolution: CanonicalStateRootResolution,
) -> str | None:
    """两根重叠时的 fail-closed 理由；不重叠返回 None。

    这是「改用运行时裁决点」的必要配套闸。本工具最常被运行的位置——Claude host
    的 hook 后代进程、以及 source 过 ``review-env.sh`` 的 reviewer / fix 子进程
    ——环境里的 ``CODEX_RVF_LOG_ROOT`` 恰恰**仍指着 legacy 根**（部署版 hook shim
    的 ``env.setdefault(..., ~/.claude/rvf)``，正是本工具要收拾的那次分裂）。
    此时若照单全收，canonical 就等于 legacy，对账表会拿 legacy 根跟它自己比，
    每一行都「一致」、每个水位都 ``identical``，得出「两根无分歧、无需合并」的
    假绿——比原本审错目录更糟，因为它看上去是通过的。

    嵌套同样不合法：任一根落在另一根内部时，``runs/`` 枚举与 tracker 配对都会把
    同一批状态数进两侧。
    """

    canonical_root = canonical_root_resolution.canonical_state_root
    if canonical_root == legacy_root:
        overlap_description = "canonical 根与 legacy 根是同一个目录"
    elif legacy_root in canonical_root.parents:
        overlap_description = "canonical 根嵌套在 legacy 根内部"
    elif canonical_root in legacy_root.parents:
        overlap_description = "legacy 根嵌套在 canonical 根内部"
    else:
        return None
    return (
        f"{overlap_description}，拒绝对账："
        f"legacy={legacy_root} / canonical={canonical_root}"
        f"（canonical 来源：{canonical_root_resolution.resolution_provenance_description}）。"
        "若来源是环境变量，通常说明当前进程继承了仍指向 legacy 根的 "
        "CODEX_RVF_LOG_ROOT（部署版 Claude hook shim 的 setdefault、或 review-env.sh "
        "传给子进程的那份）。请显式传 --canonical-root <真正的 canonical 根> 再跑。"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--legacy-root",
        type=Path,
        default=LEGACY_CLAUDE_SCOPED_STATE_ROOT,
        help=f"legacy 状态根（默认 {LEGACY_CLAUDE_SCOPED_STATE_ROOT}）",
    )
    parser.add_argument(
        "--canonical-root",
        type=Path,
        default=None,
        help=(
            "canonical 状态根（默认由 resolved_rvf_runtime_log_root 裁决，即 "
            "CODEX_RVF_LOG_ROOT → CODEX_RVF_STATE_DIR → user-local-data 的 run-logs）。"
            "在 hook 后代进程 / reviewer 子进程里该环境变量往往仍指着 legacy 根，"
            "此时两根重叠会被 fail-closed 拒绝，必须用本参数显式指定。"
        ),
    )
    parser.add_argument(
        "--highwater-conflict-policy",
        choices=(
            HIGHWATER_CONFLICT_POLICY_ADVANCE_TO_DESCENDANT,
            HIGHWATER_CONFLICT_POLICY_ADVANCE_ONLY_WHEN_PROVABLY_CLOSED,
        ),
        default=DEFAULT_HIGHWATER_CONFLICT_POLICY,
        help=(
            "两个 highwater 分歧时怎么选。两种策略都会完整计算并落账窗口内未审的 "
            "committed 单元，区别只在拿这份证据做什么（默认 advance-to-descendant）。"
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    report_parser = subparsers.add_parser(
        "report", help="只读对账（默认子命令）；什么都不写"
    )
    report_parser.add_argument(
        "--include-orphan-repositories",
        action="store_true",
        help="连本地路径已消失的 repo（几乎全是测试残留）一起列出",
    )
    report_parser.add_argument("--json", action="store_true", help="输出 JSON 而非人读表格")
    report_parser.add_argument(
        "--output", type=Path, default=None, help="把报告同时写到该文件"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    command = arguments.command or "report"

    canonical_root_resolution = resolve_canonical_state_root_with_provenance(
        arguments.canonical_root
    )
    canonical_root = canonical_root_resolution.canonical_state_root
    legacy_root = arguments.legacy_root.expanduser().resolve()

    overlap_rejection = overlapping_state_root_rejection_message(
        legacy_root, canonical_root_resolution
    )
    if overlap_rejection is not None:
        parser.error(overlap_rejection)

    if command != "report":
        parser.error(f"未实现的子命令: {command}")

    if not legacy_root.is_dir():
        print(f"legacy 根不存在，无需合并: {legacy_root}")
        return 0

    report = build_reconciliation_report(
        legacy_root,
        canonical_root,
        conflict_policy=arguments.highwater_conflict_policy,
        include_repositories_without_working_directory=getattr(
            arguments, "include_orphan_repositories", False
        ),
        canonical_root_provenance_description=(
            canonical_root_resolution.resolution_provenance_description
        ),
    )

    rendered = (
        json.dumps(report, ensure_ascii=False, indent=2)
        if getattr(arguments, "json", False)
        else render_reconciliation_report_text(report)
    )
    print(rendered)
    output_path = getattr(arguments, "output", None)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
