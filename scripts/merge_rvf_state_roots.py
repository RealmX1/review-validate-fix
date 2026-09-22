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
``verify``   校验 staging 结果的 FK / 状态 invariant / 路径引用；并回读账本，把
             「有 marker 无法裁决」与「merge 之后源又被写过」一律判成阻断——这两类
             都是**不会在 staging 里留下痕迹**的缺陷，只看产物永远看不见。
``switch``   分阶段换入 canonical 根，并把 legacy 根改名、整棵封成只读归档。
             整条序列**不是**一次原子操作：每个阶段完成即写 staging 里的阶段账本
             （``switch-stage-journal.json``），崩溃后用同一份 staging 重跑即从
             中断处续跑；此时全量校验不会重跑（源库已变成合并结果，那道闸必然误报），
             改由账本承担「校验已通过」这一凭据。见 ``run_switch_command``。

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
import fnmatch
import hashlib
import json
import os
import re
import shutil
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

# 刻意复用 tracker **自己**的 schema 迁移入口，而不是在这里重写一遍 ALTER：两根的
# 库很可能停在不同的 schema 版本（实测 review-validate-fix 那个仓，legacy 侧已带
# ``review_not_required_declared_*`` 三列——Fix D 的 decline 是以
# ``CODEX_RVF_LOG_ROOT=~/.claude/rvf`` 跑的，于是只有那一侧被顺带迁了；canonical 侧
# 还没被新代码打开过）。迁移工具若自带一份 ALTER 清单，就又多出一个会跟真相源漂移的
# 副本——正是本工具在收拾的那类缺陷。
from core.session_scope_allocation.reviewable_unit_diff_tracker import (  # noqa: E402
    _ensure_schema as ensure_tracker_schema_is_current,
)

