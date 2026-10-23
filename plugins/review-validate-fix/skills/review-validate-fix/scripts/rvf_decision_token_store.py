#!/usr/bin/env python3
"""Agentic-initiation 决策 token 落盘存储（RVF overhaul S1 基建）。

背景：RVF「hook 发起 → agent 发起」overhaul 把发起决策权交给主 agent。Stop hook 降级为
「决策点信使」：它**铸造**一个决策 token 并把 token 注入给主 agent；主 agent 显式
``start`` 或 ``decline``。本模块是该 token 的**唯一落盘真相源**，被两类调用方共享：

  * Stop hook 侧：``mint_decision_token`` / ``mark_injected`` / ``is_fingerprint_in_declined_cooldown``；
  * rvf-agent CLI 侧（S3）：``acknowledge_decision_token`` / ``mark_dispatched`` /
    ``mark_declined`` / ``record_start_failure``（start --retry 回退）。

设计要点（实施计划 §四 S1 / §二 R24·R25）：

  * 状态机 ``minted → injected → acknowledged → dispatched | declined``，旁路 ``expired | lost``；
    ``acknowledged`` 为**非终态**（允许 scope 子命令与 start --retry，dispatched 前可从失败回退回
    acknowledged）；``dispatched`` / ``declined`` 为终态。非法迁移一律显式抛
    ``DecisionTokenStateError``。
  * 去重键 ``(worktree, session_id, candidate_fingerprint)``：同键存在活跃（非终态、且 TTL 未过期或
    run 在途可复活）token 时**拒绝重铸并返回既有 token**——这是注入幂等的硬保证（R25）。
    ``candidate_fingerprint`` 对本模块是**不透明字符串**（由 S2 摘要生成器计算，本模块不实现指纹算法）。
  * declined 指纹冷却表：decline 时把 ``(worktree, candidate_fingerprint)`` 写入冷却表，摘要生成器
    在再次注入前查表，指纹未变则不再注入（防 decline→Stop→再注入的 nag 循环）。
  * declined 会话级 snooze（闸 2b）：内容指纹逐字节敏感，工作进行中每次 Stop 都是「新候选」，上面
    的冷却表因此挡不住同一会话里的重复谢绝。decline 时另按 ``(worktree, session_id)`` 记一条
    粗粒度的工作范围快照；在出现真正的工作边界（HEAD 变化 / 范围明显扩大 / 硬 TTL）之前，同会话同
    worktree 的自动铸造一律静默。开发者发来真实 prompt 不解除 snooze，只把「范围扩大」的对比基线
    挪到发话那一刻。
  * 谢绝分两类（``decline_kind``）：只有**结构性**谢绝写冷却表与 snooze；**延后型**谢绝（「等 X 之后
    再审」）两者都不写，只挡谢绝所在的那个回合，新回合或 HEAD 变化后重新征询（闸 2c）。
  * 铸造侧频率熔断（R24 裁决 b）：同 session、3600 秒滚动窗口内第 ≥3 次**铸造请求**即拒铸，写
    circuit-breaker-tripped ledger，并回结构化结果供 hook 出 systemMessage 告警。纯计数，零语义判断。
  * TTL 复活（参照 rvf_prep_file 的 FU-1 方案 B）：token 已过 TTL、但其锚定 run_dir 仍在磁盘（run
    在途）→ 就地续期为原状态而非判定 expired，避免半途丢弃在途审查。

所有落盘根目录既可用显式参数注入、也可用环境变量覆盖，测试须用 tmp 隔离，绝不写真实 ``~/.rvf``。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
DEFAULT_DECISION_TOKEN_TTL_SECONDS = 900  # 15 分钟：足够主 agent 在同一 turn 内简析后 start/decline

# 带外投递（横幅经 cline-kanban 终端投递成为一条真实 user message）专用 TTL。
#
# **为什么不能沿用 900s**：同步自注入的 900s 是按「同一个 turn 内决策」定的；带外投递的横幅要先
# 等 cline-kanban 把它投成 user message，对方契约承诺的收敛预算就有 196.5s
# （``core.cline_kanban_terminal_delivery`` 的
# ``TERMINAL_DELIVERY_STATUS_CONVERGENCE_CONTRACT_BUDGET_SECONDS``），落地后还要再给 agent 一个
# 回合去简析。下界由两条契约共同决定：
#   * **> 200s**——覆盖对方的投递状态收敛预算；
#   * **> 900s**——严格大于 ``kanban_followup_lock`` 的 pending marker TTL
#     （``DEFAULT_PENDING_TTL_SECONDS`` = 15 分钟）。token 若先于投递意图死掉，撤回逻辑接手时
#     已经没有 token 可对账，那正是 D6 要根治的「撤回对横幅是空的」。
# 取 1200s（20 分钟）同时满足两者，并留出一个 agent 回合的余量。
DECISION_TOKEN_TTL_SECONDS_FOR_OUT_OF_BAND_DELIVERY = 1200
DEFAULT_TOKEN_ALLOCATION_ATTEMPTS = 8
TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")

# 铸造侧频率熔断参数（R24 裁决 b）：同 session 3600 秒窗口内第 ≥3 次铸造请求拒铸。
DEFAULT_MINT_CIRCUIT_BREAKER_WINDOW_SECONDS = 3600
DEFAULT_MINT_CIRCUIT_BREAKER_MAX_REQUESTS = 3

# mint-request ledger 的两个字段名（append-only 记账 + 追加式冲销）。
#   * ``mint_request_record_id``：每一次铸造请求的稳定 id，同时写进 ledger 行与 token payload，
#     使「哪一枚 token 占用了哪一份熔断额度」可对账；
#   * ``reverses_mint_request_record_id``：一条**冲销**记录，声明上面那份额度已被撤回释放。
# 冲销刻意做成追加行而不是回改/删除既有行——ledger 的可审计性要求它 append-only。
MINT_REQUEST_LEDGER_RECORD_ID_FIELD = "mint_request_record_id"
MINT_REQUEST_LEDGER_CHARGE_REVERSAL_FIELD = "reverses_mint_request_record_id"

# 落盘根：显式参数 > 专用环境变量 > ``$RVF_HOME`` 或 ``~/.rvf`` 下的固定子目录。
ENV_RVF_HOME = "RVF_HOME"
ENV_DECISION_TOKEN_ROOT = "RVF_AGENTIC_INITIATION_DECISION_TOKEN_ROOT"
ENV_DECLINED_FINGERPRINT_ROOT = "RVF_AGENTIC_INITIATION_DECLINED_FINGERPRINT_ROOT"
ENV_CIRCUIT_BREAKER_ROOT = "RVF_AGENTIC_INITIATION_CIRCUIT_BREAKER_ROOT"

DECISION_TOKEN_DIRNAME = "agentic-initiation-decision-tokens"
DECLINED_FINGERPRINT_DIRNAME = "agentic-initiation-declined-fingerprints"
CIRCUIT_BREAKER_DIRNAME = "agentic-initiation-circuit-breaker-ledger"

# 去重键各字段的分隔符（ASCII Unit Separator，正常字段值不含）。
_KEY_FIELD_SEPARATOR = "\x1e"

# declined 会话级 snooze（闸 2b）的落盘位置：**declined 冷却根下的子目录**。刻意不另立一个根 /
# 环境变量——它与冷却表同属「agent 谢绝过什么」这一族状态，挂在冷却根下就自动继承冷却根的全部
# 隔离手段（显式 ``cooldown_root`` / 专用 env / ``$RVF_HOME``），不会出现「测试注入了冷却根、
# snooze 却写进真实 ~/.rvf」的漏网。冷却表只在根目录顶层放 ``<hash>.json``，子目录与它互不相干。
# 目录结构：``<snooze 根>/<sha(session_id)>/<sha(worktree)>.json``——按会话分目录，UPS 在开发者
# 发来真实 prompt 时按会话整目录**重置各 snooze 的「范围扩大」对比基线**（不解除 snooze，见下方
# 方向 C 的 EXPERIMENT_NODE），无需知道 Stop 侧用的 worktree 字符串。
DECLINED_SESSION_SCOPE_SNOOZE_SUBDIRNAME = "declined-session-scope-snoozes"
DECLINED_SESSION_SCOPE_SNOOZE_LIFECYCLE_EVENT_LEDGER_FILENAME = (
    "declined-session-scope-snooze-lifecycle-events.jsonl"
)
# 生命周期事件种类（写进上面那份 ledger 的 ``snooze_lifecycle_event`` 字段）。
DECLINED_SESSION_SCOPE_SNOOZE_LIFECYCLE_EVENT_RELEASED = "released"
DECLINED_SESSION_SCOPE_SNOOZE_LIFECYCLE_EVENT_SCOPE_EXPANSION_BASELINE_REBASED_ON_DEVELOPER_PROMPT = (
    "scope_expansion_baseline_rebased_on_developer_prompt"
)

# EXPERIMENT_NODE: declined 会话级 snooze 的硬 TTL 取 12h——跨过一个工作日就重新征询一次；更短会让长时间自主工作的会话重新被横幅打扰，更长则让被遗忘的大改动长期无人提醒。
DECLINED_SESSION_SCOPE_SNOOZE_HARD_TTL_SECONDS = 12 * 3600
# EXPERIMENT_NODE: 「范围明显扩大」= 出现新路径 **且** 未审行数估计增长超过 50%（两者须同时成立）；只看新路径会被顺手改一个小文件解除，只看行数会被同一批文件的持续打磨解除。
DECLINED_SESSION_SCOPE_SNOOZE_UNREVIEWED_LINE_GROWTH_RATIO_THRESHOLD = 0.5
# EXPERIMENT_NODE: 「同一批文件上的工作量翻几倍」单独即可解除：未审行数 ≥ 对比基线的 3 倍 **且** 绝对增量 ≥ 150 非空行（两者须同时成立；倍数挡住大基线上的持续打磨，绝对值挡住小基线被几行改动顶开）。d7741 裁定的默认值；本机 48 对历史「谢绝→下一次征询」里它命中 9 对、全部与上面那条规则重合，唯一「无新路径、行数大涨」的样本（2.36 倍，+7257 行）agent 自己又谢绝了一次，暂无放宽依据。
DECLINED_SESSION_SCOPE_SNOOZE_UNREVIEWED_LINE_GROWTH_ALONE_MINIMUM_MULTIPLE_OF_BASELINE = 3
DECLINED_SESSION_SCOPE_SNOOZE_UNREVIEWED_LINE_GROWTH_ALONE_MINIMUM_ABSOLUTE_NONBLANK_LINES = 150

# snooze 解除原因（写进生命周期事件 ledger，供部署后评估上面几个 EXPERIMENT_NODE 的阈值）。
# EXPERIMENT_NODE: HEAD 变化（任何 commit / checkout / reset）一律视为工作边界、解除 snooze；备选是只认「HEAD 前进且新提交落在候选路径上」。
DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_WORKTREE_HEAD_CHANGE = "worktree_head_changed"
DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_SCOPE_EXPANSION = (
    "new_candidate_paths_and_unreviewed_line_growth_exceeded_threshold"
)
DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_UNREVIEWED_LINE_GROWTH_ALONE = (
    "unreviewed_line_count_alone_grew_past_multiple_of_scope_expansion_baseline"
)
DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_HARD_TTL = "hard_ttl_elapsed"
DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_EXPLICIT_USER_REQUEST = "explicit_user_request"
# EXPERIMENT_NODE: 开发者发来真实 prompt **不解除** snooze，只把「范围明显扩大」的对比基线（纳入路径集合 + 未审行数）重置为发话那一刻的状态（取发话前最后一次 Stop 观测到的工作范围）；计划原文是「发话即清除」，历史复算显示那样闸 2b 只能挡约 12% 的重复横幅（RVF run rvf-20260929T153245Z 的 RVF-001，经开发者裁定改为本规则）。


# ---------------------------------------------------------------------------
# 生命周期状态机（决策状态，独立于 TTL）
# ---------------------------------------------------------------------------

STATE_MINTED = "minted"
STATE_INJECTED = "injected"
STATE_ACKNOWLEDGED = "acknowledged"
STATE_DISPATCHED = "dispatched"
STATE_DECLINED = "declined"
STATE_EXPIRED = "expired"
STATE_LOST = "lost"
# 用户把这次征询本身 rewind / 撤回掉了（INV-C）。**不是**决策，也不是 TTL 死亡：它表示
# 「这枚 token 承载的那次征询已经不在会话历史里了」。进入即移出去重索引，使下一次 Stop 能且
# 只能重新征询一次；且**不计 declined 冷却、不计铸造熔断**——用户刚刻意回退掉的征询，不该反过来
# 把接下来的 TTL 窗口堵死。
STATE_WITHDRAWN = "withdrawn"

# ``withdrawn`` 的**成因**：同一个终态，两种完全不同的起因，对熔断额度的处置必须相反。
#
# INV-C（「withdrawn 不计冷却也不计熔断」）当初是为**用户 rewind** 这一支立的，理由正当：用户
# 刚把征询回退掉，说明他还想被问，若让这次撤回烧掉一份熔断额度，连续 rewind 两次之后第三次征询
# 就会撞上 ``circuit_breaker_tripped``，会话被静默一整个窗口（默认 3600s）。
#
# 但 ``mark_withdrawn`` 历史上把**带外投递从未落地**这一支也按同样处置，于是产生了一个空转环：
# 横幅铸出 → 投递从未成为 turn → 判搁浅撤回 → 冲销额度 → 下一次 Stop 无成本地再铸一枚 → 再空转。
# 实测（2026-08-21）一个 session 内出现 ``minted → withdrawn`` 连铸三枚、全程从未经过 ``injected``。
# 这一支里用户**根本没看到过**横幅，「他还想被问」的前提不成立；而投递通道坏着时重铸多少次都会
# 空转，此时烧掉额度、让熔断如实跳闸，正是所需的退避。
#
# 故按成因分叉，而不是取消 INV-C：``rewound_by_user`` 一支行为逐字不变，只有空转这一支不冲销。
# 自动路径退避期间用户仍可手动发起——手动触发已由 ``initiated_by_explicit_user_request`` 豁免熔断。
DECISION_POINT_WITHDRAWAL_CAUSE_REWOUND_BY_USER = "rewound_by_user"
"""用户把承载这次征询的那条消息 rewind 掉了。冲销熔断额度（INV-C 原义）。"""

DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_DELIVERY_NEVER_BECAME_A_TURN = (
    "out_of_band_delivery_never_became_a_turn"
)
"""带外横幅投出去后在有效期内从未成为一条真实 user message。**不**冲销熔断额度。

⚠️ 这个成因是一句**关于事实的断言**，只有在拿到「这枚 token 的横幅从未出现在 transcript 里」
这样的确凿证据时才能用。把「无法确证」也塞进来会撞上下面那个成因描述的可达路径。
"""

DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_DELIVERY_BECAME_A_TURN_BUT_TOKEN_ADVANCE_FAILED = (
    "out_of_band_delivery_became_a_turn_but_token_advance_failed"
)
"""带外横幅**确实**成为了一条真实 user message，但 ``minted → injected`` 那一步没能落上。
冲销熔断额度。

可达路径（本仓已记录）：``rvf_user_prompt_submit._observe_delivered_decision_point_banner``
在 ``mark_injected`` 抛 ``DecisionTokenExpiredError`` 时**提前返回且刻意不清投递意图**（留给
后续对账），而 ``read_decision_token`` 对已过 TTL 的 token 仍原样返回 ``state=minted``。于是
一枚「agent 已经看到过横幅」的 token 在对账侧与「从未成为 turn」的那一族形状完全一致。

为什么它必须**冲销**（即进白名单）：这一支里横幅其实送达了、通道是**健康**的，退避的前提
（「通道坏着，重铸多少次都会空转」）根本不成立。按空转计费的话，连续 3 次这种记账失败就会把
铸造熔断打跳（默认窗口 3600s），整个 session 的自动发起被静默封禁——那是**漏审**方向，比多问
一次严重得多。冲销并不会让熔断失效：它只在拿到「横幅确实落地过」的正向证据时才成立，而那份
证据本身就说明这不是一次空转。

⚠️ **59ffd 之后「落地过」的正向证据只能是「决策正文确实注入过」**：粘贴通道只送一行无指令的边界
标记，正文由 UPS 注入。标记在 transcript 祖先链上只说明那一行成了 turn，agent 看到的是「未随附即
忽略」。故搁浅对账只在投递意图里记着 UPS「正文已注入」时才用本成因；标记落地而正文没注入，用下面
那个**不冲销**的成因（审计 M9）。
"""

DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_BOUNDARY_MARKER_BECAME_A_TURN_BUT_BANNER_BODY_NEVER_INJECTED = (
    "out_of_band_boundary_marker_became_a_turn_but_banner_body_never_injected"
)
"""带外边界标记**确实**成了一条 user message，但决策正文**从未**注入 agent 上下文。**不**冲销熔断额度。

正文只能经 UserPromptSubmit hook 以 additionalContext 注入。UPS 没跑 / 超时 / 崩溃、token 里没有
正文、标记到达时 token 已过 TTL——这几种情形下 agent 只拿到一行「未随附即忽略」的标记，每次征询
都空转一回合。它们在对账侧都表现为「标记在祖先链上、token 仍是 minted」。UPS 持续失效时若照旧
冲销，就是无退避的「铸 → 空转 → 撤回」循环且开发者看不到 UPS 坏了；所以计入熔断，并由调用方以
developer_only 如实点名 UPS 那一侧的原因。
"""

DECISION_POINT_WITHDRAWAL_CAUSE_STOP_HOOK_REENTRY_BUDGET_EXHAUSTED = (
    "stop_hook_reentry_budget_exhausted"
)
"""跨 turn 再入预算耗尽，RVF **主动决定不注入**这一枚横幅。冲销熔断额度。

与上面两个带外成因的关键区别：那两支是「发出去了、但（可能）没到」，这一支是**根本没发**——
判定发生在 ``_decision_point_self_injection_decision`` 入口，早于任何投递动作。

为什么它进冲销白名单（这条要论证，因为白名单的默认方向是「不冲销」）：熔断存在的意义是
**通道坏着时退避**。这一支里通道是健康的，重铸也不会空转——下一次 Stop 只要预算复位
（驱动正文换了一类事 / 用户提交了新 prompt / TTL 过期），横幅立刻就能正常发出。把它按空转计费，
连续 3 次就会把铸造熔断打跳、整个 session 的自动发起被静默封禁，那是**漏审**方向。

而且再入预算**本身就是一层退避**，且比熔断更精确（它按 reason_code 分类、有三条复位路径）。
在它之上再叠一层 session 级熔断，是对同一件事退避两次。
"""

DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_DELIVERY_FAILED_PER_CLINE_KANBAN_TERMINAL_DELIVERY_RECEIPT = (
    "out_of_band_delivery_failed_per_cline_kanban_terminal_delivery_receipt"
)
"""cline-kanban 的终端投递回执（``terminal_delivery_status``）已判定这条横幅**投递失败**（开发者正在该
终端打字 / 输入框有草稿、会话没起来等）。**不**冲销熔断额度。

与 ``…_never_became_a_turn`` 同属「没送到」：横幅从未到达 agent 眼前，退避的前提成立。区别只在
证据来源与时机——那一支要等投递意图 900s TTL 到期、再拿 transcript 证明「没成为 turn」；这一支在
意图 TTL 之内就拿到了对方的确定结论（kanban 判失败的 P50 约 75s），于是下一次 Stop 不必再空等十几
分钟（回溯实测：从铸出到撤回 P50 26 分钟，这期间共 25 次 Stop 被「已有横幅在途」静默）。
"""

DECISION_POINT_WITHDRAWAL_CAUSE_AGENT_RESUMED_WORK_BEFORE_DECISION_POINT_DELIVERY_LANDED = (
    "agent_resumed_work_before_decision_point_delivery_landed"
)
"""Stop 铸出带外横幅之后、边界标记以「空闲时开出的新回合」落地之前，agent **已经复工**（后台任务通知
唤醒、开发者发话、Stop 续跑、标记在回合中途被并入……）。**冲销**熔断额度。

复工本身说明两件事都过时了：候选范围是铸 token 那一刻的工作树（之后又改了），时机也不对（agent 正在
干活）。所以不注入正文、撤回 token，由 Stop 在同一次（或下一次）按当时的工作树重新评估。判据见
``decision_point_agent_resumed_work_after_token_mint_evidence``。

