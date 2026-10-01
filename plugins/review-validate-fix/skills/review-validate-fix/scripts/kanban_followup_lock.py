#!/usr/bin/env python3
"""三族 task/session 维度的 ~/.rvf marker。**名字已经名实不符，读之前先看清这一节。**

本模块承载三个**语义不同**的 marker 族：

1. **in-progress 锁**（``SUBDIR_NAME`` = ``kanban-followup-in-progress``，``marker_paths`` /
   ``write_marker`` / ``read_marker`` / ``clear_marker`` / ``acquire_marker`` 那一族）——
   P1 之后它**不再是 kanban-followup 专属**，而是「**任何**已被 agent 接受、仍在途的 RVF
   run」的抑制凭证：既由 UPS 在 kanban 终端投递落地时 arm（``dispatch_token``），也由
   ``rvf-agent start`` 在 agent 于发起决策点上就地接受时 arm（``decision_token``）。
2. **pending 派发记录**（``PENDING_SUBDIR_NAME`` = ``kanban-followup-dispatched``，
   ``*_pending_marker`` 那一族）—— 这一族仍然**确实**是 kanban 终端投递专属的
   「已派发、未确认落地」记账。
3. **决策点横幅的带外投递意图**（``DECISION_POINT_DELIVERY_INTENT_SUBDIR_NAME`` =
   ``kanban-decision-point-delivery-intent``，``*_decision_point_delivery_intent`` 那一族）——
   记的是「一次**尚未被接受**的发起征询已经交给 cline-kanban 终端投递」。与 (2) 的分别是本质的：
   (2) 背后有 run / lease / prep 要善后，(3) 背后**只有一枚 decision token**（见计划 D0），
   两者的撤回动作完全不同，合用一个文件只会让「撤回」语义分叉。

所以模块名 ``kanban_followup_lock`` 对 (1) 已经偏窄、对 (2)(3) 仍然准确。**没有顺手整体改名**
是刻意的：一次性把模块改成 ``accepted_review_run_in_flight_lock`` 只会让 (2) 变成新的名实不符
——正确的收敛是先把两族拆成两个模块、再各自命名，那是独立一刀，不该混进本轮的行为改动里。

磁盘目录名 ``~/.rvf/kanban-followup-in-progress`` 同理**刻意保持不变**：``marker_paths()`` 是
它唯一的构造点，改目录名等于一次活状态迁移，收益只有「好看」，风险却是真的。
"""

from __future__ import annotations

import json
import os
import secrets
import fcntl
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from core.run_ledger.run_ledger import safe_token  # noqa: E402
from core.session_scope_allocation.scope_marker_identity_key import (  # noqa: E402
    SESSION_KEYED_MARKER_FILE_NAME_PREFIX,
    TASK_KEYED_MARKER_FILE_NAME_PREFIX,
    resolve_repository_identity_for_scope_marker_key,
    scope_marker_file_name,
)


SUBDIR_NAME = "kanban-followup-in-progress"
MARKER_VERSION = 1
# 锁的合法寿命 = agent 跑完**一整轮 RVF**（发起 → review → validate/fix → handoff）的时间。
#
# ⚠️ 这把锁**早已不是 kanban follow-up 专属**：``0aac3ba`` 起它是「任何在途 RVF run」的通用
# 在途凭证（模块 docstring 有边界说明；名字未改是因为同名根下还住着确实属 kanban 的 pending
# 派发记录，整体改名会让后者变成新的名实不符）。旧注释按「一轮 follow-up，实测最长约 47min」
# 标定 1h，泛化之后没跟着改，于是 TTL 短于它要保护的事件——这是结构性失效，不是偶发。
#
# 实测（30 个 agentic-initiation-start run）：p50 = 84.3min，**53% 超过 60min**。近 4 天落盘的
# 7 次 ``kanban_followup_in_progress_stale`` 清锁后面全都紧跟一次重铸+decline（间隔
# 63.9 / 68.2 / 73.6min），而同期这把锁**成功**抑制只有 6 次——失效比成功还多。
#
# 取 2h 而不是照抄某个分位数：真实分布是重尾的（p90 被卡死 run 污染到 ~20h），纯延长永远追不上。
# TTL 在本轮之后只承担一个**收窄**的职责：「**没有存活证据时**，多久判这把锁已死」。正常长 review
# 由心跳续期覆盖（``revive_marker`` + ``REVIVAL_ABSOLUTE_CAP_SECONDS`` 硬顶），2h 只是心跳证据
# 不可用时的兜底，同时把卡死锁的最坏冻结窗口控制在 2h。需要更长可经 TTL_ENV 覆盖。
DEFAULT_TTL_SECONDS = 2 * 60 * 60
TTL_ENV = "RVF_KANBAN_FOLLOWUP_IN_PROGRESS_TTL_SECONDS"
LOCK_ROOT_ENV = "RVF_KANBAN_FOLLOWUP_LOCK_ROOT"
# 三族 marker 的落盘根解析链：显式 ``root`` > ``LOCK_ROOT_ENV`` > ``$RVF_HOME`` > ``~/.rvf``——与
# token store / 注入尝试 ledger / withdrawn tombstone 等 RVF_HOME 家族成员同一口径。此前这里跳过了
# ``$RVF_HOME`` 一档：测试把 RVF_HOME 指向 tmp 以为已经隔离，锁照样写进真实 ``~/.rvf``（F5·H5b：
# ``kanban-followup-in-progress/`` 872 个 marker 里 854 个是测试残留）。生产环境不设 RVF_HOME，行为不变。
RVF_HOME_ENV = "RVF_HOME"
STATUS_ACTIVE = "active"
STATUS_STALE = "stale"
STATUS_INVALID = "invalid"

# marker 的 ``state``：区分「已在跑 review」与「还卡在 pre-review local-base bootstrap 门前」。
# 历史上只有一个笼统的 in_progress，于是读侧对一个**尚未开始 review**、只是在等 base 吸收的
# agent 发出「继续收尾、输出 handoff」的错误提示；1h TTL 到期后又直接清锁回落常规 gate、重开
# 一轮（32cad 家族）。拆成两个 state 后，等待态才能被读侧识别、复活并给出定向指令。
STATE_IN_PROGRESS = "in_progress"
STATE_WAITING_BOOTSTRAP = "waiting_bootstrap"

# 上述缺陷族的**第二个实例**：UPS 在「prompt 被提交」那一刻 arm 锁，但提交 ≠ 落地成回合 ≠
# agent 接手。注入的 follow-up trigger 可能被同回合的中断冲掉（enqueue→dequeue，从未成为
# transcript 条目），或落地后被用户 rewind 掉；而锁一旦是 in_progress，读侧就会对一个**从未
# 看到过这条 prompt** 的 agent 发「继续收尾这一轮、输出 handoff」的 re-engage nudge，把它拽去
# 跑一次它从未接受的 RVF（实测 run rvf-20260808T090747Z-stop-hook-ea36796d）。
#
# 故投递落地时先落**这一态**：它仍满足互斥去重（marker 在场 → 不 mint 新 run），但读侧
# **不发** re-engage nudge；只有确认 trigger 真的在当前 transcript 祖先链上（= agent 这一轮真
# 收到了它）才 promote 成 in_progress。该态另用一条**更短**的独立 TTL（默认 600s）：到期即清
# 锁交回常规 gate 按**当时**工作树重新决策，绝不复活一条早已被会话走远抛下的旧派发。
STATE_DELIVERED_AWAITING_AGENT_DECISION = "delivered_awaiting_agent_decision"

# awaiting 态专属 TTL。刻意短于 in-progress 的 1h：这段窗口只覆盖「trigger 已投递 → agent 在
# 下一回合看到它」的正常延迟（秒~分钟级），不覆盖「agent 跑完一轮 review」。留长了就退化成
# 今天的缺陷——一把无人认领的锁把后续 Stop 静默挡住。
DEFAULT_AWAITING_AGENT_DECISION_TTL_SECONDS = 600
AWAITING_AGENT_DECISION_TTL_ENV = "RVF_KANBAN_FOLLOWUP_AWAITING_DECISION_TTL_SECONDS"