# marker 的目标文件名一律由**运行时**的 ``marker_paths()`` 决定（见
# ``runtime_marker_file_name_for_migrated_marker``）。同上一条：绝不在这里另写一份命名规则。
from core.host_adapter.parent_harness_identity import (  # noqa: E402
    PARENT_HARNESS_IDENTITY_UNSPECIFIED,
    normalize_parent_harness_identity,
)
from core.session_scope_allocation import review_highwater_marker  # noqa: E402
from core.session_scope_allocation import round_baseline_marker  # noqa: E402
from core.session_scope_allocation.scope_marker_identity_key import (  # noqa: E402
    MARKER_FILE_NAME_SEGMENT_SEPARATOR,
    PARENT_HARNESS_IDENTITY_FILE_NAME_SEGMENT_PREFIX,
    REPOSITORY_IDENTITY_FILE_NAME_SEGMENT_PREFIX,
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

# 写入顺序必须尊重外键：被引用的表先写。``merge`` 期间 ``PRAGMA foreign_keys`` 关闭
# （见 ``merge_tracker_repository_into_staging``），但顺序仍按拓扑排好，这样
# ``PRAGMA foreign_key_check`` 报出的违规必然是**数据本身**的悬挂引用，而不是写入
# 次序造成的假阳性——否则每次合并都会冒出一堆要人去分辨真假的噪声。
TRACKER_TABLE_INSERTION_ORDER_RESPECTING_FOREIGN_KEYS = (
    "meta",
    "branches",
    "worktrees",
    "units",
    "sessions",
    "session_units",
    "edit_claims",
    "edit_claim_units",
    "manual_rvf_runs",
    "leases",
    "lease_units",
    "lease_participants",
    "tombstones",
    "rvf_issues",
    "rvf_fix_attempts",
    "rvf_fix_patch_events",
    "rvf_issue_patch_links",
)


# ---------------------------------------------------------------------------
# 状态根顶层条目登记表：未登记即 fail-closed（与表登记表同一原则）
# ---------------------------------------------------------------------------

# 起初这份计划只列了四处分裂面（``runs`` / ``diff-tracker`` / ``review-highwater``
# / ``round-baseline-pending``）。实测磁盘发现**至少还有四处**同样是活的、同样分裂：
# ``handoff-notified``（legacy 238 / canonical 5）、``kanban-task-suppressions``
# （16 / 0）、``dispatch-confirmations``、以及根下的 ``latest.json``（两根各一份、
# 内容不同，``rvf-analyze --latest`` 直接读它）。全部挂在 ``log_root()`` 之下，
# 因此全部随那行 ``env.setdefault`` 一起分裂了。
#
# 这正是「同一个逻辑标识在两个物理位置各有一份，而没有任何东西比较过它们」的又一
# 实例，所以这里采用与 ``TRACKER_TABLE_MERGE_STRATEGIES`` 完全相同的对策：**枚举
# 磁盘、未登记即拒绝**。下一个人往状态根里加目录时，迁移会当场红，而不是静默漏迁。
STATE_ROOT_ENTRY_KIND_TRACKER_DATABASE_TREE = "tracker_database_tree"
STATE_ROOT_ENTRY_KIND_RUN_DIRECTORY_TREE = "run_directory_tree"
STATE_ROOT_ENTRY_KIND_REVIEW_HIGHWATER_MARKERS = "review_highwater_markers"
STATE_ROOT_ENTRY_KIND_ROUND_BASELINE_MARKERS = "round_baseline_markers"
STATE_ROOT_ENTRY_KIND_UNION_BY_FILE_NAME = "union_by_file_name_canonical_wins"
STATE_ROOT_ENTRY_KIND_LATEST_RUN_POINTER = "latest_run_pointer"
STATE_ROOT_ENTRY_KIND_ARCHIVE_ONLY_NO_LIVE_READER = "archive_only_no_live_reader"

# 「merge 之后源是否又被改过」这道闸必须覆盖**每一类被 merge 读进 staging 的来源**，
# 而不只是 tracker 库。marker / ``latest.json`` / 三个并集目录同样是 merge 读、switch
# 整体换入、legacy 侧随后整棵归档的来源；漏掉它们，一次「merge 与 switch 之间新增了
# 一个 suppression 戳」就会在两条闸全绿的情况下只留在 archive 里、canonical 侧不可见。
#
# 刻意**不**含 ``run_directory_tree``：``switch`` 遍历的是 legacy 里的活目录而不是账本
# 里的计划，merge 之后新增的 run 目录照样会被 rename 过去（不丢），而 legacy 侧 runs
# 实测 18 GB，逐文件取指纹不可行。也不含 ``tracker_database_tree``：它由按仓记录的
# ``legacy_source_sha256`` / ``canonical_source_sha256`` 覆盖。归档专用条目没有活读者、
# 压根不参与合并，自然也不在内。
STATE_ROOT_ENTRY_KINDS_FINGERPRINTED_AS_NON_TRACKER_MERGE_SOURCES = frozenset(
    {
        STATE_ROOT_ENTRY_KIND_REVIEW_HIGHWATER_MARKERS,
        STATE_ROOT_ENTRY_KIND_ROUND_BASELINE_MARKERS,
        STATE_ROOT_ENTRY_KIND_UNION_BY_FILE_NAME,
        STATE_ROOT_ENTRY_KIND_LATEST_RUN_POINTER,
    }
)


@dataclass(frozen=True)
class StateRootEntryMigrationStrategy:
    """状态根下一个顶层条目的迁移策略。

    ``entry_name_pattern`` 用 ``fnmatch`` 匹配（绝大多数是字面名）。
    ``rationale`` 必填：登记一个条目就是在断言「我查过谁读它」，理由要能被复核。
    """

    entry_name_pattern: str
    kind: str
    rationale: str


STATE_ROOT_ENTRY_MIGRATION_STRATEGIES: tuple[StateRootEntryMigrationStrategy, ...] = (
    StateRootEntryMigrationStrategy(
        "diff-tracker",
        STATE_ROOT_ENTRY_KIND_TRACKER_DATABASE_TREE,
        "reviewable_unit_diff_tracker 的 per-repo SQLite 库；本工具的合并主体。",
    ),
    StateRootEntryMigrationStrategy(
        RUNS_SUBDIRECTORY,
        STATE_ROOT_ENTRY_KIND_RUN_DIRECTORY_TREE,
        "run 日志树（legacy 侧实测 18 GB）；逐目录 os.rename，绝不复制。",
    ),
    StateRootEntryMigrationStrategy(
        REVIEW_HIGHWATER_SUBDIRECTORY,
        STATE_ROOT_ENTRY_KIND_REVIEW_HIGHWATER_MARKERS,
        "committed-round 窗口下界；冲突裁决见 choose_review_highwater。",
    ),
    StateRootEntryMigrationStrategy(
        ROUND_BASELINE_PENDING_SUBDIRECTORY,
        STATE_ROOT_ENTRY_KIND_ROUND_BASELINE_MARKERS,
        "committed-round 的 bootstrap 下界；冲突取更保守（更旧）的一侧。",
    ),
    StateRootEntryMigrationStrategy(
        "handoff-notified",
        STATE_ROOT_ENTRY_KIND_UNION_BY_FILE_NAME,
        (
            "rvf_handoff._notified_marker_path（ledger.root 之下）的通知去重戳；"
            "并集即『已通知过』，方向安全（至多少发一条通知，不会重复轰炸用户）。"
        ),
    ),
    StateRootEntryMigrationStrategy(
        "kanban-task-suppressions",
        STATE_ROOT_ENTRY_KIND_UNION_BY_FILE_NAME,
        (
            "stop_review_validate_fix.kanban_task_suppression_path（state_dir() == "
            "log_root()）；并集保留抑制，方向是少派而非多派。"
        ),
    ),
    StateRootEntryMigrationStrategy(
        "dispatch-confirmations",
        STATE_ROOT_ENTRY_KIND_UNION_BY_FILE_NAME,
        "rvf_bootstrap_confirm 的确认戳（自带 TTL）；并集无害。",
    ),
    StateRootEntryMigrationStrategy(
        "latest.json",
        STATE_ROOT_ENTRY_KIND_LATEST_RUN_POINTER,
        (
            "RunLedger.latest_pointer 写、rvf_analyze --latest 读；两根各一份且内容"
            "不同，取 updated_at 更新的一侧并重写其中的绝对路径。"
        ),
    ),
    # 以下四项在当前代码里**查无读者**（已 grep 全仓 py/md/sh）。它们是退役特性的
    # 残留：``post-analyze-quiet`` 的 marker 已随 3f552ca 全量退役。迁移不搬、不删，
    # 留在只读归档里，需要时还能考古。
    StateRootEntryMigrationStrategy(
        "handoff-advised",
        STATE_ROOT_ENTRY_KIND_ARCHIVE_ONLY_NO_LIVE_READER,
        "无任何读者（全仓 grep 0 命中）；退役特性残留，留归档。",
    ),
    StateRootEntryMigrationStrategy(
        "handoff-opened",
        STATE_ROOT_ENTRY_KIND_ARCHIVE_ONLY_NO_LIVE_READER,
        "无任何读者；handoff 通知化改造前的『用编辑器打开』残留，留归档。",
    ),
    StateRootEntryMigrationStrategy(
        "followups",
        STATE_ROOT_ENTRY_KIND_ARCHIVE_ONLY_NO_LIVE_READER,
        "无任何读者；一份跨仓 followup 手写文档，留归档。",
    ),
    StateRootEntryMigrationStrategy(
        "post-analyze-quiet",
        STATE_ROOT_ENTRY_KIND_ARCHIVE_ONLY_NO_LIVE_READER,
        "marker 已随 3f552ca 全量退役（改 turn 级守卫）；留归档。",
    ),
    StateRootEntryMigrationStrategy(
        "cross-run-analysis-*.md",
        STATE_ROOT_ENTRY_KIND_ARCHIVE_ONLY_NO_LIVE_READER,
        "手写的跨 run 复盘文档，不是运行时状态；留归档。",
    ),
    StateRootEntryMigrationStrategy(
        "state-root-migration-switch-receipt.json",
        STATE_ROOT_ENTRY_KIND_ARCHIVE_ONLY_NO_LIVE_READER,
        "本工具 switch 自己落在 canonical 根的回执；登记它是为了迁移重跑时不自判未登记。",
    ),
)


def classify_state_root_entry(entry_name: str) -> StateRootEntryMigrationStrategy | None:
    """顶层条目 → 迁移策略；未登记返回 None（调用方必须 fail-closed）。"""

    for strategy in STATE_ROOT_ENTRY_MIGRATION_STRATEGIES:
        if fnmatch.fnmatch(entry_name, strategy.entry_name_pattern):
            return strategy
    return None


def unregistered_state_root_entry_names(*roots: Path) -> list[str]:
    """两根里出现过、却没有登记迁移策略的顶层条目名（排序去重）。

    刻意忽略以 ``.`` 开头的条目：marker 的原子写会留下 ``.name.<hex>.tmp`` 中间文件，
    以及迁移自己落下的 ``.migrated-from-claude-root`` 戳；把它们算成未登记条目只会
    让闸在正常情况下也红。真正的 tmp 残留由 verify 单列报出。
    """

    unregistered: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.name.startswith("."):
                continue
            if classify_state_root_entry(child.name) is None:
                unregistered.add(child.name)
    return sorted(unregistered)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso_timestamp_text(value: Any) -> float | None:
    """ISO-8601 文本 → epoch 秒；解析不了返回 ``None``。

    **直接委派给运行时自己的解析器**，不在这里复制一份。本工具写出的 marker 正是
    ``round_baseline_status()`` 要读的，两边对「什么算过期」必须逐字节同口径——而这个文件
    通篇的规矩就是绝不另立第二份实现（marker 命名派生自 ``marker_paths``、schema 迁移调用
    tracker 自己的 ``_ensure_schema``，都是同一条理由）。抄一份即使当下逐字相同，也只是又
    造了一个会漂移的副本。

    委派一个带下划线的名字是刻意的取舍：运行时若改了解析口径，这里**跟着变**；若把它改名
    或删掉，导入期就会炸——响亮地失败，而不是静默地跟真相源分叉。
    """

    return round_baseline_marker._parse_iso_ts(value)


def read_json_document(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def most_recent_modification_time(root: Path, *, sample_limit: int | None = 4000) -> float:
    """root 子树里最新的 mtime。

    ``sample_limit=None`` = 走完整棵树（legacy 根实测 389583 个条目 / 19s）。
    ``sample_limit`` 给了数字就只看前 N 个条目——**这是个采样，不是事实**：
    ``rglob`` 的顺序是目录遍历序，legacy 根光 ``runs/`` 就有 3909 个子目录，采样很
    可能在走到 ``round-baseline-pending`` 之前就被 4000 用完，于是漏掉真正最新的那次
    写入、把「刚刚还在写」报成「已静默很久」。

    所以分工是：``report`` 用采样（它只是给人看的一行提示，快比准重要），
    **静默期闸一律走完整遍历**——那里一次假的「已静默」就等于允许一次会丢数据的
    合并（``evaluate_migration_quiescence_gates`` 显式传 ``None``）。
    """

    newest = 0.0
    inspected = 0
    for path in root.rglob("*"):
        try:
            newest = max(newest, path.lstat().st_mtime)
        except OSError:
            continue
        inspected += 1
        if sample_limit is not None and inspected >= sample_limit:
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


@dataclass
class RoundBaselineReconciliation:
    """一个 task 的 round-baseline 裁决结果。

    与 highwater **方向相反**：round-baseline 是 ``baseline..HEAD`` 这个观察窗口的
    **下界种子**，取得越旧、窗口越宽、候选越多。所以这里没有 owner 的
    ``advance-to-descendant`` 裁决可言——那条裁决是拿「已审证据」换「不再重复派发」，
    而 round-baseline 根本不承载已审事实（它只是「上一条 prompt 时的 HEAD」，
    committed-round 仅在**从未完成过 review**、拿不到高水位时才回退到它作 bootstrap）。
    既然它的唯一职责是 bootstrap，合并时一律取**可证明更旧**的一侧：宁可让某个从未
    审过的 task 多看见几个 commit，也不要让它一上来就把一段工作挡在窗口之外。
    """

    task_marker_name: str
    legacy_baseline_head: str | None
    canonical_baseline_head: str | None
    ancestry_relation: str
    chosen_baseline_head: str | None
    chosen_side: str
    conflict_classes: list[str] = field(default_factory=list)


def choose_round_baseline(
    *,
    task_marker_name: str,
    legacy_marker: Mapping[str, Any] | None,
    canonical_marker: Mapping[str, Any] | None,
    legacy_marker_file_present_but_unreadable: bool = False,
    canonical_marker_file_present_but_unreadable: bool = False,
) -> RoundBaselineReconciliation:
    """裁决一个 task 的 round-baseline：取可证明更旧的一侧。

    校验与 highwater 同口径（损坏 marker 一律拒绝、仓身份不一致一律拒绝），只是
    ``baseline_head`` 取代 ``reviewed_head``、选择方向取反。
    """

    def classify(
        marker: Mapping[str, Any] | None, unreadable: bool
    ) -> tuple[str, str | None]:
        if unreadable:
            return MARKER_SIDE_FILE_UNREADABLE, None
        if marker is None:
            return MARKER_SIDE_ABSENT, None
        if "baseline_head" not in marker:
            return MARKER_SIDE_REVIEWED_HEAD_FIELD_ABSENT, None
        value = marker["baseline_head"]
        if isinstance(value, str) and REVIEW_HIGHWATER_FULL_COMMIT_SHA_PATTERN.match(value):
            return MARKER_SIDE_USABLE, value
        return MARKER_SIDE_REVIEWED_HEAD_NOT_A_FULL_COMMIT_SHA, None

    legacy_classification, legacy_head = classify(
        legacy_marker, legacy_marker_file_present_but_unreadable
    )
    canonical_classification, canonical_head = classify(
        canonical_marker, canonical_marker_file_present_but_unreadable
    )
    conflicts: list[str] = []
    for side_name, classification in (
        ("legacy", legacy_classification),
        ("canonical", canonical_classification),
    ):
        if classification in MARKER_SIDE_CLASSIFICATIONS_REQUIRING_FAIL_CLOSED_REFUSAL:
            conflicts.append(f"{side_name}_{classification}")
    if legacy_classification == canonical_classification == MARKER_SIDE_ABSENT:
        conflicts.append("no_readable_round_baseline_marker_on_either_side")
    if conflicts:
        return RoundBaselineReconciliation(
            task_marker_name, legacy_head, canonical_head,
            "round_baseline_marker_unreadable", None, "refused", conflicts,
        )

    legacy_repository = (legacy_marker or {}).get("repo")
    canonical_repository = (canonical_marker or {}).get("repo")
    if (
        isinstance(legacy_repository, str)
        and isinstance(canonical_repository, str)
        and legacy_repository != canonical_repository
    ):
        legacy_common = git_common_directory_of(Path(legacy_repository))
        canonical_common = git_common_directory_of(Path(canonical_repository))
        if legacy_common is None or canonical_common is None or legacy_common != canonical_common:
            conflicts.append("repository_identity_mismatch_refusing_to_merge_marker")
            return RoundBaselineReconciliation(
                task_marker_name, legacy_head, canonical_head,
                "repository_identity_mismatch", None, "refused", conflicts,
            )

    if legacy_head and not canonical_head:
        return RoundBaselineReconciliation(
            task_marker_name, legacy_head, canonical_head,
            "single_side_only", legacy_head, "legacy", conflicts,
        )
    if canonical_head and not legacy_head:
        return RoundBaselineReconciliation(
            task_marker_name, legacy_head, canonical_head,
            "single_side_only", canonical_head, "canonical", conflicts,
        )

    repository_directory: Path | None = None
    for candidate in (canonical_repository, legacy_repository):
        if isinstance(candidate, str) and Path(candidate).is_dir():
            repository_directory = Path(candidate)
            break

    relation = ancestry_relation_between(repository_directory, legacy_head, canonical_head)
    if relation == "identical":
        return RoundBaselineReconciliation(
            task_marker_name, legacy_head, canonical_head, relation,
            legacy_head, "identical", conflicts,
        )
    if relation in {"repository_unavailable", "commit_missing_from_repository"}:
        conflicts.append("ancestry_unverifiable_falling_back_to_manual_review")
        return RoundBaselineReconciliation(
            task_marker_name, legacy_head, canonical_head, relation, None, "refused", conflicts,
        )
    if relation == "diverged":
        assert repository_directory is not None
        exit_code, merge_base = run_git(
            repository_directory, "merge-base", str(legacy_head), str(canonical_head)
        )
        conflicts.append("diverged_heads_fell_back_to_merge_base")
        chosen = merge_base.strip() if exit_code == 0 else None
        if chosen is not None and not REVIEW_HIGHWATER_FULL_COMMIT_SHA_PATTERN.match(chosen):
            conflicts.append("merge_base_output_is_not_a_full_40_hex_commit_sha")
            chosen = None
        if chosen is None:
            conflicts.append("no_provable_common_ancestor_refusing_to_choose_round_baseline")
        return RoundBaselineReconciliation(
            task_marker_name, legacy_head, canonical_head, relation, chosen,
            "merge_base" if chosen else "refused", conflicts,
        )
    if relation == "left_is_ancestor_of_right":
        return RoundBaselineReconciliation(
            task_marker_name, legacy_head, canonical_head, relation,
            legacy_head, "legacy_is_older_ancestor", conflicts,
        )
    return RoundBaselineReconciliation(
        task_marker_name, legacy_head, canonical_head, relation,
        canonical_head, "canonical_is_older_ancestor", conflicts,
    )


def _marker_repository_directory(marker_document: Mapping[str, Any] | None) -> Path | None:
    """marker 正文里那个仓此刻是否还在磁盘上；不在就返回 None（祖先关系判不了）。"""

    repository = (marker_document or {}).get("repo")
    if not isinstance(repository, str) or not repository:
        return None
    candidate = Path(repository)
    return candidate if candidate.is_dir() else None


@dataclass(frozen=True)
class MarkerCandidateForOneRuntimeFileName:
    """重键之后落到同一个运行时文件名上的一份候选 marker。

    ``source_marker_name`` 是**重键前**的源 key（旧格式是 ``task-<id>``，新格式还含
    仓身份 / harness 段），``chosen_head`` 是它自己那条 legacy↔canonical 裁决已经选定
    的 commit。
    """

    source_marker_name: str
    chosen_head: str
    payload: Mapping[str, Any]
    repository_directory: Path | None


def choose_conservative_oldest_marker_colliding_on_one_runtime_file_name(
    *,
    runtime_file_stem: str,
    candidates: Sequence[MarkerCandidateForOneRuntimeFileName],
) -> tuple[MarkerCandidateForOneRuntimeFileName | None, dict[str, Any]]:
    """在「多个源 key 重键到同一个文件名」时裁决，并把这次撞车整条落账。

    **为什么会撞车、且必然撞车**：F1.2 的硬顺序是「先部署新代码、再迁移数据」，所以
    跑迁移那一刻两根里必然同时存在旧格式 ``task-<id>`` 与新格式
    ``task-<id>--repo-<仓>[--harness-<h>]`` 两份 marker。它们指的是同一个逻辑标识，
    重键后自然落到同一个目标文件名上。而 legacy↔canonical 那层裁决是**按源 key 逐个**
    做的，天生看不见这条跨 key 的冲突。

    不修的后果不是「少一个文件」这么轻：源 key 排序里 ``task-X`` 恒在
    ``task-X--repo-…`` 之前，于是**永远是新格式那份最后写、把先写的覆盖掉**，两条却都
    被记成 written；更隐蔽的是落盘 marker 的 ``merged_from_state_roots`` 只剩赢家那条
    裁决，磁盘上的审计记录会**声称另一侧根本不存在**。

    **裁决方向刻意取「可证明更旧」，两种 marker 都如此**——这不与 owner 关于 highwater
    的 ``advance-to-descendant`` 裁决冲突：那条裁决针对的是 legacy↔canonical 之间积了
    数月、牵涉上百个单元的分歧；这里是**迁移自己造出来的、只有部署到迁移之间那几分钟宽**
    的新冲突类，owner 从未对它拍过板。取更旧的一侧至多让那几分钟内的工作多审一轮，且
    **不产生任何 write-off**（无单元被推出窗口 ⇒ 无不可逆损失），符合本工具「冲突时一律
    保守重审、禁止静默决胜」的总原则。

    祖先关系判不出来（发散 / 仓不可用 / commit 不在仓里）一律 **refuse 该文件名**，
    而不是挑一个：这类撞车在真实数据里不该出现，出现了就是需要人看一眼的信号，而静默期
    本来就有人在场。
    """

    record: dict[str, Any] = {
        "runtime_file_stem": runtime_file_stem,
        "contributing_marker_names": [candidate.source_marker_name for candidate in candidates],
        "contributing_marker_heads": {
            candidate.source_marker_name: candidate.chosen_head for candidate in candidates
        },
        "chosen_marker_name": None,
        "chosen_head": None,
        "conflict_classes": [],
    }
    chosen = candidates[0]
    for challenger in candidates[1:]:
        repository_directory = chosen.repository_directory or challenger.repository_directory
        relation = ancestry_relation_between(
            repository_directory, chosen.chosen_head, challenger.chosen_head
        )
        if relation in {"identical", "left_is_ancestor_of_right"}:
            continue
        if relation == "right_is_ancestor_of_left":
            chosen = challenger
            continue
        record["conflict_classes"].append(
            f"{relation}_between_{chosen.source_marker_name}_and_{challenger.source_marker_name}"
        )
        record["conflict_classes"].append(
            "runtime_file_name_collision_without_provable_ancestry_refusing_to_choose"
        )
        return None, record
    record["chosen_marker_name"] = chosen.source_marker_name
    record["chosen_head"] = chosen.chosen_head
    return chosen, record


ROUND_BASELINE_TIME_TO_LIVE_FIELD_NAMES = ("captured_at", "expires_at", "ttl_seconds")


def round_baseline_marker_expiry_timestamp(
    marker_document: Mapping[str, Any],
) -> float | None:
    """按运行时 ``round_baseline_status`` 的口径复算这份 marker 的到期时刻。

    优先 ``expires_at``；缺失时退回 ``captured_at`` + marker **自己记的** ``ttl_seconds``。
    刻意不读 ``RVF_ROUND_BASELINE_TTL_SECONDS``：迁移工具跑在哪个 shell 里与这些 marker
    当初被写出时的 TTL 无关，拿迁移进程的环境去重算别人的到期时刻只会引入第三种口径。
    两者都算不出来返回 ``None``（该 marker 不参与新鲜度排序）。
    """

    expires_at = parse_iso_timestamp_text(marker_document.get("expires_at"))
    if expires_at is not None:
        return expires_at
    captured_at = parse_iso_timestamp_text(marker_document.get("captured_at"))
    if captured_at is None:
        return None
    recorded_ttl = marker_document.get("ttl_seconds")
    if not isinstance(recorded_ttl, (int, float)) or isinstance(recorded_ttl, bool):
        return None
    return captured_at + float(recorded_ttl)


def adopt_freshest_time_to_live_across_markers_merged_into_one_round_baseline_file(
    *,
    payload: dict[str, Any],
    contributing_marker_documents: Sequence[tuple[str, Mapping[str, Any]]],
) -> dict[str, Any] | None:
    """把落盘 payload 的 TTL 元数据换成所有贡献者里**最新**的那份。就地改 ``payload``。

    **为什么必须拆开 head 与 TTL 这两个方向**：round-baseline 的裁决一律取「可证明更旧」
    的 ``baseline_head``（窗口下界更低 ⇒ 多审，不产生 write-off）。但 ``captured_at`` /
    ``expires_at`` 跟着同一份候选走的话，方向会当场翻转——F1.2 的硬顺序是「先部署新代码、
    再迁移数据」，撞车里那份旧格式 marker 必然是部署**之前**写的，只要部署到迁移之间隔了
    超过一个 TTL（默认 6h）它就已经 expired。于是「保守取更旧」系统性地选中已过期的那份，
    落盘 marker 被 ``round_baseline_status`` 判成 ``stale``、``resolve_round_baseline_head()``
    返回 ``None``，committed-round 的 bootstrap 下界**整个消失**：从「多审几个 commit」
    变成「一个都不审」。合并结果比任何一侧的输入都差，正是本工具最不该产出的东西。

    **界限刻意划在「不得比最新鲜的那份输入更旧」，而不是「一律续期」**：两侧都已过期时结果
    保持 ``stale``。TTL 表达的是「『上一条 prompt 时的 HEAD』这个下界还有没有意义」；两侧
    都过期说明它本来就该失效，运行时按设计降级成「无 committed 观测」。迁移工具无权替运行时
    把它改判成有效——那等于拿一个几天前的 HEAD 当本轮窗口下界，反过来变成大面积过触发。
    所以这里既不凭空生成 TTL、也不延长任何一份输入的寿命，只是不让合并把仍然有效的那份丢掉。

    TTL 三个字段**整组替换**（缺的就删掉），不逐字段挑：混着一份的 ``expires_at`` 和另一份
    的 ``captured_at``，落盘 marker 就会自相矛盾，取证时无从分辨哪个才是真的。

    ``reviewed_head`` 那边**不需要同款处理**：``review_highwater_marker`` 是 durable 的、
    刻意没有 TTL 概念（见该模块头「无 TTL 过期」），根本不存在这条失效路径。
    """

    ranked: list[tuple[float, str, Mapping[str, Any]]] = []
    for source_label, document in contributing_marker_documents:
        expiry = round_baseline_marker_expiry_timestamp(document)
        if expiry is not None:
            ranked.append((expiry, source_label, document))
    if not ranked:
        return None
    freshest_expiry, freshest_label, freshest_document = max(ranked, key=lambda item: item[0])
    current_expiry = round_baseline_marker_expiry_timestamp(payload)
    if current_expiry is not None and current_expiry >= freshest_expiry:
        return None
    replaced_expires_at = payload.get("expires_at")
    for field_name in ROUND_BASELINE_TIME_TO_LIVE_FIELD_NAMES:
        payload.pop(field_name, None)
        if field_name in freshest_document:
            payload[field_name] = freshest_document[field_name]
    return {
        "adopted_from_marker": freshest_label,
        "replaced_expires_at": replaced_expires_at,
        "adopted_expires_at": payload.get("expires_at"),
    }


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


def render_migration_command_result_text(payload: Mapping[str, Any]) -> str:
    """``merge`` / ``verify`` / ``switch`` 的人读回执。

    只渲染**要人做决定**的那几行：拒绝理由、阻断项、被写掉的单元、还没搬的东西。
    完整证据一律在 staging 的 JSON 账本里，不往终端上倒。
    """

    command = str(payload.get("command", "?"))
    status = str(payload.get("status", "?"))
    lines = [f"RVF 状态根合并 · {command} → {status}"]

    if payload.get("reason"):
        lines.append(f"  拒绝理由: {payload['reason']}")
    if payload.get("detail"):
        lines.append(f"  {payload['detail']}")
    for reason in payload.get("blocking_reasons") or []:
        lines.append(f"  ⛔ {reason}")
    quiescence = payload.get("quiescence") or {}
    if quiescence:
        lines.append(
            f"  静默期: legacy {quiescence.get('legacy_root_seconds_since_last_write')}s 前写过"
            f" / canonical {quiescence.get('canonical_root_seconds_since_last_write')}s 前写过"
            f" / 未 drain lease {quiescence.get('undrained_lease_count')}"
        )
    if payload.get("staging_was_produced_without_quiescence_gates"):
        lines.append("  ⚠️ 这份 staging 是排练产物（跳过了静默期闸），switch 会拒绝消费")

    if command == "merge" and status.startswith("staged"):
        repositories = payload.get("tracker_repositories") or []
        imported = sum(
            sum((item.get("rows_imported_by_table") or {}).values()) for item in repositories
        )
        updated = sum(
            sum((item.get("rows_updated_by_table") or {}).values()) for item in repositories
        )
        skipped = sum(
            sum((item.get("rows_skipped_by_table") or {}).values()) for item in repositories
        )
        rewritten = sum(int(item.get("absolute_paths_rewritten") or 0) for item in repositories)
        identity_mismatches = sum(
            len(item.get("identity_mismatch_rows") or []) for item in repositories
        )
        lines.append(
            f"  tracker: {len(repositories)} 个仓，导入 {imported} 行 / 更新 {updated} 行 /"
            f" 跳过 {skipped} 行 / 重写绝对路径 {rewritten} 处 / 身份冲突隔离 {identity_mismatches} 行"
        )
        highwater = payload.get("review_highwater_staging") or {}
        round_baseline = payload.get("round_baseline_staging") or {}
        lines.append(
            f"  marker: highwater 写 {len(highwater.get('written_marker_names') or [])}"
            f" / 拒 {len(highwater.get('refused_marker_names') or [])}；"
            f"round-baseline 写 {len(round_baseline.get('written_marker_names') or [])}"
            f" / 拒 {len(round_baseline.get('refused_marker_names') or [])}"
        )
        # 「仓已不在磁盘上」单独报数，不许并进「拒」也不许省略：它不阻断（那份 marker
        # 无论迁不迁都不会再被命中），但静默丢掉几百个 marker 是这类工具最典型的假绿。
        markers_whose_repository_is_gone = sum(
            len(section.get("skipped_marker_names_whose_repository_is_gone") or [])
            for section in (highwater, round_baseline)
        )
        if markers_whose_repository_is_gone:
            lines.append(
                f"  · 另有 {markers_whose_repository_is_gone} 个 marker 的仓已不在磁盘上"
                f"（多为已删的 task worktree），跳过不迁——运行时同样算不出它们的仓身份，"
                f"迁过去也永远不会被查到"
            )
        # 重键撞车（部署到迁移之间新旧两种文件名并存造成）同样必须报数：它是「两个源 key
        # 落到同一个目标文件」，静默处理就退化成后写覆盖先写。
        for section_label, section in (
            ("highwater", highwater),
            ("round-baseline", round_baseline),
        ):
            for collision in section.get("runtime_file_name_collisions") or []:
                chosen = collision.get("chosen_marker_name")
                lines.append(
                    f"  {'·' if chosen else '⛔'} {section_label} 重键撞车 "
                    f"{collision.get('runtime_file_stem')}：源 key "
                    f"{collision.get('contributing_marker_names')} → "
                    + (
                        f"取可证明更旧的 {chosen}（{(collision.get('chosen_head') or '-')[:8]}）"
                        if chosen
                        else f"祖先关系判不出，整个文件名拒绝迁移 {collision.get('conflict_classes')}"
                    )
                )
        written_off = [
            item
            for item in (payload.get("reconciliation_report") or {}).get("review_highwater", [])
            if item.get("written_off_unit_ids")
        ]
        if written_off:
            total = sum(len(item["written_off_unit_ids"]) for item in written_off)
            lines.append(
                f"  ⚠️ 因水位前移被推出观察窗口: {total} 个 committed 单元，"
                f"分布在 {len(written_off)} 个 task（unit_id 已落进账本）"
            )
        plan = payload.get("run_directory_move_plan") or {}
        lines.append(
            f"  runs: 待搬 {len(plan.get('run_identifiers_to_move') or [])} 个目录"
            f"（撞名 {len(plan.get('colliding_run_identifiers') or [])}）——switch 才会真的搬"
        )
        for refusal in payload.get("repositories_refusing_to_merge") or []:
            lines.append(f"  ⛔ {refusal['repository_key']}: {refusal['blocking_reasons']}")

    for finding in payload.get("findings") or []:
        marker = "⛔" if finding.get("severity") == "blocking" else "·"
        lines.append(f"  {marker} [{finding.get('check')}] {finding.get('detail')}")
    verification = payload.get("verification") or {}
    for finding in verification.get("findings") or []:
        if finding.get("severity") == "blocking":
            lines.append(f"  ⛔ [{finding.get('check')}] {finding.get('detail')}")

    if command == "switch" and status == "switched":
        lines.append(
            f"  换入 tracker {len(payload.get('installed_tracker_repositories') or [])} 个仓；"
            f"搬走 run 目录 {payload.get('moved_run_directory_count')} 个"
        )
        if payload.get("resumed_from_switch_stage_journal"):
            lines.append(
                f"  ↻ 本次是从阶段账本续跑的（前一次在破坏性阶段中断）: "
                f"{payload.get('switch_stage_journal_path')}"
            )
        sealing = payload.get("legacy_root_archive_read_only_sealing") or {}
        lines.append(
            f"  legacy 根已改名为归档: {payload.get('legacy_root_archive')}"
            f"（整棵封只读：目录 {sealing.get('directory_count')} 个 "
            f"{sealing.get('directory_mode')} / 文件 {sealing.get('file_count')} 个 "
            f"{sealing.get('file_mode')}）"
        )
        if sealing.get("failure_count"):
            lines.append(
                f"  ⚠️ 归档树有 {sealing['failure_count']} 处 chmod 失败，"
                f"这部分归档**没有**只读保护: {sealing.get('failures')}"
            )
        lines.append(
            f"  原位置留空哨兵目录（{payload.get('legacy_root_sentinel_mode')}）——"
            "任何仍写 legacy 根的路径会硬失败而不是静默重建"
        )
    if payload.get("staging_root"):
        lines.append(f"  staging: {payload['staging_root']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# staging 布局与迁移账本
# ---------------------------------------------------------------------------

MIGRATION_TOOL_CONTRACT_VERSION = 1

STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME = "merged-state-root"
STAGING_SOURCE_SNAPSHOT_DIRECTORY_NAME = "source-snapshots"
MERGE_LEDGER_FILE_NAME = "merge-ledger.json"
VERIFICATION_RECEIPT_FILE_NAME = "verification-receipt.json"
SWITCH_RECEIPT_FILE_NAME = "state-root-migration-switch-receipt.json"
SWITCH_STAGE_JOURNAL_FILE_NAME = "switch-stage-journal.json"
LEGACY_ROOT_ORIGIN_STAMP_FILE_NAME = ".migrated-from-claude-scoped-state-root.json"

# 切根之后 legacy 根位置留下的空哨兵目录权限：可进入、可读，但**不可写**。任何仍试图
# 写 ``~/.claude/rvf`` 的遗漏代码路径会当场 EACCES 报错，而不是静默重建第三棵状态树。
# 这类回归最好的探测器就是让它硬失败。
LEGACY_ROOT_SENTINEL_DIRECTORY_MODE = 0o500

# 归档树的权限。哨兵不可写还不够：归档的**存在理由**就是「保留回滚证据」，一棵
# owner 可写的归档树随时会被某个 rmtree / 手滑改动，证据就没了。目录 ``0o500``、
# 文件 ``0o400``：可读可考古，不可写不可删（目录没有 w 位，里面的文件也删不掉）。
LEGACY_ROOT_ARCHIVE_DIRECTORY_MODE = 0o500
LEGACY_ROOT_ARCHIVE_FILE_MODE = 0o400

# ``switch`` 破坏性阶段的阶段名。账本记的是「这一步已经做完」，故每个名字都必须能
# 单独判定幂等，顺序即执行顺序。
SWITCH_STAGE_TRACKER_REPOSITORIES_INSTALLED = "tracker-repositories-installed"
SWITCH_STAGE_STATE_ROOT_ENTRIES_INSTALLED = "state-root-entries-installed"
SWITCH_STAGE_RUN_DIRECTORIES_MOVED = "run-directories-moved"
SWITCH_STAGE_LEGACY_ROOT_ORIGIN_STAMPED = "legacy-root-origin-stamped"
SWITCH_STAGE_LEGACY_ROOT_ARCHIVED = "legacy-root-archived"
SWITCH_STAGE_LEGACY_ROOT_ARCHIVE_SEALED_READ_ONLY = "legacy-root-archive-sealed-read-only"
SWITCH_STAGE_LEGACY_ROOT_SENTINEL_SEALED = "legacy-root-sentinel-sealed"


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_of_sqlite_database_including_write_ahead_log(database_path: Path) -> str:
    """库的指纹**必须把 ``-wal`` 一起算进去**。

    tracker 跑 WAL：一次提交先落 ``-wal``，主文件要等 checkpoint 才变。只对主文件取
    sha256，一次「merge 之后又写了一批事务」的违规就会完全看不见——主文件字节没动，
    闸照样绿，而 ``switch`` 会把那批事务整体丢掉。这正是这道闸要挡的事情本身。

    ``-wal`` 缺席与 0 字节是同一种「无未 checkpoint frame」状态。新 SQLite 的
    ``Connection.backup()`` 会把缺席 sidecar 建成空文件；把两态算成不同指纹，会在
    零应用层写入下误报并发写。
    """

    digest = hashlib.sha256()
    for suffix in ("", "-wal"):
        sidecar = database_path.with_name(database_path.name + suffix)
        digest.update(suffix.encode("ascii"))
        # 缺席的 ``-wal`` 与 0 字节 ``-wal`` 都表示「没有任何未 checkpoint 的 WAL
        # frame」。SQLite 3.50 在 ``Connection.backup()`` 打开只读源库时会把本来不
        # 存在的 ``-wal`` 建成空文件；若把两态算成不同指纹，快照闸会在零应用层写入
        # 下误报并发写。真事务要么让 ``-wal`` 变成非空，要么 checkpoint 后改主文件。
        if not sidecar.is_file() or sidecar.stat().st_size == 0:
            digest.update(b"\x00absent")
            continue
        with sidecar.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def sha256_of_state_root_entry(entry_path: Path) -> str:
    """状态根下一个非 tracker 顶层条目的内容指纹（文件或整棵目录）。

    与 ``sha256_of_sqlite_database_including_write_ahead_log`` 同一个设计问题：**什么样
    的变化必须被看见**。tracker 库那边的答案是「未 checkpoint 的 WAL 事务」；这边的答案
    是**文件名清单**——marker 与并集目录最典型的丢数据形态是「多了一个文件」/「少了一个
    文件」，而不是某个文件的字节被改。只把各文件内容拌进摘要的实现，遇到两个内容相同的
    marker（``{}`` 形状的 suppression 戳实测就是如此）会完全看不见增删，闸照样绿。

    因此摘要里显式编进：① 条目是缺席 / 文件 / 目录（空目录与不存在必须可区分）；
    ② 每个文件相对条目根的路径名；③ 每个文件的内容 sha256。

    以 ``.`` 开头的名字一律跳过，与 ``unregistered_state_root_entry_names`` 及
    ``stage_union_by_file_name_directory`` 同口径：marker 的原子写会留下
    ``.name.<hex>.tmp`` 中间文件，把它们算进指纹只会让闸在正常情况下也红。
    """

    digest = hashlib.sha256()
    if entry_path.is_file():
        digest.update(b"file\x00")
        digest.update(sha256_of_file(entry_path).encode("ascii"))
        return digest.hexdigest()
    if not entry_path.is_dir():
        digest.update(b"absent\x00")
        return digest.hexdigest()
    digest.update(b"directory\x00")
    for child in sorted(entry_path.rglob("*")):
        relative = child.relative_to(entry_path)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if not child.is_file():
            continue
        digest.update(str(relative).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(sha256_of_file(child).encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()


def fingerprint_non_tracker_merge_sources(root: Path) -> dict[str, str]:
    """一根之下、每一个被 merge 读取的非 tracker 顶层条目 → 内容指纹。

    条目清单直接从 ``STATE_ROOT_ENTRY_MIGRATION_STRATEGIES`` 派生，**不另抄一份**：
    下一个人往登记表里加一个并集目录时，这道闸自动跟着覆盖它，而不是悄悄漏掉。
    与 ``stage_union_by_file_name_directory`` 同口径地把 ``entry_name_pattern`` 当**字面
    名**用——这几类条目的登记项当前全是字面名，合并侧也是这么读的；缺席同样要记进来，
    否则「merge 之后凭空多出一个条目」就没有可比对的基准。
    """

    fingerprints: dict[str, str] = {}
    for strategy in STATE_ROOT_ENTRY_MIGRATION_STRATEGIES:
        if strategy.kind not in STATE_ROOT_ENTRY_KINDS_FINGERPRINTED_AS_NON_TRACKER_MERGE_SOURCES:
            continue
        entry_name = strategy.entry_name_pattern
        fingerprints[entry_name] = sha256_of_state_root_entry(root / entry_name)
    return fingerprints


def _remove_sqlite_database_and_sidecars(database: Path) -> None:
    for sidecar_suffix in ("", "-wal", "-shm"):
        stale = database.with_name(database.name + sidecar_suffix)
        if stale.exists():
            stale.unlink()


def _write_ahead_log_has_uncheckpointed_frames(database: Path) -> bool:
    wal = database.with_name(database.name + "-wal")
    return wal.is_file() and wal.stat().st_size > 0


def _backup_sqlite_connection_into(
    source: sqlite3.Connection, destination_database: Path
) -> None:
    destination = sqlite3.connect(str(destination_database))
    try:
        source.backup(destination)
        destination.execute("PRAGMA journal_mode = DELETE")
        destination.commit()
    finally:
        destination.close()


def create_sqlite_coherent_backup(source_database: Path, destination_database: Path) -> None:
    """用 SQLite 的 backup API 做一致性快照，**不是** ``cp``。

    tracker 库跑在 WAL 模式（``_open_conn`` 里 ``PRAGMA journal_mode = WAL``），磁盘上
    实测确有 ``-wal`` / ``-shm`` 兄弟文件。``cp`` 只拷主文件会丢掉尚未 checkpoint 的
    那部分事务，得到一个**看起来正常、内容却少了一截**的库——这是迁移里最坏的一类
    错误，因为它不报错。``Connection.backup`` 走的是页级复制且会读 WAL，产出的是自洽
    的单文件库；随后显式把目标切回 DELETE 日志模式，让 staging 里不留 sidecar，
    ``switch`` 的目录整体换入才不会把新库配上旧 ``-wal``。

    源库默认 ``mode=ro``：最后一个读写连接关闭时 SQLite 会把 WAL checkpoint 进主文件，
    快照前后指纹闸会把一次合法备份判成并发写。部分构建（系统 Python 3.9 所带
    SQLite 3.51.0）在已静默、磁盘上没有 ``-wal`` 的 WAL 库上，不肯为只读连接创建
    ``-shm``，``backup`` 报 ``unable to open database file``——包括 3.50 关连接后不留
    sidecar、再拿到 3.51 上快照的生产路径。只在确认没有未 checkpoint 的 frame 时才
    回退读写打开：缺席与空 ``-wal`` 都是无 frame，关连接不会改主文件指纹。有 frame
    时绝不能回退。禁止 ``immutable=1``：它会跳过 WAL。
    """

    destination_database.parent.mkdir(parents=True, exist_ok=True)
    _remove_sqlite_database_and_sidecars(destination_database)
    source = sqlite3.connect(f"file:{source_database}?mode=ro", uri=True)
    try:
        _backup_sqlite_connection_into(source, destination_database)
        return
    except sqlite3.OperationalError as error:
        if (
            _write_ahead_log_has_uncheckpointed_frames(source_database)
            or "unable to open database file" not in str(error)
        ):
            raise
    finally:
        source.close()

    _remove_sqlite_database_and_sidecars(destination_database)
    source = sqlite3.connect(str(source_database))
    try:
        _backup_sqlite_connection_into(source, destination_database)
    finally:
        source.close()


def snapshot_source_database_with_verified_fingerprint(
    source_database: Path, destination_database: Path
) -> str | None:
    """落一致性快照，并返回**与这份快照对应**的源库指纹；期间源库被写过则返回 ``None``。

    账本里的 ``*_source_sha256`` 与 staging 里的快照必须是**同一份字节**的两个说法。
    先备份、再另行算指纹这两步之间只要落进一笔事务，账本记的就是**新**指纹而 staging
    躺着**旧**快照：verify 的 ``source_database_unchanged_since_merge`` 闸一比对，
    发现「源库自 merge 以来没变过」而放行，``switch`` 于是把那笔事务整体丢掉，全程
    没有任何 finding——这恰好就是那道闸本来要挡的事情本身。

    故指纹在备份前后各取一次并要求相等。**刻意不重试**：静默期闸已经要求两根都静下来，
    备份期间还有人写，说明前置条件已经被破坏，正确的反应是拒绝这个仓、让人把写者停掉
    再重跑 merge（merge 本来就是可重跑、从干净 staging 重建的），而不是原地转圈把一次
    前置条件违规磨平。
    """

    fingerprint_before_backup = sha256_of_sqlite_database_including_write_ahead_log(
        source_database
    )
    create_sqlite_coherent_backup(source_database, destination_database)
    fingerprint_after_backup = sha256_of_sqlite_database_including_write_ahead_log(
        source_database
    )
    if fingerprint_before_backup != fingerprint_after_backup:
        return None
    return fingerprint_after_backup


def rewrite_legacy_root_absolute_path(
    value: Any,
    legacy_root: Path,
    canonical_root: Path,
) -> tuple[Any, bool]:
    """把指向 legacy 根的绝对路径前缀换成 canonical 根。返回 ``(新值, 是否改写)``。

    纯前缀替换，成本极低——而「历史 run 失效可接受」这个说法不成立：
    ``rvf_fix_attempts.baseline_overlay_path`` / ``fix_patch_path`` 被 fix 复现流程
    直接读，``rvf_issues.artifact_path`` 被 handoff / analyze 读，
    ``latest.json.summary_path`` 是 ``rvf-analyze --latest`` 的唯一入口。
    """

    if not isinstance(value, str) or not value:
        return value, False
    legacy_prefix = str(legacy_root)
    if value == legacy_prefix:
        return str(canonical_root), True
    if value.startswith(legacy_prefix + os.sep):
        return str(canonical_root) + value[len(legacy_prefix) :], True
    return value, False


# ---------------------------------------------------------------------------
# 静默期闸
# ---------------------------------------------------------------------------


@dataclass
class MigrationQuiescenceVerdict:
    blocking_reasons: list[str] = field(default_factory=list)
    legacy_root_seconds_since_last_write: int | None = None
    canonical_root_seconds_since_last_write: int | None = None
    undrained_lease_count: int = 0

    @property
    def quiesced(self) -> bool:
        return not self.blocking_reasons


def evaluate_migration_quiescence_gates(
    legacy_root: Path,
    canonical_root: Path,
    pairs: Sequence[TrackerRepositoryPair],
) -> MigrationQuiescenceVerdict:
    """迁移前置闸：两根都得静下来，且没有未 drain 的 lease。

    为什么 canonical 根也要闸（原计划只闸了 legacy）：``switch`` 是把 staging 里合并好
    的库**整体换入** canonical。若 merge 与 switch 之间还有进程在写 canonical，那些写
    会被这次换入**静默丢掉**——方向与 legacy 仍在被写恰好相反，但同样是数据损失，而且
    更难发现（丢的是刚发生的事，不是历史）。``switch`` 另外还会复核每个 canonical 源库
    的 sha256 是否与 merge 当时一致，那是第二道闸；这里是第一道。
    """

    verdict = MigrationQuiescenceVerdict()
    now = datetime.now(timezone.utc).timestamp()
    for side_name, root in (("legacy", legacy_root), ("canonical", canonical_root)):
        if not root.is_dir():
            continue
        idle_seconds = int(
            round(now - most_recent_modification_time(root, sample_limit=None))
        )
        if side_name == "legacy":
            verdict.legacy_root_seconds_since_last_write = idle_seconds
        else:
            verdict.canonical_root_seconds_since_last_write = idle_seconds
        if idle_seconds < LEGACY_ROOT_QUIESCENCE_MINIMUM_IDLE_SECONDS:
            verdict.blocking_reasons.append(
                f"{side_name}_root_was_written_{idle_seconds}s_ago_"
                f"minimum_idle_is_{LEGACY_ROOT_QUIESCENCE_MINIMUM_IDLE_SECONDS}s"
            )

    for pair in pairs:
        for directory in (pair.legacy_directory, pair.canonical_directory):
            if directory is None:
                continue
            database_path = directory / TRACKER_DATABASE_FILE_NAME
            if not database_path.is_file():
                continue
            connection = open_sqlite_readonly(database_path)
            try:
                if "leases" not in table_names_in_database(connection):
                    continue
                placeholders = ",".join("?" for _ in UNDRAINED_LEASE_STATES)
                verdict.undrained_lease_count += connection.execute(
                    f"SELECT COUNT(*) FROM leases WHERE state IN ({placeholders})",
                    tuple(sorted(UNDRAINED_LEASE_STATES)),
                ).fetchone()[0]
            finally:
                connection.close()
    if verdict.undrained_lease_count:
        verdict.blocking_reasons.append(
            f"{verdict.undrained_lease_count}_undrained_leases_must_be_released_before_merge"
        )
    return verdict


# ---------------------------------------------------------------------------
# tracker 库合并（写进 staging）
# ---------------------------------------------------------------------------


@dataclass
class TrackerRepositoryMergeOutcome:
    repository_key: str
    merged_database_path: str
    legacy_source_sha256: str | None = None
    canonical_source_sha256: str | None = None
    rows_imported_by_table: dict[str, int] = field(default_factory=dict)
    rows_updated_by_table: dict[str, int] = field(default_factory=dict)
    rows_skipped_by_table: dict[str, int] = field(default_factory=dict)
    unit_decision_class_counts: dict[str, int] = field(default_factory=dict)
    absolute_paths_rewritten: int = 0
    identity_mismatch_rows: list[str] = field(default_factory=list)
    identity_quarantined_dependent_rows: list[str] = field(default_factory=list)
    foreign_key_violations: list[str] = field(default_factory=list)
    blocking_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def table_column_names(connection: sqlite3.Connection, table_name: str) -> list[str]:
    return [
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    ]


@dataclass(frozen=True)
class TrackerForeignKeyReference:
    """一条单列外键：``本表.referencing_column -> parent_table_name.parent_column``。"""

    referencing_column: str
    parent_table_name: str
    parent_column: str


def single_column_foreign_key_references(
    connection: sqlite3.Connection, table_name: str
) -> tuple[list[TrackerForeignKeyReference], list[str]]:
    """从 schema 本身读出这张表的外键，返回 ``(单列外键, 无法处理的复合外键)``。

    刻意**从 ``PRAGMA foreign_key_list`` 读，而不是手写一张引用登记表**：身份隔离闭包
    的正确性取决于「有没有漏掉一条指向被隔离 key 的引用」，而手写登记表漏一条不会报错，
    只会静默地让一批 legacy 子行改嫁到 canonical 的实体上。让 DDL 自己当唯一事实源，
    新表 / 新外键一进 schema 就自动进闭包。

    复合外键单独报出而不是忽略：当前 schema 一条都没有，真出现了就该当场拒绝这个仓，
    而不是按「只看第一列」这种半对半错的规则往下走。
    """

    foreign_keys_by_identifier: dict[int, list[sqlite3.Row]] = {}
    for row in connection.execute(f"PRAGMA foreign_key_list({table_name})").fetchall():
        foreign_keys_by_identifier.setdefault(int(row["id"]), []).append(row)

    references: list[TrackerForeignKeyReference] = []
    unsupported: list[str] = []
    for identifier, rows in sorted(foreign_keys_by_identifier.items()):
        if len(rows) != 1:
            unsupported.append(f"{table_name}:foreign_key_{identifier}_spans_multiple_columns")
            continue
        row = rows[0]
        parent_table_name = str(row["table"])
        parent_column = row["to"]
        if parent_column is None:
            # ``REFERENCES parent`` 省略列名 = 指向父表主键。
            parent_strategy = TRACKER_TABLE_MERGE_STRATEGIES.get(parent_table_name)
            if parent_strategy is None or len(parent_strategy.primary_key_columns) != 1:
                unsupported.append(
                    f"{table_name}:foreign_key_{identifier}_targets_unresolvable_parent_key"
                )
                continue
            parent_column = parent_strategy.primary_key_columns[0]
        references.append(
            TrackerForeignKeyReference(
                referencing_column=str(row["from"]),
                parent_table_name=parent_table_name,
                parent_column=str(parent_column),
            )
        )
    return references, unsupported


def _upsert_row(
    connection: sqlite3.Connection,
    table_name: str,
    columns: Sequence[str],
    row: Mapping[str, Any],
) -> None:
    placeholders = ",".join("?" for _ in columns)
    column_list = ",".join(columns)
    connection.execute(
        f"INSERT OR REPLACE INTO {table_name} ({column_list}) VALUES ({placeholders})",
        tuple(row.get(column) for column in columns),
    )


def _bump(counter: dict[str, int], key: str, amount: int = 1) -> None:
    counter[key] = counter.get(key, 0) + amount


def merge_tracker_repository_into_staging(
    pair: TrackerRepositoryPair,
    *,
    legacy_root: Path,
    canonical_root: Path,
    staging_root: Path,
) -> TrackerRepositoryMergeOutcome:
    """把一个 repo 的 legacy tracker 库合并进 staging 里的一份新库。

    基库取 canonical 一侧的 coherent 快照（它带着正确的 DDL 与 canonical 全部历史），
    legacy 侧的行按登记表逐表并入。canonical 缺席时改用 legacy 快照作基库。

    合并期间 ``PRAGMA foreign_keys`` **关闭**、写入按拓扑序进行：关掉外键是为了让
    ``foreign_key_check`` 事后一次性报出**数据本身**的悬挂引用（例如某条 unit 指着一个
    从未被记录过的 branch），而不是在写入中途因次序问题炸掉、留下半份库。

    ⚠️ 外键关闭在这里还兼着第二个、更要命的职责：本函数用 ``INSERT OR REPLACE`` 落行，
    而 SQLite 的 REPLACE 是**先 DELETE 再 INSERT**。``units`` 被 ``session_units`` /
    ``lease_units`` / ``edit_claim_units`` 以 ``ON DELETE CASCADE`` 引用，所以只要外键
    是开的，每更新一条 unit 就会连带删掉它的全部归属行与 lease 行——静默、不报错、事后
    只表现为「归属证据凭空少了一批」。把外键打开会让这段代码从「安全」变成「灾难」，
    ``tests/test_merge_rvf_state_roots.py`` 有一条用例专门钉住这一点。
    """

    merged_repository_directory = (
        staging_root
        / STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME
        / TRACKER_REPOSITORIES_SUBDIRECTORY
        / pair.repository_key
    )
    merged_database_path = merged_repository_directory / TRACKER_DATABASE_FILE_NAME
    outcome = TrackerRepositoryMergeOutcome(
        repository_key=pair.repository_key,
        merged_database_path=str(merged_database_path),
    )

    if not pair.git_common_directory_matches:
        outcome.blocking_reasons.append(
            "two_roots_disagree_on_git_common_dir_for_this_repo_key_refusing_to_merge"
        )
        return outcome

    snapshot_directory = (
        staging_root / STAGING_SOURCE_SNAPSHOT_DIRECTORY_NAME / pair.repository_key
    )
    legacy_snapshot: Path | None = None
    canonical_snapshot: Path | None = None
    if pair.legacy_directory is not None:
        legacy_snapshot = snapshot_directory / "legacy.sqlite3"
        outcome.legacy_source_sha256 = snapshot_source_database_with_verified_fingerprint(
            pair.legacy_directory / TRACKER_DATABASE_FILE_NAME, legacy_snapshot
        )
        if outcome.legacy_source_sha256 is None:
            outcome.blocking_reasons.append(
                "legacy_source_database_changed_while_being_snapshotted_"
                "quiesce_the_writer_and_rerun_merge"
            )
            return outcome
    if pair.canonical_directory is not None:
        canonical_snapshot = snapshot_directory / "canonical.sqlite3"
        outcome.canonical_source_sha256 = (
            snapshot_source_database_with_verified_fingerprint(
                pair.canonical_directory / TRACKER_DATABASE_FILE_NAME, canonical_snapshot
            )
        )
        if outcome.canonical_source_sha256 is None:
            outcome.blocking_reasons.append(
                "canonical_source_database_changed_while_being_snapshotted_"
                "quiesce_the_writer_and_rerun_merge"
            )
            return outcome

    base_snapshot = canonical_snapshot or legacy_snapshot
    if base_snapshot is None:
        outcome.blocking_reasons.append("no_tracker_database_on_either_side")
        return outcome

    merged_repository_directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(base_snapshot, merged_database_path)

    # 先把基库拉到当前 schema，再谈合并。两个理由：
    # ① 两根的库可能停在不同版本，缺列的一侧会让「legacy 有、merged 没有」的漂移闸
    #    直接把整个仓拒掉（实测 review-validate-fix 仓正是如此）；
    # ② 切根之后部署版会直接打开这个库，它本来就必须是当前 schema。
    schema_connection = sqlite3.connect(str(merged_database_path))
    # ``_ensure_schema`` 的各条 ``_ensure_*`` 分支按列名读 ``PRAGMA table_info``
    # （``row["name"]``），没有 Row factory 就会 TypeError。与 tracker 的 ``_open_conn``
    # 保持一致。
    schema_connection.row_factory = sqlite3.Row
    try:
        version_before = schema_connection.execute("PRAGMA user_version").fetchone()[0]
        ensure_tracker_schema_is_current(schema_connection)
        schema_connection.commit()
        version_after = schema_connection.execute("PRAGMA user_version").fetchone()[0]
        if version_before != version_after:
            outcome.notes.append(
                f"base_snapshot_schema_migrated_from_v{version_before}_to_v{version_after}"
            )
    except Exception as exc:  # noqa: BLE001 - 迁移不上去就拒绝合并这个仓，绝不硬写
        outcome.blocking_reasons.append(
            f"could_not_bring_base_snapshot_to_current_tracker_schema:{type(exc).__name__}: {exc}"
        )
        return outcome
    finally:
        schema_connection.close()

    # 侧车文件（meta.json / events.jsonl）随基库一起进 staging。events.jsonl 是诊断
    # 日志，两根各有一份且没有可靠的交错次序，故**不发明合并语义**：canonical 的留原
    # 名，legacy 的以 ``events.legacy-root.jsonl`` 并存。
    source_directory = pair.canonical_directory or pair.legacy_directory
    assert source_directory is not None
    for sidecar_name in (TRACKER_REPOSITORY_METADATA_FILE_NAME, "events.jsonl"):
        sidecar = source_directory / sidecar_name
        if sidecar.is_file():
            shutil.copyfile(sidecar, merged_repository_directory / sidecar_name)
    if pair.canonical_directory is not None and pair.legacy_directory is not None:
        legacy_events = pair.legacy_directory / "events.jsonl"
        if legacy_events.is_file():
            shutil.copyfile(
                legacy_events, merged_repository_directory / "events.legacy-root.jsonl"
            )

    if legacy_snapshot is None or canonical_snapshot is None:
        # 单侧存在：基库已经是全部内容，只剩「全局策略」要跑（assigned 降级 / 路径重写）。
        outcome.notes.append(
            "repository_present_on_one_root_only_base_snapshot_is_already_complete"
        )

    merged = sqlite3.connect(str(merged_database_path))
    merged.row_factory = sqlite3.Row
    legacy = (
        sqlite3.connect(f"file:{legacy_snapshot}?mode=ro", uri=True)
        if legacy_snapshot is not None and canonical_snapshot is not None
        else None
    )
    if legacy is not None:
        legacy.row_factory = sqlite3.Row
    try:
        merged.execute("PRAGMA foreign_keys = OFF")
        merged_tables = table_names_in_database(merged)
        legacy_tables = table_names_in_database(legacy) if legacy is not None else set()

        unregistered = sorted(
            (merged_tables | legacy_tables) - set(TRACKER_TABLE_MERGE_STRATEGIES)
        )
        if unregistered:
            outcome.blocking_reasons.append(
                "unregistered_tracker_tables_refusing_to_merge:" + ",".join(unregistered)
            )
            return outcome
        missing_order = sorted(
            (merged_tables | legacy_tables)
            - set(TRACKER_TABLE_INSERTION_ORDER_RESPECTING_FOREIGN_KEYS)
        )
        if missing_order:
            outcome.blocking_reasons.append(
                "tables_absent_from_insertion_order:" + ",".join(missing_order)
            )
            return outcome

        # 身份隔离闭包：某个 key 上两根说的不是同一个东西时，legacy 侧**引用**它的行
        # 也一并排除。写入顺序已按外键拓扑排好，所以父表先被处理、隔离集在子表用得上。
        identity_quarantined_keys: dict[str, set[Any]] = {}
        merged.execute("BEGIN")
        for table_name in TRACKER_TABLE_INSERTION_ORDER_RESPECTING_FOREIGN_KEYS:
            if table_name not in merged_tables:
                continue
            strategy = TRACKER_TABLE_MERGE_STRATEGIES[table_name]
            _merge_one_tracker_table(
                merged=merged,
                legacy=legacy if table_name in legacy_tables else None,
                strategy=strategy,
                legacy_root=legacy_root,
                canonical_root=canonical_root,
                outcome=outcome,
                identity_quarantined_keys=identity_quarantined_keys,
            )
        merged.execute("COMMIT")

        for violation in merged.execute("PRAGMA foreign_key_check").fetchall():
            outcome.foreign_key_violations.append(
                f"{violation[0]}:rowid={violation[1]}:references={violation[2]}"
            )
        merged.execute("PRAGMA journal_mode = DELETE")
        merged.commit()
    finally:
        merged.close()
        if legacy is not None:
            legacy.close()
    return outcome


def _merge_one_tracker_table(
    *,
    merged: sqlite3.Connection,
    legacy: sqlite3.Connection | None,
    strategy: TableMergeStrategy,
    legacy_root: Path,
    canonical_root: Path,
    outcome: TrackerRepositoryMergeOutcome,
    identity_quarantined_keys: dict[str, set[Any]],
) -> None:
    table_name = strategy.table_name
    merged_columns = table_column_names(merged, table_name)

    def keyed(connection: sqlite3.Connection) -> dict[tuple[Any, ...], dict[str, Any]]:
        return fetch_table_rows_keyed_by_primary_key(connection, strategy)

    merged_rows = keyed(merged)
    legacy_rows = keyed(legacy) if legacy is not None else {}

    references, unsupported_foreign_keys = single_column_foreign_key_references(
        merged, table_name
    )
    if unsupported_foreign_keys:
        outcome.blocking_reasons.append(
            "foreign_keys_outside_the_identity_quarantine_closure:"
            + ",".join(unsupported_foreign_keys)
        )
        return

    def reference_into_quarantined_identity(row: Mapping[str, Any]) -> str | None:
        """这条 legacy 行是否引用了一个被隔离的身份 key；是则返回可读的引用描述。"""

        for reference in references:
            value = row.get(reference.referencing_column)
            if value is None:
                continue
            if value in identity_quarantined_keys.get(reference.parent_table_name, ()):
                return (
                    f"{reference.referencing_column}={value}"
                    f"->{reference.parent_table_name}.{reference.parent_column}"
                )
        return None

    def quarantine_this_key(key: tuple[Any, ...]) -> None:
        """把这一行的主键也纳入隔离集。

        ⚠️ 无论 merged 侧有没有同 key 的行都要纳入。同 key 的 canonical 行在场时最容易
        误以为「key 还是有效的、子行照迁即可」——恰恰相反：legacy 的子行描述的是
        **legacy 那个实体**，迁进来只会挂到 canonical 的实体上，正是要防的改嫁本身。
        """

        identity_quarantined_keys.setdefault(table_name, set()).add(
            key[0] if len(key) == 1 else key
        )

    def record_quarantined_dependent_row(key: tuple[Any, ...], detail: str) -> None:
        outcome.identity_quarantined_dependent_rows.append(
            f"{table_name}:{'/'.join(str(part) for part in key)}:{detail}"
        )
        _bump(outcome.rows_skipped_by_table, table_name)
        quarantine_this_key(key)

    if legacy_rows:
        legacy_columns = set(table_column_names(legacy, table_name))  # type: ignore[arg-type]
        drifted = sorted(legacy_columns - set(merged_columns))
        if drifted:
            outcome.blocking_reasons.append(
                f"schema_drift_{table_name}_legacy_only_columns:" + ",".join(drifted)
            )
            return

    def path_rewritten(row: dict[str, Any]) -> dict[str, Any]:
        for column in ABSOLUTE_PATH_COLUMNS_BY_TABLE.get(table_name, ()):
            if column not in row:
                continue
            new_value, changed = rewrite_legacy_root_absolute_path(
                row[column], legacy_root, canonical_root
            )
            if changed:
                row[column] = new_value
                outcome.absolute_paths_rewritten += 1
        return row

    # --- units：真正的决策在 merge_unit_rows 里，这里只负责落库 ---------------
    if strategy.custom_policy == "units":
        for key in sorted(set(legacy_rows) | set(merged_rows), key=lambda item: tuple(map(str, item))):
            legacy_row = legacy_rows.get(key)
            if legacy_row is not None:
                quarantined_reference = reference_into_quarantined_identity(legacy_row)
                if quarantined_reference is not None:
                    # legacy 这条 unit 说的是 legacy 那个 worktree/branch 里的改动。
                    record_quarantined_dependent_row(key, quarantined_reference)
                    if key not in merged_rows:
                        continue
                    legacy_row = None
            unit_outcome = merge_unit_rows(legacy_row, merged_rows.get(key))
            for decision in unit_outcome.decision_classes:
                _bump(outcome.unit_decision_class_counts, decision)
            new_row = {column: unit_outcome.merged_row.get(column) for column in merged_columns}
            existing = merged_rows.get(key)
            if existing is None:
                _upsert_row(merged, table_name, merged_columns, new_row)
                _bump(outcome.rows_imported_by_table, table_name)
            elif {column: existing.get(column) for column in merged_columns} != new_row:
                _upsert_row(merged, table_name, merged_columns, new_row)
                _bump(outcome.rows_updated_by_table, table_name)
        return

    # --- leases：只迁已终结的；活 lease 不迁（前置闸已要求 drain） -------------
    if strategy.custom_policy == "leases":
        for key, legacy_row in legacy_rows.items():
            quarantined_reference = reference_into_quarantined_identity(legacy_row)
            if quarantined_reference is not None:
                record_quarantined_dependent_row(key, quarantined_reference)
                continue
            if legacy_row.get("state") not in TERMINAL_LEASE_STATES:
                _bump(outcome.rows_skipped_by_table, table_name)
                continue
            if key in merged_rows:
                continue
            _upsert_row(merged, table_name, merged_columns, path_rewritten(dict(legacy_row)))
            _bump(outcome.rows_imported_by_table, table_name)
        return

    # --- tombstones：主键是 AUTOINCREMENT 整数，两根**必然**从 1 开始撞号 ------
    # 实测 legacy 侧 12 个仓的 max(tombstone_id) 全部等于行数（即都从 1 起算），
    # canonical 侧同理。按主键并集会让 legacy 的 1..N 覆盖 canonical 的 1..M，
    # 静默丢掉两边各一批退休记录——而 tombstone 丢失会让**已退休的 legacy claim
    # 复活**。故这里改按内容去重、让 AUTOINCREMENT 重新发号。
    if strategy.custom_policy == "tombstones_append_only":
        content_columns = [column for column in merged_columns if column != "tombstone_id"]
        existing_contents = {
            tuple(row.get(column) for column in content_columns) for row in merged_rows.values()
        }
        # tombstone 靠 ``ref_id`` 指向一个 unit / lease / session / branch / worktree，
        # 但**没有外键**，所以上面那条通用检查看不见它。被隔离的 key 上导入一条 legacy
        # 退休记录，读起来就是「canonical 的那个实体已退休」——同一类改嫁。这里刻意不按
        # ``kind`` 建映射表：映射表会随 DDL 的 kind 取值悄悄过期，而拿 ``ref_id`` 去撞
        # 全部隔离 key 只会**多**排除、不会漏，且没有需要同步维护的第二处真相。
        quarantined_reference_identifiers = {
            value for keys in identity_quarantined_keys.values() for value in keys
        }
        for legacy_key, legacy_row in legacy_rows.items():
            if legacy_row.get("ref_id") in quarantined_reference_identifiers:
                outcome.identity_quarantined_dependent_rows.append(
                    f"{table_name}:{legacy_key[0]}:ref_id={legacy_row.get('ref_id')}"
                )
                _bump(outcome.rows_skipped_by_table, table_name)
                continue
            content = tuple(legacy_row.get(column) for column in content_columns)
            if content in existing_contents:
                _bump(outcome.rows_skipped_by_table, table_name)
                continue
            existing_contents.add(content)
            placeholders = ",".join("?" for _ in content_columns)
            merged.execute(
                f"INSERT INTO {table_name} ({','.join(content_columns)}) VALUES ({placeholders})",
                content,
            )
            _bump(outcome.rows_imported_by_table, table_name)
        return

    # --- meta：canonical 说了算，legacy 独有的键导入 --------------------------
    if strategy.custom_policy == "meta":
        for key, legacy_row in legacy_rows.items():
            if key in merged_rows:
                if merged_rows[key].get("value") != legacy_row.get("value"):
                    _bump(outcome.rows_skipped_by_table, table_name)
                continue
            _upsert_row(merged, table_name, merged_columns, dict(legacy_row))
            _bump(outcome.rows_imported_by_table, table_name)
        return

    # --- 通用策略 -------------------------------------------------------------
    parent_keys: set[Any] | None = None
    if strategy.follows_parent_table:
        parent_strategy = TRACKER_TABLE_MERGE_STRATEGIES[strategy.follows_parent_table]
        parent_column = parent_strategy.primary_key_columns[0]
        parent_keys = {
            row[0]
            for row in merged.execute(
                f"SELECT {parent_column} FROM {strategy.follows_parent_table}"
            ).fetchall()
        }

    for key, legacy_row in legacy_rows.items():
        quarantined_reference = reference_into_quarantined_identity(legacy_row)
        if quarantined_reference is not None:
            # 这条 legacy 行挂在一个两根说法不一致的身份上。既不能导入（会挂到
            # canonical 的实体上），也不能拿去归并同 key 的 canonical 行（同样是
            # 让 legacy 的说法覆盖过去）——整条排除，并把它自己的 key 也纳入隔离。
            record_quarantined_dependent_row(key, quarantined_reference)
            continue
        if parent_keys is not None:
            parent_value = legacy_row.get(
                TRACKER_TABLE_MERGE_STRATEGIES[
                    strategy.follows_parent_table  # type: ignore[index]
                ].primary_key_columns[0]
            )
            if parent_value not in parent_keys:
                # 父行没迁（多半是未终结的 lease），子行跟着不迁——否则会留下悬挂引用。
                _bump(outcome.rows_skipped_by_table, table_name)
                continue
        existing = merged_rows.get(key)
        if existing is None:
            _upsert_row(merged, table_name, merged_columns, path_rewritten(dict(legacy_row)))
            _bump(outcome.rows_imported_by_table, table_name)
            continue

        identity_conflict = [
            column
            for column in strategy.identity_columns
            if existing.get(column) != legacy_row.get(column)
        ]
        if identity_conflict:
            # 身份字段不一致 = 两根说的不是同一个东西。保留 canonical、隔离报出，
            # 绝不静默决胜——决胜的代价是把一个 worktree/branch 的身份栽到另一个头上。
            #
            # ⚠️ 只跳过这一行父行是**假隔离**：``units.worktree_key`` /
            # ``sessions.current_worktree_key`` / ``session_units.session_id`` 这些
            # legacy 子行仍会照常导入，最后全部解析到 canonical 那条实体上——对外报
            # 「已隔离」、实际做「父行保留、子行改嫁」。故把这个 key 纳入隔离集，让
            # 引用它的 legacy 行整条闭包一起排除（写入顺序已按外键拓扑排好）。
            outcome.identity_mismatch_rows.append(
                f"{table_name}:{'/'.join(str(part) for part in key)}:"
                + ",".join(identity_conflict)
            )
            _bump(outcome.rows_skipped_by_table, table_name)
            quarantine_this_key(key)
            continue

        merged_row = dict(existing)
        legacy_is_newer = strategy.recency_column is not None and text_is_later(
            legacy_row.get(strategy.recency_column), existing.get(strategy.recency_column)
        )
        if legacy_is_newer:
            merged_row = dict(legacy_row)
        for column in strategy.earliest_columns:
            candidates = [
                value
                for value in (existing.get(column), legacy_row.get(column))
                if isinstance(value, str) and value
            ]
            if candidates:
                merged_row[column] = min(candidates)
        for column in strategy.latest_columns:
            candidates = [
                value
                for value in (existing.get(column), legacy_row.get(column))
                if isinstance(value, str) and value
            ]
            if candidates:
                merged_row[column] = max(candidates)
        merged_row = path_rewritten(merged_row)
        normalised = {column: merged_row.get(column) for column in merged_columns}
        if {column: existing.get(column) for column in merged_columns} != normalised:
            _upsert_row(merged, table_name, merged_columns, normalised)
            _bump(outcome.rows_updated_by_table, table_name)

    # canonical 独有的行也要过一次路径重写：legacy 根的绝对路径不只出现在 legacy
    # 库里——同一个 run 的 tracker 行会跨两根写（订正 7），canonical 库里同样存着
    # 指向 legacy runs 的 artifact_path。
    if table_name in ABSOLUTE_PATH_COLUMNS_BY_TABLE:
        for key, existing in merged_rows.items():
            if key in legacy_rows:
                continue
            rewritten = path_rewritten(dict(existing))
            normalised = {column: rewritten.get(column) for column in merged_columns}
            if {column: existing.get(column) for column in merged_columns} != normalised:
                _upsert_row(merged, table_name, merged_columns, normalised)
                _bump(outcome.rows_updated_by_table, table_name)


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
# marker / 并集目录 / latest 指针 的 staging 落盘
# ---------------------------------------------------------------------------


def _write_marker_document(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


TASK_KEYED_LEGACY_MARKER_NAME_PREFIX = "task-"
SESSION_KEYED_LEGACY_MARKER_NAME_PREFIX = "sess-"


def migrated_marker_repository_is_absent_from_disk(
    marker_document: Mapping[str, Any] | None,
) -> bool:
    """marker 指向的仓此刻还在不在磁盘上。

    区分这一类**不是**为了放宽 fail-closed，而是因为它和「无法裁决」根本不是同一件事：
    仓已经不在了（绝大多数是被删掉的 cline-kanban task worktree），那份 marker 无论迁不迁
    都不会再被任何一次查找命中——它的 key 里那段仓身份运行时同样算不出来。把它算成阻断，
    静默期就会被一批早已消失的 worktree 永久卡住；而真正需要人来裁决的那些（正文没有 repo、
    key 前缀认不出）仍然阻断。

    实测：真实两根共 842 个 marker，169 个属于这一类，且**全部**是这个原因。
    """
    repository = (marker_document or {}).get("repo")
    if not isinstance(repository, str) or not repository.strip():
        return False
    return not Path(repository.strip()).exists()


def parent_harness_identity_recorded_in_marker_file_name(marker_file_name: str) -> str | None:
    """从 marker 文件名里读回**已经写在名字里**的 harness 身份段；没有这一段返回 None。

    返回 None 的语义是「这个名字是旧格式（根本没记过 harness）」，而不是「记过但判不出」
    ——后者在文件名里是货真价实的 ``--harness-unspecified`` 段，会被原样读回来。两者必须
    分得开：只有前者才允许回退到 ``unspecified``。

    从**最后**一个 ``--harness-`` 处切，不是第一个：仓身份段来自 ``repo_key()``，其名字
    部分过的是 ``safe_token``，而 ``safe_token`` 把 ``-`` 留在允许字符集里、并不折叠连续
    的 ``-``，所以一个叫 ``foo--harness-bar`` 的仓目录能让仓身份段自己就含这个串。反过来
    harness token 过的是 ``_sanitize_harness_identity_token``，它显式折叠 ``--``，因此
    harness 段内部永远不含 ``--harness-`` —— 最后一个出现位置必定是真正的段边界。
    """
    marker_file_name_segment_prefix_for_parent_harness_identity = (
        MARKER_FILE_NAME_SEGMENT_SEPARATOR
        + PARENT_HARNESS_IDENTITY_FILE_NAME_SEGMENT_PREFIX
        + "-"
    )
    (
        text_before_harness_segment,
        harness_segment_separator_was_found,
        harness_identity_token,
    ) = marker_file_name.rpartition(
        marker_file_name_segment_prefix_for_parent_harness_identity
    )
    if not harness_segment_separator_was_found or not text_before_harness_segment:
        return None
    return harness_identity_token.strip() or None


def parent_harness_identity_for_migrated_round_baseline_marker(
    *,
    legacy_marker_name: str,
    marker_document: Mapping[str, Any] | None,
) -> str:
    """迁移一份 round-baseline marker 时，应该按哪个 harness 身份重新派生它的文件名。

    优先级（**第一顺位必须是文件名**）：

    1. 文件名里已有的 ``--harness-<id>`` 段。F1.2 的硬顺序是「先部署新代码、再迁移数据」，
       所以真正跑迁移时两根里必然新旧两种名字并存，新名那批的 harness 身份是运行时**当场
       判定并落盘过**的事实，比任何事后重判都可信。
    2. marker 正文的 ``parent_harness_identity``。同一次写入里文件名与正文共用同一个取值
       （见 ``round_baseline_marker.write_round_baseline_marker``），所以它只是同一事实的
       第二个副本；留作次要来源，接住「正文健在但文件名被人手工改过」这类残缺输入。
    3. 都没有 ⇒ ``unspecified``。这**只**适用于旧格式：旧 marker 里根本没记过 harness，
       无从恢复，而读侧候选列表显式包含 ``--harness-unspecified`` 作回退，命中它至多让窗口
       偏宽（多审），不会让 marker 消失。

    为什么不能像最初那样对所有输入无条件写 ``unspecified``：那样一来同一个 task 下
    ``--harness-claude-code`` 与 ``--harness-codex-cli`` 两份 marker 会被重键到**同一个**
    目标文件、后写的覆盖先写的，而且对账表两条都记成 written（静默丢一份）。那恰好把
    「harness 身份编进 round-baseline key」这一刀要消除的跨 harness 互相覆盖原样恢复回来。
    """
    identity_recorded_in_file_name = parent_harness_identity_recorded_in_marker_file_name(
        legacy_marker_name
    )
    if identity_recorded_in_file_name:
        return identity_recorded_in_file_name
    identity_recorded_in_document = (marker_document or {}).get("parent_harness_identity")
    normalized_identity_from_document = normalize_parent_harness_identity(
        identity_recorded_in_document if isinstance(identity_recorded_in_document, str) else None
    )
    if normalized_identity_from_document:
        return normalized_identity_from_document
    return PARENT_HARNESS_IDENTITY_UNSPECIFIED


def runtime_marker_file_name_for_migrated_marker(
    *,
    legacy_marker_name: str,
    marker_document: Mapping[str, Any] | None,
    marker_subdirectory: str,
) -> str | None:
    """旧 marker 名 → 运行时今天真正会去读的那个文件名；无法确定返回 None（拒绝迁移）。

    旧 key 只有 ``task-<id>`` / ``sess-<id>``，新 key 还含仓身份（round-baseline 另含
    harness 身份）——见 ``core.session_scope_allocation.scope_marker_identity_key``。
    迁移必须按**新**名字落盘，否则产出的是一堆运行时读不到的文件：每个 task 静默丢掉
    自己的水位，而对账表上一切正常。

    仓身份从 marker 正文的 ``repo`` 现场解析（``git_common_dir``），因此这条路径要求
    该仓此刻仍在磁盘上。解析不出来一律返回 None 让调用方记进 ``refused`` —— **不许**
    退化成某个共享占位桶：那等于把「不同仓撞同一个 task id」原样搬个地方，而这正是
    本次改 key 要消除的东西（实测反例 ``task-a877b`` 在两根分别指 cline-kanban 与
    my-ai-setup 两个毫不相干的仓）。

    harness 身份（仅 round-baseline）按
    ``parent_harness_identity_for_migrated_round_baseline_marker`` 判定：文件名里已有的
    ``--harness-<id>`` 段优先、正文的 ``parent_harness_identity`` 次之，两者都没有（即真正的
    旧格式）才回退 ``unspecified``。**不许对所有输入无条件写 ``unspecified``**：那会把同一
    task 下两个 harness 的新格式 marker 重键到同一个目标文件、互相覆盖。回退本身不是降级
    ——读侧候选列表**显式包含** ``--harness-unspecified``，所以旧格式迁过来照样读得到；只是
    它对「哪个 harness 写的」不作断言，命中它至多让窗口偏宽。

    **必须幂等**：F1.2 的硬顺序是「先部署新代码、再迁移数据」，所以真正跑迁移时两个根里
    必然**新旧两种文件名并存**——部署之后写出的是新名，之前的是旧名。这里先把 key token
    从任一种格式里剥出来（切掉 ``--repo-`` 及其之后的一切）再统一重新派生，于是「已经是
    新名」的输入产出同一个名字，而不是 ``task-x--repo-A--repo-A`` 这种叠加。幂等的判据是
    **整个文件名**原样不变，因此 harness 段必须跟着一起还原，而不只是 key token。
    """
    if marker_subdirectory == REVIEW_HIGHWATER_SUBDIRECTORY:
        marker_module: Any = review_highwater_marker
        harness_keyword_arguments: dict[str, Any] = {}
    elif marker_subdirectory == ROUND_BASELINE_PENDING_SUBDIRECTORY:
        marker_module = round_baseline_marker
        harness_keyword_arguments = {
            "parent_harness_identity": (
                parent_harness_identity_for_migrated_round_baseline_marker(
                    legacy_marker_name=legacy_marker_name,
                    marker_document=marker_document,
                )
            )
        }
    else:  # pragma: no cover - 调用点只有上面两种
        return None

    # 幂等剥壳：新名形如 ``task-<id>--repo-<identity>[--harness-<identity>]``，旧名只有
    # ``task-<id>``。切在第一个 ``--repo-`` 上即可两种都还原出 key token。（task id 本身
    # 含字面量 ``--repo-`` 会误切，但 cline-kanban 的 task id 是 5 位十六进制、session id
    # 是 UUID，都产生不了这个串。）
    key_segment = legacy_marker_name.split(
        MARKER_FILE_NAME_SEGMENT_SEPARATOR
        + REPOSITORY_IDENTITY_FILE_NAME_SEGMENT_PREFIX
        + "-",
        1,
    )[0]
    if key_segment.startswith(TASK_KEYED_LEGACY_MARKER_NAME_PREFIX):
        task_identifier: str | None = key_segment[
            len(TASK_KEYED_LEGACY_MARKER_NAME_PREFIX) :
        ]
        session_identifier: str | None = None
    elif key_segment.startswith(SESSION_KEYED_LEGACY_MARKER_NAME_PREFIX):
        task_identifier = None
        session_identifier = key_segment[len(SESSION_KEYED_LEGACY_MARKER_NAME_PREFIX) :]
    else:
        return None
    if not (task_identifier or session_identifier):
        return None

    repository = (marker_document or {}).get("repo")
    if not isinstance(repository, str) or not repository.strip():
        return None

    candidate_paths = marker_module.marker_paths(
        task_id=task_identifier,
        session_id=session_identifier,
        repo=repository.strip(),
        root=Path("/nonexistent-staging-anchor"),
        **harness_keyword_arguments,
    )
    if not candidate_paths:
        return None
    return candidate_paths[0].name


def stage_review_highwater_markers(
    *,
    legacy_root: Path,
    canonical_root: Path,
    staging_root: Path,
    highwater_report_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """按 ``report`` 已经算好的裁决把 highwater marker 写进 staging。

    刻意复用 ``report`` 的结论而不是重算：那份对账表是**要给人看、也要进账本**的
    审计凭据，如果落盘走的是另一条计算路径，账本与实际写入就可能悄悄分叉——正是本
    工具在收拾的那类「同一个逻辑标识两份实现」。``verify`` 会重跑一次 report 并比对，
    这条复用关系因此是可检验的，不只是约定。

    marker 的目标文件名由 ``runtime_marker_file_name_for_migrated_marker`` 从运行时的
    ``review_highwater_marker.marker_paths()`` 现场派生（旧 key 只有 ``task-<id>``，
    新 key 还含仓身份）；本工具绝不另立一套命名。``tests/test_merge_rvf_state_roots.py``
    有一条结构性守卫钉死两者一致——运行时再改 key，那条测试会先红，而不是迁移悄悄写出
    一堆读不到的文件。派生不出目标名（仓已不在磁盘上等）一律记进 ``refused``。

    重键会让**多个源 key 落到同一个目标文件名**（部署到迁移之间新旧两种名字并存），因此
    落盘分两趟：先把每个源 key 的目标名与裁决算出来分组，再对每个目标名裁决一次
    （``choose_conservative_oldest_marker_colliding_on_one_runtime_file_name``）。直接
    边算边写会变成后写覆盖先写，而两条都记成 written。
    """

    legacy_scan = collect_marker_documents(legacy_root, REVIEW_HIGHWATER_SUBDIRECTORY)
    canonical_scan = collect_marker_documents(canonical_root, REVIEW_HIGHWATER_SUBDIRECTORY)
    destination = (
        staging_root / STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME / REVIEW_HIGHWATER_SUBDIRECTORY
    )
    written: list[str] = []
    written_runtime_file_stems: list[str] = []
    refused: list[str] = []
    skipped_because_repository_is_gone: list[str] = []
    candidates_by_runtime_file_name: dict[
        str, list[tuple[MarkerCandidateForOneRuntimeFileName, Mapping[str, Any]]]
    ] = {}
    for row in highwater_report_rows:
        marker_name = str(row["task_marker"])
        chosen_head = row.get("chosen_reviewed_head")
        if not isinstance(chosen_head, str) or not chosen_head:
            refused.append(marker_name)
            continue
        chosen_side = str(row.get("chosen_side") or "")
        base_payload = (
            legacy_scan.parsed_documents_by_marker_name.get(marker_name)
            if chosen_side == "legacy"
            else canonical_scan.parsed_documents_by_marker_name.get(marker_name)
        )
        if base_payload is None:
            base_payload = (
                canonical_scan.parsed_documents_by_marker_name.get(marker_name)
                or legacy_scan.parsed_documents_by_marker_name.get(marker_name)
            )
        if base_payload is None:
            refused.append(marker_name)
            continue
        runtime_file_name = runtime_marker_file_name_for_migrated_marker(
            legacy_marker_name=marker_name,
            marker_document=base_payload,
            marker_subdirectory=REVIEW_HIGHWATER_SUBDIRECTORY,
        )
        if runtime_file_name is None:
            if migrated_marker_repository_is_absent_from_disk(base_payload):
                skipped_because_repository_is_gone.append(marker_name)
            else:
                refused.append(marker_name)
            continue
        candidates_by_runtime_file_name.setdefault(runtime_file_name, []).append(
            (
                MarkerCandidateForOneRuntimeFileName(
                    source_marker_name=marker_name,
                    chosen_head=chosen_head,
                    payload=base_payload,
                    repository_directory=_marker_repository_directory(base_payload),
                ),
                row,
            )
        )
    collisions: list[dict[str, Any]] = []
    for runtime_file_name, entries in sorted(candidates_by_runtime_file_name.items()):
        if len(entries) == 1:
            candidate, row = entries[0]
            collision_record: dict[str, Any] | None = None
        else:
            chosen_candidate, collision_record = (
                choose_conservative_oldest_marker_colliding_on_one_runtime_file_name(
                    runtime_file_stem=Path(runtime_file_name).stem,
                    candidates=[candidate for candidate, _ in entries],
                )
            )
            collisions.append(collision_record)
            if chosen_candidate is None:
                refused.extend(candidate.source_marker_name for candidate, _ in entries)
                continue
            candidate, row = next(
                entry
                for entry in entries
                if entry[0].source_marker_name == chosen_candidate.source_marker_name
            )
        payload = dict(candidate.payload)
        payload["reviewed_head"] = candidate.chosen_head
        payload["merged_from_state_roots"] = {
            "legacy_reviewed_head": row.get("legacy_reviewed_head"),
            "canonical_reviewed_head": row.get("canonical_reviewed_head"),
            "ancestry_relation": row.get("ancestry_relation"),
            "chosen_side": str(row.get("chosen_side") or ""),
            "conflict_classes": list(row.get("conflict_classes") or []),
            "written_off_unit_ids": list(row.get("written_off_unit_ids") or []),
            "merged_at": utc_now_text(),
            "runtime_file_name_collision": collision_record,
        }
        _write_marker_document(destination / runtime_file_name, payload)
        written.append(candidate.source_marker_name)
        written_runtime_file_stems.append(Path(runtime_file_name).stem)
    return {
        "written_marker_names": sorted(written),
        "written_marker_file_stems": sorted(written_runtime_file_stems),
        "refused_marker_names": sorted(refused),
        "skipped_marker_names_whose_repository_is_gone": sorted(
            skipped_because_repository_is_gone
        ),
        "runtime_file_name_collisions": collisions,
    }


def stage_round_baseline_markers(
    *,
    legacy_root: Path,
    canonical_root: Path,
    staging_root: Path,
) -> dict[str, Any]:
    legacy_scan = collect_marker_documents(legacy_root, ROUND_BASELINE_PENDING_SUBDIRECTORY)
    canonical_scan = collect_marker_documents(
        canonical_root, ROUND_BASELINE_PENDING_SUBDIRECTORY
    )
    destination = (
        staging_root
        / STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME
        / ROUND_BASELINE_PENDING_SUBDIRECTORY
    )
    reconciliations: list[dict[str, Any]] = []
    written: list[str] = []
    written_runtime_file_stems: list[str] = []
    refused: list[str] = []
    skipped_because_repository_is_gone: list[str] = []
    candidates_by_runtime_file_name: dict[
        str, list[tuple[MarkerCandidateForOneRuntimeFileName, RoundBaselineReconciliation]]
    ] = {}
    for marker_name in sorted(
        set(legacy_scan.parsed_documents_by_marker_name)
        | set(canonical_scan.parsed_documents_by_marker_name)
        | legacy_scan.marker_names_whose_file_could_not_be_parsed
        | canonical_scan.marker_names_whose_file_could_not_be_parsed
    ):
        reconciliation = choose_round_baseline(
            task_marker_name=marker_name,
            legacy_marker=legacy_scan.parsed_documents_by_marker_name.get(marker_name),
            canonical_marker=canonical_scan.parsed_documents_by_marker_name.get(marker_name),
            legacy_marker_file_present_but_unreadable=(
                marker_name in legacy_scan.marker_names_whose_file_could_not_be_parsed
            ),
            canonical_marker_file_present_but_unreadable=(
                marker_name in canonical_scan.marker_names_whose_file_could_not_be_parsed
            ),
        )
        reconciliations.append(
            {
                "task_marker": reconciliation.task_marker_name,
                "legacy_baseline_head": reconciliation.legacy_baseline_head,
                "canonical_baseline_head": reconciliation.canonical_baseline_head,
                "ancestry_relation": reconciliation.ancestry_relation,
                "chosen_baseline_head": reconciliation.chosen_baseline_head,
                "chosen_side": reconciliation.chosen_side,
                "conflict_classes": reconciliation.conflict_classes,
            }
        )
        if not reconciliation.chosen_baseline_head:
            refused.append(marker_name)
            continue
        base_payload = (
            legacy_scan.parsed_documents_by_marker_name.get(marker_name)
            if reconciliation.chosen_side.startswith("legacy")
            else canonical_scan.parsed_documents_by_marker_name.get(marker_name)
        )
        if base_payload is None:
            base_payload = (
                canonical_scan.parsed_documents_by_marker_name.get(marker_name)
                or legacy_scan.parsed_documents_by_marker_name.get(marker_name)
            )
        if base_payload is None:
            refused.append(marker_name)
            continue
        runtime_file_name = runtime_marker_file_name_for_migrated_marker(
            legacy_marker_name=marker_name,
            marker_document=base_payload,
            marker_subdirectory=ROUND_BASELINE_PENDING_SUBDIRECTORY,
        )
        if runtime_file_name is None:
            if migrated_marker_repository_is_absent_from_disk(base_payload):
                skipped_because_repository_is_gone.append(marker_name)
            else:
                refused.append(marker_name)
            continue
        candidates_by_runtime_file_name.setdefault(runtime_file_name, []).append(
            (
                MarkerCandidateForOneRuntimeFileName(
                    source_marker_name=marker_name,
                    chosen_head=reconciliation.chosen_baseline_head,
                    payload=base_payload,
                    repository_directory=_marker_repository_directory(base_payload),
                ),
                reconciliation,
            )
        )
    collisions: list[dict[str, Any]] = []
    for runtime_file_name, entries in sorted(candidates_by_runtime_file_name.items()):
        if len(entries) == 1:
            candidate, reconciliation = entries[0]
            collision_record: dict[str, Any] | None = None
        else:
            chosen_candidate, collision_record = (
                choose_conservative_oldest_marker_colliding_on_one_runtime_file_name(
                    runtime_file_stem=Path(runtime_file_name).stem,
                    candidates=[candidate for candidate, _ in entries],
                )
            )
            collisions.append(collision_record)
            if chosen_candidate is None:
                refused.extend(candidate.source_marker_name for candidate, _ in entries)
                continue
            candidate, reconciliation = next(
                entry
                for entry in entries
                if entry[0].source_marker_name == chosen_candidate.source_marker_name
            )
        payload = dict(candidate.payload)
        payload["baseline_head"] = candidate.chosen_head
        # head 取「可证明更旧」，TTL 取「所有贡献者里最新鲜」——两个方向都得保守，否则
        # 取更旧的一侧会连它已经过期的 expires_at 一起继承，落盘 marker 变 stale ⇒
        # resolve_round_baseline_head() 返回 None ⇒ bootstrap 下界整个消失（比不合并还差）。
        # 贡献者要连 legacy↔canonical 两侧一起数：那层裁决按 chosen_side 取 base_payload，
        # 同一条失效路径不撞车也能走到。
        contributing_marker_documents: list[tuple[str, Mapping[str, Any]]] = []
        for entry_candidate, _entry_reconciliation in entries:
            for side_label, side_scan in (
                ("legacy", legacy_scan),
                ("canonical", canonical_scan),
            ):
                document = side_scan.parsed_documents_by_marker_name.get(
                    entry_candidate.source_marker_name
                )
                if document is not None:
                    contributing_marker_documents.append(
                        (f"{side_label}:{entry_candidate.source_marker_name}", document)
                    )
        time_to_live_adoption = (
            adopt_freshest_time_to_live_across_markers_merged_into_one_round_baseline_file(
                payload=payload,
                contributing_marker_documents=contributing_marker_documents,
            )
        )
        payload["merged_from_state_roots"] = {
            "legacy_baseline_head": reconciliation.legacy_baseline_head,
            "canonical_baseline_head": reconciliation.canonical_baseline_head,
            "ancestry_relation": reconciliation.ancestry_relation,
            "chosen_side": reconciliation.chosen_side,
            "conflict_classes": reconciliation.conflict_classes,
            "merged_at": utc_now_text(),
            "runtime_file_name_collision": collision_record,
            "time_to_live_adoption": time_to_live_adoption,
        }
        _write_marker_document(destination / runtime_file_name, payload)
        written.append(candidate.source_marker_name)
        written_runtime_file_stems.append(Path(runtime_file_name).stem)
    return {
        "reconciliations": reconciliations,
        "written_marker_names": sorted(written),
        "written_marker_file_stems": sorted(written_runtime_file_stems),
        "refused_marker_names": sorted(refused),
        "skipped_marker_names_whose_repository_is_gone": sorted(
            skipped_because_repository_is_gone
        ),
        "runtime_file_name_collisions": collisions,
    }


def stage_union_by_file_name_directory(
    *,
    entry_name: str,
    legacy_root: Path,
    canonical_root: Path,
    staging_root: Path,
) -> dict[str, Any]:
    """按文件名并集把一个目录搬进 staging；同名以 canonical 为准。

    这些目录（``handoff-notified`` / ``kanban-task-suppressions`` /
    ``dispatch-confirmations``）都是「存在即事实」的戳，没有需要裁决的内部状态：
    并集的方向一律是**更少动作**（已通知过就不再通知、已抑制就保持抑制），所以同名
    冲突取哪一侧都不改变行为，取 canonical 只是为了让结果确定。
    """

    destination = staging_root / STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME / entry_name
    copied_from_legacy = 0
    copied_from_canonical = 0
    same_name_on_both_sides = 0
    # canonical 先落，legacy 只补 canonical 没有的名字——次序反过来会让同名文件被拷
    # 两次，计数也就跟着虚高。
    for source_root, side in ((canonical_root, "canonical"), (legacy_root, "legacy")):
        source_directory = source_root / entry_name
        if not source_directory.is_dir():
            continue
        for child in sorted(source_directory.iterdir()):
            if not child.is_file() or child.name.startswith("."):
                continue
            target = destination / child.name
            if target.exists():
                same_name_on_both_sides += 1
                continue
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(child, target)
            if side == "canonical":
                copied_from_canonical += 1
            else:
                copied_from_legacy += 1
    return {
        "entry_name": entry_name,
        "copied_from_legacy": copied_from_legacy,
        "copied_from_canonical": copied_from_canonical,
        "same_name_on_both_sides_kept_canonical": same_name_on_both_sides,
    }


def stage_latest_run_pointer(
    *,
    legacy_root: Path,
    canonical_root: Path,
    staging_root: Path,
) -> dict[str, Any]:
    """``latest.json``：取 ``updated_at`` 更新的一侧，并重写其中的绝对路径。

    这是 ``rvf-analyze --latest`` 的唯一入口（``rvf_analyze._read_latest_run_dir``
    读 ``summary_path`` 的父目录）。两根各有一份、内容不同；若不合并，切根之后
    ``--latest`` 会指向一个已经被移走的 legacy 路径。
    """

    documents: dict[str, dict[str, Any]] = {}
    for side, root in (("legacy", legacy_root), ("canonical", canonical_root)):
        payload = read_json_document(root / "latest.json")
        if payload is not None:
            documents[side] = payload
    if not documents:
        return {"chosen_side": None, "rewritten_path_count": 0}

    chosen_side = max(
        documents,
        key=lambda side: str(documents[side].get("updated_at") or ""),
    )
    payload = dict(documents[chosen_side])
    rewritten = 0
    for column in ("summary_path", "events_path"):
        new_value, changed = rewrite_legacy_root_absolute_path(
            payload.get(column), legacy_root, canonical_root
        )
        if changed:
            payload[column] = new_value
            rewritten += 1
    _write_marker_document(
        staging_root / STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME / "latest.json", payload
    )
    return {
        "chosen_side": chosen_side,
        "chosen_run_id": payload.get("run_id"),
        "rewritten_path_count": rewritten,
    }


def plan_run_directory_moves(legacy_root: Path, canonical_root: Path) -> dict[str, Any]:
    """``runs/`` 的搬迁计划。**只出计划，不动文件**——真正的 rename 在 ``switch``。

    legacy 侧实测 18 GB，canonical 侧 446 MB。「整棵树复制进 staging 再换入」会产生
    36 GB churn，不可行；两根同在一个卷、run id 撞名实测为 0，故逐个 run 目录
    ``os.rename`` 即可：O(1) 元数据操作，且每个 run 目录的移动本身是原子的、
    run 目录之间互相独立。

    这个**循环**天然可重入：搬走的目录不再出现在 legacy ``runs/`` 里，所以崩在中间
    时重入看到的就是「剩下没搬的那些」（因此循环里「目标已存在」只能是 merge 之后
    新出现的真撞名，不是自己上一轮搬的）。但请注意：``switch`` **整条序列**的可续跑
    不是靠这条性质，而是靠 ``run_switch_command`` 的阶段账本——换入 tracker 之后源库
    已变成合并结果，前置校验必然误报，光靠「重跑一次命令」是接不上的。
    """

    legacy_runs = run_identifiers_in_root(legacy_root)
    canonical_runs = run_identifiers_in_root(canonical_root)
    colliding = sorted(legacy_runs & canonical_runs)
    return {
        "legacy_run_count": len(legacy_runs),
        "canonical_run_count": len(canonical_runs),
        "run_identifiers_to_move": sorted(legacy_runs - canonical_runs),
        "colliding_run_identifiers": colliding,
    }


# ---------------------------------------------------------------------------
# merge / verify / switch
# ---------------------------------------------------------------------------


def default_staging_root_for(canonical_root: Path) -> Path:
    """staging 放 canonical 根的**兄弟位置**。

    两个硬约束：① 必须与 canonical 同卷，``switch`` 的 ``os.replace`` / ``os.rename``
    才是原子的；② 必须落在 canonical 根**之外**，否则它自己会被顶层条目登记闸当成一个
    未登记的状态根条目（也会被 ``runs`` 枚举、tracker 配对重复计数）。
    """

    return canonical_root.parent / "rvf-state-root-merge-staging"


def load_merge_ledger(staging_root: Path) -> dict[str, Any] | None:
    return read_json_document(staging_root / MERGE_LEDGER_FILE_NAME)


def switch_stage_journal_path(staging_root: Path) -> Path:
    return staging_root / SWITCH_STAGE_JOURNAL_FILE_NAME


def load_switch_stage_journal(staging_root: Path) -> dict[str, Any] | None:
    return read_json_document(switch_stage_journal_path(staging_root))


def switch_has_started_consuming_staging(staging_root: Path) -> bool:
    """staging 是否已经被某次 ``switch`` 动过（哪怕只动了一步）。

    存在即代表「至少一个破坏性操作已经发生」：账本是在第一次 rename **之前**落盘的。
    """

    journal = load_switch_stage_journal(staging_root)
    return bool(journal and journal.get("destructive_phase_entered"))


def paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def device_identifier_of_nearest_existing_ancestor(path: Path) -> int | None:
    """``path`` 所在卷的 ``st_dev``；路径还不存在就沿父目录上溯。

    ``os.rename`` 跨卷会 EXDEV。归档目标通常尚不存在（正是要建出来的那个），所以
    只能拿它**将要落在**的那个已存在祖先来判卷。
    """

    candidate = path
    while True:
        if candidate.exists():
            return os.stat(candidate).st_dev
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent


def archive_root_rejection(
    *,
    archive_root: Path,
    legacy_root: Path,
    canonical_root: Path,
    staging_root: Path,
) -> tuple[str, str] | None:
    """归档目标的资格闸；合格返回 ``None``，否则给 ``(reason, detail)``。

    这道闸**必须排在整个破坏性阶段之前**。``os.rename(legacy_root, archive_root)``
    是整条序列的最后一步：那时 tracker 已换入、runs 已搬完、origin stamp 已写。
    把归档指向 legacy 子目录会在这一步 EINVAL、跨卷会 EXDEV，失败位置最不可恢复且
    不产出 switch 回执；指向 canonical / staging 内部更糟——rename 会**成功**，
    于是整棵 legacy 树变成 canonical 根里的一个未登记顶层条目（顺带被 ``runs``
    枚举与 tracker 配对重复计数），一次「成功」的切换就此埋下下一次分裂。
    """

    if archive_root.exists():
        return ("archive_root_already_exists", str(archive_root))
    for label, other_root in (
        ("legacy", legacy_root),
        ("canonical", canonical_root),
        ("staging", staging_root),
    ):
        if paths_overlap(archive_root, other_root):
            return (
                "archive_root_overlaps_state_roots",
                f"归档目标与 {label} 根重叠：archive={archive_root} / {label}={other_root}。"
                "归档必须落在三者之外——落在 legacy 内部会 EINVAL，落在 canonical / "
                "staging 内部则会「成功」地把旧状态树塞进正在使用的根里。",
            )
    parent_directory = archive_root.parent
    if not parent_directory.is_dir():
        return (
            "archive_root_parent_directory_missing",
            f"归档目标的父目录不存在：{parent_directory}",
        )
    legacy_device = device_identifier_of_nearest_existing_ancestor(legacy_root)
    archive_device = device_identifier_of_nearest_existing_ancestor(archive_root)
    if legacy_device is None or archive_device is None or legacy_device != archive_device:
        return (
            "archive_root_on_a_different_volume",
            f"归档目标与 legacy 根不同卷（legacy st_dev={legacy_device} / "
            f"archive st_dev={archive_device}）：archive={archive_root}。"
            "``os.rename`` 跨卷会 EXDEV，而这一步排在所有破坏性操作之后。",
        )
    return None


def seal_directory_tree_read_only(root: Path) -> dict[str, Any]:
    """把归档树整棵封成只读：目录 ``0o500``、文件 ``0o400``。

    自底向上走（``topdown=False``）：某个目录被 yield 时它的子孙都已经列举完，
    所以「先摘掉目录的 w 位再想读它」这种自锁不会发生。symlink 一律跳过——
    ``os.chmod`` 跟随符号链接，跟过去就会改到归档树**外面**的东西。

    失败不抛：走到这一步，所有破坏性操作都已完成，为一个 chmod 崩掉只会让状态更
    难收拾。失败逐条记进回执与阶段账本，让「归档没封住」是一个可见事实而不是沉默。
    """

    sealed_directory_count = 0
    sealed_file_count = 0
    failures: list[str] = []
    for current_directory, _subdirectory_names, file_names in os.walk(root, topdown=False):
        current_path = Path(current_directory)
        for file_name in file_names:
            target = current_path / file_name
            if target.is_symlink():
                continue
            try:
                os.chmod(target, LEGACY_ROOT_ARCHIVE_FILE_MODE)
            except OSError as error:
                failures.append(f"{target}: {error}")
            else:
                sealed_file_count += 1
        try:
            os.chmod(current_path, LEGACY_ROOT_ARCHIVE_DIRECTORY_MODE)
        except OSError as error:
            failures.append(f"{current_path}: {error}")
        else:
            sealed_directory_count += 1
    return {
        "directory_count": sealed_directory_count,
        "file_count": sealed_file_count,
        "failure_count": len(failures),
        "failures": failures[:20],
        "directory_mode": oct(LEGACY_ROOT_ARCHIVE_DIRECTORY_MODE),
        "file_mode": oct(LEGACY_ROOT_ARCHIVE_FILE_MODE),
    }


def run_merge_command(
    *,
    legacy_root: Path,
    canonical_root: Path,
    staging_root: Path,
    conflict_policy: str,
    canonical_root_provenance_description: str | None,
    rehearse_without_quiescence_gates: bool,
) -> tuple[int, dict[str, Any]]:
    # 半途而废的 switch 之后**绝不能**重跑 merge：那时 canonical 侧已有若干个仓被
    # 换成了合并结果，再合一次就是拿合并库当新基库、把 legacy 的行并第二遍。这里
    # fail-closed 并指向续跑，而不是让「重跑 merge」看上去像一条可行的退路。
    if switch_has_started_consuming_staging(staging_root):
        return 2, {
            "command": "merge",
            "status": "refused",
            "blocking_reasons": ["switch_already_started_consuming_this_staging"],
            "detail": (
                f"{switch_stage_journal_path(staging_root)} 显示这份 staging 已被一次 "
                "switch 消费过（哪怕只换入了一个仓）。重跑 merge 会把已换入的合并库当成"
                "新基库再合并一遍。正确做法是用**同一份** staging 重跑 switch 续跑；"
                "确实要重来则须先从 replaced-canonical-tracker-repositories 与归档树"
                "回滚，再删掉阶段账本。"
            ),
        }

    pairs = discover_tracker_repository_pairs(legacy_root, canonical_root)
    unregistered_entries = unregistered_state_root_entry_names(legacy_root, canonical_root)
    quiescence = evaluate_migration_quiescence_gates(legacy_root, canonical_root, pairs)

    blocking: list[str] = []
    if unregistered_entries:
        blocking.append(
            "unregistered_state_root_entries:" + ",".join(unregistered_entries)
        )
    if not quiescence.quiesced and not rehearse_without_quiescence_gates:
        blocking.extend(quiescence.blocking_reasons)
    if blocking:
        return 2, {
            "command": "merge",
            "status": "refused",
            "blocking_reasons": blocking,
            "unregistered_state_root_entries": unregistered_entries,
            "quiescence": {
                "legacy_root_seconds_since_last_write": (
                    quiescence.legacy_root_seconds_since_last_write
                ),
                "canonical_root_seconds_since_last_write": (
                    quiescence.canonical_root_seconds_since_last_write
                ),
                "undrained_lease_count": quiescence.undrained_lease_count,
            },
        }

    # 非 tracker 来源的指纹，取在**一切读取之前**。方向是刻意的：若某个写落在「取指纹」
    # 与「读进 staging」之间，账本记的是旧指纹、staging 里是新数据 ⇒ verify 会报不一致
    # 而拒绝（保守误报，重跑 merge 即可）。反过来在读之后取，同一个写就会让账本记下新
    # 指纹、staging 里躺着旧数据，verify 认为「没变过」而放行，switch 把那个写整体丢掉，
    # 全程没有任何 finding——正是这道闸要挡的事情本身。
    non_tracker_source_fingerprints_by_side = {
        "legacy": fingerprint_non_tracker_merge_sources(legacy_root),
        "canonical": fingerprint_non_tracker_merge_sources(canonical_root),
    }

    staging_root.mkdir(parents=True, exist_ok=True)
    merged_state_root = staging_root / STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME
    if merged_state_root.exists():
        # 合并是有状态的（highwater 只会被裁决一次），所以「目标已存在就跳过」不是
        # 合法的幂等判据。重跑一律从干净的 staging 重建，再由账本里的源库 sha256
        # 承担真正的幂等语义（F1.3）。
        shutil.rmtree(merged_state_root)
    snapshot_root = staging_root / STAGING_SOURCE_SNAPSHOT_DIRECTORY_NAME
    if snapshot_root.exists():
        shutil.rmtree(snapshot_root)
    # 无条件建出来：一次「两根都没有可合并内容」的合并同样是一次**成功**的合并，
    # 不该让 verify 因为目录不存在而报「merged_state_root 缺失」这种误导性阻断。
    merged_state_root.mkdir(parents=True, exist_ok=True)

    # 对账表本身进账本：它是「写进去的每一条水位为什么是这个值」的唯一凭据。
    # 这里用 include_repositories_without_working_directory=True——``report`` 默认
    # 过滤掉本地路径已消失的 repo（几乎全是测试残留）是为了让人读得下去，**迁移
    # 不能这么过滤**：跳过一个 repo 就是丢掉它的全部 tracker 行。
    reconciliation_report = build_reconciliation_report(
        legacy_root,
        canonical_root,
        include_repositories_without_working_directory=True,
        conflict_policy=conflict_policy,
        canonical_root_provenance_description=canonical_root_provenance_description,
    )

    tracker_outcomes: list[TrackerRepositoryMergeOutcome] = []
    for pair in pairs:
        if pair.legacy_directory is None:
            # canonical 独有的 repo 完全不需要动：合并的方向是「legacy 并入
            # canonical」，切换时也不会去碰它们的目录。它们里面已有的孤儿 assigned
            # 单元是先于本次迁移就存在的问题（F7 invariant 3），不在本工具职责内，
            # 顺手改会把 568 个库都卷进这次不可逆操作。
            continue
        tracker_outcomes.append(
            merge_tracker_repository_into_staging(
                pair,
                legacy_root=legacy_root,
                canonical_root=canonical_root,
                staging_root=staging_root,
            )
        )

    highwater_staging = stage_review_highwater_markers(
        legacy_root=legacy_root,
        canonical_root=canonical_root,
        staging_root=staging_root,
        highwater_report_rows=reconciliation_report["review_highwater"],
    )
    round_baseline_staging = stage_round_baseline_markers(
        legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
    )
    union_directory_staging = [
        stage_union_by_file_name_directory(
            entry_name=strategy.entry_name_pattern,
            legacy_root=legacy_root,
            canonical_root=canonical_root,
            staging_root=staging_root,
        )
        for strategy in STATE_ROOT_ENTRY_MIGRATION_STRATEGIES
        if strategy.kind == STATE_ROOT_ENTRY_KIND_UNION_BY_FILE_NAME
    ]
    latest_pointer_staging = stage_latest_run_pointer(
        legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
    )
    run_move_plan = plan_run_directory_moves(legacy_root, canonical_root)

    repositories_refusing_to_merge = [
        {
            "repository_key": outcome.repository_key,
            "blocking_reasons": outcome.blocking_reasons,
        }
        for outcome in tracker_outcomes
        if outcome.blocking_reasons
    ]

    ledger = {
        "migration_tool_contract_version": MIGRATION_TOOL_CONTRACT_VERSION,
        "command": "merge",
        "status": "staged" if not repositories_refusing_to_merge else "staged_with_refusals",
        "generated_at": utc_now_text(),
        "legacy_root": str(legacy_root),
        "canonical_root": str(canonical_root),
        "canonical_root_provenance": canonical_root_provenance_description,
        "staging_root": str(staging_root),
        "highwater_conflict_policy": conflict_policy,
        "staging_was_produced_without_quiescence_gates": rehearse_without_quiescence_gates,
        "quiescence": {
            "legacy_root_seconds_since_last_write": (
                quiescence.legacy_root_seconds_since_last_write
            ),
            "canonical_root_seconds_since_last_write": (
                quiescence.canonical_root_seconds_since_last_write
            ),
            "undrained_lease_count": quiescence.undrained_lease_count,
            "blocking_reasons": quiescence.blocking_reasons,
        },
        "reconciliation_report": reconciliation_report,
        "tracker_repositories": [
            {
                "repository_key": outcome.repository_key,
                "merged_database_path": outcome.merged_database_path,
                "legacy_source_sha256": outcome.legacy_source_sha256,
                "canonical_source_sha256": outcome.canonical_source_sha256,
                "rows_imported_by_table": outcome.rows_imported_by_table,
                "rows_updated_by_table": outcome.rows_updated_by_table,
                "rows_skipped_by_table": outcome.rows_skipped_by_table,
                "unit_decision_class_counts": outcome.unit_decision_class_counts,
                "absolute_paths_rewritten": outcome.absolute_paths_rewritten,
                "identity_mismatch_rows": outcome.identity_mismatch_rows,
                "identity_quarantined_dependent_rows": (
                    outcome.identity_quarantined_dependent_rows
                ),
                "foreign_key_violations": outcome.foreign_key_violations,
                "blocking_reasons": outcome.blocking_reasons,
                "notes": outcome.notes,
            }
            for outcome in tracker_outcomes
        ],
        "repositories_refusing_to_merge": repositories_refusing_to_merge,
        "review_highwater_staging": highwater_staging,
        "round_baseline_staging": round_baseline_staging,
        "union_directory_staging": union_directory_staging,
        "latest_run_pointer_staging": latest_pointer_staging,
        "run_directory_move_plan": run_move_plan,
        # marker / ``latest.json`` / 并集目录这些来源没有对应的 per-repo 记录点，
        # 故整根记在账本顶层；``verify`` 拿它比对 merge 与 switch 之间的写。
        "non_tracker_source_fingerprints_by_side": non_tracker_source_fingerprints_by_side,
    }
    _write_marker_document(staging_root / MERGE_LEDGER_FILE_NAME, ledger)
    return (0 if not repositories_refusing_to_merge else 1), ledger


@dataclass
class VerificationFinding:
    severity: str
    check: str
    detail: str


def run_verify_command(
    *,
    legacy_root: Path,
    canonical_root: Path,
    staging_root: Path,
) -> tuple[int, dict[str, Any]]:
    findings: list[VerificationFinding] = []
    ledger = load_merge_ledger(staging_root)
    if ledger is None:
        return 2, {
            "command": "verify",
            "status": "refused",
            "findings": [
                {
                    "severity": "blocking",
                    "check": "merge_ledger_present",
                    "detail": f"没有可校验的 staging：{staging_root / MERGE_LEDGER_FILE_NAME} 不存在或不可解析",
                }
            ],
        }

    def blocking(check: str, detail: str) -> None:
        findings.append(VerificationFinding("blocking", check, detail))

    def advisory(check: str, detail: str) -> None:
        findings.append(VerificationFinding("advisory", check, detail))

    if str(ledger.get("legacy_root")) != str(legacy_root) or str(
        ledger.get("canonical_root")
    ) != str(canonical_root):
        blocking(
            "roots_match_ledger",
            f"账本记的两根与本次传入的不一致："
            f"账本 legacy={ledger.get('legacy_root')} canonical={ledger.get('canonical_root')}",
        )

    # 源库自 merge 之后是否又被改过。这是 merge 与 switch 之间那段时间窗的第二道闸：
    # 第一道（静默期）挡的是「现在还有人在写」，这一道挡的是「merge 之后有人写过」。
    for entry in ledger.get("tracker_repositories") or []:
        repository_key = str(entry.get("repository_key"))
        for side, root, recorded in (
            ("legacy", legacy_root, entry.get("legacy_source_sha256")),
            ("canonical", canonical_root, entry.get("canonical_source_sha256")),
        ):
            if not recorded:
                continue
            database_path = (
                root / TRACKER_REPOSITORIES_SUBDIRECTORY / repository_key
                / TRACKER_DATABASE_FILE_NAME
            )
            if not database_path.is_file():
                blocking(
                    "source_database_still_present",
                    f"{side} 侧 {repository_key} 的源库在 merge 之后消失了",
                )
                continue
            if (
                sha256_of_sqlite_database_including_write_ahead_log(database_path)
                != recorded
            ):
                blocking(
                    "source_database_unchanged_since_merge",
                    f"{side} 侧 {repository_key} 的源库在 merge 之后被改动过；"
                    f"必须重跑 merge，否则这些写入会被 switch 静默丢掉",
                )
        if entry.get("blocking_reasons"):
            blocking(
                "tracker_repository_merged_without_refusal",
                f"{repository_key}: {entry['blocking_reasons']}",
            )
        if entry.get("foreign_key_violations"):
            blocking(
                "foreign_key_check_clean",
                f"{repository_key}: {entry['foreign_key_violations'][:5]}",
            )
        if entry.get("identity_mismatch_rows"):
            advisory(
                "identity_columns_agreed_across_roots",
                f"{repository_key}: 保留 canonical 的 "
                f"{len(entry['identity_mismatch_rows'])} 行、并连带排除了 legacy 侧 "
                f"{len(entry.get('identity_quarantined_dependent_rows') or [])} 行"
                f"引用这些 key 的依赖行；明细见账本",
            )

    # 同一道闸的其余覆盖面：marker / ``latest.json`` / 三个并集目录。上面按仓比对的只有
    # tracker 库，可 ``merge`` 从两根读进 staging 的远不止它们，而 ``switch`` 会把 legacy
    # 根整棵改名归档 ⇒ 这段窗口里对这些来源的写最终只留在 archive 里、canonical 侧不可见。
    recorded_fingerprints_by_side = ledger.get("non_tracker_source_fingerprints_by_side")
    if not isinstance(recorded_fingerprints_by_side, dict):
        blocking(
            "non_tracker_sources_fingerprinted_in_ledger",
            "账本没有记录非 tracker 来源（marker / latest.json / 并集目录）的指纹，"
            "无法证明它们在 merge 之后没被写过；这份 staging 出自旧版本的 merge，请重跑 merge",
        )
    else:
        for side, root in (("legacy", legacy_root), ("canonical", canonical_root)):
            recorded_fingerprints = recorded_fingerprints_by_side.get(side)
            if not isinstance(recorded_fingerprints, dict):
                blocking(
                    "non_tracker_sources_fingerprinted_in_ledger",
                    f"账本缺 {side} 侧的非 tracker 来源指纹",
                )
                continue
            current_fingerprints = fingerprint_non_tracker_merge_sources(root)
            for entry_name in sorted(set(recorded_fingerprints) | set(current_fingerprints)):
                if recorded_fingerprints.get(entry_name) == current_fingerprints.get(entry_name):
                    continue
                blocking(
                    "non_tracker_source_unchanged_since_merge",
                    f"{side} 侧 {entry_name} 在 merge 之后被改动过（新增/删除/改写文件）；"
                    f"必须重跑 merge，否则这些写入会被 switch 静默丢掉",
                )

    merged_state_root = staging_root / STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME
    if not merged_state_root.is_dir():
        blocking("merged_state_root_present", f"{merged_state_root} 不存在")
        return _verification_result(findings, ledger, staging_root)

    legacy_prefix = str(legacy_root)
    for database_path in sorted(
        (merged_state_root / TRACKER_REPOSITORIES_SUBDIRECTORY).glob(
            f"*/{TRACKER_DATABASE_FILE_NAME}"
        )
    ):
        repository_key = database_path.parent.name
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if str(integrity).lower() != "ok":
                blocking("integrity_check", f"{repository_key}: {integrity}")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                blocking(
                    "foreign_key_check", f"{repository_key}: {len(violations)} 条悬挂引用"
                )
            tables = table_names_in_database(connection)
            if "leases" in tables:
                placeholders = ",".join("?" for _ in UNDRAINED_LEASE_STATES)
                undrained = connection.execute(
                    f"SELECT COUNT(*) FROM leases WHERE state IN ({placeholders})",
                    tuple(sorted(UNDRAINED_LEASE_STATES)),
                ).fetchone()[0]
                if undrained:
                    blocking(
                        "no_undrained_leases_in_merged_database",
                        f"{repository_key}: {undrained} 把 active/paused lease",
                    )
            if {"units", "lease_units"} <= tables:
                orphan_assigned = connection.execute(
                    "SELECT COUNT(*) FROM units u WHERE u.review_state='assigned' "
                    "AND NOT EXISTS (SELECT 1 FROM lease_units lu WHERE lu.unit_id=u.unit_id)"
                ).fetchone()[0]
                if orphan_assigned:
                    blocking(
                        "no_orphan_assigned_units",
                        f"{repository_key}: {orphan_assigned} 个 assigned 却无 lease_units 行的单元"
                        f"（合并规则②本应把它们全部降回 available）",
                    )
            for table_name, columns in ABSOLUTE_PATH_COLUMNS_BY_TABLE.items():
                if table_name not in tables:
                    continue
                for column in columns:
                    remaining = connection.execute(
                        f"SELECT COUNT(*) FROM {table_name} WHERE {column} LIKE ?",
                        (f"{legacy_prefix}%",),
                    ).fetchone()[0]
                    if remaining:
                        blocking(
                            "legacy_absolute_paths_rewritten",
                            f"{repository_key}.{table_name}.{column}: 还有 {remaining} 行指向 legacy 根",
                        )
        finally:
            connection.close()
        for sidecar_suffix in ("-wal", "-shm"):
            sidecar = database_path.with_name(database_path.name + sidecar_suffix)
            if sidecar.exists():
                blocking(
                    "no_sqlite_sidecar_in_staging",
                    f"{repository_key}: staging 里残留 {sidecar.name}，整目录换入会把新库配上旧 WAL",
                )

    for subdirectory, head_field in (
        (REVIEW_HIGHWATER_SUBDIRECTORY, "reviewed_head"),
        (ROUND_BASELINE_PENDING_SUBDIRECTORY, "baseline_head"),
    ):
        for marker_path in sorted((merged_state_root / subdirectory).glob("*.json")):
            payload = read_json_document(marker_path)
            if payload is None:
                blocking("merged_marker_parses", f"{subdirectory}/{marker_path.name} 解析失败")
                continue
            head = payload.get(head_field)
            if not isinstance(head, str) or not REVIEW_HIGHWATER_FULL_COMMIT_SHA_PATTERN.match(
                head
            ):
                blocking(
                    "merged_marker_head_is_full_sha",
                    f"{subdirectory}/{marker_path.name}: {head_field}={head!r}",
                )

    # marker 的**拒绝**必须成为阻断，而且只有回读账本才看得见。``stage_review_highwater_markers``
    # / ``stage_round_baseline_markers`` 对无法裁决的 marker 是**不写出文件**的，于是它在
    # staging 里与「两根本来就没有这个 marker」完全不可区分——上面那段只遍历 staging 现存
    # 文件的检查因此永远看不到它。而 ``switch`` 随后把 legacy 根整棵改名归档：那份**本来
    # 可读**的水位就此脱离活树、永久取不回来。review 高水位是 committed-round 的窗口下界，
    # 丢一个水位＝那个 task 的一批已提交改动要么被永久漏审、要么被反复重审。
    for staging_ledger_key, subdirectory in (
        ("review_highwater_staging", REVIEW_HIGHWATER_SUBDIRECTORY),
        ("round_baseline_staging", ROUND_BASELINE_PENDING_SUBDIRECTORY),
    ):
        staging_summary = ledger.get(staging_ledger_key)
        if not isinstance(staging_summary, dict):
            blocking(
                "marker_staging_summary_present_in_ledger",
                f"账本缺 {staging_ledger_key}：无法证明 {subdirectory} 下每个 marker 都有归宿",
            )
            continue
        refused_marker_names = sorted(
            str(name) for name in (staging_summary.get("refused_marker_names") or [])
        )
        markers_whose_repository_is_gone = sorted(
            str(name)
            for name in (
                staging_summary.get("skipped_marker_names_whose_repository_is_gone") or []
            )
        )
        if markers_whose_repository_is_gone:
            advisory(
                "markers_skipped_because_repository_is_gone",
                f"{subdirectory}: {len(markers_whose_repository_is_gone)} 个 marker 的仓已不在"
                f"磁盘上（{markers_whose_repository_is_gone[:5]}），跳过不迁。不阻断：运行时同样"
                f"算不出这些仓的身份，迁过去也永远不会被查到；但数目记在这里，别当它不存在",
            )
        if refused_marker_names:
            blocking(
                "no_marker_left_unreconciled",
                f"{subdirectory}: {len(refused_marker_names)} 个 marker 无法裁决、未写进 staging"
                f"（{refused_marker_names[:5]}）；switch 会把 legacy 根整棵归档，这些 task 的"
                f"水位将永久取不回来。先手工裁决它们，再重跑 merge",
            )
        # 比的是**落盘文件名**，不是账本里那个来源侧 marker 名：两者自「marker key 编入
        # 仓身份 / harness」起不再相等（``runtime_marker_file_name_for_migrated_marker``）。
        # 账本仍保留来源名以便与对账表逐行对上，但这条双向集合检查必须走同一个命名空间，
        # 否则它会对**每一个** marker 恒报缺失，从一条真检查退化成一条噪声。
        recorded_written_file_stems = staging_summary.get("written_marker_file_stems")
        if not isinstance(recorded_written_file_stems, list):
            blocking(
                "marker_staging_summary_records_written_file_stems",
                f"账本的 {staging_ledger_key} 缺 written_marker_file_stems：无法证明 "
                f"{subdirectory} 下写出的文件与账本一致",
            )
            continue
        recorded_written_marker_names = {str(name) for name in recorded_written_file_stems}
        staged_marker_names = {
            path.stem for path in (merged_state_root / subdirectory).glob("*.json")
        }
        missing_from_staging = sorted(recorded_written_marker_names - staged_marker_names)
        if missing_from_staging:
            blocking(
                "ledger_written_markers_all_present_in_staging",
                f"{subdirectory}: 账本记录已写出、staging 里却找不到 "
                f"{missing_from_staging[:5]}（共 {len(missing_from_staging)} 个）",
            )
        absent_from_ledger = sorted(staged_marker_names - recorded_written_marker_names)
        if absent_from_ledger:
            blocking(
                "staged_markers_all_recorded_in_ledger",
                f"{subdirectory}: staging 里有账本未登记的 marker "
                f"{absent_from_ledger[:5]}；staging 在 merge 之后被改过",
            )

    latest_pointer = read_json_document(merged_state_root / "latest.json")
    if latest_pointer is not None:
        for column in ("summary_path", "events_path"):
            value = latest_pointer.get(column)
            if isinstance(value, str) and value.startswith(legacy_prefix):
                blocking(
                    "latest_pointer_rewritten", f"latest.json.{column} 仍指向 legacy 根"
                )

    plan = ledger.get("run_directory_move_plan") or {}
    if plan.get("colliding_run_identifiers"):
        blocking(
            "no_colliding_run_identifiers",
            f"{len(plan['colliding_run_identifiers'])} 个 run id 在两根同名；逐目录 rename 会撞车",
        )

    return _verification_result(findings, ledger, staging_root)


def _verification_result(
    findings: Sequence[VerificationFinding],
    ledger: Mapping[str, Any],
    staging_root: Path,
) -> tuple[int, dict[str, Any]]:
    blocking_findings = [item for item in findings if item.severity == "blocking"]
    receipt = {
        "command": "verify",
        "status": "passed" if not blocking_findings else "failed",
        "generated_at": utc_now_text(),
        "staging_root": str(staging_root),
        "merge_ledger_generated_at": ledger.get("generated_at"),
        "staging_was_produced_without_quiescence_gates": ledger.get(
            "staging_was_produced_without_quiescence_gates", False
        ),
        "findings": [
            {"severity": item.severity, "check": item.check, "detail": item.detail}
            for item in findings
        ],
    }
    _write_marker_document(staging_root / VERIFICATION_RECEIPT_FILE_NAME, receipt)
    return (0 if not blocking_findings else 1), receipt


def switch_stage_journal_mismatch(
    journal: Mapping[str, Any],
    *,
    legacy_root: Path,
    canonical_root: Path,
    archive_root: Path,
    ledger: Mapping[str, Any],
) -> tuple[str, str] | None:
    """续跑前核对阶段账本的身份；对得上返回 ``None``。

    账本是「这份 staging 的全量校验已经通过过一次」的**唯一凭据**——续跑正是靠它
    才敢跳过必然会红的源库指纹闸。所以凭据对不上号时一律拒绝，绝不静默往下走。
    """

    if int(journal.get("migration_tool_contract_version") or 0) != (
        MIGRATION_TOOL_CONTRACT_VERSION
    ):
        return (
            "switch_stage_journal_contract_version_mismatch",
            f"账本 contract={journal.get('migration_tool_contract_version')} / "
            f"本工具 contract={MIGRATION_TOOL_CONTRACT_VERSION}",
        )
    if str(journal.get("legacy_root")) != str(legacy_root) or str(
        journal.get("canonical_root")
    ) != str(canonical_root):
        return (
            "switch_stage_journal_records_different_state_roots",
            f"账本 legacy={journal.get('legacy_root')} canonical={journal.get('canonical_root')}；"
            f"本次传入 legacy={legacy_root} canonical={canonical_root}",
        )
    if str(journal.get("merge_ledger_generated_at")) != str(ledger.get("generated_at")):
        return (
            "switch_stage_journal_belongs_to_a_different_merge",
            f"账本对应的 merge generated_at={journal.get('merge_ledger_generated_at')}，"
            f"当前 staging 的 merge generated_at={ledger.get('generated_at')}",
        )
    completed_stages = journal.get("completed_stages") or []
    if SWITCH_STAGE_LEGACY_ROOT_ARCHIVED in completed_stages and str(
        journal.get("archive_root")
    ) != str(archive_root):
        return (
            "switch_stage_journal_records_a_different_archive_root",
            f"legacy 根已经被归档到 {journal.get('archive_root')}，"
            f"本次却传入 {archive_root}；续跑必须沿用同一个归档目标。",
        )
    return None


def run_switch_command(
    *,
    legacy_root: Path,
    canonical_root: Path,
    staging_root: Path,
    archive_root: Path,
) -> tuple[int, dict[str, Any]]:
    """把 staging 的合并结果换入 canonical，搬走 runs，再把 legacy 根改名并封成只读归档。

    **这条序列不是原子的，而是「分阶段 + 可续跑」的**，理由与协议如下。

    换入 tracker 用的是**目录整体 rename**而不是逐文件 ``os.replace``：tracker 库跑
    WAL，主文件被换掉而 ``-wal`` / ``-shm`` 还留在原地就是直接的库损坏。目录换入让
    「新库 + 它自己的（无）sidecar」作为一个整体生效，中途崩溃也只会留下一个显式命名
    的备份目录，而不是一个半新半旧的库。

    但**单步原子救不了整条序列**：换入第一个仓之后，canonical 侧的源库就已经是合并
    结果，重跑 switch 时 ``run_verify_command`` 的 ``source_database_unchanged_since_merge``
    必然红；而 staging 的 merged 目录已被 rename 消费掉，重跑 merge 又会把已换入的
    合并库当成新基库再并一遍。两条路都堵死 ⇒ 必须有阶段账本。

    协议：

    1. 全量校验（``run_verify_command``）是**进入破坏性阶段的前置条件**，只在第一次
       跑；通过后立刻把阶段账本落到 staging（``switch-stage-journal.json``），标上
       ``destructive_phase_entered``。账本存在即代表「校验已通过、且至少一步已经开始」。
    2. 每完成一个阶段（tracker 换入还细到每个仓）就更新账本。
    3. 续跑时不再重跑校验——那道闸此时必然误报——改为核对账本身份（工具 contract
       版本 / 两根 / merge 账本的 ``generated_at`` / 已归档时的归档目标），对不上就
       拒绝。随后从账本记的下一个阶段接手；已完成的阶段一律跳过。
       这条「只跑一次」对 ``run_verify_command`` 里那两道**回读账本**的闸同样成立且
       同样安全：``non_tracker_source_unchanged_since_merge`` 与源库指纹闸是同一类，
       换入之后必然误报；``no_marker_left_unreconciled`` 读的是账本里的静态字段，
       第一次校验通过即蕴含「没有 marker 被拒」，续跑不可能凭空长出一条 refusal。
       反过来说：新增的阻断项**必须**加进 ``run_verify_command``（而不是 switch 自己的
       某段代码），否则它就只在某一条路径上生效。
    4. ``run_merge_command`` 见到已被消费的 staging 直接拒绝，堵死「重跑 merge」这条
       看似可行、实则会把 legacy 数据并两遍的退路。

    各阶段本身也都是幂等的：被 rename 走的 merged 仓目录不会再出现在 staging 里，
    被搬走的 run 目录不会再出现在 legacy ``runs/`` 里，所以即使崩在某个循环中间，
    重入时该循环看到的就是「剩下没做的那些」。

    回滚是 forward-only：切换后不能简单退回旧代码（那样又开始写 legacy 根）。
    失败要从 staging 的 ``replaced-canonical-tracker-repositories`` 与归档树恢复。
    """

    # 授权闸排在校验闸**前面**：排练产物即使内容完全合法也不该被切换消费，先答
    # 「这份 staging 有没有资格」再答「它对不对」。反过来排，拒绝理由会变成
    # ``verification_failed``，把真正的原因埋掉。
    ledger = load_merge_ledger(staging_root)
    if ledger is None:
        return 2, {
            "command": "switch",
            "status": "refused",
            "reason": "merge_ledger_absent",
            "detail": str(staging_root / MERGE_LEDGER_FILE_NAME),
        }
    if ledger.get("staging_was_produced_without_quiescence_gates"):
        return 2, {
            "command": "switch",
            "status": "refused",
            "reason": "staging_was_produced_without_quiescence_gates",
            "detail": (
                "这份 staging 是用 --rehearse-without-quiescence-gates 排练出来的，"
                "只能用来读对账结果；正式切换必须在静默期重新跑一次 merge。"
            ),
        }

    journal: dict[str, Any] = load_switch_stage_journal(staging_root) or {}
    resumed_from_journal = bool(journal.get("destructive_phase_entered"))
    verification: dict[str, Any] | None = None
    if resumed_from_journal:
        mismatch = switch_stage_journal_mismatch(
            journal,
            legacy_root=legacy_root,
            canonical_root=canonical_root,
            archive_root=archive_root,
            ledger=ledger,
        )
        if mismatch is not None:
            reason, detail = mismatch
            return 2, {
                "command": "switch",
                "status": "refused",
                "reason": reason,
                "detail": detail,
                "switch_stage_journal_path": str(switch_stage_journal_path(staging_root)),
            }
    else:
        journal = {}

    completed_stages: list[str] = list(journal.get("completed_stages") or [])
    already_archived = SWITCH_STAGE_LEGACY_ROOT_ARCHIVED in completed_stages
    if already_archived:
        # 归档目标由账本裁决（上面的身份闸已确认两者一致），归档树自然「已存在」，
        # 不该再走一遍资格闸。
        archive_root = Path(str(journal.get("archive_root")))
    else:
        rejection = archive_root_rejection(
            archive_root=archive_root,
            legacy_root=legacy_root,
            canonical_root=canonical_root,
            staging_root=staging_root,
        )
        if rejection is not None:
            reason, detail = rejection
            return 2, {
                "command": "switch",
                "status": "refused",
                "reason": reason,
                "detail": detail,
            }

    if not resumed_from_journal:
        verify_exit_code, verification = run_verify_command(
            legacy_root=legacy_root, canonical_root=canonical_root, staging_root=staging_root
        )
        if verify_exit_code != 0:
            return 2, {
                "command": "switch",
                "status": "refused",
                "reason": "verification_failed",
                "verification": verification,
            }

    merged_state_root = staging_root / STAGING_MERGED_STATE_ROOT_DIRECTORY_NAME
    installed_tracker_repositories: list[str] = list(
        journal.get("installed_tracker_repositories") or []
    )
    replaced_backup_directories: list[str] = list(
        journal.get("replaced_tracker_backup_directories") or []
    )
    installed_file_counts: dict[str, int] = dict(
        journal.get("installed_file_counts_by_entry") or {}
    )
    moved_run_identifiers: list[str] = list(journal.get("moved_run_directory_identifiers") or [])
    skipped_run_identifiers: list[str] = list(
        journal.get("skipped_run_directory_identifiers") or []
    )
    archive_read_only_sealing: dict[str, Any] = dict(
        journal.get("archive_read_only_sealing") or {}
    )

    journal.update(
        {
            "migration_tool_contract_version": MIGRATION_TOOL_CONTRACT_VERSION,
            "command": "switch",
            "destructive_phase_entered": True,
            "started_at": journal.get("started_at") or utc_now_text(),
            "legacy_root": str(legacy_root),
            "canonical_root": str(canonical_root),
            "staging_root": str(staging_root),
            "archive_root": str(archive_root),
            "merge_ledger_generated_at": ledger.get("generated_at"),
            "verification_receipt_generated_at": (
                (verification or {}).get("generated_at")
                if verification is not None
                else journal.get("verification_receipt_generated_at")
            ),
            "completed_stages": completed_stages,
        }
    )

    def persist_journal(*, completed_stage: str | None = None, **updates: Any) -> None:
        journal.update(updates)
        if completed_stage is not None and completed_stage not in completed_stages:
            completed_stages.append(completed_stage)
        journal["completed_stages"] = list(completed_stages)
        journal["updated_at"] = utc_now_text()
        _write_marker_document(switch_stage_journal_path(staging_root), journal)

    # 破坏性阶段的第一笔写入是**账本本身**：这样「账本存在」严格蕴含「校验已通过」，
    # 而「账本不存在」严格蕴含「一个字节都还没动过」。
    persist_journal()

    if SWITCH_STAGE_TRACKER_REPOSITORIES_INSTALLED not in completed_stages:
        merged_repositories_root = merged_state_root / TRACKER_REPOSITORIES_SUBDIRECTORY
        # 被替换掉的 canonical 目录搬到 staging 下，**绝不**留在 ``diff-tracker/repos/``
        # 里：那个目录的每个子目录都会被 ``discover_tracker_repository_pairs`` 当成一个
        # repo_key，一个 ``<key>.replaced-by-...`` 备份会在下一次对账时变成一个凭空多出来
        # 的仓（还带着全套旧数据）。
        replaced_backup_root = staging_root / "replaced-canonical-tracker-repositories"
        if merged_repositories_root.is_dir():
            for merged_repository_directory in sorted(merged_repositories_root.iterdir()):
                if not merged_repository_directory.is_dir():
                    continue
                repository_key = merged_repository_directory.name
                target = canonical_root / TRACKER_REPOSITORIES_SUBDIRECTORY / repository_key
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    replaced_backup_root.mkdir(parents=True, exist_ok=True)
                    backup = replaced_backup_root / repository_key
                    if backup.exists():
                        shutil.rmtree(backup)
                    os.rename(target, backup)
                    replaced_backup_directories.append(str(backup))
                os.rename(merged_repository_directory, target)
                installed_tracker_repositories.append(repository_key)
                # 逐仓落账：崩在循环中间时，续跑要能说清「已经换入了哪些」。
                persist_journal(
                    installed_tracker_repositories=list(installed_tracker_repositories),
                    replaced_tracker_backup_directories=list(replaced_backup_directories),
                )
        persist_journal(completed_stage=SWITCH_STAGE_TRACKER_REPOSITORIES_INSTALLED)

    if SWITCH_STAGE_STATE_ROOT_ENTRIES_INSTALLED not in completed_stages:
        for entry in sorted(merged_state_root.iterdir()):
            if entry.name == TRACKER_REPOSITORIES_SUBDIRECTORY.parts[0]:
                continue
            if entry.is_file():
                os.replace(entry, canonical_root / entry.name)
                installed_file_counts[entry.name] = 1
                persist_journal(installed_file_counts_by_entry=dict(installed_file_counts))
                continue
            destination_directory = canonical_root / entry.name
            destination_directory.mkdir(parents=True, exist_ok=True)
            count = installed_file_counts.get(entry.name, 0)
            for child in sorted(entry.iterdir()):
                if not child.is_file():
                    continue
                os.replace(child, destination_directory / child.name)
                count += 1
            installed_file_counts[entry.name] = count
            persist_journal(installed_file_counts_by_entry=dict(installed_file_counts))
        persist_journal(completed_stage=SWITCH_STAGE_STATE_ROOT_ENTRIES_INSTALLED)

    if SWITCH_STAGE_RUN_DIRECTORIES_MOVED not in completed_stages:
        legacy_runs_directory = legacy_root / RUNS_SUBDIRECTORY
        canonical_runs_directory = canonical_root / RUNS_SUBDIRECTORY
        if legacy_runs_directory.is_dir():
            canonical_runs_directory.mkdir(parents=True, exist_ok=True)
            for run_directory in sorted(legacy_runs_directory.iterdir()):
                if not run_directory.is_dir():
                    continue
                target = canonical_runs_directory / run_directory.name
                if target.exists():
                    # 源还在、目标也在 ⇒ 是真撞名（merge 时的 verify 已经排除过撞名，
                    # 所以这只能是 merge 之后新出现的），不是自己上一轮搬过的。
                    skipped_run_identifiers.append(run_directory.name)
                    continue
                os.rename(run_directory, target)
                moved_run_identifiers.append(run_directory.name)
        persist_journal(
            completed_stage=SWITCH_STAGE_RUN_DIRECTORIES_MOVED,
            moved_run_directory_identifiers=list(moved_run_identifiers),
            skipped_run_directory_identifiers=list(skipped_run_identifiers),
        )

    if SWITCH_STAGE_LEGACY_ROOT_ORIGIN_STAMPED not in completed_stages:
        _write_marker_document(
            legacy_root / LEGACY_ROOT_ORIGIN_STAMP_FILE_NAME,
            {
                "migrated_from_absolute_path": str(legacy_root),
                "migrated_into_absolute_path": str(canonical_root),
                "migrated_at": utc_now_text(),
                "migration_tool_contract_version": MIGRATION_TOOL_CONTRACT_VERSION,
                "merge_ledger_generated_at": ledger.get("generated_at"),
            },
        )
        persist_journal(completed_stage=SWITCH_STAGE_LEGACY_ROOT_ORIGIN_STAMPED)

    if SWITCH_STAGE_LEGACY_ROOT_ARCHIVED not in completed_stages:
        os.rename(legacy_root, archive_root)
        persist_journal(completed_stage=SWITCH_STAGE_LEGACY_ROOT_ARCHIVED)

    if SWITCH_STAGE_LEGACY_ROOT_ARCHIVE_SEALED_READ_ONLY not in completed_stages:
        archive_read_only_sealing = seal_directory_tree_read_only(archive_root)
        persist_journal(
            completed_stage=SWITCH_STAGE_LEGACY_ROOT_ARCHIVE_SEALED_READ_ONLY,
            archive_read_only_sealing=archive_read_only_sealing,
        )

    if SWITCH_STAGE_LEGACY_ROOT_SENTINEL_SEALED not in completed_stages:
        legacy_root.mkdir(parents=True, exist_ok=True)
        os.chmod(legacy_root, LEGACY_ROOT_SENTINEL_DIRECTORY_MODE)
        persist_journal(completed_stage=SWITCH_STAGE_LEGACY_ROOT_SENTINEL_SEALED)

    receipt = {
        "command": "switch",
        "status": "switched",
        "generated_at": utc_now_text(),
        "legacy_root": str(legacy_root),
        "legacy_root_archive": str(archive_root),
        "canonical_root": str(canonical_root),
        "staging_root": str(staging_root),
        "merge_ledger_generated_at": ledger.get("generated_at"),
        "resumed_from_switch_stage_journal": resumed_from_journal,
        "switch_stage_journal_path": str(switch_stage_journal_path(staging_root)),
        "completed_stages": list(completed_stages),
        "installed_tracker_repositories": installed_tracker_repositories,
        "replaced_tracker_backup_directories": replaced_backup_directories,
        "installed_file_counts_by_entry": installed_file_counts,
        "moved_run_directory_count": len(moved_run_identifiers),
        "skipped_run_directory_identifiers": skipped_run_identifiers,
        "legacy_root_sentinel_mode": oct(LEGACY_ROOT_SENTINEL_DIRECTORY_MODE),
        "legacy_root_archive_read_only_sealing": archive_read_only_sealing,
    }
    _write_marker_document(canonical_root / SWITCH_RECEIPT_FILE_NAME, receipt)
    persist_journal(status="switched", switch_receipt_generated_at=receipt["generated_at"])
    return 0, receipt


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

    def add_staging_root_argument(target_parser: argparse.ArgumentParser) -> None:
        target_parser.add_argument(
            "--staging-root",
            type=Path,
            default=None,
            help=(
                "staging 目录（默认 canonical 根的兄弟目录 "
                "``rvf-state-root-merge-staging``）。必须与 canonical 根同卷、且不在其内部。"
            ),
        )

    merge_parser = subparsers.add_parser(
        "merge",
        help="在 staging 里合并（绝不原地改 canonical 根）；跑完先看 verify",
    )
    add_staging_root_argument(merge_parser)
    merge_parser.add_argument(
        "--rehearse-without-quiescence-gates",
        action="store_true",
        help=(
            "跳过静默期闸做一次排练。产出的 staging 会被打上标记，``switch`` 一律拒绝"
            "消费它——排练只能用来读对账结果，正式切换必须在静默期重跑 merge。"
        ),
    )

    verify_parser = subparsers.add_parser(
        "verify", help="校验 staging：FK / 状态 invariant / 路径引用 / 源库未被改动"
    )
    add_staging_root_argument(verify_parser)

    switch_parser = subparsers.add_parser(
        "switch",
        help=(
            "分阶段换入 canonical 根、搬走 runs、把 legacy 根改名并封成只读归档；"
            "中断后用同一份 staging 重跑即从阶段账本续跑"
        ),
    )
    add_staging_root_argument(switch_parser)
    switch_parser.add_argument(
        "--archive-root",
        type=Path,
        default=None,
        help=(
            "legacy 根改名后的归档路径（默认 ``<legacy 根>.migrated-<UTC 日期>``）。"
            "必须与 legacy 根同卷（跨卷 rename 会 EXDEV），且不得落在 legacy / "
            "canonical / staging 任一根内部——这道闸排在所有破坏性操作之前。"
            "归档树整棵会被封成只读（目录 0o500 / 文件 0o400）；原位置另留一个 0o500 "
            "的空哨兵目录，让任何仍写 legacy 根的遗漏路径硬失败。"
            "续跑时归档目标以阶段账本记录的为准。"
        ),
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

    staging_root: Path | None = None
    if command in {"merge", "verify", "switch"}:
        staging_root = (
            arguments.staging_root.expanduser().resolve()
            if getattr(arguments, "staging_root", None) is not None
            else default_staging_root_for(canonical_root)
        )
        if staging_root == canonical_root or canonical_root in staging_root.parents:
            parser.error(
                f"staging 目录不能落在 canonical 根内部：staging={staging_root} / "
                f"canonical={canonical_root}。它会被顶层条目登记闸当成未登记条目，"
                "也会被 runs 枚举与 tracker 配对重复计数。"
            )

    # 「legacy 根不存在」通常确实意味着无事可做——**除了**一种情况：上一次 switch 已经
    # 把 legacy 根改名成归档、却崩在后面的封只读 / 哨兵 / 回执上。这时 legacy 根当然
    # 不存在，但活儿没干完。少了这个例外，续跑会在这里静默 return 0，留下一个没封只读、
    # 没有哨兵、也没有回执的「看起来成功了」的迁移。
    resumable_switch_is_in_flight = (
        command == "switch"
        and staging_root is not None
        and switch_has_started_consuming_staging(staging_root)
    )
    if not legacy_root.is_dir() and not resumable_switch_is_in_flight:
        print(f"legacy 根不存在，无需合并: {legacy_root}")
        return 0

    if staging_root is not None:
        if command == "merge":
            exit_code, payload = run_merge_command(
                legacy_root=legacy_root,
                canonical_root=canonical_root,
                staging_root=staging_root,
                conflict_policy=arguments.highwater_conflict_policy,
                canonical_root_provenance_description=(
                    canonical_root_resolution.resolution_provenance_description
                ),
                rehearse_without_quiescence_gates=(
                    arguments.rehearse_without_quiescence_gates
                ),
            )
        elif command == "verify":
            exit_code, payload = run_verify_command(
                legacy_root=legacy_root,
                canonical_root=canonical_root,
                staging_root=staging_root,
            )
        else:
            recorded_archive_root = (load_switch_stage_journal(staging_root) or {}).get(
                "archive_root"
            )
            if getattr(arguments, "archive_root", None) is not None:
                archive_root = arguments.archive_root.expanduser().resolve()
            elif recorded_archive_root:
                # 续跑时归档目标以阶段账本为准。默认值里带 UTC 日期，隔天续跑会算出
                # 另一个路径，撞上账本身份闸被拒——那不是真的不一致，只是默认值漂了。
                # 显式传了 --archive-root 则一律照传的用（对不上就该被身份闸拒绝）。
                archive_root = Path(str(recorded_archive_root))
            else:
                archive_root = legacy_root.with_name(
                    f"{legacy_root.name}.migrated-"
                    f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
                )
            exit_code, payload = run_switch_command(
                legacy_root=legacy_root,
                canonical_root=canonical_root,
                staging_root=staging_root,
                archive_root=archive_root,
            )
        print(render_migration_command_result_text(payload))
        return exit_code

    if command != "report":
        parser.error(f"未实现的子命令: {command}")

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