为什么进冲销白名单（白名单默认不冲销，这里要论证）：与 ``stop_hook_reentry_budget_exhausted`` 同理——
投递通道是**健康**的，只是时机不对；agent 也从没看到过这份正文。按空转计费的话，agent 连续几次在横幅
落地前复工就会把铸造熔断打跳、整个 session 的自动发起被静默一个窗口，那是**漏审**方向。这一支自己的
空转由 Stop 侧的独立上界约束（连续若干次复工撤回后改走同步自注入），不借用熔断。
"""

DECISION_POINT_WITHDRAWAL_CAUSES = frozenset(
    {
        DECISION_POINT_WITHDRAWAL_CAUSE_AGENT_RESUMED_WORK_BEFORE_DECISION_POINT_DELIVERY_LANDED,
        DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_DELIVERY_FAILED_PER_CLINE_KANBAN_TERMINAL_DELIVERY_RECEIPT,
        DECISION_POINT_WITHDRAWAL_CAUSE_REWOUND_BY_USER,
        DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_DELIVERY_NEVER_BECAME_A_TURN,
        DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_DELIVERY_BECAME_A_TURN_BUT_TOKEN_ADVANCE_FAILED,
        DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_BOUNDARY_MARKER_BECAME_A_TURN_BUT_BANNER_BODY_NEVER_INJECTED,
        DECISION_POINT_WITHDRAWAL_CAUSE_STOP_HOOK_REENTRY_BUDGET_EXHAUSTED,
    }
)

# 哪些成因该把铸造时占用的那一份熔断额度还回去。**白名单而非黑名单**：将来新增成因时，
# 默认落到「不冲销」这一侧——多退避一次只是多问一次的延迟，而错误地无限冲销会让熔断整个失效。
#
# 判据（两条成因共享同一条）：**有正向证据表明这次征询确实到达过 agent/用户眼前** ⇒ 冲销。
# 「通道坏着要退避」只对拿不到那份证据的那一支成立。
DECISION_POINT_WITHDRAWAL_CAUSES_THAT_REVERSE_MINT_REQUEST_CHARGE = frozenset(
    {
        DECISION_POINT_WITHDRAWAL_CAUSE_REWOUND_BY_USER,
        DECISION_POINT_WITHDRAWAL_CAUSE_OUT_OF_BAND_DELIVERY_BECAME_A_TURN_BUT_TOKEN_ADVANCE_FAILED,
        # 「根本没发出去」这一支：通道健康、重铸不会空转 ⇒ 退避前提不成立。理由见该常量 docstring。
        DECISION_POINT_WITHDRAWAL_CAUSE_STOP_HOOK_REENTRY_BUDGET_EXHAUSTED,
        # 「发出去了、但 agent 先复工」这一支：通道健康、只是时机不对；空转由 Stop 侧独立上界约束。
        DECISION_POINT_WITHDRAWAL_CAUSE_AGENT_RESUMED_WORK_BEFORE_DECISION_POINT_DELIVERY_LANDED,
    }
)

# 非终态且仍在决策进行中的活跃状态（去重、复活只认这三种）。
ACTIVE_NONTERMINAL_STATES = frozenset({STATE_MINTED, STATE_INJECTED, STATE_ACKNOWLEDGED})
# 决策已定的终态（dispatched=已派发，declined=已谢绝）。
TERMINAL_STATES = frozenset({STATE_DISPATCHED, STATE_DECLINED})
# 旁路终态（TTL 死亡 / 投递回执丢失 / 用户 rewind 撤回）。三者都不是「agent 做出的决策」。
BYPASS_STATES = frozenset({STATE_EXPIRED, STATE_LOST, STATE_WITHDRAWN})

# 合法迁移邻接表：key=当前状态，value=允许迁入的目标状态集合。
# acknowledged 自环 = start --retry 幂等重入（回退失败后再 start）。TTL 复活不是一次迁移
# （状态不变、只续期），故不出现在本表中，由 revive_decision_token 单独处理。
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_MINTED: frozenset(
        {STATE_INJECTED, STATE_ACKNOWLEDGED, STATE_DECLINED, STATE_EXPIRED, STATE_LOST, STATE_WITHDRAWN}
    ),
    STATE_INJECTED: frozenset(
        {STATE_ACKNOWLEDGED, STATE_DECLINED, STATE_EXPIRED, STATE_LOST, STATE_WITHDRAWN}
    ),
    # acknowledged → withdrawn 同样合法：用户可以在 agent 已经 ``start`` 之后才 rewind 掉横幅
    # （D7 明确要求「start 前 rewind」与「start 后 rewind」两种都覆盖）。
    STATE_ACKNOWLEDGED: frozenset(
        {
            STATE_ACKNOWLEDGED,
            STATE_DISPATCHED,
            STATE_DECLINED,
            STATE_EXPIRED,
            STATE_LOST,
            STATE_WITHDRAWN,
        }
    ),
    STATE_DISPATCHED: frozenset(),
    STATE_DECLINED: frozenset(),
    STATE_EXPIRED: frozenset(),
    STATE_LOST: frozenset(),
    STATE_WITHDRAWN: frozenset(),
}

# 每个目标状态对应的落盘时间戳字段名（进入该状态时打戳）。
_STATE_TIMESTAMP_FIELD: dict[str, str] = {
    STATE_INJECTED: "injected_at",
    STATE_ACKNOWLEDGED: "acknowledged_at",
    STATE_DISPATCHED: "dispatched_at",
    STATE_DECLINED: "declined_at",
    STATE_EXPIRED: "expired_at",
    STATE_LOST: "lost_at",
    STATE_WITHDRAWN: "withdrawn_at",
}

# 决策点横幅的两条投递通道（transport，**不是** lifecycle）。两侧共享同一份取值：Stop hook 用它
# 决定往哪投、token payload 用它记「这枚 token 当初是怎么投出去的」、TTL 也按它选。
DECISION_POINT_DELIVERY_TRANSPORT_STOP_HOOK_SELF_INJECTION = "stop_hook_self_injection"
DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION = "cline_kanban_task_chat_session"
DECISION_POINT_DELIVERY_TRANSPORTS = frozenset(
    {
        DECISION_POINT_DELIVERY_TRANSPORT_STOP_HOOK_SELF_INJECTION,
        DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION,
    }
)


#: 带外投递时决策点正文在 token payload 里的键。粘贴通道只送一行无指令的边界标记；正文在 mint 后、
#: 投递前写进这里，目标会话的 UPS hook 观测到标记时读出，以 ``additionalContext`` 注入。
DECISION_POINT_BANNER_BODY_TEXT_FOR_AGENT_PAYLOAD_KEY = "decision_point_banner_body_text_for_agent"


def render_decision_point_out_of_band_boundary_marker(*, token: str) -> str:
    """带外投递时终端粘贴通道唯一送出的那一行边界标记——Stop 渲染它、UPS 按它逐字识别的**唯一**真相源。

    文案为什么长这样见 Stop 侧 ``_render_decision_point_out_of_band_boundary_marker``（它委派到这里）。
    放在本模块是因为 UPS 必须按「与这一行逐字一致」认出标记，而 UPS 刻意不 import Stop 那个巨型引擎。
    """
    return f"RVF 发起决策点 · RVF_DECISION_TOKEN={token}（决策正文见随附的 hook 上下文；未随附即为重复提交，忽略本行）"


_DECISION_TOKEN_LITERAL_INSIDE_OUT_OF_BAND_BOUNDARY_MARKER_RE = re.compile(
    r"RVF_DECISION_TOKEN=([0-9A-Fa-f]{16})"
)


def decision_token_of_text_that_is_exactly_one_out_of_band_boundary_marker(text: str | None) -> str | None:
    """``text`` 去掉首尾空白后**逐字等于**某枚 token 的边界标记（见上）时返回该 token（小写），否则 None。

    只认整条独立的标记：机器投递与 rewind 重交送来的都是这一行本身。开发者在一条 prompt 里引用标记、
    再附上自己的话（粘日志 / handoff / 旧横幅）不是标记落地，一律返回 None（RVF-001）。host 的粘贴
    包裹不在这里剥——那是 host 行为，由调用方经 HostAdapter 剥掉后再传进来。
    """
    if not isinstance(text, str):
        return None
    stripped_text = text.strip()
    match = _DECISION_TOKEN_LITERAL_INSIDE_OUT_OF_BAND_BOUNDARY_MARKER_RE.search(stripped_text)
    if match is None:
        return None
    if stripped_text != render_decision_point_out_of_band_boundary_marker(token=match.group(1)):
        return None
    return match.group(1).lower()

#: 铸出这枚 token 的那次 Stop 事件所属 agent 回合的标识（host 提供：Claude Code 的 ``prompt_id``、
#: Codex 的 ``turn_id``；拿不到为 None）。之后任一 hook 事件带着**同一个**回合标识出现 ⇒ 铸 token 的
#: 那个回合在铸出之后又被续跑了（Stop 续跑 / 标记被并入那个回合）⇒ agent 复工。只有带外投递写它。
DECISION_TOKEN_PAYLOAD_KEY_AGENT_TURN_IDENTIFIER_OF_THE_STOP_EVENT_THAT_MINTED_THIS_TOKEN = (
    "agent_turn_identifier_of_the_stop_event_that_minted_this_token"
)

def decision_token_ttl_seconds_for_delivery_transport(transport: str | None) -> int:
    """按 transport 选 TTL：带外投递用 1200s，其余（含 None=同步自注入）沿用 900s。

    同步路径的 900s **逐字不动**——那个值对「同一个 turn 内决策」是对的，改它会连带影响手动触发
    与所有既有用例。
    """
    if transport == DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION:
        return DECISION_TOKEN_TTL_SECONDS_FOR_OUT_OF_BAND_DELIVERY
    return DEFAULT_DECISION_TOKEN_TTL_SECONDS


# mint 结果的五种结局。
MINT_OUTCOME_MINTED = "minted"
MINT_OUTCOME_DEDUPLICATED = "deduplicated"
MINT_OUTCOME_DECLINED_COOLDOWN = "declined_cooldown"
MINT_OUTCOME_DECLINED_SESSION_SCOPE_SNOOZE = "declined_session_scope_snooze"
# 闸 2c：本会话最近一次是**延后型**谢绝，且此后还没出现新回合 / HEAD 变化。
MINT_OUTCOME_DEFERRED_UNTIL_CONDITION_DECLINE_AWAITING_TURN_BOUNDARY = (
    "deferred_until_condition_decline_awaiting_turn_boundary"
)
MINT_OUTCOME_CIRCUIT_BREAKER_TRIPPED = "circuit_breaker_tripped"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class DecisionTokenError(ValueError):
    """决策 token 存储的基类异常。"""


class DecisionTokenStateError(DecisionTokenError):
    """非法状态迁移（如从终态再迁移、或跳过前置状态）。"""


class DecisionTokenWithdrawalPreconditionNotMetError(DecisionTokenStateError):
    """``mark_withdrawn`` 的调用方要求「锁内状态必须仍是某几种之一」，而锁内读到的不是（CAS 输了）。

    典型：锁外读到 ``minted`` 判定 agent 复工、要撤回，进锁时 UPS 已经抢先 ``minted → injected`` 把正文
    送到了 agent 眼前；或锁外读到 ``injected`` 判定 rewind 重交，进锁时 agent 已经 ``start``。此时
    ``… → withdrawn`` 在状态机上仍合法，但撤回的前提已经不成立，必须什么都不改。
    """


class DecisionTokenExpiredError(DecisionTokenError):
    """token 已过 TTL 且不可复活（无在途 run 锚点），无法继续推进状态。"""


class DecisionTokenNotFoundError(DecisionTokenError):
    """token 文件缺失或损坏，无法读取。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DecisionTokenRecord:
    """一枚落盘决策 token 的内存视图（token 值 + 文件路径 + 完整 payload）。"""

    token: str
    path: Path
    payload: dict[str, Any]

    @property
    def state(self) -> str:
        return str(self.payload.get("state"))


@dataclass(frozen=True)
class DecisionTokenLookup:
    """``read_decision_token`` 的读取结果。

    ``status`` 反映读取/TTL 结局：``valid`` / ``expired`` / ``missing`` / ``invalid_token`` /
    ``invalid_json`` / ``unreadable`` / ``invalid_payload`` / ``schema_mismatch`` /
    ``token_mismatch``。``state`` 是 payload 中记录的生命周期状态（与 TTL 正交）。
    ``revivable`` 表示「TTL 已过期但状态仍活跃且 run_dir 在途」——即可用 ``revive_decision_token`` 续期。
    """

    status: str
    token: str
    path: Path
    payload: dict[str, Any] | None = None
    state: str | None = None
    revivable: bool = False
    error: str | None = None


@dataclass(frozen=True)
class CircuitBreakerStatus:
    """铸造频率熔断的结构化结果，供 hook 生成 systemMessage 告警。"""

    tripped: bool
    request_count: int
    window_seconds: int
    max_requests: int
    session_id: str
    ledger_path: Path | None = None
    # 本次铸造请求写进 mint-request ledger 的那一行的稳定 id。它同时落进 token payload，
    # 使「撤回这枚 token」能追加一条冲销记录、把这份熔断额度还回去（INV-C）。
    mint_request_record_id: str | None = None


@dataclass(frozen=True)
class MintResult:
    """``mint_decision_token`` 的结构化返回。

    ``outcome`` ∈ {``minted``, ``deduplicated``, ``declined_cooldown``,
    ``declined_session_scope_snooze``, ``deferred_until_condition_decline_awaiting_turn_boundary``,
    ``circuit_breaker_tripped``}。仅 ``minted`` / ``deduplicated`` 携带可用 token/record；其余表示
    拒铸（``record`` 为 None），调用方据 ``message`` 出用户告警。
    """

    outcome: str
    token: str | None = None
    record: DecisionTokenRecord | None = None
    circuit_breaker: CircuitBreakerStatus | None = None
    message: str | None = None
    # 事实一（告知类）：「用户显式推翻了 agent 早前的谢绝」。仅在
    # ``initiated_by_explicit_user_request=True`` 且本次铸造确实**命中**了一条既有 declined 冷却
    # 记录时为 True——命中即已证成，与冷却记录能否被物理删除**无关**。调用方据此把该事实如实告知
    # 用户与 agent；不告知的话，agent 拿到 token 后极可能照上次判断再谢绝一次，用户白打一遍。
    explicit_user_request_overrode_agent_declined_cooldown: bool = False
    # 事实二（磁盘状态）：上面那次推翻之后冷却记录**仍留在盘上**（unlink 因 I/O 原因失败），
    # 于是后续**自动**路径依旧会被这条陈旧 decline 挡住。它只影响自动路径与面向用户的措辞，
    # 绝不参与「是否告知推翻」的判定。未发生推翻时恒为 False（无记录可挡）。
    declined_cooldown_record_still_blocks_automatic_path: bool = False
    # 闸 2b 的同构两事实。事实一：用户显式触发时本 ``(worktree, session_id)`` 确有一份 declined 会话级
    # snooze 快照——命中即证成「用户推翻了 agent 在本会话对这片工作范围的谢绝」。T3 之后这恰是最常见
    # 的被推翻形态（内容已变 ⇒ 闸二冷却不命中，只剩 snooze 挡着），只回报闸二字段会让 agent 收不到告知。
    explicit_user_request_overrode_agent_declined_session_scope_snooze: bool = False
    # 事实二：那份快照删不掉（I/O 失败），后续**自动**路径仍会被它静默。同样绝不参与告知判定。
    declined_session_scope_snooze_record_still_blocks_automatic_path: bool = False
    # 闸 2c 的告知类事实：用户显式触发时本 ``(worktree, session_id)`` 有一条延后型谢绝记录（agent 说过
    # 「等 X 之后再审」）。用户显式触发即「现在就审」，调用方据此告知 agent 别再以同一条件延后。
    explicit_user_request_overrode_agent_deferred_until_condition_decline: bool = False
    # 被推翻的那条延后理由（原文），供告知文案引用；无推翻时为 None。
    overridden_deferred_until_condition_decline_reason: str | None = None


# ---------------------------------------------------------------------------
# 时间戳工具（与 rvf_prep_file 保持一致的 UTC ISO-8601「Z」格式）
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(timezone.utc)


def format_timestamp(value: datetime) -> str:
    """把 datetime 归一化为 UTC ISO-8601、尾随 ``Z`` 的字符串。"""
    normalized = value.astimezone(timezone.utc)
    if normalized.microsecond:
        return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    """解析 ISO-8601（含尾随 ``Z``）时间戳为带 UTC 时区的 datetime。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_now(now: datetime | None) -> datetime:
    return (now or utc_now()).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# token 值工具
# ---------------------------------------------------------------------------

def generate_token() -> str:
    """生成一枚 16 位十六进制随机 token。"""
    return secrets.token_hex(8)


def validate_token(token: str) -> str:
    """校验并归一化 token（16 位小写十六进制），非法则抛 ``DecisionTokenError``。"""
    normalized = token.strip().lower()
    if not TOKEN_RE.fullmatch(normalized):
        raise DecisionTokenError(f"非法决策 token: {token!r}")
    return normalized


# ---------------------------------------------------------------------------
# 落盘根目录解析
# ---------------------------------------------------------------------------

def _rvf_home() -> Path:
    """RVF 状态根目录：``$RVF_HOME`` 或 ``~/.rvf``。"""
    configured = os.environ.get(ENV_RVF_HOME)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".rvf"


def _resolve_root(explicit: str | Path | None, env_name: str, dirname: str) -> Path:
    """通用根目录解析：显式参数 > 专用环境变量 > ``$RVF_HOME``/``~/.rvf`` 下的固定子目录。"""
    if explicit is not None:
        return Path(explicit).expanduser()
    configured = os.environ.get(env_name)
    if configured:
        return Path(configured).expanduser()
    return _rvf_home() / dirname


def decision_token_root(root: str | Path | None = None) -> Path:
    """决策 token 落盘根目录。"""
    return _resolve_root(root, ENV_DECISION_TOKEN_ROOT, DECISION_TOKEN_DIRNAME)


def declined_fingerprint_root(root: str | Path | None = None) -> Path:
    """declined 指纹冷却表根目录。"""
    return _resolve_root(root, ENV_DECLINED_FINGERPRINT_ROOT, DECLINED_FINGERPRINT_DIRNAME)


def declined_session_scope_snooze_root(cooldown_root: str | Path | None = None) -> Path:
    """declined 会话级 snooze 根（declined 冷却根下的固定子目录，见常量处说明）。"""
    return declined_fingerprint_root(cooldown_root) / DECLINED_SESSION_SCOPE_SNOOZE_SUBDIRNAME


def circuit_breaker_root(root: str | Path | None = None) -> Path:
    """铸造频率熔断 ledger 根目录。"""
    return _resolve_root(root, ENV_CIRCUIT_BREAKER_ROOT, CIRCUIT_BREAKER_DIRNAME)


def decision_token_path(token: str, root: str | Path | None = None) -> Path:
    """给定 token 的落盘文件路径。"""
    return decision_token_root(root) / f"{validate_token(token)}.json"


# ---------------------------------------------------------------------------
# 原子落盘工具（O_EXCL 创建 / replace 覆盖 / append），复用 rvf_prep_file 家族的语义
# ---------------------------------------------------------------------------

def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass


def _write_json_tmp(path: Path, payload: dict[str, Any]) -> Path:
    _ensure_parent_dir(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        return tmp
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _atomic_create_json(path: Path, payload: dict[str, Any]) -> None:
    """O_EXCL 语义原子创建：目标已存在则抛 ``FileExistsError``（作为去重竞态互斥用）。"""
    tmp = _write_json_tmp(path, payload)
    try:
        os.link(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    tmp.unlink()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """replace 语义原子覆盖：目标存在则整份替换。"""
    tmp = _write_json_tmp(path, payload)
    try:
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """向 jsonl ledger 追加一行（0o600 文件）。"""
    _ensure_parent_dir(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fd = -1
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    finally:
        if fd >= 0:
            os.close(fd)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 jsonl ledger 全部有效行（坏行跳过，best-effort）。"""
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


def _digest(*parts: str) -> str:
    """把若干字段拼接后取 sha256 十六进制摘要（用作安全文件名/去重键哈希）。"""
    joined = _KEY_FIELD_SEPARATOR.join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 每 token 的 payload 读-改-写排他锁（整份覆盖之间的互斥）