# ---------------------------------------------------------------------------
# 心跳续期的两个边界：多久算「还活着」，以及**最多**能续到什么时候
# ---------------------------------------------------------------------------
#
# 光把 TTL 调长追不上真实分布（重尾，p90 被卡死 run 污染到 ~20h），而调得越长，一把**真的**
# 卡死的锁就把后续 Stop 静默挡得越久。正解是把「猜一个够长的常数」换成「看它还活不活着」：
#
#   * 还活着（近期有 run 活动）  → 就地续期，锁跟着这一轮走，多长都行；
#   * 不活了                     → 按 TTL 判死、清锁，交回常规 gate。
#
# **存活证据刻意复用 run 目录的既有写入活动，不新造心跳机制**：一轮还在跑的 RVF 会在每个阶段
# 边界往自己的 run 目录里落工件（ledger 事件、reviewer 输出、fix attempt 结果、handoff）。
#
# ⚠️ **但「run 目录的 mtime 是一条覆盖整轮的心跳」是错的，别再照抄这句。** 那句断言写在一个只扫
# 「run 根 + 直接子项」的实现旁边，而对 215 个真实已完成 run（20min~6h）做重放实测：
#
#   * 只扫 run 根 + 直接子项 ⇒ **51.6% 的长 run** 至少有一个时刻会被判「已死」，
#     **46.2% 的在途时长是盲的**；每轮最大盲窗 p50 = 15.2min、p90 = 48.8min、最长 185.6min。
#
# 也就是说：靠它续期基本等于掷硬币。盲窗的成因**不是** external reviewer 等待段——那一段恰恰是
# 覆盖最好的，因为 reviewer 跑命令会持续往 ledger 落 ``lock_wait_started`` / ``lock_acquired``
# / ``lock_released``，而 ``events.jsonl`` 本来就是 run 根的直接子项。真正的盲窗有两类：
#
#   甲、**深处有写、浅扫看不见**：validate/fix 段只写 ``artifacts/fix-attempts/<attempt>/…``、
#       reviewer 段只写 ``artifacts/reviewers/<id>/…``——都在 ``artifacts/`` 之**下**两三层，而
#       目录的 mtime 只被**直接子项**的创建/rename 顶起，故 ``artifacts/`` 自己的 mtime 对它们
#       完全无感。这一类由下面的**阶段锚点**堵上。
#   乙、**整棵 run 树都没人写**：主 agent 在**仓库工作树**里读 review 结果、改代码、跑测试的那
#       几十分钟，run 目录一个字节都不动（实测单个盲窗 26.3 / 32.3 / 37.5 / 79.8min，全树 rglob
#       核对过窗内写入路径数 = 0）。这一类**任何扫描深度都探不到**，只能靠把窗口放宽到足以覆盖
#       真实的阶段间隔来吸收。
#
# 顺带纠正同一段旧注释里的另一句：``.dispatch-thread.status.json`` 并不是「只覆盖 external
# reviewer 派发那一段所以不值得看」——它就在 ``artifacts/reviewers/`` 下，本轮已随阶段锚点一并
# 纳入观察面；它的真实局限只是「不覆盖 validate/fix 段」，而那一段另有自己的锚点。
#
# 窗口取值同样按实测标定，而不是拍一个「感觉够长」的数。**加上阶段锚点之后**对同一批 215 个 run
# 重放：
#
#   窗口     会被误判「已死」的 run      在途盲时长占比
#   15min        74/215 (34.4%)            32.2%     ← 旧默认
#   30min        34/215 (15.8%)            19.9%
#   45min        17/215 ( 7.9%)            14.1%
#   60min        11/215 ( 5.1%)            10.4%     ← 新默认，≈ 每轮最大盲窗的 p95（58.3min）
#   90min         5/215 ( 2.3%)             6.6%
#
# 取 60min：把主因修复从「掷硬币」（46.2% 盲）拉到 10.4% 盲，而再翻一倍只多买 3.8 个百分点。
# 放宽窗口的代价是**有界**的：一把真的死掉的锁最多多活「窗口 + 一个 TTL」，且整体仍被
# ``REVIVAL_ABSOLUTE_CAP_SECONDS``（自 ``armed_at`` 起算 6h）压住——放宽窗口不改变最坏冻结时长。
RUN_ACTIVITY_LIVENESS_WINDOW_SECONDS = 60 * 60
RUN_ACTIVITY_LIVENESS_WINDOW_ENV = "RVF_IN_FLIGHT_RUN_ACTIVITY_LIVENESS_WINDOW_SECONDS"

# 活动探针在 run 根与其直接子项之外，再显式列举 ``artifacts/`` 下这几个「一轮 RVF 各阶段真正在
# 写」的子树根，并只向下走**两层**（子树根的直接子项 + 它们的直接子项）。
#
# 为什么是「列举 + 定深」而不是 rglob 整棵树：一个 attempt 目录里挂着一整棵 git worktree（实测
# 单个 run 树 6 万+ 路径），而 Stop hook **每回合**都要跑这一段。列举让代价只与「reviewer 数 ×
# issue 数 × 常数」成正比、与被审仓库的大小无关；实测 stat 次数 p50 = 36、p90 = 65、最大 157。
#
# 定深两层刚好停在 ``fix-attempts/<attempt>/worktree`` 这个**目录本身**上：它作为叶子被 stat
# （能捕捉到「worktree 刚被创建 / 顶层条目被增删」），但**绝不进去**——进去就退化成深扫。
RUN_ACTIVITY_PROBE_ARTIFACT_PHASE_ANCHOR_SUBDIRECTORY_NAMES = (
    "reviewers",  # external reviewer 派发段：.dispatch-thread.log / <id>/reviewer.stdout.txt
    "fix-attempts",  # validate/fix 段：<attempt>/attempt.json / result.json / fix.patch
    "fix-issues",  # issue upsert 段：RVF-00N.json
)
RUN_ACTIVITY_PROBE_ARTIFACT_PHASE_ANCHOR_MAX_DEPTH_BELOW_SUBDIRECTORY = 2
# 代价的**结构性**上界，而不只是「实测下来不大」。按实测最大 157 次留 3 倍余量；真撞上界时探针
# 只会退化回更浅的观察面 = 更容易判「已死」= 失败方向仍是「多问一次」，绝不会变成静默续命。
RUN_ACTIVITY_PROBE_ANCHOR_STAT_BUDGET = 512

# **绝对硬顶：与被等对象无关的上界。** 这是 F2a 立下的判据——「任何 wait 分支都必须有一个与被等
# 对象无关的上界」。没有它，一个持续写日志却永远不收尾的 run（活锁、卡在重试循环、reviewer
# 一直吐输出）会把锁**无限**续下去，把「TTL 太短导致多问」换成一个远更危险的「永久漏审」。
#
# 从 ``armed_at``（这一轮最初 arm 的时刻）起算，**不随续期前移**——续的是同一轮，不是新一轮。
# 6h 取自：实测最长的正常一轮远在其下，而超过 6h 仍未 handoff 的一轮，判它已死几乎不会错。
#
# 它要成立需要**两道**判定，缺一道就只是个建议值：续期之前判「还能不能续」
# （``marker_exceeds_revival_absolute_cap``），续期之时判「最多能续到哪」
# （``revive_marker(truncate_expiry_to_revival_absolute_cap=True)``）。只有前者时，一把在
# ``cap - 1min`` 处续上的锁会被推到 ``now + TTL``、一路 active 到 ``armed + cap + TTL``——读侧的
# ACTIVE 分支不复查硬顶，于是硬顶被整整绕过一个 TTL。
REVIVAL_ABSOLUTE_CAP_SECONDS = 6 * 60 * 60
REVIVAL_ABSOLUTE_CAP_ENV = "RVF_IN_FLIGHT_LOCK_REVIVAL_ABSOLUTE_CAP_SECONDS"

# bootstrap receipt 校验通过后那次 prepare 重放的**持久占位**。它是入口 B 专属的进程外去重
# 记账，不属于 gate 的白名单身份字段，故不落进 gate；也**刻意不落进 marker payload**：marker
# 的每一个写侧（``stamp_bootstrap_gate`` / ``bump_*`` / ``revive_marker``）都是「读-改-写整份
# payload」，两个并发 Stop 各持一份陈旧副本时，后写的一次会把前一次刚盖上的在途字段整份抹掉
# ——占位于是失去原子性，两个进程各起一次 prepare 重放（同一 prep / 同一 run 目录被并发重复
# 写）。改为 marker 旁的独立 sidecar 文件 + ``O_EXCL`` 抢占：这是「起 replay 之前先原子 claim」
# 的最小原语，不受任何 marker 整份重写影响。文件名以 ``.`` 开头，故不会被锁根的 ``*.json``
# 扫描（marker 枚举 / 测试断言）看成第二把锁。
BOOTSTRAP_REPLAY_CLAIM_SUFFIX = ".bootstrap-replay-claim"


@dataclass(frozen=True)
class AcquireResult:
    acquired: bool
    path: Path | None
    marker: dict[str, Any] | None
    status: str


def _rvf_home_directory() -> Path:
    raw = os.environ.get(RVF_HOME_ENV)
    if raw and raw.strip():
        return Path(raw).expanduser()
    return Path.home() / ".rvf"


def _root(root: Path | None = None) -> Path:
    if root is not None:
        return root.expanduser() / SUBDIR_NAME
    raw = os.environ.get(LOCK_ROOT_ENV)
    if raw and raw.strip():
        return Path(raw).expanduser()
    return _rvf_home_directory() / SUBDIR_NAME


def _task_path(task_id: str, repository_identity: str, root: Path | None = None) -> Path:
    return _root(root) / scope_marker_file_name(
        key_prefix=TASK_KEYED_MARKER_FILE_NAME_PREFIX,
        key_token=safe_token(task_id),
        repository_identity=repository_identity,
        parent_harness_identity=None,
    )


def _session_path(session_id: str, repository_identity: str, root: Path | None = None) -> Path:
    return _root(root) / scope_marker_file_name(
        key_prefix=SESSION_KEYED_MARKER_FILE_NAME_PREFIX,
        key_token=safe_token(session_id),
        repository_identity=repository_identity,
        parent_harness_identity=None,
    )


def marker_paths(
    *,
    task_id: str | None,
    session_id: str | None,
    repo: str | Path | None,
    root: Path | None = None,
) -> list[Path]:
    """本把锁的候选文件名：``task-<id>--repo-<identity>`` / ``sess-<id>--repo-<identity>``。

    **为什么 key 里必须有仓身份**：cline-kanban 的 task id 是短哈希前缀，不同 project 会撞
    （实测反例见 ``core.session_scope_allocation.scope_marker_identity_key`` 模块头）。这把锁
    自 P2 起不再只挡「kanban follow-up 投递」，而是「**任何已被 agent 接受、仍在途的 RVF
    run**」的抑制凭证——撞键就是 A 仓的在途 run 静默压掉 B 仓的发起决策点（= 漏审）。

    **刻意不按 harness 分键**（``parent_harness_identity=None``，与 round-baseline 相反）：
    它是「这个工作树里有没有一轮 RVF 在跑」的互斥锁，两个 harness 若真共用一个工作树，本就
    **应该**争同一把锁；按 harness 切开只会让两边各派一轮。不分键也顺带免掉
    ``--harness-unspecified`` 那层宽回退候选——对抑制性 marker 而言，读到别人的那一份就是
    静默漏审，方向正好是错的。

    ``repo`` 解析不出仓身份 ⇒ 返回空列表（fail-closed）。对这把锁而言这是安全方向：写者
    no-op（不落抑制凭证）、读者读不到（当作没有在途 run）⇒ 至多多征询一次，绝不静默漏审。
    仅 ``clear_*`` 会因此变成 no-op，但那只让一把锁按 TTL 自然过期，不会误挡到下一轮。
    """
    repository_identity = resolve_repository_identity_for_scope_marker_key(repo)
    if repository_identity is None:
        return []
    paths: list[Path] = []
    if task_id:
        paths.append(_task_path(task_id, repository_identity, root))
    if session_id:
        paths.append(_session_path(session_id, repository_identity, root))
    return paths


def ttl_seconds() -> float:
    raw = os.environ.get(TTL_ENV)
    if raw is None or not raw.strip():
        return float(DEFAULT_TTL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_TTL_SECONDS)
    return max(0.0, value)


def awaiting_agent_decision_ttl_seconds() -> float:
    """``delivered_awaiting_agent_decision`` 态专属 TTL（秒）。坏值一律回落默认。"""
    raw = os.environ.get(AWAITING_AGENT_DECISION_TTL_ENV)
    if raw is None or not raw.strip():
        return float(DEFAULT_AWAITING_AGENT_DECISION_TTL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_AWAITING_AGENT_DECISION_TTL_SECONDS)
    return max(0.0, value)


def ttl_seconds_for_state(state: str | None) -> float:
    """按 marker ``state`` 选 TTL——awaiting 态短、其余沿用 in-progress 的长 TTL。"""
    if state == STATE_DELIVERED_AWAITING_AGENT_DECISION:
        return awaiting_agent_decision_ttl_seconds()
    return ttl_seconds()