# ---------------------------------------------------------------------------
#
# **为什么必须有**：本模块所有写侧都是「读整份 payload → 改 dict 副本 → ``_atomic_write_json``
# 整份覆盖」。两个持有各自陈旧副本的写侧交错时，后写的那次会把先写的那次**整份抹掉**——这不是
# 纸上推演的竞态，两条真实链路本就并发：
#
#   * Stop hook 侧：``start_cline_kanban_followup_message`` 返回后调
#     ``record_out_of_band_delivery_status`` 落投递回执；
#   * 目标会话 UserPromptSubmit hook 侧：横幅一旦落成 user message 就调 ``mark_injected``
#     把 ``minted → injected``。
#
# 回执为 ``delivered_and_submit_confirmed`` 时这个交错**几乎必然发生**——该回执的语义就是
# 「消息已确认进入 agent」，也就是 UPS 那一侧正在跑。回执线程若后写，``state`` 被旧快照回滚成
# ``minted``、``injected_at`` 与 history 条目一并丢失；随后投递意图对账
# （``reconcile_stranded_decision_point_delivery_intents``）把一份**确实送达且已被 agent 看到**
# 的横幅判成搁浅并 ``mark_withdrawn``，``rvf-agent start`` 硬失败。**直接破坏 INV-A。**
#
# 互斥用 token 文件旁的独立 flock 锁文件。
# 锁文件名以 ``.`` 开头且以 ``.lock`` 结尾，故 ``sweep_stale_decision_tokens`` 的 ``*.json``
# 扫描与测试里的 token 计数都看不见它。铁律：**读与写必须在同一把锁内**——只调换语句顺序不构成
# 互斥，只锁住写入同样不构成互斥（陈旧副本仍会被整份写回去）。
DECISION_TOKEN_PAYLOAD_MUTATION_LOCK_SUFFIX = ".payload-mutation.lock"

# flock 以「打开文件描述」为单位：同一进程用第二个 fd 再 flock 同一文件会把自己锁死。故按线程
# 记持有深度，实现**同线程可重入**——外层公开函数持锁后，内层 ``_apply_lifecycle_transition``
# 再进同一把锁时只加深度、不再 open。不同线程各自 open 自己的描述，flock 仍然互斥。
_decision_token_payload_mutation_lock_state_per_thread = threading.local()


def decision_token_payload_mutation_lock_path(token_path: Path) -> Path:
    """给定 token 文件路径，返回其 payload 读-改-写锁文件路径。"""
    return token_path.with_name(
        f".{token_path.name}{DECISION_TOKEN_PAYLOAD_MUTATION_LOCK_SUFFIX}"
    )


def _decision_token_path_for_payload_mutation_lock(
    token: str, root: str | Path | None
) -> Path | None:
    """算出待加锁的 token 文件路径；token 非法则返回 None。

    刻意**不**在这里抛异常：非法 token 的既有语义由随后的读取（``_read_record_any`` /
    ``_load_active_record`` 抛 ``DecisionTokenNotFoundError``）负责，加锁这一步不该抢先改变
    调用方看到的异常类型。
    """
    try:
        return decision_token_path(token, root)
    except DecisionTokenError:
        return None


@contextmanager
def _with_decision_token_payload_mutation_lock(token_path: Path | None):
    """持有某枚 token 的 payload 读-改-写排他锁；``token_path=None`` 或文件系统报错即降级为无锁。

    降级是刻意的 best-effort：拿不到锁文件不该把决策链路整条堵死（与本模块「记账永不阻断决策」
    一致），降级后的行为与加锁前逐字相同（即退回到原来的竞态窗口，而不是新的失败模式）。
    """
    if token_path is None:
        yield
        return
    lock_path = decision_token_payload_mutation_lock_path(token_path)
    lock_key = str(lock_path)
    depth_per_lock_key = getattr(
        _decision_token_payload_mutation_lock_state_per_thread, "depth_per_lock_key", None
    )
    if depth_per_lock_key is None:
        depth_per_lock_key = {}
        _decision_token_payload_mutation_lock_state_per_thread.depth_per_lock_key = (
            depth_per_lock_key
        )
    if depth_per_lock_key.get(lock_key, 0) > 0:
        # 同线程重入：外层已经持有这把锁，只加深度。
        depth_per_lock_key[lock_key] += 1
        try:
            yield
        finally:
            depth_per_lock_key[lock_key] -= 1
        return
    try:
        _ensure_parent_dir(lock_path)
        fd = os.open(lock_key, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield
        return
    handle = os.fdopen(fd, "w", encoding="utf-8")
    acquired_flock = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            acquired_flock = True
        except OSError:
            acquired_flock = False
        depth_per_lock_key[lock_key] = 1
        try:
            yield
        finally:
            depth_per_lock_key.pop(lock_key, None)
    finally:
        if acquired_flock:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _reload_token_payload_from_disk_or_keep(record: DecisionTokenRecord) -> dict[str, Any]:
    """锁内重读 token 文件拿最新整份 payload；读不到 / 结构不符即沿用传入副本。

    这是「读+写同锁」的最后一道保险：即便某个调用方在锁**之外**读出了 record，落盘那一刻用的
    也是锁内重读的最新整份 payload，不会把别人刚写进去的 ``state`` / ``history`` 抹掉。
    """
    try:
        reloaded = json.loads(record.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return dict(record.payload)
    if not isinstance(reloaded, dict):
        return dict(record.payload)
    if reloaded.get("token") != record.payload.get("token"):
        return dict(record.payload)
    return reloaded


# ---------------------------------------------------------------------------
# run_dir 复活锚点（参照 rvf_user_prompt_submit._run_artifacts_present / FU-1 方案 B）
# ---------------------------------------------------------------------------

def _run_anchor_present(payload: dict[str, Any] | None) -> bool:
    """best-effort：token payload 锚定的 run_dir 是否仍在磁盘（run 在途）。

    只复活 run_dir 仍存在的过期 token，不复活已被清理 / 从未开工的过期 token。任何结构异常或
    OSError → False（保守不复活）。
    """
    if not isinstance(payload, dict):
        return False
    run_dir = payload.get("run_anchor_dir")
    if not isinstance(run_dir, str) or not run_dir:
        return False
    try:
        return Path(run_dir).is_dir()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def read_decision_token(
    token: str,
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> DecisionTokenLookup:
    """读取一枚决策 token，返回其读取/TTL 结局与生命周期状态。

    对已过 TTL 的文件仍带回 payload 与 ``state``（供复活/旁路 mark 使用），并计算
    ``revivable``（TTL 过期 + 状态活跃 + run_dir 在途）。
    """
    try:
        normalized_token = validate_token(token)
    except DecisionTokenError as exc:
        return DecisionTokenLookup(status="invalid_token", token=token, path=decision_token_root(root), error=str(exc))

    path = decision_token_path(normalized_token, root)
    if not path.is_file():
        return DecisionTokenLookup(status="missing", token=normalized_token, path=path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return DecisionTokenLookup(status="invalid_json", token=normalized_token, path=path, error=str(exc))
    except OSError as exc:
        return DecisionTokenLookup(status="unreadable", token=normalized_token, path=path, error=str(exc))
    if not isinstance(payload, dict):
        return DecisionTokenLookup(status="invalid_payload", token=normalized_token, path=path, error="token 文件根须为对象")
    if payload.get("schema_version") != SCHEMA_VERSION:
        return DecisionTokenLookup(status="schema_mismatch", token=normalized_token, path=path, payload=payload)
    if payload.get("token") != normalized_token:
        return DecisionTokenLookup(status="token_mismatch", token=normalized_token, path=path, payload=payload)

    state = payload.get("state")
    expires_raw = payload.get("expires_at")
    if not isinstance(expires_raw, str):
        return DecisionTokenLookup(status="invalid_payload", token=normalized_token, path=path, payload=payload, error="缺 expires_at")
    try:
        expires_at = parse_timestamp(expires_raw)
    except ValueError as exc:
        return DecisionTokenLookup(status="invalid_payload", token=normalized_token, path=path, payload=payload, error=str(exc))

    if expires_at <= _coerce_now(now):
        revivable = state in ACTIVE_NONTERMINAL_STATES and _run_anchor_present(payload)
        return DecisionTokenLookup(
            status="expired", token=normalized_token, path=path, payload=payload, state=state, revivable=revivable
        )
    return DecisionTokenLookup(
        status="valid", token=normalized_token, path=path, payload=payload, state=state, revivable=False
    )


def _record_from_lookup(lookup: DecisionTokenLookup) -> DecisionTokenRecord:
    assert lookup.payload is not None
    return DecisionTokenRecord(token=lookup.token, path=lookup.path, payload=lookup.payload)


def _read_record_any(token: str, *, root: str | Path | None, now: datetime | None) -> DecisionTokenRecord:
    """读取 token 记录（含 TTL 已过期者），仅在缺失/损坏时抛 ``DecisionTokenNotFoundError``。

    供旁路终态标注（mark_expired / mark_lost）使用——它们本就作用于 TTL 死亡的 token。
    """
    lookup = read_decision_token(token, root=root, now=now)
    if lookup.status in ("valid", "expired") and lookup.payload is not None:
        return _record_from_lookup(lookup)
    raise DecisionTokenNotFoundError(f"无法读取决策 token {lookup.token}：status={lookup.status}")


def _load_active_record(
    token: str,
    *,
    root: str | Path | None,
    now: datetime | None,
    ttl_seconds: int,
) -> DecisionTokenRecord:
    """读取一枚仍可推进的 token：TTL 有效直接返回；TTL 过期但可复活则先复活再返回；否则抛
    ``DecisionTokenExpiredError`` / ``DecisionTokenNotFoundError``。
    """
    lookup = read_decision_token(token, root=root, now=now)
    if lookup.status == "valid":
        return _record_from_lookup(lookup)
    if lookup.status == "expired":
        if lookup.revivable:
            return revive_decision_token(lookup, root=root, now=now, ttl_seconds=ttl_seconds)
        raise DecisionTokenExpiredError(f"决策 token {lookup.token} 已过 TTL 且无在途 run 锚点，无法复活")
    raise DecisionTokenNotFoundError(f"无法读取决策 token {lookup.token}：status={lookup.status}")


# ---------------------------------------------------------------------------
# 状态迁移引擎
# ---------------------------------------------------------------------------

def _ensure_lifecycle_transition_is_legal(
    current_state: str, target_state: str, *, token: str
) -> None:
    """按 ``LEGAL_TRANSITIONS`` 邻接表前验一次迁移是否合法，非法抛 ``DecisionTokenStateError``。

    单独拆出来是为了让「有副作用的迁移」（如 ``mark_withdrawn`` 要先删去重索引、先冲销熔断额度）
    能在**动任何东西之前**先把合法性验掉：非法迁移必须什么都不改。
    """
    allowed = LEGAL_TRANSITIONS.get(current_state, frozenset())
    if target_state not in allowed:
        raise DecisionTokenStateError(
            f"非法状态迁移: {current_state} → {target_state}（token={token}；"
            f"当前状态允许迁往 {sorted(allowed) or '无（终态）'}）"
        )


def _apply_lifecycle_transition(
    record: DecisionTokenRecord,
    target_state: str,
    *,
    now: datetime,
    extra_updates: dict[str, Any] | None = None,
) -> DecisionTokenRecord:
    """校验并落盘一次生命周期状态迁移；非法迁移抛 ``DecisionTokenStateError``。

    合法性以 ``LEGAL_TRANSITIONS`` 邻接表为准。落盘时打对应状态时间戳、追加一条 history。

    整段（重读 + 校验 + 整份覆盖）都在该 token 的
    ``_with_decision_token_payload_mutation_lock`` 内完成，与 ``record_out_of_band_delivery_status``
    这类正交字段写入**互斥**。锁可重入，故公开入口先持锁再读 record 也不会自锁死；而锁内那次
    ``_reload_token_payload_from_disk_or_keep`` 保证即便 record 是锁外读出的陈旧副本，写回去的
    也是最新整份 payload。
    """
    with _with_decision_token_payload_mutation_lock(record.path):
        payload = _reload_token_payload_from_disk_or_keep(record)
        current = str(payload.get("state"))
        _ensure_lifecycle_transition_is_legal(current, target_state, token=record.token)
        payload["state"] = target_state
        stamp_field = _STATE_TIMESTAMP_FIELD.get(target_state)
        if stamp_field:
            payload[stamp_field] = format_timestamp(now)
        history = list(payload.get("history") or [])
        history.append({"from": current, "to": target_state, "at": format_timestamp(now)})
        payload["history"] = history
        if extra_updates:
            payload.update(extra_updates)
        _atomic_write_json(record.path, payload)
    return DecisionTokenRecord(token=record.token, path=record.path, payload=payload)


# ---------------------------------------------------------------------------
# 去重索引（key = (worktree, session_id, candidate_fingerprint)）
# ---------------------------------------------------------------------------

def _dedup_index_root(root: str | Path | None) -> Path:
    return decision_token_root(root) / "dedup-index"


def _dedup_index_path(worktree: str, session_id: str, candidate_fingerprint: str, root: str | Path | None) -> Path:
    key_hash = _digest(worktree, session_id, candidate_fingerprint)
    return _dedup_index_root(root) / f"{key_hash}.json"


def _read_dedup_index(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _clear_dedup_index_for_token_payload(
    payload: dict[str, Any] | None, *, root: str | Path | None
) -> Path | None:
    """把某枚 token 对应的去重索引条目移除（返回被删路径；无索引/删不掉一律 None）。

    仅在 ``withdrawn`` 这一支使用：用户 rewind 掉征询后，去重索引若还活着，下一次 Stop 会
    命中 ``deduplicated`` 而静默停——**用户刚刚刻意回退掉的征询会把整个 TTL 窗口堵死**。
    删索引前先核对它确实指向本 token，避免误删一枚更新的 token 的索引。

    **残留（如实记录，本轮判定可接受）**：这个「先读索引确认指向本 token、再 unlink」不是原子
    CAS。每 token 的 ``_with_decision_token_payload_mutation_lock`` 锁的是**本 token 的文件**，
    而去重索引文件是按 ``(worktree, session_id, candidate_fingerprint)`` 哈希命名、由同键的
    **任意** token 共享的，故本锁覆盖不到它。最坏情况是多删或少删一个索引条目：多删 → 下一次
    Stop 重新征询一次（``mint`` 会重建索引）；少删 → 索引指向的 token 已是 withdrawn 终态，
    ``_dedup_active_lookup`` 判它非活跃，下一轮 mint 会在占坑锁内把它顶掉。两个方向都会被下一轮
    mint 收敛，不会永久卡死。

    **这里刻意不加 :func:`_with_dedup_index_mint_claim_exclusive_lock`**（哪怕它正好锁的就是这个
    索引文件）：本函数是在 ``mark_withdrawn`` 的**每 token payload 锁内**被调用的，在此再取占坑锁
    会形成「payload 锁 → 占坑锁」的持有顺序，而铸造侧是「占坑锁 → payload 锁」（锁内
    ``_dedup_active_lookup`` 可能触发 :func:`revive_decision_token`）——两者相反即构成经典死锁环。
    上面那点收敛得掉的多删/少删，比死锁便宜得多。
    """
    if not isinstance(payload, dict):
        return None
    worktree = payload.get("worktree")
    session_id = payload.get("session_id")
    fingerprint = payload.get("candidate_fingerprint")
    token = payload.get("token")
    if not (isinstance(worktree, str) and isinstance(fingerprint, str) and isinstance(token, str)):
        return None
    index_path = _dedup_index_path(worktree, str(session_id or ""), fingerprint, root)
    existing = _read_dedup_index(index_path)
    if existing is None or existing.get("token") != token:
        return None
    try:
        index_path.unlink()
    except OSError:
        return None
    return index_path


def _dedup_active_lookup(
    index_payload: dict[str, Any],
    *,
    root: str | Path | None,
    now: datetime | None,
    ttl_seconds: int,
) -> DecisionTokenRecord | None:
    """给定去重索引指向的 token，若其仍活跃（非终态、TTL 有效或可复活）则返回可用 record，
    否则返回 None（索引已陈旧）。可复活者就地复活后返回。
    """
    token = index_payload.get("token")
    if not isinstance(token, str):
        return None
    lookup = read_decision_token(token, root=root, now=now)
    if lookup.state not in ACTIVE_NONTERMINAL_STATES:
        return None
    if lookup.status == "valid":
        return _record_from_lookup(lookup)
    if lookup.status == "expired" and lookup.revivable:
        return revive_decision_token(lookup, root=root, now=now, ttl_seconds=ttl_seconds)
    return None


# ---------------------------------------------------------------------------
# 同键去重索引的「铸造占坑」排他锁（同一 dedup key 至多一枚活跃 token）
# ---------------------------------------------------------------------------
#
# **为什么必须有**：铸造侧对去重索引做的是「读索引 → 判它指向的 token 还活不活跃 → 写索引」这样
# 一段读-改-写，而闸一那次读发生在铸造**之前**、且不在任何锁内。同键的两个请求完全可能双双读到
# 「索引陈旧」（指向的 token 已终态 / 真过期不可复活），于是各自 ``_create_token_record`` 铸出
# **不同**的 token，再各自把索引整份覆盖成自己，双双返回 ``minted``——两枚活跃 token 指向同一个
# dedup key，同一批改动被重复注入 / 重复发起 RVF，直接违反 R25 的注入幂等硬保证。
#
# 这条竞态在自动路径（Stop hook，token 自然过期后）与手动路径（UPS 清掉 declined 冷却后继续下铸）
# 上都可达。修复前首建路径靠 ``_atomic_create_json`` 的 O_EXCL 有内核级互斥，覆盖陈旧索引的路径
# 却是**无条件覆盖**、什么互斥都没有，两条路径不对称。这把锁把「重读索引 → 判活跃 → 写索引」整段
# 圈成一个临界区，让首建与覆盖走**同一条**互斥路径：至多一方写成索引并返回 ``minted``，其余方丢弃
# 自己刚铸的 token 并幂等返回胜者（``deduplicated``）。
#
# 锁文件与索引文件同目录（落盘根随 ``root`` 参数 / ``$RVF_HOME``，测试靠显式根隔离，绝不写真实
# ``~/.rvf``），名字以 ``.`` 开头并以 ``.lock`` 结尾，因此进不了本模块任何 ``*.json`` 扫描。
#
# **陈旧回收：不需要，且这是刻意选型。** 互斥由 ``flock`` 提供，锁的所有权绑在「打开文件描述」上
# ——持有者中途崩溃 / 被硬杀 / 进程退出时内核自动释放，不存在「崩溃后残留把同键铸造永久堵死」的
# 形态，故既不需要 TTL 窗口也不需要接管逻辑（与文件占位
# 占位 sidecar 相反：那种占位靠文件**存在性**互斥，才必须自带过窗接管）。磁盘上留下的只是一个空
# 锁文件，下一次同键铸造直接复用同一个 inode。
DEDUP_INDEX_MINT_CLAIM_LOCK_SUFFIX = ".dedup-index-mint-claim.lock"


def dedup_index_mint_claim_lock_path(index_path: Path) -> Path:
    """给定去重索引文件路径，返回其「铸造占坑」锁文件路径。"""
    return index_path.with_name(f".{index_path.name}{DEDUP_INDEX_MINT_CLAIM_LOCK_SUFFIX}")


@contextmanager
def _with_dedup_index_mint_claim_exclusive_lock(index_path: Path):
    """持有某个 dedup key 的铸造占坑排他锁；文件系统报错即降级为无锁（best-effort）。

    降级与 :func:`_with_decision_token_payload_mutation_lock` 同款且同理由：拿不到锁文件不该把
    决策链路整条堵死，降级后的行为**逐字退回**到加锁前的竞态窗口，而不是变成一种新的失败模式。
    """
    lock_path = dedup_index_mint_claim_lock_path(index_path)
    try:
        _ensure_parent_dir(lock_path)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield
        return
    handle = os.fdopen(fd, "w", encoding="utf-8")
    acquired_flock = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            acquired_flock = True
        except OSError:
            acquired_flock = False
        yield
    finally:
        if acquired_flock:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


# ---------------------------------------------------------------------------
# 铸造侧频率熔断（R24 裁决 b）：纯计数
# ---------------------------------------------------------------------------

def _mint_request_ledger_path(session_id: str, root: str | Path | None) -> Path:
    return circuit_breaker_root(root) / "mint-requests" / f"{_digest(session_id)}.jsonl"


def _tripped_ledger_path(session_id: str, root: str | Path | None) -> Path:
    return circuit_breaker_root(root) / "circuit-breaker-tripped" / f"{_digest(session_id)}.jsonl"


def count_recent_mint_requests(
    session_id: str,
    *,
    now: datetime | None = None,
    window_seconds: int = DEFAULT_MINT_CIRCUIT_BREAKER_WINDOW_SECONDS,
    root: str | Path | None = None,
) -> int:
    """统计给定 session 在 ``[now - window, now]`` 滚动窗口内**仍占用熔断额度**的铸造请求条数。

    纯计数，零语义判断，但要扣掉两类行：

      * **冲销记录本身**（带 ``reverses_mint_request_record_id``）不是一次铸造请求；
      * **已被冲销的铸造请求**——token 进 ``withdrawn`` 时会向同一 ledger 追加一条冲销记录。
        INV-C 要求撤回「不计冷却也不计熔断」：用户刚刻意 rewind 掉的那次征询，不该反过来把接
        下来一整个窗口（默认 3600s）堵死。冲销刻意做成**追加行**而不是删既有行，ledger 保持
        append-only 的可审计性。
    """
    ledger = _mint_request_ledger_path(session_id, root)
    ledger_records = _read_jsonl(ledger)
    reversed_mint_request_record_ids = {
        record[MINT_REQUEST_LEDGER_CHARGE_REVERSAL_FIELD]
        for record in ledger_records
        if isinstance(record.get(MINT_REQUEST_LEDGER_CHARGE_REVERSAL_FIELD), str)
    }
    horizon = _coerce_now(now) - timedelta(seconds=window_seconds)
    count = 0
    for record in ledger_records:
        if isinstance(record.get(MINT_REQUEST_LEDGER_CHARGE_REVERSAL_FIELD), str):
            continue
        mint_request_record_id = record.get(MINT_REQUEST_LEDGER_RECORD_ID_FIELD)
        if (
            isinstance(mint_request_record_id, str)
            and mint_request_record_id in reversed_mint_request_record_ids
        ):
            continue
        raw = record.get("at")
        if not isinstance(raw, str):
            continue
        try:
            at = parse_timestamp(raw)
        except ValueError:
            continue
        if at >= horizon:
            count += 1
    return count


def _reverse_mint_request_charge_for_withdrawn_token(
    payload: dict[str, Any],
    *,
    reason: str,
    now: datetime,
    circuit_breaker_root_dir: str | Path | None,
) -> str | None:
    """向 mint-request ledger **追加**一条冲销记录，把这枚 token 铸造时占用的熔断额度还回去。

    只在 ``mark_withdrawn`` 这一支调用（INV-C：撤回不计冷却、也**不计熔断**）。返回被冲销的
    ``mint_request_record_id``；没有可冲销的额度占用（去重命中返回的既有 token、或本字段落地
    之前铸出的老 token）即返回 None，不留噪声记录。

    追加而非回改/删行：ledger 是 append-only 的可审计记录，删行会毁掉「这个 session 到底请求过
    几次铸造」的证据。扣减发生在 ``count_recent_mint_requests`` 的统计侧。
    """
    mint_request_record_id = payload.get(MINT_REQUEST_LEDGER_RECORD_ID_FIELD)
    session_id = payload.get("session_id")
    if not isinstance(mint_request_record_id, str) or not mint_request_record_id:
        return None
    if not isinstance(session_id, str):
        return None
    try:
        _append_jsonl(
            _mint_request_ledger_path(session_id, circuit_breaker_root_dir),
            {
                MINT_REQUEST_LEDGER_CHARGE_REVERSAL_FIELD: mint_request_record_id,
                "at": format_timestamp(now),
                "reversed_because": reason,
                "token": payload.get("token"),
            },
        )
    except OSError:
        return None
    return mint_request_record_id


def _register_mint_request_and_check_breaker(
    *,
    session_id: str,
    worktree: str,
    candidate_fingerprint: str,
    now: datetime,
    window_seconds: int,
    max_requests: int,
    root: str | Path | None,
) -> CircuitBreakerStatus:
    """记录本次铸造请求并判断是否熔断。

    先向 mint-request ledger 追加本次请求（含当前请求本身参与计数），再统计窗口内条数；
    达到 ``max_requests`` 即熔断，向 circuit-breaker-tripped ledger 追加一条并返回 tripped 状态。

    追加的那一行带一个稳定的 ``mint_request_record_id`` 并随 ``CircuitBreakerStatus`` 带回；
    调用方把它写进 token payload，使这枚 token 被 ``mark_withdrawn`` 撤回时能追加一条冲销记录
    把额度还回去（INV-C）。

    **被熔断拒掉的请求当场冲销自己那一份**：它没有铸出任何 token，不是一次征询。不冲销的话，熔断
    会自我续命——只要 Stop 间隔短于「窗口 / 阈值」（默认约 20 分钟），每次被拒的请求都把窗口内
    计数顶在阈值以上，熔断永不解除。实例：会话 893079be 在 09-18 11:03–13:33 的 11 个 Stop 全被
    熔断（计数 3→7），期间没有任何递归发起；该会话里「唯一看见它的 Stop 被熔断拦下、随后落出
    窗口」的已提交工作共约 8,700 行，从未经审。冲销后熔断仍把「滚动窗口内真正铸出的 token」限制在
    阈值以下。
    """
    mint_request_record_id = f"mint-request-{secrets.token_hex(8)}"
    ledger = _mint_request_ledger_path(session_id, root)
    _append_jsonl(
        ledger,
        {
            MINT_REQUEST_LEDGER_RECORD_ID_FIELD: mint_request_record_id,
            "at": format_timestamp(now),
            "worktree": worktree,
            "candidate_fingerprint": candidate_fingerprint,
        },
    )
    count = count_recent_mint_requests(session_id, now=now, window_seconds=window_seconds, root=root)
    if count >= max_requests:
        try:
            _append_jsonl(
                ledger,
                {
                    MINT_REQUEST_LEDGER_CHARGE_REVERSAL_FIELD: mint_request_record_id,
                    "at": format_timestamp(now),
                    "reversed_because": "circuit_breaker_refused_this_mint_request_so_no_token_was_minted",
                    "token": None,
                },
            )
        except OSError:
            pass
        tripped_ledger = _tripped_ledger_path(session_id, root)
        _append_jsonl(
            tripped_ledger,
            {
                "at": format_timestamp(now),
                "session_id": session_id,
                "worktree": worktree,
                "candidate_fingerprint": candidate_fingerprint,
                "request_count": count,
                "window_seconds": window_seconds,
                "max_requests": max_requests,
            },
        )
        return CircuitBreakerStatus(
            tripped=True,
            request_count=count,
            window_seconds=window_seconds,
            max_requests=max_requests,
            session_id=session_id,
            ledger_path=tripped_ledger,
            mint_request_record_id=mint_request_record_id,
        )
    return CircuitBreakerStatus(
        tripped=False,
        request_count=count,
        window_seconds=window_seconds,
        max_requests=max_requests,
        session_id=session_id,
        ledger_path=None,
        mint_request_record_id=mint_request_record_id,
    )


# ---------------------------------------------------------------------------
# declined 指纹冷却表
# ---------------------------------------------------------------------------

def _declined_fingerprint_path(worktree: str, candidate_fingerprint: str, root: str | Path | None) -> Path:
    key_hash = _digest(worktree, candidate_fingerprint)
    return declined_fingerprint_root(root) / f"{key_hash}.json"


def record_declined_fingerprint(
    *,
    worktree: str,
    candidate_fingerprint: str,
    reason: str | None = None,
    token: str | None = None,
    now: datetime | None = None,
    cooldown_root: str | Path | None = None,
) -> Path:
    """把 ``(worktree, candidate_fingerprint)`` 写入 declined 冷却表（decline 时调用，幂等覆盖）。

    冷却语义为「指纹未变即持续冷却」：无 TTL，直到候选文件集变化导致指纹变化才不再命中。
    """
    path = _declined_fingerprint_path(worktree, candidate_fingerprint, cooldown_root)
    _atomic_write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "worktree": worktree,
            "candidate_fingerprint": candidate_fingerprint,
            "declined_at": format_timestamp(_coerce_now(now)),
            "reason": reason,
            "token": token,
        },
    )
    return path


def is_fingerprint_in_declined_cooldown(
    *,
    worktree: str,
    candidate_fingerprint: str,
    cooldown_root: str | Path | None = None,
) -> bool:
    """查询 ``(worktree, candidate_fingerprint)`` 是否处于 declined 冷却中（供摘要生成器「指纹未变不再注入」）。"""
    return _declined_fingerprint_path(worktree, candidate_fingerprint, cooldown_root).is_file()


def read_declined_fingerprint(
    *,
    worktree: str,
    candidate_fingerprint: str,
    cooldown_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """读取 declined 冷却记录的完整 payload（不存在或损坏返回 None）。"""
    path = _declined_fingerprint_path(worktree, candidate_fingerprint, cooldown_root)
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return parsed if isinstance(parsed, dict) else None


def clear_declined_fingerprint(
    *,
    worktree: str,
    candidate_fingerprint: str,
    cooldown_root: str | Path | None = None,
) -> bool:
    """从冷却表移除某指纹（返回是否真的删除了文件）。"""
    path = _declined_fingerprint_path(worktree, candidate_fingerprint, cooldown_root)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# ---------------------------------------------------------------------------
# declined 会话级 snooze（闸 2b）
# ---------------------------------------------------------------------------
#
# 为什么需要它：上面的冷却表按**内容指纹**记账，而工作还在进行时两次 Stop 之间几乎总有字节改动，
# 于是每次 Stop 都成了「新候选」——实测 75 次谢绝里有 24 次是同一会话的重复谢绝，最长一段跨 8 天
# 12 次，铸造熔断（3 次 / 会话 / 小时）对这种低频长尾完全无感。修内容指纹无解（内容本来就在变），
# 所以另记一份**会话级、粗粒度**的快照：agent 在这个会话里对这片工作范围说过「不」，那么直到出现
# 真正的工作边界之前，自动路径都不再问第二次。
#
# 键：``(worktree, session_id)``。值：decline 那一刻的工作范围身份（由候选摘要模块给出的
# ``candidate_scope_identity_for_declined_session_snooze`` 子字典，本模块只按字段读、不重算）。
# 解除条件见常量区的 EXPERIMENT_NODE。


@dataclass(frozen=True)
class DeclinedSessionScopeSnoozeConsultation:
    """闸 2b 对一次自动铸造请求的裁决。

    ``still_snoozed=True`` ⇒ 拒铸。``release_reason`` 非 None ⇒ 本次发现 snooze 已满足解除条件并
    已把它清掉（清除失败也照样放行——解除条件成立是事实，文件残留只会让下次再判一次同样的结论）。
    """

    still_snoozed: bool
    release_reason: str | None = None
    snooze_record: dict[str, Any] | None = None


def _declined_session_scope_snooze_session_dir(
    session_id: str, cooldown_root: str | Path | None
) -> Path:
    return declined_session_scope_snooze_root(cooldown_root) / _digest(session_id)


def _declined_session_scope_snooze_path(
    worktree: str, session_id: str, cooldown_root: str | Path | None
) -> Path:
    return _declined_session_scope_snooze_session_dir(session_id, cooldown_root) / (
        f"{_digest(worktree)}.json"
    )


# 同一会话的 snooze 快照有四个读改写方：decline 写入（CLI）、Stop 侧 consult 回写观测 / 就地解除、
# UPS 侧开发者发话重置基线、显式手动触发清除。它们各自都是「读 → 改 → 整份写回」，交错时后写者会
# 静默覆盖先写者（实测可复现：Stop 读到快照后 UPS 重置了基线，Stop 再把旧快照写回，重置丢失）。
# 按会话一把排他锁把它们串行化；锁文件放在会话目录里、不以 ``.json`` 结尾，重置时的 ``*.json``
# 枚举不会把它当快照。互斥靠 ``flock``（崩溃即释放，无需陈旧回收，理由同上方铸造占坑锁）。
DECLINED_SESSION_SCOPE_SNOOZE_SESSION_MUTATION_LOCK_FILENAME = (
    ".declined-session-scope-snooze-session-mutation.lock"
)


@contextmanager
def _with_declined_session_scope_snooze_session_mutation_exclusive_lock(
    session_id: str, cooldown_root: str | Path | None
):
    """持有某会话全部 snooze 快照的读改写排他锁；文件系统报错即降级为无锁（与铸造占坑锁同款、同理由）。

    **不可重入**：``flock`` 按「打开文件描述」计，同一进程里再开一次同一个锁文件也会互斥。锁内只调
    ``_…_without_session_mutation_lock`` 系列内部函数，绝不回调加锁的公开函数。
    """
    lock_path = (
        _declined_session_scope_snooze_session_dir(session_id, cooldown_root)
        / DECLINED_SESSION_SCOPE_SNOOZE_SESSION_MUTATION_LOCK_FILENAME
    )
    try:
        _ensure_parent_dir(lock_path)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield
        return
    handle = os.fdopen(fd, "w", encoding="utf-8")
    acquired_flock = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            acquired_flock = True
        except OSError:
            acquired_flock = False
        yield
    finally:
        if acquired_flock:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def candidate_scope_identity_from_candidate_summary(
    candidate_summary: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """从候选摘要里取出闸 2b 所需的工作范围身份；缺失或形状不对 → None（闸 2b 放行，不猜）。"""
    if not isinstance(candidate_summary, Mapping):
        return None
    identity = candidate_summary.get("candidate_scope_identity_for_declined_session_snooze")
    if not isinstance(identity, Mapping):
        return None
    if not isinstance(identity.get("candidate_scope_fingerprint"), str):
        return None
    if not isinstance(identity.get("included_candidate_paths"), list):
        return None
    return dict(identity)


def _append_declined_session_scope_snooze_lifecycle_event(
    *,
    snooze_lifecycle_event: str,
    worktree: str | None,
    session_id: str,
    snooze_record: Mapping[str, Any] | None,
    now: datetime,
    cooldown_root: str | Path | None,
    release_reason: str | None = None,
    scope_expansion_baseline_changed: bool | None = None,
) -> None:
    """best-effort 追加一条 snooze 生命周期事件（解除 / 开发者发话重置基线；部署后评估阈值用，写不成
    绝不影响铸造或 prompt）。"""
    recorded_identity = (
        (snooze_record or {}).get("candidate_scope_identity_for_declined_session_snooze") or {}
    )
    try:
        _append_jsonl(
            declined_session_scope_snooze_root(cooldown_root)
            / DECLINED_SESSION_SCOPE_SNOOZE_LIFECYCLE_EVENT_LEDGER_FILENAME,
            {
                "schema_version": SCHEMA_VERSION,
                "recorded_at": format_timestamp(now),
                "snooze_lifecycle_event": snooze_lifecycle_event,
                "release_reason": release_reason,
                "scope_expansion_baseline_changed": scope_expansion_baseline_changed,
                "worktree": worktree,
                "session_id": session_id,
                "snooze_declined_at": (snooze_record or {}).get("declined_at"),
                "snooze_decline_token": (snooze_record or {}).get("token"),
                "snooze_candidate_scope_fingerprint": recorded_identity.get(
                    "candidate_scope_fingerprint"
                )
                if isinstance(recorded_identity, Mapping)
                else None,
            },
        )
    except OSError:
        pass


def record_declined_session_scope_snooze(
    *,
    worktree: str,
    session_id: str,
    candidate_summary: Mapping[str, Any] | None,
    reason: str | None = None,
    token: str | None = None,
    now: datetime | None = None,
    cooldown_root: str | Path | None = None,
) -> Path | None:
    """decline 时写入（幂等覆盖）会话级 snooze 快照；返回落盘路径。

    不写（返回 None）的三种情形：无 session_id（无法限定到会话，宁可不 snooze 也不跨会话误伤）、
    候选摘要缺工作范围身份（无从判定解除条件）、落盘 I/O 失败（snooze 是降噪优化，失败只退回到
    今天的行为，绝不能让 decline 本身失败）。
    """
    if not session_id:
        return None
    identity = candidate_scope_identity_from_candidate_summary(candidate_summary)
    if identity is None:
        return None
    path = _declined_session_scope_snooze_path(worktree, session_id, cooldown_root)
    try:
        with _with_declined_session_scope_snooze_session_mutation_exclusive_lock(
            session_id, cooldown_root
        ):
            _write_declined_session_scope_snooze_snapshot_at_decline_without_session_mutation_lock(
                path,
                worktree=worktree,
                session_id=session_id,
                identity=identity,
                reason=reason,
                token=token,
                now=now,
            )
    except OSError:
        return None
    return path


def _write_declined_session_scope_snooze_snapshot_at_decline_without_session_mutation_lock(
    path: Path,
    *,
    worktree: str,
    session_id: str,
    identity: dict[str, Any],
    reason: str | None,
    token: str | None,
    now: datetime | None,
) -> None:
    """（调用方须已持有会话 snooze 锁）落 decline 那一刻的快照。"""
    _atomic_write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "worktree": worktree,
            "session_id": session_id,
            "declined_at": format_timestamp(_coerce_now(now)),
            "reason": reason,
            "token": token,
            # decline 那一刻的工作范围：HEAD 变化判据永远对照它。
            "candidate_scope_identity_for_declined_session_snooze": identity,
            # 「范围明显扩大」判据的对比基线：初始即 decline 那一刻；开发者发话时被重置。
            "scope_expansion_baseline_identity": identity,
            "scope_expansion_baseline_source": "declined",
            "scope_expansion_baseline_rebased_at": None,
            # 仍处 snooze 期间最近一次 Stop 观测到的工作范围——开发者发话时拿它当「发话那一刻」
            # 的状态（发话前 agent 已停下，最后一次 Stop 的观测即发话时刻的工作树）。
            "latest_observed_scope_identity_while_snoozed": None,
        },
    )


def read_declined_session_scope_snooze(
    *,
    worktree: str,
    session_id: str,
    cooldown_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """读取 ``(worktree, session_id)`` 的 snooze 快照（不存在 / 损坏 / 无 session_id → None）。"""
    if not session_id:
        return None
    path = _declined_session_scope_snooze_path(worktree, session_id, cooldown_root)
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return parsed if isinstance(parsed, dict) else None


def clear_declined_session_scope_snooze(
    *,
    worktree: str,
    session_id: str,
    cooldown_root: str | Path | None = None,
) -> bool:
    """移除 ``(worktree, session_id)`` 的 snooze 快照（返回是否真的删除了文件）。"""
    if not session_id:
        return False
    with _with_declined_session_scope_snooze_session_mutation_exclusive_lock(
        session_id, cooldown_root
    ):
        return _unlink_declined_session_scope_snooze_snapshot_without_session_mutation_lock(
            worktree=worktree, session_id=session_id, cooldown_root=cooldown_root
        )


def _unlink_declined_session_scope_snooze_snapshot_without_session_mutation_lock(
    *, worktree: str, session_id: str, cooldown_root: str | Path | None
) -> bool:
    try:
        _declined_session_scope_snooze_path(worktree, session_id, cooldown_root).unlink()
        return True
    except OSError:
        return False


def rebase_declined_session_scope_snooze_expansion_baselines_on_developer_prompt(
    *,
    session_id: str,
    now: datetime | None = None,
    cooldown_root: str | Path | None = None,
) -> int:
    """开发者发来真实 prompt（UPS）：把该会话**所有** worktree 上 snooze 的「范围扩大」对比基线重置为
    发话那一刻的工作范围；**不解除** snooze。返回处理的快照条数。

    「发话那一刻」取快照里 ``latest_observed_scope_identity_while_snoozed``（发话前最后一次 Stop 的
    观测）；snooze 期间还没有任何观测时基线保持不变（仍是 decline 那一刻）。HEAD 判据不受影响。
    这样开发者转去做新工作时，只要出现新路径且新增工作量超过阈值就会重新征询；而纯提问 / 讨论 /
    继续打磨同一批文件的回合仍保持安静。

    按会话整目录处理：UPS 侧解析出的 cwd/repo 未必与 Stop 侧铸造时用的 worktree 字符串逐字一致，
    而「开发者发话了」本就是会话级事实。best-effort，任何 I/O 失败都吞掉。
    """
    if not session_id:
        return 0
    session_dir = _declined_session_scope_snooze_session_dir(session_id, cooldown_root)
    try:
        snooze_paths = sorted(session_dir.glob("*.json"))
    except OSError:
        return 0
    if not snooze_paths:
        return 0
    now_dt = _coerce_now(now)
    rebased = 0
    with _with_declined_session_scope_snooze_session_mutation_exclusive_lock(
        session_id, cooldown_root
    ):
        for snooze_path in snooze_paths:
            if _rebase_one_declined_session_scope_snooze_without_session_mutation_lock(
                snooze_path, session_id=session_id, now=now_dt, cooldown_root=cooldown_root
            ):
                rebased += 1
    return rebased


def _rebase_one_declined_session_scope_snooze_without_session_mutation_lock(
    snooze_path: Path,
    *,
    session_id: str,
    now: datetime,
    cooldown_root: str | Path | None,
) -> bool:
    """（调用方须已持有会话 snooze 锁）重置一份快照的「范围扩大」基线；返回这份快照是否被处理了。

    基线**真的变了**才改写快照（含 ``scope_expansion_baseline_source``）——没有新观测、或观测与基线
    相同时，来源仍如实是上一次设定它的那件事。lifecycle 事件两种情形都记，并标明基线变没变。
    """
    try:
        parsed = json.loads(snooze_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(parsed, dict):
        return False
    latest_observed = candidate_scope_identity_from_candidate_summary(
        {
            "candidate_scope_identity_for_declined_session_snooze": parsed.get(
                "latest_observed_scope_identity_while_snoozed"
            )
        }
    )
    previous_baseline = parsed.get("scope_expansion_baseline_identity")
    scope_expansion_baseline_changed = (
        latest_observed is not None and latest_observed != previous_baseline
    )
    if scope_expansion_baseline_changed:
        parsed["scope_expansion_baseline_identity"] = latest_observed
        parsed["scope_expansion_baseline_source"] = "developer_prompt"
        parsed["scope_expansion_baseline_rebased_at"] = format_timestamp(now)
        try:
            _atomic_write_json(snooze_path, parsed)
        except OSError:
            return False
    _append_declined_session_scope_snooze_lifecycle_event(
        snooze_lifecycle_event=(
            DECLINED_SESSION_SCOPE_SNOOZE_LIFECYCLE_EVENT_SCOPE_EXPANSION_BASELINE_REBASED_ON_DEVELOPER_PROMPT
        ),
        worktree=parsed.get("worktree"),
        session_id=session_id,
        snooze_record=parsed,
        now=now,
        cooldown_root=cooldown_root,
        scope_expansion_baseline_changed=scope_expansion_baseline_changed,
    )
    return True


def evaluate_declined_session_scope_snooze_release_reason(
    *,
    snooze_record: Mapping[str, Any],
    current_scope_identity: Mapping[str, Any],
    now: datetime,
) -> str | None:
    """纯函数：snooze 是否已满足解除条件；满足 → 返回解除原因，仍应静默 → None。

    判定顺序（先命中者为准）：硬 TTL → HEAD 变化（对照 decline 那一刻）→ 范围明显扩大（对照
    ``scope_expansion_baseline_identity``：新路径 **且** 未审行数增长超阈值）→ 同一批文件上的工作量
    翻几倍（同一对比基线；只看行数，阈值见 EXPERIMENT_NODE）。
    快照损坏（时间戳 / 身份字段读不出）按「已过期」处理——宁可多问一次，也不让一份坏快照永久静音。
    """
    try:
        declined_at = parse_timestamp(str(snooze_record.get("declined_at")))
    except (TypeError, ValueError):
        return DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_HARD_TTL
    if (now - declined_at).total_seconds() >= DECLINED_SESSION_SCOPE_SNOOZE_HARD_TTL_SECONDS:
        return DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_HARD_TTL
    recorded_identity = candidate_scope_identity_from_candidate_summary(
        {
            "candidate_scope_identity_for_declined_session_snooze": snooze_record.get(
                "candidate_scope_identity_for_declined_session_snooze"
            )
        }
    )
    if recorded_identity is None:
        return DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_HARD_TTL
    if recorded_identity.get("worktree_head_commit_sha") != current_scope_identity.get(
        "worktree_head_commit_sha"
    ):
        return DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_WORKTREE_HEAD_CHANGE
    # 「范围扩大」对照的是对比基线（开发者发话时会被重置）；缺失或损坏则回退到 decline 那一刻。
    scope_expansion_baseline_identity = (
        candidate_scope_identity_from_candidate_summary(
            {
                "candidate_scope_identity_for_declined_session_snooze": snooze_record.get(
                    "scope_expansion_baseline_identity"
                )
            }
        )
        or recorded_identity
    )
    new_candidate_paths = set(current_scope_identity.get("included_candidate_paths") or []) - set(
        scope_expansion_baseline_identity.get("included_candidate_paths") or []
    )
    try:
        recorded_lines = int(
            scope_expansion_baseline_identity.get("unreviewed_nonblank_changed_line_estimate") or 0
        )
        current_lines = int(current_scope_identity.get("unreviewed_nonblank_changed_line_estimate") or 0)
    except (TypeError, ValueError):
        return DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_HARD_TTL
    unreviewed_line_growth_exceeds_threshold = current_lines > recorded_lines * (
        1 + DECLINED_SESSION_SCOPE_SNOOZE_UNREVIEWED_LINE_GROWTH_RATIO_THRESHOLD
    )
    if new_candidate_paths and unreviewed_line_growth_exceeds_threshold:
        return DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_SCOPE_EXPANSION
    unreviewed_line_count_alone_grew_past_multiple_of_baseline = (
        current_lines
        >= recorded_lines * DECLINED_SESSION_SCOPE_SNOOZE_UNREVIEWED_LINE_GROWTH_ALONE_MINIMUM_MULTIPLE_OF_BASELINE
        and current_lines - recorded_lines
        >= DECLINED_SESSION_SCOPE_SNOOZE_UNREVIEWED_LINE_GROWTH_ALONE_MINIMUM_ABSOLUTE_NONBLANK_LINES
    )
    if unreviewed_line_count_alone_grew_past_multiple_of_baseline:
        return DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_UNREVIEWED_LINE_GROWTH_ALONE
    return None


def consult_declined_session_scope_snooze(
    *,
    worktree: str,
    session_id: str,
    candidate_summary: Mapping[str, Any] | None,
    now: datetime | None = None,
    cooldown_root: str | Path | None = None,
) -> DeclinedSessionScopeSnoozeConsultation:
    """闸 2b 的自动路径裁决：有 snooze 且未满足解除条件 ⇒ ``still_snoozed=True``。

    放行而不判的情形：无 session_id、无 snooze 快照、本次摘要缺工作范围身份（无从比较——退回今天
    的行为，快照保留给下一次带身份的 Stop 判）。满足解除条件时就地清掉快照并记一条解除事件。
    """
    if not session_id:
        return DeclinedSessionScopeSnoozeConsultation(still_snoozed=False)
    # 快路径（锁外）：绝大多数 Stop 根本没有 snooze，不为它们建会话目录与锁文件。
    unlocked_snooze_record = read_declined_session_scope_snooze(
        worktree=worktree, session_id=session_id, cooldown_root=cooldown_root
    )
    if unlocked_snooze_record is None:
        return DeclinedSessionScopeSnoozeConsultation(still_snoozed=False)
    current_scope_identity = candidate_scope_identity_from_candidate_summary(candidate_summary)
    if current_scope_identity is None:
        return DeclinedSessionScopeSnoozeConsultation(
            still_snoozed=False, snooze_record=unlocked_snooze_record
        )
    now_dt = _coerce_now(now)
    # 读 → 判 → 回写观测 / 就地解除 必须在同一把会话锁里：锁外读到的快照在 UPS 并发重置基线后
    # 就是旧的，拿它整份写回会把那次重置静默抹掉。
    with _with_declined_session_scope_snooze_session_mutation_exclusive_lock(
        session_id, cooldown_root
    ):
        snooze_record = read_declined_session_scope_snooze(
            worktree=worktree, session_id=session_id, cooldown_root=cooldown_root
        )
        if snooze_record is None:
            return DeclinedSessionScopeSnoozeConsultation(still_snoozed=False)
        release_reason = evaluate_declined_session_scope_snooze_release_reason(
            snooze_record=snooze_record, current_scope_identity=current_scope_identity, now=now_dt
        )
        if release_reason is None:
            # 记下本次观测，供开发者下一次发话时当作「发话那一刻」的对比基线。best-effort。
            snooze_record["latest_observed_scope_identity_while_snoozed"] = current_scope_identity
            try:
                _atomic_write_json(
                    _declined_session_scope_snooze_path(worktree, session_id, cooldown_root),
                    snooze_record,
                )
            except OSError:
                pass
            return DeclinedSessionScopeSnoozeConsultation(
                still_snoozed=True, snooze_record=snooze_record
            )
        _unlink_declined_session_scope_snooze_snapshot_without_session_mutation_lock(
            worktree=worktree, session_id=session_id, cooldown_root=cooldown_root
        )
    _append_declined_session_scope_snooze_lifecycle_event(
        snooze_lifecycle_event=DECLINED_SESSION_SCOPE_SNOOZE_LIFECYCLE_EVENT_RELEASED,
        worktree=worktree,
        session_id=session_id,
        release_reason=release_reason,
        snooze_record=snooze_record,
        now=now_dt,
        cooldown_root=cooldown_root,
    )
    return DeclinedSessionScopeSnoozeConsultation(
        still_snoozed=False, release_reason=release_reason, snooze_record=snooze_record
    )


# ---------------------------------------------------------------------------
# 延后型谢绝（deferred_until_condition）：等到回合边界再重新征询（闸 2c）
# ---------------------------------------------------------------------------
#
# 为什么要和结构性谢绝分开：回溯 160 条谢绝，结构性谢绝（R24 / 在途、琐碎、非本会话、开发者明确
# 不要审）72 条 0 条强证据错误；「等测试跑完 / 提交后 / 子代理写完再审」这类延后型谢绝 25 条错误率
# 16–48%，主因是机制兑现不了「之后再审」：内容不变时下一次 Stop 被同指纹冷却静默，T3 的会话级
# snooze 又会把它整片压住。所以延后型谢绝**不写**冷却表、**不写** snooze，只在这里记一条「延后到
# 什么条件」。
#
# 但也不能对「谢绝之后什么都没发生」的那次 Stop 重问：agent 谢绝完立刻结束回合时，这次 Stop 离
# 谢绝只隔零秒，条件不可能已经满足；重问只会和 agent 乒乓，把铸造熔断（每会话每小时 2 枚）烧光，
# 结果真正该重问的那一次反而被熔断静默——那正是回溯里列出的失灵机制之一。所以满足以下任一条件才
# 放行重新征询（「谢绝之后世界变了」）：
#
#   * UPS 观测到一个**新回合**：开发者发来真实 prompt，或 host 自己合成的回合（Claude 的
#     ``<task-notification>``——后台测试跑完、后台子代理写完，恰好就是最常见的延后条件）；
#     RVF 自己产生的机器正文（dispatch token / origin marker / 带外横幅）不算；
#   * 工作树 HEAD 变了（「提交后再审」的条件本身就是提交）；
#   * 候选的内容指纹变了：agent 谢绝后在**同一回合**里接着干活、把条件做完再停下（回溯样例
#     ac08e 08-22 03:12：「提炼子代理仍在读」→ 同一回合内改完文件停下 → 重问后开审，没有新回合
#     也没有提交，只有内容变了）。
#
# 放行后记录随即清除（这次延后已兑现为一次重新征询），并把理由盖进候选摘要，横幅据此写「你上次
# 延后到：<理由>」。重新征询照常经过铸造熔断。
#
# EXPERIMENT_NODE: 延后型谢绝的放行条件 = 新回合 / HEAD 变化 / 候选内容指纹变化，任一即放行（只挡「谢绝后原样停下」的那次 Stop）；审计原文是「下一次 Stop 只要候选还在就重问、只受熔断保护」，收紧的理由是那样会乒乓烧熔断。备选是再加「距谢绝超过 N 分钟」或去掉内容指纹条件。
# 「用户显式推翻 agent 谢绝」这一类铸造的来源（落进 token payload 的
# ``explicit_user_request_provenance``，回执与遥测据此区分两条入口）：
#   * 开发者在 prompt 里手动触发 RVF 主 skill（UPS manual 分支）；
EXPLICIT_USER_REQUEST_PROVENANCE_DEVELOPER_MANUAL_TRIGGER_VIA_USER_PROMPT_SUBMIT = (
    "developer_manual_trigger_via_user_prompt_submit"
)
EXPLICIT_USER_REQUEST_PROVENANCES = frozenset(
    {
        EXPLICIT_USER_REQUEST_PROVENANCE_DEVELOPER_MANUAL_TRIGGER_VIA_USER_PROMPT_SUBMIT,
    }
)
# :func:`mint_decision_token` 自己写进 token payload 的显式请求两键。调用方的
# ``extra_token_payload_fields`` 不得带同名键——否则能铸出 ``initiated_by_explicit_user_request=False``
# 的手动 token 或伪造来源。它们不在 :func:`_create_token_record` 的基础 payload 里，那里的覆盖守卫
# 看不到，故在铸造入口、任何闸门副作用之前单独拦截。
MINT_SYSTEM_WRITTEN_EXPLICIT_USER_REQUEST_TOKEN_PAYLOAD_FIELDS = (
    "initiated_by_explicit_user_request",
    "explicit_user_request_provenance",
)

DECLINE_KIND_STRUCTURAL_NOT_NEEDED = "structural_not_needed"
DECLINE_KIND_DEFERRED_UNTIL_CONDITION = "deferred_until_condition"
DECLINE_KINDS = frozenset({DECLINE_KIND_STRUCTURAL_NOT_NEEDED, DECLINE_KIND_DEFERRED_UNTIL_CONDITION})

# 与 snooze 同理挂在 declined 冷却根下（自动继承冷却根的全部隔离手段）。结构：
# ``<子目录>/<sha(session_id)>/<sha(worktree)>.json``——按会话分目录，UPS 按会话整目录打回合边界标志。
DEFERRED_UNTIL_CONDITION_DECLINE_AWAITING_TURN_BOUNDARY_SUBDIRNAME = (
    "deferred-until-condition-declines-awaiting-turn-boundary"
)
DEFERRED_UNTIL_CONDITION_DECLINE_SESSION_MUTATION_LOCK_FILENAME = (
    ".deferred-until-condition-declines-session-mutation.lock"
)
# 候选摘要里「本会话上一次延后型谢绝」的键：闸 2c 放行时由铸造写入，横幅渲染读取。
CANDIDATE_SUMMARY_PREVIOUS_DEFERRED_UNTIL_CONDITION_DECLINE_IN_THIS_SESSION_KEY = (
    "previous_deferred_until_condition_decline_in_this_session"
)
DEFERRED_UNTIL_CONDITION_DECLINE_RELEASED_BY_TURN_BOUNDARY = (
    "user_prompt_submit_observed_a_new_turn_since_deferral"
)
DEFERRED_UNTIL_CONDITION_DECLINE_RELEASED_BY_WORKTREE_HEAD_CHANGE = (
    "worktree_head_changed_since_deferral"
)
DEFERRED_UNTIL_CONDITION_DECLINE_RELEASED_BY_CANDIDATE_CONTENT_CHANGE = (
    "candidate_content_fingerprint_changed_since_deferral"
)


def deferred_until_condition_decline_root(cooldown_root: str | Path | None = None) -> Path:
    return declined_fingerprint_root(cooldown_root) / DEFERRED_UNTIL_CONDITION_DECLINE_AWAITING_TURN_BOUNDARY_SUBDIRNAME


def _deferred_until_condition_decline_session_dir(
    session_id: str, cooldown_root: str | Path | None
) -> Path:
    return deferred_until_condition_decline_root(cooldown_root) / _digest(session_id)


def _deferred_until_condition_decline_path(
    worktree: str, session_id: str, cooldown_root: str | Path | None
) -> Path:
    return _deferred_until_condition_decline_session_dir(session_id, cooldown_root) / (
        f"{_digest(worktree)}.json"
    )


@contextmanager
def _with_deferred_until_condition_declines_session_mutation_exclusive_lock(
    session_id: str, cooldown_root: str | Path | None
):
    """某会话全部延后型谢绝记录的读改写排他锁（Stop 侧放行清除 vs UPS 侧打回合边界）。

    文件系统报错即降级为无锁（与 snooze 锁同款、同理由）；**不可重入**。
    """
    lock_path = (
        _deferred_until_condition_decline_session_dir(session_id, cooldown_root)
        / DEFERRED_UNTIL_CONDITION_DECLINE_SESSION_MUTATION_LOCK_FILENAME
    )
    try:
        _ensure_parent_dir(lock_path)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield
        return
    handle = os.fdopen(fd, "w", encoding="utf-8")
    acquired_flock = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            acquired_flock = True
        except OSError:
            acquired_flock = False
        yield
    finally:
        if acquired_flock:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _read_json_object_or_none(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return parsed if isinstance(parsed, dict) else None


def record_deferred_until_condition_decline(
    *,
    worktree: str,
    session_id: str,
    candidate_summary: Mapping[str, Any] | None,
    candidate_fingerprint: str | None,
    reason: str,
    token: str | None = None,
    now: datetime | None = None,
    cooldown_root: str | Path | None = None,
) -> Path | None:
    """延后型谢绝时写入（幂等覆盖）「等到回合边界再重新征询」的记录；返回落盘路径。

    无 session_id 时不写（返回 None）：此时闸 2c 无从按会话放行，退回「下一次 Stop 即重新征询」，
    只受熔断保护——方向是多问，不会漏审。落盘失败同样返回 None、绝不让 decline 失败。
    """
    if not session_id:
        return None
    identity = candidate_scope_identity_from_candidate_summary(candidate_summary) or {}
    path = _deferred_until_condition_decline_path(worktree, session_id, cooldown_root)
    try:
        with _with_deferred_until_condition_declines_session_mutation_exclusive_lock(
            session_id, cooldown_root
        ):
            _atomic_write_json(
                path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "worktree": worktree,
                    "session_id": session_id,
                    "declined_at": format_timestamp(_coerce_now(now)),
                    "reason": reason,
                    "token": token,
                    "worktree_head_commit_sha_at_deferral": identity.get("worktree_head_commit_sha"),
                    "candidate_fingerprint_at_deferral": candidate_fingerprint,
                    "user_prompt_submit_observed_a_new_turn_since_deferral": False,
                    "user_prompt_submit_new_turn_observed_at": None,
                },
            )
    except OSError:
        return None
    return path


def read_deferred_until_condition_decline(
    *,
    worktree: str,
    session_id: str,
    cooldown_root: str | Path | None = None,
) -> dict[str, Any] | None:
    if not session_id:
        return None
    return _read_json_object_or_none(
        _deferred_until_condition_decline_path(worktree, session_id, cooldown_root)
    )


def clear_deferred_until_condition_decline(
    *,
    worktree: str,
    session_id: str,
    cooldown_root: str | Path | None = None,
) -> bool:
    """移除 ``(worktree, session_id)`` 的延后型谢绝记录（返回是否真的删除了文件）。"""
    if not session_id:
        return False
    with _with_deferred_until_condition_declines_session_mutation_exclusive_lock(
        session_id, cooldown_root
    ):
        try:
            _deferred_until_condition_decline_path(worktree, session_id, cooldown_root).unlink()
            return True
        except OSError:
            return False


def mark_new_turn_observed_for_deferred_until_condition_declines_in_session(
    *,
    session_id: str,
    now: datetime | None = None,
    cooldown_root: str | Path | None = None,
) -> int:
    """UPS 观测到一个新回合（开发者 prompt 或 host 合成回合）：给本会话**所有** worktree 上的延后型
    谢绝记录打上回合边界标志。返回打标的条数。

    按会话整目录处理（与 snooze 基线重置同理：UPS 侧的 repo 字符串未必与 Stop 侧逐字一致，而「出现了
    新回合」本就是会话级事实）。best-effort，I/O 失败吞掉——少打一次标志只会让重新征询晚一个回合。
    """
    if not session_id:
        return 0
    session_dir = _deferred_until_condition_decline_session_dir(session_id, cooldown_root)
    try:
        record_paths = sorted(session_dir.glob("*.json"))
    except OSError:
        return 0
    if not record_paths:
        return 0
    observed_at = format_timestamp(_coerce_now(now))
    marked = 0
    with _with_deferred_until_condition_declines_session_mutation_exclusive_lock(
        session_id, cooldown_root
    ):
        for record_path in record_paths:
            record = _read_json_object_or_none(record_path)
            if record is None or record.get("user_prompt_submit_observed_a_new_turn_since_deferral"):
                continue
            record["user_prompt_submit_observed_a_new_turn_since_deferral"] = True
            record["user_prompt_submit_new_turn_observed_at"] = observed_at
            try:
                _atomic_write_json(record_path, record)
            except OSError:
                continue
            marked += 1
    return marked


def evaluate_deferred_until_condition_decline_release_reason(
    *,
    deferred_record: Mapping[str, Any],
    current_scope_identity: Mapping[str, Any] | None,
    current_candidate_fingerprint: str | None,
) -> str | None:
    """纯函数：延后型谢绝是否已到重新征询的时机；到了 → 返回放行原因，否则 None（本回合静默）。"""
    if deferred_record.get("user_prompt_submit_observed_a_new_turn_since_deferral"):
        return DEFERRED_UNTIL_CONDITION_DECLINE_RELEASED_BY_TURN_BOUNDARY
    head_at_deferral = deferred_record.get("worktree_head_commit_sha_at_deferral")
    current_head = (current_scope_identity or {}).get("worktree_head_commit_sha")
    # 任一侧读不出 HEAD 就不拿它判（不猜），只等回合边界。
    if head_at_deferral and current_head and head_at_deferral != current_head:
        return DEFERRED_UNTIL_CONDITION_DECLINE_RELEASED_BY_WORKTREE_HEAD_CHANGE
    fingerprint_at_deferral = deferred_record.get("candidate_fingerprint_at_deferral")
    if (
        fingerprint_at_deferral
        and current_candidate_fingerprint
        and fingerprint_at_deferral != current_candidate_fingerprint
    ):
        return DEFERRED_UNTIL_CONDITION_DECLINE_RELEASED_BY_CANDIDATE_CONTENT_CHANGE
    return None


@dataclass(frozen=True)
class DeferredUntilConditionDeclineConsultation:
    """闸 2c 对一次自动铸造请求的裁决。``still_awaiting_turn_boundary=True`` ⇒ 拒铸。"""

    still_awaiting_turn_boundary: bool
    release_reason: str | None = None
    deferred_record: dict[str, Any] | None = None


def consult_deferred_until_condition_decline(
    *,
    worktree: str,
    session_id: str,
    candidate_fingerprint: str | None,
    candidate_summary: Mapping[str, Any] | None,
    cooldown_root: str | Path | None = None,
) -> DeferredUntilConditionDeclineConsultation:
    """闸 2c 的自动路径裁决。**只读不删**：放行时记录原样保留，由铸造在真正取得可投递 token 之后
    调 :func:`_consume_deferred_until_condition_decline_after_deliverable_token` 消费。

    放行 ≠ 这次延后已兑现：放行之后还有闸三熔断与 token 落盘，二者任一失败本次都没有横幅。若在这里
    就删记录，下一次重试时「你上次延后到：「理由」」的盖章依据已经没了。
    """
    if not session_id:
        return DeferredUntilConditionDeclineConsultation(still_awaiting_turn_boundary=False)
    if read_deferred_until_condition_decline(
        worktree=worktree, session_id=session_id, cooldown_root=cooldown_root
    ) is None:
        return DeferredUntilConditionDeclineConsultation(still_awaiting_turn_boundary=False)
    current_scope_identity = candidate_scope_identity_from_candidate_summary(candidate_summary)
    path = _deferred_until_condition_decline_path(worktree, session_id, cooldown_root)
    with _with_deferred_until_condition_declines_session_mutation_exclusive_lock(
        session_id, cooldown_root
    ):
        deferred_record = _read_json_object_or_none(path)
        if deferred_record is None:
            return DeferredUntilConditionDeclineConsultation(still_awaiting_turn_boundary=False)
        release_reason = evaluate_deferred_until_condition_decline_release_reason(
            deferred_record=deferred_record,
            current_scope_identity=current_scope_identity,
            current_candidate_fingerprint=candidate_fingerprint,
        )
        if release_reason is None:
            return DeferredUntilConditionDeclineConsultation(
                still_awaiting_turn_boundary=True, deferred_record=deferred_record
            )
    return DeferredUntilConditionDeclineConsultation(
        still_awaiting_turn_boundary=False,
        release_reason=release_reason,
        deferred_record=deferred_record,
    )


def _consume_deferred_until_condition_decline_after_deliverable_token(
    *,
    worktree: str,
    session_id: str,
    consulted_deferred_record: Mapping[str, Any],
    cooldown_root: str | Path | None = None,
) -> bool:
    """铸造已取得可投递 token（新铸或并发竞态的胜者）之后，消费闸 2c 当初读到的那条延后记录。

    只删「仍是同一条」的记录（按 ``token`` + ``declined_at`` 认身份；UPS 打回合边界标志只改标志字段、
    不改身份）：裁决与消费之间若已写入一条新的延后谢绝，那是另一次延后，不能被这次重新征询顺手吞掉。
    返回是否真的删除了文件；删不掉吞掉——残留记录下次会再判出放行并重新盖章，方向是多提一次理由。
    """
    if not session_id:
        return False
    path = _deferred_until_condition_decline_path(worktree, session_id, cooldown_root)
    with _with_deferred_until_condition_declines_session_mutation_exclusive_lock(
        session_id, cooldown_root
    ):
        current_deferred_record = _read_json_object_or_none(path)
        if current_deferred_record is None:
            return False
        if any(
            current_deferred_record.get(identity_field) != consulted_deferred_record.get(identity_field)
            for identity_field in ("token", "declined_at")
        ):
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False


def _stamp_previous_deferred_until_condition_decline_on_candidate_summary(
    candidate_summary: dict[str, Any] | None,
    deferred_record: Mapping[str, Any],
    *,
    release_reason: str | None,
) -> None:
    """把「本会话上一次延后型谢绝」盖进候选摘要（就地修改调用方传入的那份 dict）。

    就地修改是刻意的：Stop 的两条 transport 与 UPS 都在铸造**之后**用同一个 ``summary`` 对象渲染横幅，
    而 token payload 里的 ``candidate_summary`` 也是同一份——一处写入、三处可见。
    """
    if not isinstance(candidate_summary, dict):
        return
    candidate_summary[CANDIDATE_SUMMARY_PREVIOUS_DEFERRED_UNTIL_CONDITION_DECLINE_IN_THIS_SESSION_KEY] = {
        "reason": deferred_record.get("reason"),
        "declined_at": deferred_record.get("declined_at"),
        "token": deferred_record.get("token"),
        "release_reason": release_reason,
    }


# ---------------------------------------------------------------------------
# 铸造（Stop hook 侧唯一铸造入口）
# ---------------------------------------------------------------------------

def mint_decision_token(
    *,
    worktree: str,
    session_id: str,
    candidate_fingerprint: str,
    candidate_summary: dict[str, Any] | None = None,
    delivery_transport: str | None = None,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
    root: str | Path | None = None,
    cooldown_root: str | Path | None = None,
    circuit_breaker_root_dir: str | Path | None = None,
    circuit_breaker_window_seconds: int = DEFAULT_MINT_CIRCUIT_BREAKER_WINDOW_SECONDS,
    circuit_breaker_max_requests: int = DEFAULT_MINT_CIRCUIT_BREAKER_MAX_REQUESTS,
    initiated_by_explicit_user_request: bool = False,
    explicit_user_request_provenance: str | None = None,
    extra_token_payload_fields: Mapping[str, Any] | None = None,
    agent_requested_deferred_reconsideration_token: str | None = None,
) -> MintResult:
    """铸造一枚决策 token（Stop hook 侧唯一入口），依次经三道闸：

    1. **去重（注入幂等硬保证，R25）**：``(worktree, session_id, candidate_fingerprint)`` 同键存在活跃
       token 时不重铸、直接返回既有 token（``outcome=deduplicated``；过期可复活者就地复活）。
    2. **declined 冷却**：指纹处于 declined 冷却中则拒铸（``outcome=declined_cooldown``），防 nag 循环。
    2b. **declined 会话级 snooze**：本会话在本 worktree 上谢绝过、且尚未出现工作边界（HEAD 变化 /
       范围明显扩大 / 硬 TTL；开发者真实 prompt 只重置「范围扩大」基线）则拒铸（``outcome=declined_session_scope_snooze``）。
       它补的是闸二的盲区——内容一直在变的「边做边停」会话。放在闸二之后、闸三之前：两种 transport
       与 UPS 的铸造都经过它；被它拦下的请求也不占熔断额度（本就不是递归自发起）。闸二与 2b 只由
       **结构性**谢绝写入。
    2c. **延后型谢绝等回合边界**：本会话最近一次是延后型谢绝、此后没有新回合、HEAD 没变、候选内容
       指纹也没变则拒铸
       （``outcome=deferred_until_condition_decline_awaiting_turn_boundary``）；之后放行（重新征询照常
       经过闸三），并把延后理由盖进候选摘要供横幅引用。同样不占熔断额度。延后记录直到本次真正取得可投递
       token 才被消费：被闸三拒铸或落盘失败时记录原样保留，重试仍能盖章。
    3. **铸造频率熔断（R24 裁决 b）**：同 session 窗口内第 ≥K 次铸造请求即拒铸
       （``outcome=circuit_breaker_tripped``），写 ledger 并回告警文案。纯计数，零语义判断。

    三闸皆过则新铸 token（``outcome=minted``），落盘 + 建/覆盖去重索引。``candidate_fingerprint`` 对
    本模块不透明。

    「同一 dedup key 至多一枚活跃 token」是**硬保证**，对**首建索引与顶掉陈旧索引一视同仁**：占坑
    全程持 :func:`_with_dedup_index_mint_claim_exclusive_lock`，锁内重读索引裁决，同键并发中至多
    一方返回 ``minted``，其余方丢弃自己刚铸的 token 并幂等返回胜者（``deduplicated``）。

    ``delivery_transport`` 只描述「这枚 token 的横幅打算怎么送出去」（见
    :data:`DECISION_POINT_DELIVERY_TRANSPORTS`），既落进 payload 供后续对账，也在
    ``ttl_seconds`` 缺省时决定 TTL（带外投递 1200s / 其余 900s）。显式传 ``ttl_seconds``
    时以显式值为准。

    ``initiated_by_explicit_user_request=True`` 表示这次铸造源自**用户当场显式打出的手动触发**
    （UPS 的 manual 分支），此时闸二、闸三**均不适用**——它们保护的都是自动路径，对用户显式意图
    则是误伤：

    * 闸二不适用：declined 冷却的语义是「agent 谢绝过就别再骚扰 agent」，而用户显式触发本身就是
      *推翻 agent 的谢绝*。命中冷却时不再拒铸，而是调 :func:`clear_declined_fingerprint`
      **清掉**那条记录再往下铸。清除而非仅本次绕过：用户既已推翻谢绝，这批改动就该整体重回可审
      状态，否则会出现「手动审得了、后续自动 Stop 仍被同一条陈旧 decline 挡着」的语义割裂。
      **命中与清除是两件事**，分别由两个字段回报：``explicit_user_request_overrode_agent_
      declined_cooldown``（命中即 True，供调用方告知用户与 agent）与
      ``declined_cooldown_record_still_blocks_automatic_path``（unlink 失败时 True，只说明这条
      陈旧冷却仍会挡住后续自动路径）。清不掉绝不能吞掉推翻告知。
    * 闸 2b 不适用：理由同闸二（用户显式触发就是在推翻 agent 的谢绝），同样**清除**该
      ``(worktree, session_id)`` 的 snooze 快照再往下铸，并同样拆成两个字段回报：
      ``explicit_user_request_overrode_agent_declined_session_scope_snooze``（有快照即 True）与
      ``declined_session_scope_snooze_record_still_blocks_automatic_path``（删不掉时 True）。
    * 闸三不适用：熔断（R24）防的是 agent 对自己的 RVF 结果递归自发起；手动触发按定义来自用户
      而非自注入（manual 判定路径已把带 token/marker 的自注入 prompt 全部短路掉），因此**整段跳过**
      ——既不注册铸造请求、也不判定，不占用自动路径的窗口配额，``mint_request_record_id`` 留 None
      （下游 :func:`_reverse_mint_request_charge_for_withdrawn_token` 对 None 直接放行，不会去冲销
      一份从未占用的额度）。

    默认 ``False`` ⇒ 自动路径（Stop hook）行为完全不变。

    ``initiated_by_explicit_user_request=True`` 时必须同时给出 ``explicit_user_request_provenance``
    （仅 manual UPS 来源），落进 token payload；真正的用户消息由开审 gate 另行核验，
    Agent 自填 reason 不得升级为显式用户请求。
    ``extra_token_payload_fields`` 原样并入新铸 token 的 payload（不得覆盖既有字段）。
    """
    if initiated_by_explicit_user_request and (
        explicit_user_request_provenance not in EXPLICIT_USER_REQUEST_PROVENANCES
    ):
        raise DecisionTokenError(
            "用户显式请求的铸造必须注明来源 explicit_user_request_provenance，取值为 "
            f"{sorted(EXPLICIT_USER_REQUEST_PROVENANCES)} 之一，收到 {explicit_user_request_provenance!r}"
        )
    for system_written_field in MINT_SYSTEM_WRITTEN_EXPLICIT_USER_REQUEST_TOKEN_PAYLOAD_FIELDS:
        if system_written_field in (extra_token_payload_fields or {}):
            raise DecisionTokenError(f"extra_token_payload_fields 不得覆盖既有字段 {system_written_field!r}")
    if ttl_seconds is None:
        ttl_seconds = decision_token_ttl_seconds_for_delivery_transport(delivery_transport)
    if ttl_seconds <= 0:
        raise DecisionTokenError("ttl_seconds 必须为正")
    now_dt = _coerce_now(now)

    # 闸一：去重。存在活跃同键 token → 幂等返回。
    index_path = _dedup_index_path(worktree, session_id, candidate_fingerprint, root)
    existing_index = _read_dedup_index(index_path)
    if existing_index is not None:
        active = _dedup_active_lookup(existing_index, root=root, now=now_dt, ttl_seconds=ttl_seconds)
        if active is not None:
            return MintResult(outcome=MINT_OUTCOME_DEDUPLICATED, token=active.token, record=active)
        # 索引指向的 token 已终态 / 真过期 ⇒ 索引陈旧、可被本次铸造顶掉。这里**不**记下「陈旧」
        # 结论去指导后面怎么写：本次读在锁外、且铸造还没发生，结论到写索引那一刻早已可能过时。
        # 占坑与否一律由下方锁内的那次重读裁决。

    # 闸二：declined 冷却（指纹未变不再注入）。用户显式触发不适用——那正是在推翻 agent 的谢绝，
    # 此时把冷却记录清掉再往下铸，让这批改动整体重回可审状态。
    # 这里有**两个互不相干的事实**，必须分开记：**命中**冷却即已证成「用户推翻了 agent 的谢绝」
    # （告知用户与 agent 的唯一依据），而 unlink 成败只决定「这条陈旧冷却是否还会挡住后续自动
    # 路径」。若压成同一个布尔，一次 I/O 失败就会静默吞掉本该必发的推翻告知。
    overrode_agent_declined_cooldown = False
    declined_cooldown_record_still_blocks_automatic_path = False
    if is_fingerprint_in_declined_cooldown(
        worktree=worktree, candidate_fingerprint=candidate_fingerprint, cooldown_root=cooldown_root
    ):
        if not initiated_by_explicit_user_request:
            return MintResult(
                outcome=MINT_OUTCOME_DECLINED_COOLDOWN,
                message="候选指纹处于 declined 冷却中，跳过铸造（指纹未变不再注入）。",
            )
        overrode_agent_declined_cooldown = True
        # clear 吞掉 OSError 返回 False；删不掉只意味着自动路径仍被挡，不改变「已被推翻」这一事实。
        declined_cooldown_record_still_blocks_automatic_path = not clear_declined_fingerprint(
            worktree=worktree, candidate_fingerprint=candidate_fingerprint, cooldown_root=cooldown_root
        )

    # 闸 2b：declined 会话级 snooze（粗粒度工作范围，补闸二「内容一直在变」的盲区）。用户显式触发
    # 与闸二同理：清掉快照再往下铸，并与闸二一样把「命中」与「删掉」拆成两个事实回报。自动路径命中
    # 且未满足解除条件 ⇒ 拒铸。
    overrode_agent_declined_session_scope_snooze = False
    declined_session_scope_snooze_record_still_blocks_automatic_path = False
    if initiated_by_explicit_user_request:
        snooze_record_overridden_by_explicit_user_request = read_declined_session_scope_snooze(
            worktree=worktree, session_id=session_id, cooldown_root=cooldown_root
        )
        snooze_record_cleared = False
        if snooze_record_overridden_by_explicit_user_request is not None:
            # 读与删在同一把会话锁里：否则删之前 Stop 可能刚把新观测写回，被推翻的就不是读到的那份。
            with _with_declined_session_scope_snooze_session_mutation_exclusive_lock(
                session_id, cooldown_root
            ):
                snooze_record_overridden_by_explicit_user_request = (
                    read_declined_session_scope_snooze(
                        worktree=worktree, session_id=session_id, cooldown_root=cooldown_root
                    )
                )
                if snooze_record_overridden_by_explicit_user_request is not None:
                    snooze_record_cleared = (
                        _unlink_declined_session_scope_snooze_snapshot_without_session_mutation_lock(
                            worktree=worktree, session_id=session_id, cooldown_root=cooldown_root
                        )
                    )
        overrode_agent_declined_session_scope_snooze = (
            snooze_record_overridden_by_explicit_user_request is not None
        )
        declined_session_scope_snooze_record_still_blocks_automatic_path = (
            overrode_agent_declined_session_scope_snooze and not snooze_record_cleared
        )
        if snooze_record_cleared:
            _append_declined_session_scope_snooze_lifecycle_event(
                snooze_lifecycle_event=DECLINED_SESSION_SCOPE_SNOOZE_LIFECYCLE_EVENT_RELEASED,
                worktree=worktree,
                session_id=session_id,
                release_reason=DECLINED_SESSION_SCOPE_SNOOZE_RELEASED_BY_EXPLICIT_USER_REQUEST,
                snooze_record=snooze_record_overridden_by_explicit_user_request,
                now=now_dt,
                cooldown_root=cooldown_root,
            )
    else:
        snooze_consultation = consult_declined_session_scope_snooze(
            worktree=worktree,
            session_id=session_id,
            candidate_summary=candidate_summary,
            now=now_dt,
            cooldown_root=cooldown_root,
        )
        if snooze_consultation.still_snoozed:
            snooze_declined_at = (snooze_consultation.snooze_record or {}).get("declined_at")
            return MintResult(
                outcome=MINT_OUTCOME_DECLINED_SESSION_SCOPE_SNOOZE,
                message=(
                    f"agent 已于 {snooze_declined_at} 在本会话谢绝过这片工作范围，且此后尚未出现工作"
                    "边界（提交 / 范围明显扩大 / 12h 到期），跳过铸造。"
                ),
            )

    # 闸 2c：延后型谢绝（「等 X 之后再审」）。它不进冷却、不进 snooze，只挡「谢绝后原样停下」的 Stop：
    # 新回合（UPS 观测到）、HEAD 变化、候选内容变化之前拒铸；之后放行并把延后理由盖进候选摘要。用户
    # 显式触发与闸二 / 2b 同理：清掉记录（取得可投递 token 后消费）、如实回报「推翻了 agent 的延后」。
    overrode_agent_deferred_until_condition_decline = False
    overridden_deferred_until_condition_decline_reason: str | None = None
    # 本次铸造取得可投递 token 之后要消费的那条延后记录（显式推翻 / 自动放行两条路径共用）。在熔断或
    # 落盘失败之前就删，重试时会丢掉「你上次延后到」的理由与推翻告知的依据。
    deferred_record_to_consume_after_deliverable_token: dict[str, Any] | None = None
    if initiated_by_explicit_user_request:
        deferred_record_overridden_by_explicit_user_request = read_deferred_until_condition_decline(
            worktree=worktree, session_id=session_id, cooldown_root=cooldown_root
        )
        if deferred_record_overridden_by_explicit_user_request is not None:
            overrode_agent_deferred_until_condition_decline = True
            raw_reason = deferred_record_overridden_by_explicit_user_request.get("reason")
            overridden_deferred_until_condition_decline_reason = (
                raw_reason if isinstance(raw_reason, str) else None
            )
            deferred_record_to_consume_after_deliverable_token = (
                deferred_record_overridden_by_explicit_user_request
            )
    else:
        deferred_consultation = consult_deferred_until_condition_decline(
            worktree=worktree,
            session_id=session_id,
            candidate_fingerprint=candidate_fingerprint,
            candidate_summary=candidate_summary,
            cooldown_root=cooldown_root,
        )
        # 自主条件判断只释放延后重问门，不伪装用户请求、不跳过结构性谢绝或铸造熔断。
        if agent_requested_deferred_reconsideration_token:
            deferred_record = deferred_consultation.deferred_record
            if deferred_record and deferred_record.get("token") == agent_requested_deferred_reconsideration_token:
                deferred_consultation = DeferredUntilConditionDeclineConsultation(
                    still_awaiting_turn_boundary=False,
                    release_reason="agent_requested_deferred_condition_reconsideration",
                    deferred_record=deferred_record,
                )
        if deferred_consultation.still_awaiting_turn_boundary:
            deferred_record = deferred_consultation.deferred_record or {}
            return MintResult(
                outcome=MINT_OUTCOME_DEFERRED_UNTIL_CONDITION_DECLINE_AWAITING_TURN_BOUNDARY,
                message=(
                    f"agent 于 {deferred_record.get('declined_at')} 把本次审查延后到"
                    f"「{deferred_record.get('reason')}」，此后没有新回合、没有提交、候选内容也没变，"
                    "跳过铸造（出现其中任一即重新征询）。"
                ),
            )
        if deferred_consultation.deferred_record is not None:
            _stamp_previous_deferred_until_condition_decline_on_candidate_summary(
                candidate_summary,
                deferred_consultation.deferred_record,
                release_reason=deferred_consultation.release_reason,
            )
            deferred_record_to_consume_after_deliverable_token = deferred_consultation.deferred_record

    # 闸三：铸造频率熔断（纯计数）。用户显式触发整段跳过——不注册请求、不判定、不占配额。
    breaker: CircuitBreakerStatus | None = None
    if not initiated_by_explicit_user_request:
        breaker = _register_mint_request_and_check_breaker(
            session_id=session_id,
            worktree=worktree,
            candidate_fingerprint=candidate_fingerprint,
            now=now_dt,
            window_seconds=circuit_breaker_window_seconds,
            max_requests=circuit_breaker_max_requests,
            root=circuit_breaker_root_dir,
        )
        if breaker.tripped:
            return MintResult(
                outcome=MINT_OUTCOME_CIRCUIT_BREAKER_TRIPPED,
                circuit_breaker=breaker,
                message=(
                    f"铸造频率熔断：session {session_id} 在 {breaker.window_seconds}s 窗口内已第 "
                    f"{breaker.request_count} 次请求铸造决策 token（阈值 {breaker.max_requests}）。"
                    "疑似递归发起，已拒铸——请检查是否在对自己的 RVF 结果再次发起 RVF。"
                ),
            )

    # 诸闸皆过：新铸 token。把本次铸造请求在 ledger 里的 id 一并写进 payload——撤回时凭它
    # 追加冲销记录，把这份熔断额度还回去（INV-C：withdrawn 不计冷却、也不计熔断）。
    # 用户显式触发跳过了闸三，压根没占额度，故 id 为 None、撤回时也无从冲销。
    record = _create_token_record(
        worktree=worktree,
        session_id=session_id,
        candidate_fingerprint=candidate_fingerprint,
        candidate_summary=candidate_summary,
        delivery_transport=delivery_transport,
        mint_request_record_id=breaker.mint_request_record_id if breaker is not None else None,
        ttl_seconds=ttl_seconds,
        now=now_dt,
        root=root,
        extra_payload_fields={
            "initiated_by_explicit_user_request": initiated_by_explicit_user_request,
            "explicit_user_request_provenance": (
                explicit_user_request_provenance if initiated_by_explicit_user_request else None
            ),
            **dict(extra_token_payload_fields or {}),
        },
    )

    # 为这个 dedup key 占坑。**首建与「顶掉陈旧索引」是同一件事**（都是占同一个坑，只差坑里有没有
    # 前任），因此必须走同一条互斥路径：全程持有同键铸造占坑排他锁，锁内**重读**索引裁决——
    #
    #   * 索引已被竞态者占走且其 token 活跃 ⇒ 丢弃本次刚铸的 token，幂等返回竞态者的 token；
    #   * 否则（无索引 / 索引确认陈旧）⇒ 本方占坑成功，整份写入索引并返回 ``minted``。
    #
    # 锁内重读是这段互斥的**全部**要害：闸一那次读在锁外、且发生在铸造之前，只凭它就写索引，
    # 同键并发会各自铸出不同 token 并双双返回 ``minted``（一个 dedup key 两枚活跃 token）。
    index_payload = {
        "schema_version": SCHEMA_VERSION,
        "token": record.token,
        "worktree": worktree,
        "session_id": session_id,
        "candidate_fingerprint": candidate_fingerprint,
        "created_at": format_timestamp(now_dt),
    }
    with _with_dedup_index_mint_claim_exclusive_lock(index_path):
        index_claim_holder = _read_dedup_index(index_path)
        index_claim_holder_active = (
            _dedup_active_lookup(index_claim_holder, root=root, now=now_dt, ttl_seconds=ttl_seconds)
            if index_claim_holder is not None
            else None
        )
        if index_claim_holder_active is not None:
            _discard_token_record(record)
            # 竞态胜者的 token 同样可投递：这次延后已兑现为一次重新征询，照样消费。
            if deferred_record_to_consume_after_deliverable_token is not None:
                _consume_deferred_until_condition_decline_after_deliverable_token(
                    worktree=worktree,
                    session_id=session_id,
                    consulted_deferred_record=deferred_record_to_consume_after_deliverable_token,
                    cooldown_root=cooldown_root,
                )
            return MintResult(
                outcome=MINT_OUTCOME_DEDUPLICATED,
                token=index_claim_holder_active.token,
                record=index_claim_holder_active,
                # 本次虽被竞态者顶掉，「用户推翻了 agent 的谢绝」这件事确实发生了，如实上报。
                explicit_user_request_overrode_agent_declined_cooldown=overrode_agent_declined_cooldown,
                declined_cooldown_record_still_blocks_automatic_path=(
                    declined_cooldown_record_still_blocks_automatic_path
                ),
                explicit_user_request_overrode_agent_declined_session_scope_snooze=(
                    overrode_agent_declined_session_scope_snooze
                ),
                declined_session_scope_snooze_record_still_blocks_automatic_path=(
                    declined_session_scope_snooze_record_still_blocks_automatic_path
                ),
                explicit_user_request_overrode_agent_deferred_until_condition_decline=(
                    overrode_agent_deferred_until_condition_decline
                ),
                overridden_deferred_until_condition_decline_reason=(
                    overridden_deferred_until_condition_decline_reason
                ),
            )
        _atomic_write_json(index_path, index_payload)
    if deferred_record_to_consume_after_deliverable_token is not None:
        _consume_deferred_until_condition_decline_after_deliverable_token(
            worktree=worktree,
            session_id=session_id,
            consulted_deferred_record=deferred_record_to_consume_after_deliverable_token,
            cooldown_root=cooldown_root,
        )
    return MintResult(
        outcome=MINT_OUTCOME_MINTED,
        token=record.token,
        record=record,
        explicit_user_request_overrode_agent_declined_cooldown=overrode_agent_declined_cooldown,
        declined_cooldown_record_still_blocks_automatic_path=(
            declined_cooldown_record_still_blocks_automatic_path
        ),
        explicit_user_request_overrode_agent_declined_session_scope_snooze=(
            overrode_agent_declined_session_scope_snooze
        ),
        declined_session_scope_snooze_record_still_blocks_automatic_path=(
            declined_session_scope_snooze_record_still_blocks_automatic_path
        ),
        explicit_user_request_overrode_agent_deferred_until_condition_decline=(
            overrode_agent_deferred_until_condition_decline
        ),
        overridden_deferred_until_condition_decline_reason=(
            overridden_deferred_until_condition_decline_reason
        ),
    )


def _create_token_record(
    *,
    worktree: str,
    session_id: str,
    candidate_fingerprint: str,
    candidate_summary: dict[str, Any] | None,
    delivery_transport: str | None = None,
    mint_request_record_id: str | None = None,
    ttl_seconds: int,
    now: datetime,
    root: str | Path | None,
    extra_payload_fields: Mapping[str, Any] | None = None,
) -> DecisionTokenRecord:
    """分配唯一 token 并原子落盘初始（``minted``）payload。"""
    expires_at = now + timedelta(seconds=ttl_seconds)
    for _attempt in range(DEFAULT_TOKEN_ALLOCATION_ATTEMPTS):
        token = generate_token()
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "token": token,
            "state": STATE_MINTED,
            "worktree": worktree,
            "session_id": session_id,
            "candidate_fingerprint": candidate_fingerprint,
            "candidate_summary": candidate_summary,
            # 这枚 token 的横幅打算走哪条 transport（None = 同步 stdout 自注入的历史形状）。
            "decision_point_delivery_transport": delivery_transport,
            # 带外投递回执（cline-kanban 终端投递契约的四态之一；对本模块不透明）。与 ``state``
            # **正交**：它只说「消息送到没送到」，绝不参与 lifecycle 判定。
            "out_of_band_delivery_status": None,
            # 本次铸造请求在 mint-request ledger 里那一行的 id：撤回时凭它追加冲销记录。
            MINT_REQUEST_LEDGER_RECORD_ID_FIELD: mint_request_record_id,
            "created_at": format_timestamp(now),
            "minted_at": format_timestamp(now),
            "expires_at": format_timestamp(expires_at),
            "run_anchor_dir": None,
            "start_attempt_count": 0,
            "revived_count": 0,
            "history": [{"from": None, "to": STATE_MINTED, "at": format_timestamp(now)}],
        }
        for extra_key, extra_value in (extra_payload_fields or {}).items():
            if extra_key in payload:
                raise DecisionTokenError(f"extra_payload_fields 不得覆盖既有字段 {extra_key!r}")
            payload[extra_key] = extra_value
        path = decision_token_path(token, root)
        try:
            _atomic_create_json(path, payload)
            return DecisionTokenRecord(token=token, path=path, payload=payload)
        except FileExistsError:
            continue
    raise DecisionTokenError(f"连续 {DEFAULT_TOKEN_ALLOCATION_ATTEMPTS} 次都未能分配唯一决策 token")


def _discard_token_record(record: DecisionTokenRecord) -> None:
    """丢弃一枚刚铸但因竞态需作废的 token 文件（best-effort）。"""
    try:
        record.path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 状态推进接口（Stop hook + rvf-agent CLI 共用）
# ---------------------------------------------------------------------------

def mark_injected(
    token: str,
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
) -> DecisionTokenRecord:
    """``minted → injected`` = 「**agent 确实拿到了这份正文**」。已是 injected 则幂等返回。

    这条语义对**两条 transport 通吃**，推进权一律由「观测到 agent 拿到了」驱动：

    * **同步自注入**：``main()`` 在决策函数返回后**同步**把正文写进 stdout，故 Stop hook 侧当场推进
      （行为与本轮改动前逐字一致）。
    * **带外投递**（横幅经 cline-kanban 投成一条真实 user message）：Stop 这一刻 agent 什么也没拿到，
      **绝不**在此推进；真正的推进点是目标会话的 UserPromptSubmit hook 观测到
      ``RVF_DECISION_TOKEN=<token>`` 落进 prompt 的那一刻。

    统一之后 TTL 对账的判据自然正确：到期仍 ``minted`` = 投出去了没人接 ⇒ ``lost`` 并告警；
    到期已 ``injected`` 但未 acknowledge = agent 看到了没决定 ⇒ 按既有语义静默 ``expired``。

    遵循 87e9338「确认落地才 arm」教训：仅在真正确认交付后推进，不做乐观预 arm。

    读 + 写整段在每 token 排他锁内：这条推进与 Stop hook 侧的
    ``record_out_of_band_delivery_status`` 天然并发，无互斥时后写的一方会把先写的一方整份抹掉。
    """
    now_dt = _coerce_now(now)
    with _with_decision_token_payload_mutation_lock(
        _decision_token_path_for_payload_mutation_lock(token, root)
    ):
        record = _load_active_record(token, root=root, now=now_dt, ttl_seconds=ttl_seconds)
        if record.state == STATE_INJECTED:
            return record
        return _apply_lifecycle_transition(record, STATE_INJECTED, now=now_dt)


@dataclass(frozen=True)
class DecisionTokenFirstMintedToInjectedTransitionClaimOutcome:
    """``claim_first_minted_to_injected_transition_exclusively`` 的结果。

    ``this_call_won_first_minted_to_injected_transition`` 为真 ⇔ 本次调用在 token 排他锁内亲手
    执行了 ``minted → injected``；为假 ⇔ 锁内看到 token 已经到达过（injected / acknowledged），
    本次什么都没改。``state_observed_inside_lock_before_claim`` 是锁内读到的那一刻的状态。
    """

    record: DecisionTokenRecord
    this_call_won_first_minted_to_injected_transition: bool
    state_observed_inside_lock_before_claim: str


def claim_first_minted_to_injected_transition_exclusively(
    token: str,
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
) -> DecisionTokenFirstMintedToInjectedTransitionClaimOutcome:
    """在 token 排他锁内**原子地**抢「首次 ``minted → injected``」的迁移权，并如实报告是否抢到。

    专供带外投递的 UPS 观测点：正文只能在首次到达时注入一次。``mark_injected`` 对已 injected 幂等
    返回、分不出「是我推进的」还是「别人已推进」，调用方若在锁外先读 state 判定首次到达、再调它，
    同一 token 的两次 UPS 并发到达都会读到 minted、都认为自己是首次 ⇒ 正文被注入两次。故把
    「判定 + 迁移」收进同一把锁：

    * 锁内读到 injected / acknowledged ⇒ 已到达过，不改任何东西，返回 ``won=False``（TTL 已过也
      照此返回——重复到达与 TTL 无关，不该落进「推进失败」那条不相干的出口）；
    * 否则按 ``mark_injected`` 同一路径加载（含可复活则复活）并迁移，返回 ``won=True``；TTL 过期
      不可复活 / 缺失 / 非法迁移照旧抛 ``DecisionTokenError`` 子类。

    ``mark_injected`` 本身不变：Stop 自注入与 manual 路径仍按其幂等语义调用。
    """
    now_dt = _coerce_now(now)
    with _with_decision_token_payload_mutation_lock(
        _decision_token_path_for_payload_mutation_lock(token, root)
    ):
        lookup_inside_lock = read_decision_token(token, root=root, now=now_dt)
        if lookup_inside_lock.payload is not None and lookup_inside_lock.state in (
            STATE_INJECTED,
            STATE_ACKNOWLEDGED,
        ):
            return DecisionTokenFirstMintedToInjectedTransitionClaimOutcome(
                record=_record_from_lookup(lookup_inside_lock),
                this_call_won_first_minted_to_injected_transition=False,
                state_observed_inside_lock_before_claim=str(lookup_inside_lock.state),
            )
        record = _load_active_record(token, root=root, now=now_dt, ttl_seconds=ttl_seconds)
        state_before_claim = record.state
        advanced = _apply_lifecycle_transition(record, STATE_INJECTED, now=now_dt)
        return DecisionTokenFirstMintedToInjectedTransitionClaimOutcome(
            record=advanced,
            this_call_won_first_minted_to_injected_transition=True,
            state_observed_inside_lock_before_claim=state_before_claim,
        )


def record_out_of_band_delivery_status(
    token: str,
    *,
    status: str | None,
    detail: Mapping[str, Any] | None = None,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> DecisionTokenRecord:
    """把带外投递的回执落到 token 上（**正交字段，不动 ``state``**）。

    ``status`` 是 cline-kanban 终端投递契约的取值（``delivered_and_submit_confirmed`` /
    ``delivered_queued_behind_active_agent_turn`` / ``accepted_pending_submit_confirmation`` /
    ``delivery_failed``），对本模块不透明；``None`` 即清除。

    **为什么不做成新的 lifecycle 状态**：``acknowledge_decision_token`` 只接受
    minted/injected/acknowledged，任何插在中间的新状态都会让随后的 ``rvf-agent start`` 报非法迁移。
    投递状态与决策状态本就是两个维度，硬塞进一条状态机只会同时毁掉两者。

    作用于 TTL 已过期的 token 也合法（``_read_record_any``）：回执迟到时我们仍要如实记账。

    **读 + 写整段必须在每 token 排他锁内**：本函数与目标会话 UPS hook 的 ``mark_injected``
    真实并发（``delivered_and_submit_confirmed`` 的语义就是「消息已确认进入 agent」）。若本函数
    在锁外读快照、随后整份覆盖，``state`` 会被回滚成 ``minted``、``injected_at`` 与 history
    条目一并丢失，随后搁浅对账会作废一份**确实送达并已被 agent 看到**的横幅（破坏 INV-A）。
    """
    now_dt = _coerce_now(now)
    with _with_decision_token_payload_mutation_lock(
        _decision_token_path_for_payload_mutation_lock(token, root)
    ):
        record = _read_record_any(token, root=root, now=now_dt)
        payload = dict(record.payload)
        payload["out_of_band_delivery_status"] = status
        payload["out_of_band_delivery_status_updated_at"] = format_timestamp(now_dt)
        if detail is not None:
            payload["out_of_band_delivery_detail"] = dict(detail)
        _atomic_write_json(record.path, payload)
    return DecisionTokenRecord(token=record.token, path=record.path, payload=payload)


def record_decision_point_banner_body_text_for_agent(
    token: str,
    *,
    body_text: str,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> DecisionTokenRecord:
    """把带外投递的决策点正文持久化进 token payload（**正交字段，不动 ``state``**）。

    必须在投递**之前**成功：边界标记一旦落地，UPS 就要从这里读正文；读不到的话 agent 只拿到一行
    无指令的标记，这次征询白白消耗掉。所以 Stop 侧写失败即当场回落自注入（此刻还没有任何外部副作用）。

    与 ``record_out_of_band_delivery_status`` / ``mark_injected`` 同一把每 token 排他锁，理由相同：
    锁外读快照再整份覆盖会把并发写入的另一方抹掉。
    """
    if not isinstance(body_text, str) or not body_text.strip():
        raise ValueError("decision point banner body text must be a non-empty string")
    now_dt = _coerce_now(now)
    with _with_decision_token_payload_mutation_lock(
        _decision_token_path_for_payload_mutation_lock(token, root)
    ):
        record = _read_record_any(token, root=root, now=now_dt)
        payload = dict(record.payload)
        payload[DECISION_POINT_BANNER_BODY_TEXT_FOR_AGENT_PAYLOAD_KEY] = body_text
        _atomic_write_json(record.path, payload)
    return DecisionTokenRecord(token=record.token, path=record.path, payload=payload)


def mark_withdrawn(
    token: str,
    *,
    reason: str,
    withdrawal_cause: str = DECISION_POINT_WITHDRAWAL_CAUSE_REWOUND_BY_USER,
    root: str | Path | None = None,
    now: datetime | None = None,
    circuit_breaker_root_dir: str | Path | None = None,
    withdraw_only_if_state_inside_lock_is: frozenset[str] | None = None,
) -> DecisionTokenRecord:
    """旁路终态 ``… → withdrawn``：这次征询已被用户 rewind / 被 RVF 主动撤回。

    ``withdraw_only_if_state_inside_lock_is``：调用方在**锁外**读了一次状态、据此判定要撤回时，必须把
    「撤回的前提」写成这里的集合，由锁内复核（CAS）。锁内状态不在集合里 ⇒ 抛
    :class:`DecisionTokenWithdrawalPreconditionNotMetError`、什么都不改。``None`` = 不复核（只校验迁移合法，
    与改动前逐字一致）。合法迁移表管不住这一层：``acknowledged → withdrawn`` 对「用户在 start 之后 rewind」
    是合法的，但对「锁外读到 injected 判为 rewind 重交、进锁时 agent 已 start」就是误撤一轮已接受的 run。

    进入终态的同时做两件对账：

      * **移除去重索引条目**（**两种成因都做**），使下一次 Stop 能且只能重新征询一次。少了它，
        下一次 Stop 会命中 ``deduplicated`` 而静默停——那是漏审方向，比多问一次严重得多。
      * **按 ``withdrawal_cause`` 决定要不要冲销这枚 token 铸造时占用的那一份熔断额度**。

    ``withdrawal_cause`` 是**枚举**（``DECISION_POINT_WITHDRAWAL_CAUSES``），与自由文本的
    ``reason`` 分开：``reason`` 是给人读的诊断串，成因判定必须落在一个封闭取值上，否则改一句
    文案就会静默改变熔断行为。取值语义与分叉理由见 ``DECISION_POINT_WITHDRAWAL_CAUSE_*`` 常量组。
    默认值保持 INV-C 原义（``rewound_by_user`` → 冲销），使既有调用点行为逐字不变。

    未知取值 **fail closed 到不冲销**（而不是抛）：撤回对账是 best-effort 链路上的一环，为一个
    拼错的成因串把 Stop 决策整条打断，比少还一份额度危险；而「不冲销」的最坏后果只是提前一次
    退避，方向安全。

    刻意**不**写 declined 冷却表：撤回不是 agent 做出的谢绝决策。

    整段（读 → 前验合法性 → 删索引 → 冲销 → **单次**落盘）都在该 token 的排他锁内：
    合法性必须在动索引/动 ledger **之前**验掉（非法迁移要什么都不改），而落盘只写一次——
    「迁移写一次、补字段再写一次」是同族的「无互斥整份 read-modify-write」形状，即便有锁也不该
    留着。

    作用于可能已 TTL 过期的 token（``_read_record_any``）：撤回对账多半发生在 TTL 边缘。
    """
    now_dt = _coerce_now(now)
    with _with_decision_token_payload_mutation_lock(
        _decision_token_path_for_payload_mutation_lock(token, root)
    ):
        record = _read_record_any(token, root=root, now=now_dt)
        state_inside_lock = str(record.payload.get("state"))
        if (
            withdraw_only_if_state_inside_lock_is is not None
            and state_inside_lock not in withdraw_only_if_state_inside_lock_is
        ):
            raise DecisionTokenWithdrawalPreconditionNotMetError(
                f"撤回前提不成立：锁内状态是 {state_inside_lock}，调用方要求 "
                f"{sorted(withdraw_only_if_state_inside_lock_is)}（token={record.token}）"
            )
        _ensure_lifecycle_transition_is_legal(
            state_inside_lock, STATE_WITHDRAWN, token=record.token
        )
        extra_updates: dict[str, Any] = {
            "withdrawn_reason": reason,
            "withdrawal_cause": withdrawal_cause,
        }
        removed_index = _clear_dedup_index_for_token_payload(record.payload, root=root)
        if removed_index is not None:
            extra_updates["withdrawn_dedup_index_path"] = str(removed_index)
        # 成因决定要不要把熔断额度还回去。白名单判定 ⇒ 未知成因落到「不冲销」。
        if (
            withdrawal_cause
            in DECISION_POINT_WITHDRAWAL_CAUSES_THAT_REVERSE_MINT_REQUEST_CHARGE
        ):
            reversed_mint_request_record_id = (
                _reverse_mint_request_charge_for_withdrawn_token(
                    record.payload,
                    reason=reason,
                    now=now_dt,
                    circuit_breaker_root_dir=circuit_breaker_root_dir,
                )
            )
            if reversed_mint_request_record_id is not None:
                extra_updates["withdrawn_mint_request_charge_reversal_of_record_id"] = (
                    reversed_mint_request_record_id
                )
        else:
            # 如实记账：这枚 token 烧掉的那份额度**没有**被还回去，熔断计数照常推进。
            extra_updates["withdrawn_mint_request_charge_retained"] = True
        return _apply_lifecycle_transition(
            record, STATE_WITHDRAWN, now=now_dt, extra_updates=extra_updates
        )


def iter_decision_tokens_for_session(
    *,
    worktree: str,
    session_id: str | None,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> list[DecisionTokenLookup]:
    """枚举 ``(worktree, session_id)`` 名下的全部 token（best-effort，坏文件跳过，永不抛）。

    单一真相源：Stop hook 的投递回执对账、带外在途查询、rewind 对账三处共用它，避免各写一份
    glob+过滤而在字段名上分叉。
    """
    results: list[DecisionTokenLookup] = []
    if not session_id:
        return results
    try:
        base = decision_token_root(root)
        if not base.is_dir():
            return results
        token_files = sorted(base.glob("*.json"))
    except OSError:
        return results
    for path in token_files:
        try:
            lookup = read_decision_token(path.stem, root=root, now=now)
        except DecisionTokenError:
            continue
        payload = lookup.payload
        if not isinstance(payload, dict):
            continue
        if payload.get("worktree") != worktree or payload.get("session_id") != session_id:
            continue
        results.append(lookup)
    return results


def iter_decision_tokens_for_session_in_any_worktree(
    *,
    session_id: str | None,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> list[DecisionTokenLookup]:
    """枚举 ``session_id`` 名下、**不论哪个 worktree** 的全部 token（best-effort，坏文件跳过，永不抛）。

    专供「agent 复工」判定：复工是**会话级**事实（同一个 agent 在同一个会话里又干起活来），与那枚 token
    当初按哪个 worktree 算的候选无关；而 Stop 的递归短路分支手里只有 cwd、拿不到候选摘要解析出的
    worktree 串，按 worktree 过滤会把它整个漏掉。复工撤回的空转上界计数也必须用它（与撤回同一口径），
    否则轮流落在不同 worktree 上的撤回序列会被拆散、永远到不了阈值。
    """
    results: list[DecisionTokenLookup] = []
    if not session_id:
        return results
    try:
        base = decision_token_root(root)
        if not base.is_dir():
            return results
        token_files = sorted(base.glob("*.json"))
    except OSError:
        return results
    for path in token_files:
        try:
            lookup = read_decision_token(path.stem, root=root, now=now)
        except DecisionTokenError:
            continue
        if not isinstance(lookup.payload, dict):
            continue
        if lookup.payload.get("session_id") != session_id:
            continue
        results.append(lookup)
    return results


def count_most_recent_consecutive_decision_tokens_withdrawn_because_agent_resumed_work(
    lookups: list[DecisionTokenLookup],
) -> int:
    """按 ``minted_at`` 从新到旧数：最近连续多少枚 token 都以「agent 复工」撤回（纯函数，不读盘）。

    遇到第一枚别的结局（被决策 / 被别的成因撤回 / 仍在途 / 同步自注入……）即停：连续性一断，空转就
    不成立。``minted_at`` 不可解析的 token 排到最旧，不参与打断也不参与计数之外的判断。
    """

    def _minted_at_sort_key(lookup: DecisionTokenLookup) -> datetime:
        raw = (lookup.payload or {}).get("minted_at")
        try:
            return parse_timestamp(raw) if isinstance(raw, str) else datetime.min.replace(tzinfo=timezone.utc)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    consecutive = 0
    for lookup in sorted(lookups, key=_minted_at_sort_key, reverse=True):
        payload = lookup.payload or {}
        if (
            payload.get("state") == STATE_WITHDRAWN
            and payload.get("withdrawal_cause")
            == DECISION_POINT_WITHDRAWAL_CAUSE_AGENT_RESUMED_WORK_BEFORE_DECISION_POINT_DELIVERY_LANDED
        ):
            consecutive += 1
            continue
        break
    return consecutive


def find_decision_token_awaiting_out_of_band_delivery(
    *,
    worktree: str,
    session_id: str | None,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> DecisionTokenLookup | None:
    """本 (worktree, session) 是否已有一枚**带外投出去、尚未被 agent 接到**的横幅。

    判据 = ``transport 是带外`` **且** ``TTL 未过期`` **且** ``state 仍是 minted``。
    ``injected`` 不算在途：那说明 UPS 已经观测到它落地了，决策权已经在 agent 手上（走的是
    「已接到、还没决定」那条既有路径）。

    D2 第 2 步用它挡住迟到窗口内的重复铸币——**它在 mint 之前生效，故不写 mint-request ledger、
    不计熔断**。闸挡住不是「重复铸币」，不该烧熔断额度。
    """
    for lookup in iter_decision_tokens_for_session(
        worktree=worktree, session_id=session_id, root=root, now=now
    ):
        payload = lookup.payload or {}
        if (
            payload.get("decision_point_delivery_transport")
            != DECISION_POINT_DELIVERY_TRANSPORT_CLINE_KANBAN_TASK_CHAT_SESSION
        ):
            continue
        if lookup.status != "valid":
            continue
        if lookup.state != STATE_MINTED:
            continue
        return lookup
    return None


def acknowledge_decision_token(
    token: str,
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
) -> DecisionTokenRecord:
    """``minted|injected|acknowledged → acknowledged``（rvf-agent ``start``；acknowledged 为非终态）。

    每次调用递增 ``start_attempt_count``（首次记 ``acknowledged_at``），支撑 ``start --retry``：
    重量级准备失败后回退到 acknowledged、再次 start 即再走一轮 acknowledge。
    """
    now_dt = _coerce_now(now)
    with _with_decision_token_payload_mutation_lock(
        _decision_token_path_for_payload_mutation_lock(token, root)
    ):
        record = _load_active_record(token, root=root, now=now_dt, ttl_seconds=ttl_seconds)
        if record.state not in (STATE_MINTED, STATE_INJECTED, STATE_ACKNOWLEDGED):
            raise DecisionTokenStateError(
                f"非法状态迁移: {record.state} → {STATE_ACKNOWLEDGED}（token={record.token}；已达终态不可再 start）"
            )
        extra: dict[str, Any] = {"start_attempt_count": int(record.payload.get("start_attempt_count") or 0) + 1}
        if "acknowledged_at" not in record.payload:
            extra["acknowledged_at"] = format_timestamp(now_dt)
        return _apply_lifecycle_transition(record, STATE_ACKNOWLEDGED, now=now_dt, extra_updates=extra)


def record_start_failure(
    token: str,
    *,
    reason: str,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
) -> DecisionTokenRecord:
    """记录 ``start`` 重量级准备失败并**回退保持在 acknowledged**（供 ``start --retry``）。

    仅在 ``acknowledged`` 状态合法：dispatched/declined（终态）或 minted/injected（尚未 start）均拒绝，
    以坐实「dispatched 前才可从失败回退到 acknowledged」的边界。状态不变，仅落盘失败诊断。
    """
    now_dt = _coerce_now(now)
    with _with_decision_token_payload_mutation_lock(
        _decision_token_path_for_payload_mutation_lock(token, root)
    ):
        record = _load_active_record(token, root=root, now=now_dt, ttl_seconds=ttl_seconds)
        if record.state != STATE_ACKNOWLEDGED:
            raise DecisionTokenStateError(
                f"只能在 acknowledged 状态记录 start 失败回退，当前={record.state}（token={record.token}）"
            )
        payload = dict(record.payload)
        payload["last_start_failure"] = {"reason": reason, "at": format_timestamp(now_dt)}
        _atomic_write_json(record.path, payload)
    return DecisionTokenRecord(token=record.token, path=record.path, payload=payload)


def attach_run_anchor(
    token: str,
    *,
    run_dir: str,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
) -> DecisionTokenRecord:
    """把在途 ``run_dir`` 锚点写入 token（``start``/prepare 建好 run 后由 CLI 调用），供 TTL 复活判据。"""
    now_dt = _coerce_now(now)
    with _with_decision_token_payload_mutation_lock(
        _decision_token_path_for_payload_mutation_lock(token, root)
    ):
        record = _load_active_record(token, root=root, now=now_dt, ttl_seconds=ttl_seconds)
        if record.state not in ACTIVE_NONTERMINAL_STATES:
            raise DecisionTokenStateError(
                f"只能在活跃非终态挂载 run 锚点，当前={record.state}（token={record.token}）"
            )
        payload = dict(record.payload)
        payload["run_anchor_dir"] = run_dir
        _atomic_write_json(record.path, payload)
    return DecisionTokenRecord(token=record.token, path=record.path, payload=payload)


@contextmanager
def exclusive_review_start(token: str, *, root: str | Path | None = None):
    """同一 token 只允许一个启动调用准备 run；进程退出即释放。"""
    path = decision_token_path(token, root).with_suffix(".review-start.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def mark_dispatched(
    token: str,
    *,
    strictness: str | None = None,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
) -> DecisionTokenRecord:
    """``acknowledged → dispatched``（rvf-agent ``dispatch``，终态）。minted/injected 直接 dispatch 为非法。"""
    now_dt = _coerce_now(now)
    with _with_decision_token_payload_mutation_lock(
        _decision_token_path_for_payload_mutation_lock(token, root)
    ):
        record = _load_active_record(token, root=root, now=now_dt, ttl_seconds=ttl_seconds)
        extra: dict[str, Any] = {}
        if strictness is not None:
            extra["dispatched_strictness"] = strictness
        return _apply_lifecycle_transition(record, STATE_DISPATCHED, now=now_dt, extra_updates=extra)


def mark_declined(
    token: str,
    *,
    reason: str,
    decline_kind: str,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
    cooldown_root: str | Path | None = None,
) -> DecisionTokenRecord:
    """``minted|injected|acknowledged → declined``（rvf-agent ``decline``，终态）。

    ``decline_kind`` 必填，取 :data:`DECLINE_KINDS` 之一，决定谢绝之后多久再问：

    * ``structural_not_needed``（R24 / 在途 run、琐碎、非本会话、开发者明确不要审）：把
      ``(worktree, candidate_fingerprint)`` 写入 declined 冷却表——指纹未变即不再注入；并按
      ``(worktree, session_id)`` 写一份会话级 snooze 快照（闸 2b）——内容变了但工作范围没变也不再注入。
      snooze 写入是 best-effort（见 :func:`record_declined_session_scope_snooze`），失败不影响 decline。
    * ``deferred_until_condition``（现在不审，条件满足后再审）：冷却与 snooze **都不写**，只记一条
      「等到回合边界再重新征询」（闸 2c，见 :func:`record_deferred_until_condition_decline`）。

    结构性谢绝顺带清掉本会话本 worktree 上旧的延后记录（新判断取代旧承诺）；延后型谢绝覆盖写入。
    """
    if decline_kind not in DECLINE_KINDS:
        raise DecisionTokenError(
            f"decline_kind 必须是 {sorted(DECLINE_KINDS)} 之一，收到 {decline_kind!r}"
        )
    now_dt = _coerce_now(now)
    with _with_decision_token_payload_mutation_lock(
        _decision_token_path_for_payload_mutation_lock(token, root)
    ):
        record = _load_active_record(token, root=root, now=now_dt, ttl_seconds=ttl_seconds)
        updated = _apply_lifecycle_transition(
            record,
            STATE_DECLINED,
            now=now_dt,
            extra_updates={"decline_reason": reason, "decline_kind": decline_kind},
        )
    declined_worktree = str(updated.payload.get("worktree"))
    declined_session_id = str(updated.payload.get("session_id") or "")
    if decline_kind == DECLINE_KIND_DEFERRED_UNTIL_CONDITION:
        record_deferred_until_condition_decline(
            worktree=declined_worktree,
            session_id=declined_session_id,
            candidate_summary=updated.payload.get("candidate_summary"),
            candidate_fingerprint=str(updated.payload.get("candidate_fingerprint") or "") or None,
            reason=reason,
            token=updated.token,
            now=now_dt,
            cooldown_root=cooldown_root,
        )
        return updated
    clear_deferred_until_condition_decline(
        worktree=declined_worktree, session_id=declined_session_id, cooldown_root=cooldown_root
    )
    record_declined_fingerprint(
        worktree=str(updated.payload.get("worktree")),
        candidate_fingerprint=str(updated.payload.get("candidate_fingerprint")),
        reason=reason,
        token=updated.token,
        now=now_dt,
        cooldown_root=cooldown_root,
    )
    record_declined_session_scope_snooze(
        worktree=str(updated.payload.get("worktree")),
        session_id=str(updated.payload.get("session_id") or ""),
        candidate_summary=updated.payload.get("candidate_summary"),
        reason=reason,
        token=updated.token,
        now=now_dt,
        cooldown_root=cooldown_root,
    )
    return updated


def mark_lost(
    token: str,
    *,
    reason: str | None = None,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> DecisionTokenRecord:
    """旁路终态 ``… → lost``（S4 投递回执 TTL 超时无任一信号；作用于可能已 TTL 过期的 token）。"""
    now_dt = _coerce_now(now)
    with _with_decision_token_payload_mutation_lock(
        _decision_token_path_for_payload_mutation_lock(token, root)
    ):
        record = _read_record_any(token, root=root, now=now_dt)
        extra: dict[str, Any] = {}
        if reason is not None:
            extra["lost_reason"] = reason
        return _apply_lifecycle_transition(record, STATE_LOST, now=now_dt, extra_updates=extra)


def mark_expired(
    token: str,
    *,
    reason: str | None = None,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> DecisionTokenRecord:
    """旁路终态 ``… → expired``（显式把一枚 TTL 死亡且不复活的 token 终局化；作用于已过期 token）。"""
    now_dt = _coerce_now(now)
    with _with_decision_token_payload_mutation_lock(
        _decision_token_path_for_payload_mutation_lock(token, root)
    ):
        record = _read_record_any(token, root=root, now=now_dt)
        extra: dict[str, Any] = {}
        if reason is not None:
            extra["expired_reason"] = reason
        return _apply_lifecycle_transition(record, STATE_EXPIRED, now=now_dt, extra_updates=extra)


def revive_decision_token(
    lookup_or_token: DecisionTokenLookup | str,
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_DECISION_TOKEN_TTL_SECONDS,
) -> DecisionTokenRecord:
    """把一枚**已过 TTL 但 run 在途**的 token 就地续期为原状态（参照 rvf_prep_file 的 FU-1 方案 B）。

    仅当读取状态为 ``expired``、生命周期仍处活跃非终态、且锚定 run_dir 仍在磁盘时才复活：重写
    ``created_at`` / ``expires_at``、递增 ``revived_count``，**保持 state 不变**。其余情形抛
    ``DecisionTokenError`` / ``DecisionTokenExpiredError``。
    """
    if ttl_seconds <= 0:
        raise DecisionTokenError("ttl_seconds 必须为正")
    now_dt = _coerce_now(now)
    lookup = (
        read_decision_token(lookup_or_token, root=root, now=now_dt)
        if isinstance(lookup_or_token, str)
        else lookup_or_token
    )
    if lookup.status != "expired" or lookup.payload is None:
        raise DecisionTokenError(f"仅可复活 expired 状态的 token，当前 status={lookup.status}")
    if lookup.state not in ACTIVE_NONTERMINAL_STATES:
        raise DecisionTokenError(f"仅可复活活跃非终态 token，当前 state={lookup.state}")
    if not _run_anchor_present(lookup.payload):
        raise DecisionTokenExpiredError("token 已过 TTL 且无在途 run 锚点，不复活")
    # 续期同样是「读整份 → 改 → 整份覆盖」，一样要在每 token 排他锁内落盘，并以**锁内重读**
    # 的最新 payload 为写入基底（lookup 多半是锁外读出来的）。
    with _with_decision_token_payload_mutation_lock(lookup.path):
        payload = _reload_token_payload_from_disk_or_keep(
            DecisionTokenRecord(token=lookup.token, path=lookup.path, payload=lookup.payload)
        )
        payload["created_at"] = format_timestamp(now_dt)
        payload["expires_at"] = format_timestamp(now_dt + timedelta(seconds=ttl_seconds))
        payload["revived_count"] = int(payload.get("revived_count") or 0) + 1
        _atomic_write_json(lookup.path, payload)
    return DecisionTokenRecord(token=lookup.token, path=lookup.path, payload=payload)


# ---------------------------------------------------------------------------
# 清扫（best-effort：删除已终态或真过期不复活的 token 与其陈旧去重索引）
# ---------------------------------------------------------------------------

def sweep_stale_decision_tokens(
    *,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """清扫已终态 / 真过期不复活的 token 文件，返回被删路径列表（陈旧去重索引由下轮 mint 覆盖）。"""
    base = decision_token_root(root)
    if not base.is_dir():
        return []
    removed: list[Path] = []
    now_dt = _coerce_now(now)
    for path in sorted(base.glob("*.json")):
        lookup = read_decision_token(path.stem, root=base, now=now_dt)
        if lookup.status == "valid" and lookup.state in ACTIVE_NONTERMINAL_STATES:
            continue
        if lookup.status == "expired" and lookup.revivable:
            continue
        if lookup.status in ("invalid_token",):
            continue
        try:
            path.unlink()
        except OSError:
            continue
        # 连同它的 payload 读-改-写锁文件一并清掉，否则每枚 token 都会在根目录留一个空锁文件
        # 永久堆积。此刻该 token 已是终态/真过期，不再会有新的 payload 变更需要互斥；极端情况下
        # 若真有并发持有者，最坏也只是那一瞬失去互斥（回到加锁前的行为），不会损坏数据。
        try:
            decision_token_payload_mutation_lock_path(path).unlink()
        except OSError:
            pass
        removed.append(path)
    return removed