def run_activity_liveness_window_seconds() -> float:
    """「run 目录多久内有写入算还活着」的窗口（秒）。坏值一律回落默认。"""
    raw = os.environ.get(RUN_ACTIVITY_LIVENESS_WINDOW_ENV)
    if raw is None or not raw.strip():
        return float(RUN_ACTIVITY_LIVENESS_WINDOW_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(RUN_ACTIVITY_LIVENESS_WINDOW_SECONDS)
    return max(0.0, value)


def revival_absolute_cap_seconds() -> float:
    """从 ``armed_at`` 起算的续期绝对硬顶（秒）。坏值一律回落默认。"""
    raw = os.environ.get(REVIVAL_ABSOLUTE_CAP_ENV)
    if raw is None or not raw.strip():
        return float(REVIVAL_ABSOLUTE_CAP_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(REVIVAL_ABSOLUTE_CAP_SECONDS)
    return max(0.0, value)


def marker_state(marker: dict[str, Any] | None) -> str:
    """读出这把锁的 ``state``（缺省 / 坏值一律按 ``in_progress``，即历史语义）。"""
    if not isinstance(marker, dict):
        return STATE_IN_PROGRESS
    state = marker.get("state")
    if isinstance(state, str) and state.strip():
        return state.strip()
    return STATE_IN_PROGRESS


def _parse_iso_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _open_marker_exclusive(path: Path, encoded: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _takeover_lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.takeover.lock")


@contextmanager
def _with_takeover_lock(path: Path):
    lock_path = _takeover_lock_path(path)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def marker_payload(
    *,
    task_id: str | None,
    session_id: str | None,
    run_id: str,
    run_dir: str,
    repo: str | None,
    cwd: str | None,
    attempt_id: str | None = None,
    message_id: str | None = None,
    turn_id: str | None = None,
    prompt_path: str | None = None,
    dispatch_token: str | None = None,
    decision_token: str | None = None,
    delivered_session_id: str | None = None,
    delivered_transcript_path: str | None = None,
    state: str = STATE_IN_PROGRESS,
    bootstrap_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ttl = ttl_seconds_for_state(state)
    return {
        "marker_version": MARKER_VERSION,
        "state": state,
        "bootstrap_gate": bootstrap_gate,
        "armed_at": _iso_now(),
        "expires_at": _iso_after(ttl),
        "ttl_seconds": ttl,
        "kanban_task_id": task_id,
        "kanban_attempt_id": attempt_id,
        "session_id": session_id,
        "run_id": run_id,
        "run_dir": run_dir,
        "repo": repo,
        "cwd": cwd,
        "message_id": message_id,
        "turn_id": turn_id,
        "prompt_path": prompt_path,
        # 派发这一轮 follow-up 的 dispatch token（注入 prompt 里字面出现为
        # ``RVF_DISPATCH=token=<16 hex>``）。读侧据它在 transcript 里定位「这条 trigger 究竟
        # 有没有成为一个真实回合、又是否仍在当前祖先链上」——即 promote / rewind 对账的锚点。
        # 历史 payload 三个 id 字段全是 None，锁自己都不知道自己对应哪个回合。
        "dispatch_token": dispatch_token,
        # 「agent 在**发起决策点**上显式接受了这一轮」时由 rvf-agent CLI 落的 S3 决策 token
        # （与 ``dispatch_token`` 互斥：前者是 agent 自己 start 出来的就地 run，后者是 kanban
        # 终端投递那条 trigger）。读侧的祖先链对账据它在 transcript 里找横幅那条
        # ``RVF_DECISION_TOKEN=<token>``，于是「接受被 rewind 掉」与「trigger 被 rewind 掉」
        # 共用同一套 Fix B 对账，不必新造一套 rewind 机制。
        "decision_token": decision_token,
        # trigger 实际落地到哪个会话 / 哪份 transcript（UPS 那一刻的权威身份）。读侧优先按
        # 这份 transcript 做祖先链对账；缺失时才退回「本次 Stop 的 transcript，且仅当会话相同」。
        "delivered_session_id": delivered_session_id,
        "delivered_transcript_path": delivered_transcript_path,
    }


def write_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    run_id: str,
    run_dir: str,
    repo: str | None,
    cwd: str | None,
    attempt_id: str | None = None,
    message_id: str | None = None,
    turn_id: str | None = None,
    prompt_path: str | None = None,
    dispatch_token: str | None = None,
    decision_token: str | None = None,
    delivered_session_id: str | None = None,
    delivered_transcript_path: str | None = None,
    state: str = STATE_IN_PROGRESS,
    root: Path | None = None,
) -> Path | None:
    # 仓身份锚点用 ``repo or cwd``：``repo`` 是 payload 的权威字段，但历史 / 手动路径上它可能
    # 缺失而 ``cwd`` 仍在。身份由 ``git_common_dir`` 归一，两者落在同一个仓里就解析成同一个
    # identity，故这条回退只增加可解析率、不会把 key 指向别的仓。
    paths = marker_paths(task_id=task_id, session_id=session_id, repo=repo or cwd, root=root)
    if not paths:
        return None
    payload = marker_payload(
        task_id=task_id,
        session_id=session_id,
        run_id=run_id,
        run_dir=run_dir,
        repo=repo,
        cwd=cwd,
        attempt_id=attempt_id,
        message_id=message_id,
        turn_id=turn_id,
        prompt_path=prompt_path,
        dispatch_token=dispatch_token,
        decision_token=decision_token,
        delivered_session_id=delivered_session_id,
        delivered_transcript_path=delivered_transcript_path,
        state=state,
    )
    target = paths[0]
    # Option A（兑现 nudge 预算的 loop-break 契约）：本函数是 in-progress 锁唯一的 arm 入口
    # （arm_kanban_followup_lock_on_delivery 投递确认时调用），整份覆盖 marker。重派发的
    # delivery re-arm 若不保留既有 reengage_nudge_count，会把『预算用尽再退回静默 skip 防
    # review↔fix 死循环』的计数每轮清回 0（见 stop_review_validate_fix.py 的 nudge 分支）。
    # 故在覆盖前读出同 task 既有 marker 的计数并带入新 payload：首次 arm（无既有 marker）自然从
    # 0 起，re-arm 则让计数跨投递存活。best-effort：read_marker 吞读错、reengage_nudge_count
    # 坏值按 0（保守：宁可多给一次 nudge，也不因坏值误判预算耗尽而静默挡停）。
    carried_nudge_count = reengage_nudge_count(
        read_marker(task_id=task_id, session_id=session_id, repo=repo or cwd, root=root)
    )
    if carried_nudge_count:
        payload["reengage_nudge_count"] = carried_nudge_count
    # 新一轮 arm：上一轮遗留的重放占位（若有）必须清掉，否则它会在窗口内挡掉本轮的第一次重放。
    try:
        _bootstrap_replay_claim_path_for(target).unlink()
    except (FileNotFoundError, OSError):
        pass
    _atomic_write(target, payload)
    return target


def stamp_bootstrap_gate(
    marker: dict[str, Any],
    *,
    gate: dict[str, Any] | None,
    state: str | None = None,
) -> bool:
    """读-改-写：把 pre-review bootstrap 等待 gate 盖进**这把**锁（``gate=None`` 即摘除）。

    形状照抄 ``bump_reengage_nudge_count`` / ``stamp_pending_notified``：直接写
    ``marker['_marker_path']``（``read_marker`` 注入的实际命中文件），不重新解析 marker_paths，
    以免写到另一条路径上、也不会把同轮已累计的 ``reengage_nudge_count`` 冲掉。
    best-effort：无 ``_marker_path`` 或写失败返回 False，绝不抛——锁记账永不阻断 Stop / UPS。
    """
    raw_path = marker.get("_marker_path")
    if not isinstance(raw_path, str) or not raw_path:
        return False
    payload = {key: value for key, value in marker.items() if key != "_marker_path"}
    payload["bootstrap_gate"] = dict(gate) if gate else None
    if gate is None:
        # 摘 gate = 这一轮的 bootstrap 等待已收尾。定向 re-engage 预算随之归零，免得日后同一把锁
        # 再进等待态时，一开局就带着上一轮的计数被误判为「提示已给够」。在途重放占位同理：留着它
        # 会让下一轮等待一开局就以为「上一次 replay 还在跑」而白白跳过一次重放。
        payload.pop("bootstrap_reengage_count", None)
    if state is not None:
        payload["state"] = state
    try:
        _atomic_write(Path(raw_path), payload)
    except OSError:
        return False
    if gate is None:
        marker.pop("bootstrap_reengage_count", None)
        release_bootstrap_replay_in_flight(marker)
    marker.update(payload)
    return True


def _bootstrap_replay_claim_path_for(marker_path: Path) -> Path:
    return marker_path.with_name(f".{marker_path.name}{BOOTSTRAP_REPLAY_CLAIM_SUFFIX}")


def _bootstrap_replay_claim_path(marker: dict[str, Any] | None) -> Path | None:
    """这把锁对应的 claim sidecar 路径（无 ``_marker_path`` 时 None）。"""
    raw_path = marker.get("_marker_path") if isinstance(marker, dict) else None
    if not isinstance(raw_path, str) or not raw_path:
        return None
    return _bootstrap_replay_claim_path_for(Path(raw_path))


def _bootstrap_replay_claim_age(claim_path: Path) -> float | None:
    """claim 已持有多久（秒）。文件不在 / 内容坏了一律 None = 「没有可信的在途占位」。"""
    try:
        payload = json.loads(claim_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    claimed = _parse_iso_ts(payload.get("claimed_at"))
    if claimed is None:
        return None
    return max(0.0, datetime.now(timezone.utc).timestamp() - claimed)


def bootstrap_replay_in_flight_age_seconds(marker: dict[str, Any] | None) -> float | None:
    """在途重放占位已存在多久（秒）。无占位 / 坏值一律 None = 「没有在途重放」（可以起新的）。

    只交出「多久以前」，**替换窗口多长是调用方的策略**（Stop 侧按 env 可调），本模块只管存取。
    纯读侧诊断；真正决定「本轮能不能起 replay」的是 ``claim_bootstrap_replay_in_flight``。
    """
    claim_path = _bootstrap_replay_claim_path(marker)
    if claim_path is None:
        return None
    return _bootstrap_replay_claim_age(claim_path)


def claim_bootstrap_replay_in_flight(
    marker: dict[str, Any],
    *,
    window_seconds: float,
) -> tuple[bool, float | None]:
    """**起 prepare 重放之前**的原子占位，返回 ``(claimed, in_flight_age_seconds)``。

    ``claimed=True`` = 本进程抢到了这一轮重放，调用方可以起 replay；``claimed=False`` = 已有
    另一个进程在窗口内持有占位（``in_flight_age_seconds`` 是它的年龄），本轮**不得**再起第二次
    ——否则同一 prep / 同一 run_id 会被并发重复 prepare、同一个 run 目录被重复写。

    抢占用 ``O_EXCL`` 建 sidecar 文件（内核级互斥），**不是**「先读再写 marker」：后者两个并发
    Stop 会同时读到「无在途标记」并各自进入 replay（占位是事后写的），而且 marker 的整份
    读-改-写还会互相抹掉刚写下的字段。

    上界仍在：占位超过 ``window_seconds`` 即视为过期（worker 卡死 / 进程被杀），在 takeover
    flock 下重新校验后抢占，让本轮还能再试一次——一个卡死的 worker 不会永久堵死这把锁。

    best-effort：拿不到 claim 路径 / 文件系统报错一律放行（返回 ``True``），记账永不阻断 Stop。
    """
    claim_path = _bootstrap_replay_claim_path(marker)
    if claim_path is None:
        return True, None
    encoded = (
        json.dumps(
            {"claimed_at": _iso_now(), "claimed_by_pid": os.getpid()}, ensure_ascii=False
        )
        + "\n"
    )
    try:
        claim_path.parent.mkdir(parents=True, exist_ok=True)
        _open_marker_exclusive(claim_path, encoded)
        return True, None
    except FileExistsError:
        pass
    except OSError:
        return True, None

    age = _bootstrap_replay_claim_age(claim_path)
    if age is not None and age < window_seconds:
        return False, age
    # 占位已过窗口（或内容不可信）→ 允许抢占，但抢占本身也必须互斥：拿 takeover flock 后重新
    # 校验一次，避免两个进程同时判定「已过期」而双双接手。
    try:
        with _with_takeover_lock(claim_path):
            age = _bootstrap_replay_claim_age(claim_path)
            if age is not None and age < window_seconds:
                return False, age
            try:
                claim_path.unlink()
            except FileNotFoundError:
                pass
            try:
                _open_marker_exclusive(claim_path, encoded)
            except FileExistsError:
                return False, age
            return True, age
    except OSError:
        return True, age


def release_bootstrap_replay_in_flight(marker: dict[str, Any] | None) -> bool:
    """释放在途重放占位（重放已收尾 / 已确定失败，允许下一次 Stop 立刻再试）。绝不抛。"""
    claim_path = _bootstrap_replay_claim_path(marker)
    if claim_path is None:
        return False
    try:
        claim_path.unlink()
    except (FileNotFoundError, OSError):
        return False
    return True


def bootstrap_reengage_count(marker: dict[str, Any] | None) -> int:
    """读出这把锁上已累计的 bootstrap 定向 re-engage 次数（缺省 0，坏值按 0）。

    与 ``reengage_nudge_count`` 物理隔离：那把预算管的是「review 已在跑、催 agent 收尾」，
    这把管的是「review 还没开始、催 agent 去吸收 base」。两者混用会互相削预算。
    """
    if not isinstance(marker, dict):
        return 0
    try:
        return max(0, int(marker.get("bootstrap_reengage_count") or 0))
    except (TypeError, ValueError):
        return 0


def bump_bootstrap_reengage_count(marker: dict[str, Any]) -> int:
    """读-改-写：``bootstrap_reengage_count`` +1，原子写回其原文件，返回新计数。

    形状与 ``bump_reengage_nudge_count`` 一致（直接写 ``_marker_path``、best-effort 不抛），
    但**同时把新计数写回传入的 ``marker`` 字典**：等待态一次 Stop 里可能连着复活 + 计数两次
    落盘，若各自从同一份陈旧内存副本重写整份 payload，后写的一次会把先写的字段抹掉。
    """
    current = bootstrap_reengage_count(marker)
    raw_path = marker.get("_marker_path")
    if not isinstance(raw_path, str) or not raw_path:
        return current
    payload = {key: value for key, value in marker.items() if key != "_marker_path"}
    payload["bootstrap_reengage_count"] = current + 1
    try:
        _atomic_write(Path(raw_path), payload)
    except OSError:
        return current
    marker.update(payload)
    return current + 1


def revive_count(marker: dict[str, Any] | None) -> int:
    """读出这把锁已被就地复活过几次（缺省 0，坏值按 0）。"""
    if not isinstance(marker, dict):
        return 0
    try:
        return max(0, int(marker.get("revived_count") or 0))
    except (TypeError, ValueError):
        return 0


def run_activity_probe_anchor_paths(run_root: Path) -> list[Path]:
    """列出活动探针要 ``stat()`` 的锚点路径，**按层由浅入深**，总数受 stat 预算硬限。

    观察面按「一轮 RVF 各阶段实际往哪写」列举（见
    ``RUN_ACTIVITY_PROBE_ARTIFACT_PHASE_ANCHOR_SUBDIRECTORY_NAMES`` 上方那段实测），而不是
    rglob 整棵 run 树——attempt 目录里挂着整棵 git worktree，深扫的代价随被审仓库大小走，而这段
    代码 Stop hook 每回合都要跑。

    **按层展开而不是一路 DFS**，意义只在预算被打满时才显出来：截断永远先砍掉最深那一层，于是探针
    平滑退化回「run 根 + 直接子项」这个更浅的观察面，而不是随机地整段漏掉某一个阶段。
    """
    anchors: list[Path] = [run_root]

    def _take_within_budget(candidate_paths: list[Path]) -> list[Path]:
        remaining = RUN_ACTIVITY_PROBE_ANCHOR_STAT_BUDGET - len(anchors)
        if remaining <= 0:
            return []
        accepted = candidate_paths[:remaining]
        anchors.extend(accepted)
        return accepted

    def _direct_children_sorted(path: Path) -> list[Path]:
        # 排序只为让「预算截断时保留哪些」可复现，不表达任何优先级语义。
        try:
            return sorted(path.iterdir())
        except OSError:
            return []

    _take_within_budget(_direct_children_sorted(run_root))

    artifacts_root = run_root / "artifacts"
    frontier = _take_within_budget(
        [
            artifacts_root / subdirectory_name
            for subdirectory_name in RUN_ACTIVITY_PROBE_ARTIFACT_PHASE_ANCHOR_SUBDIRECTORY_NAMES
        ]
    )
    for _ in range(RUN_ACTIVITY_PROBE_ARTIFACT_PHASE_ANCHOR_MAX_DEPTH_BELOW_SUBDIRECTORY):
        next_frontier: list[Path] = []
        for parent in frontier:
            if not parent.is_dir():
                # 叶子（文件）与不存在的路径都在这里止步：``fix-attempts/<attempt>/worktree``
                # 是目录，但它落在最后一层、循环已经结束，故不会被走进去。
                continue
            next_frontier.extend(_take_within_budget(_direct_children_sorted(parent)))
        if not next_frontier:
            break
        frontier = next_frontier
    return anchors


def seconds_since_last_run_activity(marker: dict[str, Any] | None) -> float | None:
    """marker 指向的 run 目录距今多久没有过写入（秒）；无从判断一律 ``None``。

    ``None`` 与「很久没动」是**两件不同的事**，调用方必须分开处置：前者是「没有证据」，绝不能
    当成「还活着」——抑制性凭证的失败方向必须是「多问一次」，不是「静默续命」。

    取**锚点集合**（``run_activity_probe_anchor_paths``）里最新的 mtime。锚点 = run 根 + 其直接
    子项 + ``artifacts/`` 下各阶段子树根往下两层；刻意不深扫整棵树，理由与观察面的取法见
    ``RUN_ACTIVITY_LIVENESS_WINDOW_SECONDS`` 上方那段。

    **诚实边界**：本函数只看得见「写进 run 目录的活动」。主 agent 在**仓库工作树**里读结果、改
    代码、跑测试的那几十分钟，run 目录一个字节都不动，任何扫描深度都探不到——那一类盲窗靠
    ``RUN_ACTIVITY_LIVENESS_WINDOW_SECONDS`` 的窗口宽度吸收，不靠这里。
    """
    run_dir = marker.get("run_dir") if isinstance(marker, dict) else None
    if not isinstance(run_dir, str) or not run_dir.strip():
        return None
    root = Path(run_dir)
    try:
        if not root.is_dir():
            return None
    except OSError:
        return None
    newest: float | None = None
    for entry in run_activity_probe_anchor_paths(root):
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    if newest is None:
        return None
    return max(0.0, datetime.now(timezone.utc).timestamp() - newest)


def in_flight_run_shows_recent_activity(marker: dict[str, Any] | None) -> bool:
    """这一轮 RVF 近期是否有过写入活动（= 值得为它续期）。没有证据一律 ``False``。"""
    idle_seconds = seconds_since_last_run_activity(marker)
    if idle_seconds is None:
        return False
    return idle_seconds <= run_activity_liveness_window_seconds()


def seconds_since_marker_armed(marker: dict[str, Any] | None) -> float | None:
    """这把锁最初 arm 至今多久（秒）；``armed_at`` 缺失/坏值一律 ``None``。"""
    if not isinstance(marker, dict):
        return None
    armed_ts = _parse_iso_ts(marker.get("armed_at"))
    if armed_ts is None:
        return None
    return max(0.0, datetime.now(timezone.utc).timestamp() - armed_ts)


def revival_absolute_cap_remaining_seconds(marker: dict[str, Any] | None) -> float:
    """距「从 ``armed_at`` 起算」的续期绝对硬顶还剩多少秒（已越过 / 无从判断一律 ``0.0``）。

    ``armed_at`` 读不出来时返回 ``0.0``（**fail closed 到「一点余量都没有」**）：拿不出「这一轮
    开始于何时」的证据，就没有资格声称「还没到上界」——否则一份缺字段的旧 marker 会变成一把永不
    过期的锁，正是硬顶要防的那个形态。

    这是硬顶的**单一事实源**：``marker_exceeds_revival_absolute_cap``（能不能再续）与
    ``revive_marker`` 的续期截断（这一次最多能续到哪）都读它，免得两处各算一遍、口径漂移。
    """
    elapsed = seconds_since_marker_armed(marker)
    if elapsed is None:
        return 0.0
    return max(0.0, revival_absolute_cap_seconds() - elapsed)


def marker_exceeds_revival_absolute_cap(marker: dict[str, Any] | None) -> bool:
    """这把锁是否已越过「从 ``armed_at`` 起算」的续期绝对硬顶。"""
    return revival_absolute_cap_remaining_seconds(marker) <= 0.0


def revive_marker(
    marker: dict[str, Any],
    *,
    truncate_expiry_to_revival_absolute_cap: bool = False,
) -> bool:
    """把一把**已过 TTL 但本轮尚未完成**的锁就地续期为 active（参照 rvf_prep_file 的 FU-1 方案 B）。

    只重写 ``expires_at`` / ``ttl_seconds`` 并递增 ``revived_count``，``armed_at`` 与
    ``reengage_nudge_count`` / ``bootstrap_gate`` 原样保留——复活的是**同一轮**，不是新一轮。
    正因为 ``armed_at`` 不动，它才能同时充当续期的绝对硬顶锚点（见下）。

    **两个调用场景**（都是「这一轮还没完，但 TTL 到了」）：

      * bootstrap 等待期跨过锁 TTL——清锁会让读侧回落常规 gate 并重开一轮（32cad 家族），
        而这一轮其实只是还没等到 base 吸收完成；
      * review 本体跑得比 TTL 长——实测 p50 = 84.3min、53% 超过原先的 1h TTL，清锁后紧跟着
        就是一次重铸+decline。此支由 Stop 侧在确认 run 近期仍有活动
        （``in_flight_run_shows_recent_activity``）后才调用。

    **本函数刻意不自带上界**——上界属于调用方，因为两个场景的上界形态本就不同：

      * bootstrap 等待用**复活次数预算**（``KANBAN_FOLLOWUP_BOOTSTRAP_REVIVE_BUDGET``，预算
        耗尽即交还 stale 语义清锁）。注意其中「bootstrap 收尾时把锁续回 active」那一处**必须
        成功**，否则摘掉 gate 后锁仍是过期的，下一次 Stop 判 stale 清锁重开——正是 32cad。
        在本函数里塞一道墙钟硬顶会让那一处在长任务上静默失败，把已修好的回归放回来。
      * run 活动续期用**墙钟硬顶**（``marker_exceeds_revival_absolute_cap``，自 ``armed_at``
        起算），由 Stop 侧在调用前判。

    ``truncate_expiry_to_revival_absolute_cap``：给墙钟硬顶那一支用的**上界闭合开关**，默认关。
    「续期之前判一次硬顶」只挡得住**发起**续期，挡不住这一次续期能把锁推到多远——续期无条件把
    ``expires_at`` 推到 ``now + TTL``，而读侧的 ACTIVE 分支不复查硬顶，于是一把在 ``cap - 1min``
    处续上的锁一路 active 到 ``armed + cap + TTL``，硬顶被整整绕过一个 TTL（实测 6h 硬顶护到
    7.98h）。开关打开后 ``expires_at = min(now + TTL, armed_at + cap)``，把「最多续到哪」也钉死。

    刻意做成**显式 opt-in 而不是无条件**：bootstrap 等待那两个调用点的上界是复活次数预算，其中
    「收尾时把锁续回 active」那一处**必须成功**——在共享路径上塞墙钟硬顶会让它在长任务上静默
    截断出一把当场就过期的锁，下一次 Stop 判 stale 清锁重开，正是已修好的 32cad 回归。

    best-effort，绝不抛。

    同 ``bump_bootstrap_reengage_count``：续期字段**同时写回传入的 ``marker`` 字典**，使同一次
    Stop 里紧随其后的另一次整份重写不会把刚续的期抹回去。
    """
    raw_path = marker.get("_marker_path")
    if not isinstance(raw_path, str) or not raw_path:
        return False
    ttl = ttl_seconds_for_state(marker_state(marker))
    if truncate_expiry_to_revival_absolute_cap:
        # 截到硬顶为止。余量已被 fail-closed 成 0（``armed_at`` 不可读时），此时续出来的锁当场
        # 就是 stale——对本开关的调用方无害（它在调用前已按同一个硬顶拒绝了这类 marker），且方向
        # 正确：拿不出「这一轮开始于何时」的证据，就不该获得任何续命。
        ttl = min(ttl, revival_absolute_cap_remaining_seconds(marker))
    payload = {key: value for key, value in marker.items() if key != "_marker_path"}
    payload["expires_at"] = _iso_after(ttl)
    payload["ttl_seconds"] = ttl
    payload["revived_at"] = _iso_now()
    payload["revived_count"] = revive_count(marker) + 1
    try:
        _atomic_write(Path(raw_path), payload)
    except OSError:
        return False
    marker.update(payload)
    return True


def promote_marker_out_of_awaiting_agent_decision(
    marker: dict[str, Any], *, reason: str
) -> str | None:
    """把一把 ``delivered_awaiting_agent_decision`` 锁升格为「agent 已接手」态，返回新 state。

    目标态取决于这把锁上有没有 bootstrap gate：有 gate → ``waiting_bootstrap``（这一轮还卡在
    pre-review 的 local base 吸收门前），无 gate → ``in_progress``。这样 UPS 侧的
    ``stamp_bootstrap_gate`` 就不必在 awaiting 期间强行改写 state（改写会让「agent 到底收没收到
    这条 trigger」的信息丢失，正是本次要根治的那类混淆）。

    升格同时把 TTL 从 awaiting 的短窗（默认 600s）换成长窗（默认 1h）并**从此刻重新计时**
    ——agent 是在这一刻才接手的，不该继承投递时刻的剩余寿命。``armed_at`` /
    ``reengage_nudge_count`` / ``bootstrap_gate`` 原样保留（同一轮，不是新一轮）。

    形状同 ``revive_marker`` / ``bump_bootstrap_reengage_count``：直接写 ``_marker_path``、
    **同时写回传入的 ``marker`` 字典**（同一次 Stop 里紧随其后的整份重写不会把升格抹掉）、
    best-effort 绝不抛。非 awaiting 态 / 无 ``_marker_path`` / 写失败一律返回 None。
    """
    if marker_state(marker) != STATE_DELIVERED_AWAITING_AGENT_DECISION:
        return None
    raw_path = marker.get("_marker_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    target_state = (
        STATE_WAITING_BOOTSTRAP if marker.get("bootstrap_gate") else STATE_IN_PROGRESS
    )
    ttl = ttl_seconds()
    payload = {key: value for key, value in marker.items() if key != "_marker_path"}
    payload["state"] = target_state
    payload["expires_at"] = _iso_after(ttl)
    payload["ttl_seconds"] = ttl
    payload["promoted_out_of_awaiting_agent_decision_at"] = _iso_now()
    payload["promoted_out_of_awaiting_agent_decision_reason"] = reason
    try:
        _atomic_write(Path(raw_path), payload)
    except OSError:
        return None
    marker.update(payload)
    return target_state


def acquire_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    run_id: str,
    run_dir: str,
    repo: str | None,
    cwd: str | None,
    attempt_id: str | None = None,
    message_id: str | None = None,
    turn_id: str | None = None,
    prompt_path: str | None = None,
    root: Path | None = None,
) -> AcquireResult:
    paths = marker_paths(task_id=task_id, session_id=session_id, repo=repo or cwd, root=root)
    if not paths:
        return AcquireResult(acquired=True, path=None, marker=None, status=STATUS_INVALID)
    target = paths[0]
    payload = marker_payload(
        task_id=task_id,
        session_id=session_id,
        run_id=run_id,
        run_dir=run_dir,
        repo=repo,
        cwd=cwd,
        attempt_id=attempt_id,
        message_id=message_id,
        turn_id=turn_id,
        prompt_path=prompt_path,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    for _ in range(2):
        try:
            _open_marker_exclusive(target, encoded)
        except FileExistsError:
            marker = read_marker(task_id=task_id, session_id=session_id, repo=repo or cwd, root=root)
            status = marker_status(marker)
            if status == STATUS_ACTIVE:
                return AcquireResult(acquired=False, path=target, marker=marker, status=status)
            with _with_takeover_lock(target):
                marker = read_marker(task_id=task_id, session_id=session_id, repo=repo or cwd, root=root)
                status = marker_status(marker)
                if status == STATUS_ACTIVE:
                    return AcquireResult(acquired=False, path=target, marker=marker, status=status)
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    return AcquireResult(acquired=False, path=target, marker=marker, status=status)
                try:
                    _open_marker_exclusive(target, encoded)
                except FileExistsError:
                    continue
                return AcquireResult(acquired=True, path=target, marker=payload, status=STATUS_ACTIVE)
            continue
        return AcquireResult(acquired=True, path=target, marker=payload, status=STATUS_ACTIVE)
    marker = read_marker(task_id=task_id, session_id=session_id, repo=repo or cwd, root=root)
    return AcquireResult(
        acquired=False,
        path=target,
        marker=marker,
        status=marker_status(marker),
    )


def read_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    repo: str | Path | None,
    root: Path | None = None,
) -> dict[str, Any] | None:
    for path in marker_paths(task_id=task_id, session_id=session_id, repo=repo, root=root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("_marker_path", str(path))
            return payload
    return None


def clear_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    repo: str | Path | None,
    root: Path | None = None,
) -> list[str]:
    removed: list[str] = []
    for path in marker_paths(task_id=task_id, session_id=session_id, repo=repo, root=root):
        # 锁没了，这把锁的重放占位就失去意义：不一并摘掉，它会在窗口内（默认 15min）把**下一
        # 轮**的第一次重放白白挡掉。sidecar 不是锁本身，故不计入 removed。
        try:
            _bootstrap_replay_claim_path_for(path).unlink()
        except (FileNotFoundError, OSError):
            pass
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        removed.append(str(path))
    return removed


def marker_status(
    marker: dict[str, Any] | None,
    *,
    now_ts: float | None = None,
) -> str:
    if not isinstance(marker, dict):
        return STATUS_INVALID
    current_ts = datetime.now(timezone.utc).timestamp() if now_ts is None else now_ts
    expires_ts = _parse_iso_ts(marker.get("expires_at"))
    if expires_ts is not None:
        return STATUS_ACTIVE if current_ts <= expires_ts else STATUS_STALE
    armed_ts = _parse_iso_ts(marker.get("armed_at"))
    if armed_ts is None:
        return STATUS_INVALID
    # 缺 ``expires_at`` 的回退路径也必须按 state 取 TTL：awaiting 态若在这里误用 1h 长窗，
    # 就退回了「无人认领的锁长期挡停」的原缺陷。
    return (
        STATUS_ACTIVE
        if current_ts - armed_ts <= ttl_seconds_for_state(marker_state(marker))
        else STATUS_STALE
    )


def reengage_nudge_count(marker: dict[str, Any] | None) -> int:
    """读出 in-progress 锁 marker 上已累计的 re-engage nudge 次数（缺省 0）。

    Stop 读侧据此判断「这把 active 锁还剩多少次 re-engage 预算」：在预算内时不再静默放行
    agent 干停，而是放回常规 RVF gate 重新唤起；预算用尽再退回静默 skip（防 review↔fix 死循环）。
    任何结构异常一律按 0 处理（保守：宁可多给一次 nudge，也不因坏值误判为预算耗尽而静默挡停）。
    """
    if not isinstance(marker, dict):
        return 0
    try:
        return max(0, int(marker.get("reengage_nudge_count") or 0))
    except (TypeError, ValueError):
        return 0


def bump_reengage_nudge_count(marker: dict[str, Any]) -> int:
    """读-改-写：把 in-progress 锁 marker 的 ``reengage_nudge_count`` +1，原子写回其原文件。

    直接在 ``marker['_marker_path']``（``read_marker`` 注入的实际命中文件，task-path 或
    session-path）上原地自增，避免重新解析 marker_paths 写到另一条路径上。返回新计数。
    best-effort：无 ``_marker_path`` 或写失败时返回当前计数、绝不抛——nudge 记账永不阻断 Stop。
    """
    current = reengage_nudge_count(marker)
    raw_path = marker.get("_marker_path")
    if not isinstance(raw_path, str) or not raw_path:
        return current
    payload = {key: value for key, value in marker.items() if key != "_marker_path"}
    payload["reengage_nudge_count"] = current + 1
    try:
        _atomic_write(Path(raw_path), payload)
    except OSError:
        return current
    return current + 1


# ---------------------------------------------------------------------------
# dispatched-unconfirmed (pending) marker family
#
# 与 in-progress 锁并列、物理隔离的一族 marker：在 Stop hook **dispatch 一条 follow-up**
# 时写入（state=``dispatched_unconfirmed``），表示「已交给 Cline Kanban、但尚未确认成为真实
# turn」。投递落地的权威信号是目标 session 的 UserPromptSubmit hook（arm in-progress 锁），
# 它会按 token 清掉对应 pending；若投递静默丢失（如经 terminal fallback 注入到一个已停止的
# session），pending 永不被清，下一次 Stop 据其判定「上次静默丢投」并放行重投。pending 还
# 顺带恢复了 dispatch→delivery 在途窗口的去重保护（active pending 期间短暂跳过重复 dispatch）。
# ---------------------------------------------------------------------------

PENDING_SUBDIR_NAME = "kanban-followup-dispatched"
PENDING_STATE = "dispatched_unconfirmed"
DEFAULT_PENDING_TTL_SECONDS = 15 * 60
PENDING_TTL_ENV = "RVF_KANBAN_FOLLOWUP_PENDING_TTL_SECONDS"


def _pending_root(root: Path | None = None) -> Path:
    if root is not None:
        return root.expanduser() / PENDING_SUBDIR_NAME
    raw = os.environ.get(LOCK_ROOT_ENV)
    if raw and raw.strip():
        # in-progress 锁把该 env 视为「直接就是 in-progress 目录」；pending 在其下另起子目录，
        # 与 in-progress marker 物理隔离，避免同名 task-*.json 互相覆盖。
        return Path(raw).expanduser() / PENDING_SUBDIR_NAME
    return _rvf_home_directory() / PENDING_SUBDIR_NAME


def _pending_task_path(task_id: str, root: Path | None = None) -> Path:
    return _pending_root(root) / f"task-{safe_token(task_id)}.json"


def pending_ttl_seconds() -> float:
    raw = os.environ.get(PENDING_TTL_ENV)
    if raw is None or not raw.strip():
        return float(DEFAULT_PENDING_TTL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_PENDING_TTL_SECONDS)
    return max(0.0, value)


def pending_marker_payload(
    *,
    task_id: str | None,
    session_id: str | None,
    run_id: str,
    run_dir: str,
    repo: str | None,
    cwd: str | None,
    token: str | None,
    delivery_channel: str | None,
    attempt_id: str | None = None,
    message_id: str | None = None,
    turn_id: str | None = None,
    prompt_path: str | None = None,
    kanban_project_path: str | None = None,
    kanban_task_title: str | None = None,
    kanban_task_title_source: str | None = None,
    origin_transcript_path: str | None = None,
    idempotency_key: str | None = None,
    terminal_delivery_status: str | None = None,
    terminal_delivery_failure_reason: str | None = None,
    terminal_delivery_status_updated_at: str | None = None,
    redispatch_attempt_count: int = 0,
) -> dict[str, Any]:
    ttl = pending_ttl_seconds()
    return {
        "marker_version": MARKER_VERSION,
        "state": PENDING_STATE,
        "dispatched_at": _iso_now(),
        "expires_at": _iso_after(ttl),
        "ttl_seconds": ttl,
        "kanban_task_id": task_id,
        "kanban_attempt_id": attempt_id,
        "session_id": session_id,
        "run_id": run_id,
        "run_dir": run_dir,
        "repo": repo,
        "cwd": cwd,
        "token": token,
        "delivery_channel": delivery_channel,
        "message_id": message_id,
        "turn_id": turn_id,
        "prompt_path": prompt_path,
        # 跨 task stranded-sweep（S1b）与 OS 通知 deep-link（S1a）所需的快照字段：
        # 任意会话的 Stop 都能据此对别的 task 的 stale pending 推出 taskUrl 并通知用户。
        "kanban_project_path": kanban_project_path,
        "kanban_task_title": kanban_task_title,
        "kanban_task_title_source": kanban_task_title_source,
        "origin_transcript_path": origin_transcript_path,
        # 这条投递在 cline-kanban 账本里的主键之一（另一半是 kanban_task_id）。**没有它就没法
        # 查真实终态**——`kanban task message-status` 正是按 (task_id, idempotency_key) 定位记录。
        # 历史 pending marker 不记它，于是对账只能靠「TTL 到了还没被清 ⇒ 大概是丢了」的猜测，
        # 那正是 F2a 要根治的东西。
        "idempotency_key": idempotency_key,
        # dispatch 那一刻的回执快照（可能是非终态 accepted_pending_submit_confirmation）。
        # 只作审计与「对方是否回退到遗留字段」的取证，**不作判据**——判据一律现查 message-status。
        "terminal_delivery_status": terminal_delivery_status,
        "terminal_delivery_failure_reason": terminal_delivery_failure_reason,
        "terminal_delivery_status_updated_at": terminal_delivery_status_updated_at,
        # 这条 follow-up 已经被重投过几次。契约第 7 条要求每次重投换新 idempotency key，
        # 于是「重投了几次」不再能从 key 反推，必须显式记账（也用于给自动重投设上界）。
        "redispatch_attempt_count": max(0, int(redispatch_attempt_count or 0)),
    }


def write_pending_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    run_id: str,
    run_dir: str,
    repo: str | None,
    cwd: str | None,
    token: str | None,
    delivery_channel: str | None,
    attempt_id: str | None = None,
    message_id: str | None = None,
    turn_id: str | None = None,
    prompt_path: str | None = None,
    kanban_project_path: str | None = None,
    kanban_task_title: str | None = None,
    kanban_task_title_source: str | None = None,
    origin_transcript_path: str | None = None,
    idempotency_key: str | None = None,
    terminal_delivery_status: str | None = None,
    terminal_delivery_failure_reason: str | None = None,
    terminal_delivery_status_updated_at: str | None = None,
    redispatch_attempt_count: int = 0,
    root: Path | None = None,
) -> Path | None:
    if not (isinstance(task_id, str) and task_id.strip()):
        return None
    payload = pending_marker_payload(
        task_id=task_id,
        session_id=session_id,
        run_id=run_id,
        run_dir=run_dir,
        repo=repo,
        cwd=cwd,
        token=token,
        delivery_channel=delivery_channel,
        attempt_id=attempt_id,
        message_id=message_id,
        turn_id=turn_id,
        prompt_path=prompt_path,
        kanban_project_path=kanban_project_path,
        kanban_task_title=kanban_task_title,
        kanban_task_title_source=kanban_task_title_source,
        origin_transcript_path=origin_transcript_path,
        idempotency_key=idempotency_key,
        terminal_delivery_status=terminal_delivery_status,
        terminal_delivery_failure_reason=terminal_delivery_failure_reason,
        terminal_delivery_status_updated_at=terminal_delivery_status_updated_at,
        redispatch_attempt_count=redispatch_attempt_count,
    )
    target = _pending_task_path(task_id, root)
    _atomic_write(target, payload)
    return target


def pending_redispatch_attempt_count(marker: dict[str, Any] | None) -> int:
    """这条 pending 已被重投过几次（缺省 0，坏值按 0）。"""
    if not isinstance(marker, dict):
        return 0
    try:
        return max(0, int(marker.get("redispatch_attempt_count") or 0))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# per-task 派发单飞闸
#
# 契约第 5 条：**RVF 不得对同一 task 并发投递**——cline-kanban 每个 session 只有一个单飞槽，
# 先到的那条会被判 ``superseded_by_later_delivery``。历史实现靠「重投用一个稳定 idempotency
# key」来间接去重，但契约第 7 条要求每次重投换新 key（同 key 不会因上次失败就再投一次），
# 两条要求叠加后，去重责任必须落到**派发侧的串行化**上，而不是 key 上。
#
# 用 flock（建议性锁，进程崩溃由内核自动释放）而不是 O_EXCL 占位文件：这里要的是「同一时刻
# 只有一个 Stop 在对这个 task 做决定+投递」，不是跨进程的持久记账；持久记账仍由 pending
# marker 承担。拿不到锁一律**跳过本轮投递**（而不是等），因为 Stop hook 自身有秒级预算，
# 且下一次 Stop 会重新决策——宁可这一轮不投，也不要投出第二条把前一条撞成 superseded。
# ---------------------------------------------------------------------------

DISPATCH_SINGLE_FLIGHT_LOCK_SUFFIX = ".dispatch-single-flight.lock"


def _pending_dispatch_single_flight_lock_path(task_id: str, root: Path | None = None) -> Path:
    target = _pending_task_path(task_id, root)
    return target.with_name(f".{target.name}{DISPATCH_SINGLE_FLIGHT_LOCK_SUFFIX}")


@contextmanager
def dispatch_single_flight_for_task(task_id: str | None, *, root: Path | None = None):
    """``with`` 期间独占「对这个 task 的 follow-up 派发」；产出 True 表示拿到了闸。

    产出 False = 另一个进程正在派发同一个 task，**本轮不得投递**。缺 task_id 或文件系统
    报错一律产出 True（放行）：这道闸是防并发的，不是安全边界，绝不能让它把派发整个堵死。
    """
    if not (isinstance(task_id, str) and task_id.strip()):
        yield True
        return
    try:
        lock_path = _pending_dispatch_single_flight_lock_path(task_id, root)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield True
        return
    handle = os.fdopen(fd, "w", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def read_pending_marker(
    *,
    task_id: str | None,
    root: Path | None = None,
) -> dict[str, Any] | None:
    if not (isinstance(task_id, str) and task_id.strip()):
        return None
    path = _pending_task_path(task_id, root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        payload.setdefault("_marker_path", str(path))
        return payload
    return None


def clear_pending_marker(
    *,
    task_id: str | None,
    token: str | None = None,
    root: Path | None = None,
) -> list[str]:
    if not (isinstance(task_id, str) and task_id.strip()):
        return []
    # token 防误清：若磁盘上的 pending 已被一条更新的 dispatch（不同 token）覆盖，
    # 一条迟到的旧投递确认不应清掉这把仍未确认的新 pending。
    if token is not None:
        existing = read_pending_marker(task_id=task_id, root=root)
        if isinstance(existing, dict):
            stored = existing.get("token")
            if isinstance(stored, str) and stored and stored != token:
                return []
    path = _pending_task_path(task_id, root)
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        return []
    return [str(path)]


def pending_status(
    marker: dict[str, Any] | None,
    *,
    now_ts: float | None = None,
) -> str:
    # pending payload 始终带 ``expires_at``，故可直接复用 ``marker_status`` 的过期判定。
    return marker_status(marker, now_ts=now_ts)


def iter_pending_markers(
    *,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """枚举 pending 目录下所有 ``task-*.json`` 的 payload（跨 task）。

    供 S1b 跨 task stranded-sweep 使用：任意会话的 Stop 都能据此发现**别的** task
    遗留的 stale pending。形状仿 ``rvf_prep_file.sweep_stale``——glob 整目录、逐文件
    吞 OSError/JSON 错误，绝不因单个坏文件中断整轮扫荡。每个 payload 注入 ``_marker_path``
    以便调用方定位文件（与 ``read_pending_marker`` 一致）。
    """
    base = _pending_root(root)
    if not base.is_dir():
        return []
    markers: list[dict[str, Any]] = []
    try:
        paths = sorted(base.glob("task-*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("_marker_path", str(path))
            markers.append(payload)
    return markers


def stamp_pending_notified(
    *,
    task_id: str | None,
    token: str | None = None,
    root: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """给某条 pending marker 盖 ``last_notified_at`` 戳（防 stranded-sweep 刷屏）。

    **read-merge-write，且 ``token`` 必须原样保留**——否则会破坏
    ``clear_pending_marker`` 的 token 防误清 guard（一条迟到的旧投递确认会因 token
    不再匹配而无法清掉，导致 marker 永久误锁）。这里直接读原始文件（不经
    ``read_pending_marker`` 的 ``_marker_path`` 注入），合并戳记后整体写回，
    payload 里既有的 ``token`` 字段保持不动。

    ``token`` 传入时作为「确认在盖正确的 marker」的 guard：若磁盘上 marker 的 token
    与传入不一致（已被更新的 dispatch 覆盖），返回 False、不盖戳。
    """
    if not (isinstance(task_id, str) and task_id.strip()):
        return False
    path = _pending_task_path(task_id, root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if token is not None:
        stored = payload.get("token")
        if isinstance(stored, str) and stored and stored != token:
            return False
    payload.pop("_marker_path", None)
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload["last_notified_at"] = stamp.isoformat().replace("+00:00", "Z")
    try:
        payload["notify_count"] = int(payload.get("notify_count") or 0) + 1
    except (TypeError, ValueError):
        payload["notify_count"] = 1
    try:
        _atomic_write(path, payload)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# 决策点横幅「带外投递意图」marker family
#
# 与 in-progress 锁、dispatched-unconfirmed pending **三族并列、物理隔离**（各自子目录，
# 避免同名 ``task-*.json`` 互相覆盖）。
#
# 它解决的是「同步 → 异步」转换独有的那个崩溃窗口：Stop hook 把横幅交给 cline-kanban 终端投递
# 之后、拿到回执之前若进程被杀，本进程的返回值就永远拿不到了。**外部副作用之前先持久化意图**，
# 下一次 Stop 就能从 intent 对账，而不必靠「本进程有没有返回」去猜。
#
# 与 pending marker 的分工：pending 记的是**一条已派发的 review follow-up**（有 run、有 lease、
# 有 prep）；intent 记的是**一次尚未被接受的发起征询**（decision token 之外什么都还没有，见 D0）。
# 两者的善后完全不同，合用一个文件只会让「撤回」这个动作语义分叉。
# ---------------------------------------------------------------------------

DECISION_POINT_DELIVERY_INTENT_SUBDIR_NAME = "kanban-decision-point-delivery-intent"
#: 已落盘意图、尚未（或不知有没有）把消息交出去。进程在此崩溃 ⇒ 下一次 Stop 从这里对账。
DECISION_POINT_DELIVERY_INTENT_STATE_RECORDED_BEFORE_SEND = "recorded_before_send"
#: 消息已交给 cline-kanban 且回执不是「确定失败」，但 agent 还没接到（等 UPS 观测 needle）。
DECISION_POINT_DELIVERY_INTENT_STATE_SENT_AWAITING_AGENT_RECEIPT = "sent_awaiting_agent_receipt"
#: intent 的 TTL 必须**严格小于**决策 token 的带外 TTL（1200s），否则 token 先死、撤回接手时
#: 已无 token 可对账。默认 900s。
DEFAULT_DECISION_POINT_DELIVERY_INTENT_TTL_SECONDS = 15 * 60
DECISION_POINT_DELIVERY_INTENT_TTL_ENV = "RVF_DECISION_POINT_DELIVERY_INTENT_TTL_SECONDS"


def _decision_point_delivery_intent_root(root: Path | None = None) -> Path:
    if root is not None:
        return root.expanduser() / DECISION_POINT_DELIVERY_INTENT_SUBDIR_NAME
    raw = os.environ.get(LOCK_ROOT_ENV)
    if raw and raw.strip():
        return Path(raw).expanduser() / DECISION_POINT_DELIVERY_INTENT_SUBDIR_NAME
    return _rvf_home_directory() / DECISION_POINT_DELIVERY_INTENT_SUBDIR_NAME


def _decision_point_delivery_intent_path(task_id: str, root: Path | None = None) -> Path:
    return _decision_point_delivery_intent_root(root) / f"task-{safe_token(task_id)}.json"


def decision_point_delivery_intent_ttl_seconds() -> float:
    raw = os.environ.get(DECISION_POINT_DELIVERY_INTENT_TTL_ENV)
    if raw is None or not raw.strip():
        return float(DEFAULT_DECISION_POINT_DELIVERY_INTENT_TTL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_DECISION_POINT_DELIVERY_INTENT_TTL_SECONDS)
    return max(0.0, value)


def decision_point_delivery_intent_payload(
    *,
    task_id: str,
    session_id: str | None,
    worktree: str | None,
    cwd: str | None,
    decision_token: str,
    candidate_fingerprint: str | None,
    kanban_project_path: str | None,
    kanban_attempt_id: str | None = None,
    origin_transcript_path: str | None = None,
    state: str = DECISION_POINT_DELIVERY_INTENT_STATE_RECORDED_BEFORE_SEND,
    idempotency_key: str | None = None,
    prompt_path: str | None = None,
    out_of_band_delivery_status: str | None = None,
    out_of_band_delivery_failure_reason: str | None = None,
) -> dict[str, Any]:
    ttl = decision_point_delivery_intent_ttl_seconds()
    return {
        "marker_version": MARKER_VERSION,
        "state": state,
        "recorded_at": _iso_now(),
        "expires_at": _iso_after(ttl),
        "ttl_seconds": ttl,
        "kanban_task_id": task_id,
        "kanban_attempt_id": kanban_attempt_id,
        "kanban_project_path": kanban_project_path,
        "session_id": session_id,
        "worktree": worktree,
        "cwd": cwd,
        # 这枚 decision token 是 intent 与 token store 之间的唯一连接键，也是 clear/update 的
        # 防误清 guard（对偶于 pending marker 的 ``token``）。
        "decision_token": decision_token,
        "candidate_fingerprint": candidate_fingerprint,
        "origin_transcript_path": origin_transcript_path,
        # 没有这枚 key 就查不到 cline-kanban 侧的真实终态，也取消不掉这条投递。
        "idempotency_key": idempotency_key,
        "prompt_path": prompt_path,
        "out_of_band_delivery_status": out_of_band_delivery_status,
        "out_of_band_delivery_failure_reason": out_of_band_delivery_failure_reason,
    }


def write_decision_point_delivery_intent(
    *,
    task_id: str | None,
    root: Path | None = None,
    **fields: Any,
) -> Path | None:
    """落盘一份投递意图（**必须在把消息交给 cline-kanban 之前调用**）。"""
    if not (isinstance(task_id, str) and task_id.strip()):
        return None
    payload = decision_point_delivery_intent_payload(task_id=task_id, **fields)
    target = _decision_point_delivery_intent_path(task_id, root)
    _atomic_write(target, payload)
    return target


def read_decision_point_delivery_intent(
    *,
    task_id: str | None,
    root: Path | None = None,
) -> dict[str, Any] | None:
    if not (isinstance(task_id, str) and task_id.strip()):
        return None
    path = _decision_point_delivery_intent_path(task_id, root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        payload.setdefault("_marker_path", str(path))
        return payload
    return None


def update_decision_point_delivery_intent(
    *,
    task_id: str | None,
    decision_token: str | None = None,
    updates: dict[str, Any],
    root: Path | None = None,
) -> bool:
    """read-merge-write 一份既有意图（``decision_token`` 作防误改 guard）。

    与 ``stamp_pending_notified`` 同款：直接读原始文件、合并后整体写回，绝不经
    ``read_decision_point_delivery_intent``（它会注入 ``_marker_path``）。
    """
    if not (isinstance(task_id, str) and task_id.strip()):
        return False
    path = _decision_point_delivery_intent_path(task_id, root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if decision_token is not None:
        stored = payload.get("decision_token")
        if isinstance(stored, str) and stored and stored != decision_token:
            return False
    payload.pop("_marker_path", None)
    payload.update(updates)
    payload["updated_at"] = _iso_now()
    try:
        _atomic_write(path, payload)
    except OSError:
        return False
    return True


def clear_decision_point_delivery_intent(
    *,
    task_id: str | None,
    decision_token: str | None = None,
    root: Path | None = None,
) -> list[str]:
    """清掉某 task 的投递意图（``decision_token`` 不匹配时**不清**，避免误删更新的一份）。"""
    if not (isinstance(task_id, str) and task_id.strip()):
        return []
    if decision_token is not None:
        existing = read_decision_point_delivery_intent(task_id=task_id, root=root)
        if isinstance(existing, dict):
            stored = existing.get("decision_token")
            if isinstance(stored, str) and stored and stored != decision_token:
                return []
    path = _decision_point_delivery_intent_path(task_id, root)
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        return []
    return [str(path)]


def decision_point_delivery_intent_status(
    marker: dict[str, Any] | None,
    *,
    now_ts: float | None = None,
) -> str:
    """intent payload 始终带 ``expires_at``，故直接复用 ``marker_status`` 的过期判定。"""
    return marker_status(marker, now_ts=now_ts)


def iter_decision_point_delivery_intents(
    *,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """枚举全部投递意图（跨 task），形状仿 ``iter_pending_markers``：坏文件跳过，永不抛。"""
    base = _decision_point_delivery_intent_root(root)
    if not base.is_dir():
        return []
    markers: list[dict[str, Any]] = []
    try:
        paths = sorted(base.glob("task-*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("_marker_path", str(path))
            markers.append(payload)
    return markers
