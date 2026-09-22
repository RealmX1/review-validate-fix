# kanban-followup 迟到落地：纯计时器在途去重导致重复派发 + prep 落地即废

日期：2026-09-23（事故日 2026-07-28，诊断于 2026-07-29 完成，本文补入截至 local main `90a6160` 的现状复核）
性质：**只读诊断，不含任何源码改动**。第 5 节的修复方向是建议，本轮未实施。

## 0. 一句话

「重叠 follow-up」历史上确实修过（`5de0c59`），但 `87e9338` 显式接受了「dispatch→delivery 在途窗口
无去重」这条权衡，理由是「成功时亚秒级」——该前提只对 **app-server 通道**成立。`107e383` 事后用一个
**15 分钟纯计时器**（pending marker）把去重补了回来，而 terminal fallback 的真实投递延迟由「目标 agent
还要忙多久」决定，**无上界**，实测 10–17 分钟。计时器于是在旧 prompt 仍排队时判它「丢投」并重投。

## 1. 现象

task `052e2` / session `1f5acdcd`：一轮 RVF 尚未收尾，Stop hook 又派发了新的一轮；随后送达的
follow-up prompt 被 UPS 判为 `检测到 dispatch token 但 prep 不可用，未跑`（`status=missing`）。
主 agent 于是自行接手手动跑 RVF。

## 2. 时间线（UTC，逐条有 ledger 物证）

| 时刻 | 事件 |
|---|---|
| 16:54:54.108 | run `rvf-20260728T165420Z-stop-hook-4e74ecb0` 写 prep，token `32cce7cf7095790d`，TTL 300s → **16:59:54 过期** |
| 16:54:58.371 | dispatch 完成：`kanban_followup_delivery_channel=terminal`、`kanban_followup_delivery_confirmed=false`；写 pending，TTL 900s → **17:09:58 过期** |
| 16:59:54 后 | prep 文件被某次 `sweep_stale()` 从磁盘删除。两条**有** ledger 的 sweep（16:54:54 与 17:10:33，各 24 个文件）的 `removed_paths` 都**不含**它 → 删除者是无 ledger 记录的那个 sweeper |
| 17:10:20.911 | run `rvf-20260728T171007Z-stop-hook-671e13a2` 记 `kanban_followup_prior_dispatch_unconfirmed` → 清 pending、放行重投。**距 pending 过期仅 22 秒** |
| 17:10:37 | 第二轮 dispatch，token `2810f136fe398bce`，新 pending 至 17:25:37 |
| 17:11:37.439 | **16:54 那条 terminal prompt 这时才真正成为 user turn** —— 比重投决策晚 77 秒，比 dispatch 晚 16m39s |
| 17:11:38.212 | UPS 探针 `status=missing` → `dispatch_no_prep`，「未跑」 |
| 17:13:35 | 下一次 Stop 正确 `skipped / kanban_followup_dispatch_in_flight` —— 去重在窗口**内**是有效的 |
| 17:22:43 | handoff-advisory 顺带清掉 pending。token `2810f136fe398bce` 全程**无任何 UPS 探针** = 第二轮从未落地 |

净结果三件事叠加：被判「丢投」的那一轮其实只是迟到；重投出来的第二轮反而彻底丢了；迟到落地的那一轮
又因 prep 不在而拒跑。

## 3. 根因（四层，相互放大）

### RC-1 在途去重是纯墙钟计时器，而 terminal 投递延迟无上界
terminal fallback 只是把 prompt 打进目标 session 的**输入队列**；它何时成为 turn，取决于该 agent 当前
这一轮还要跑多久。当时的 `_kanban_followup_pending_decision` 把 pending `STALE` 直接**推断**为「静默
丢投」——这是推断，不是证据。

### RC-2 prep TTL(300s) ≪ 在途窗口(900s)，两个 TTL 倒挂
于是存在一个结构性的 10 分钟区间：既拒绝重投，手里那条投递又已不可能再跑。

物证（4 个独立日期，全部 terminal 通道）——**凡真正落地过的 terminal follow-up，无一例外是在 prep
过期之后才落地的**，UPS 一律拒跑：

| token | 日期 | 落地延迟 | UPS 判定 |
|---|---|---|---|
| `6710c29bb365c1e9` | 06-08 | +10m02s | `prep expired` |
| `c303b7c1161f8e93` | 06-09 | +15m20s | `prep expired` |
| `fb2284f552feb6ea` | 06-22 | +9m55s | `prep expired` |
| `32cce7cf7095790d` | 07-28 | +16m39s | `prep missing` |

同期共 24 次 `kanban_followup_prior_dispatch_unconfirmed`（`delivery_channel` **全部**为 terminal）；
其余 20 个 token 在任何 transcript 中都没有作为 user prompt 出现过 = 真丢投，那 20 次重投是对的。
**只有 4/24 是误判，但误判的代价是双份 dispatch + 一次白跑。**

### RC-3 FU-1「过期可复活」的前提被 sweeper 系统性拆掉
`revive_prep_file` 只在 `lookup.status == "expired"`（**文件还在**）时可用；而 `sweep_stale()` 会
unlink 一切非 `valid` 的 prep，**包括投递仍在途的那一条**。FU-1（`3fd275a`）06-24 才 land，故 06-08 /
06-09 / 06-22 三例本可被它救回；而 07-28 这例在 FU-1 之后依然死于 `missing` —— 网被提前拆了。

### RC-4 失效 trigger 对模型不可见（最直接的用户可见伤害）
UPS 的拒绝分支只写 `payload["systemMessage"]`，而 systemMessage 是 **user-only、模型看不到**的。
模型侧收到的仍是一条裸的 `$review-validate-fix` 用户消息 → 它会自行开跑 RVF。本次事故中主 agent
正是这样接手的。

### RC-5 UPS arm 无条件覆盖 in-progress 锁
`arm_kanban_followup_lock_on_delivery` 直接 `write_marker` 整份覆盖，没有「已有 ACTIVE 且 run_id 不同
的锁」这道守卫。当时之所以没真的两轮并跑，纯属 prep 已死这一**意外**——一旦只放开 TTL/sweep 而不补
守卫，就会把「侥幸不双跑」变成真双跑。

## 4. 历史脉络：为什么「修过」却复发

| commit | 日期 | 作用 |
|---|---|---|
| `5de0c59` | 05-22 | 首次「block overlapping kanban followups」：dispatch 时 arm in-progress 锁 |
| `87e9338` | 06-04 | arm 移到 UPS 投递确认（治 squat 锁）。commit message 明写「**残留权衡（已选定接受）：dispatch→delivery 在途窗口无重复 dispatch 防护（成功时亚秒级）**」 |
| `107e383` | 06-07 | terminal fallback 诚实上报 + pending marker 恢复在途去重，**窗口 = 15min 纯计时器** |
| `3fd275a` | 06-24 | FU-1：prep 过期且 run artifacts 在场时就地复活 |

不是回归：是 `87e9338` 显式接受的那条权衡，在 terminal 通道上被现实证伪；而 `107e383` 用一个计时器
近似了「投递是否还在途」这个**本应由证据回答**的问题。

## 5. 现状复核（对照 local main `90a6160`，2026-09-23）

事故诊断完成于 `d6e246c`。此后 base 前进了 60 个提交，本节逐条重验。

| 编号 | 截至 `90a6160` 的状态 | 代码锚点 |
|---|---|---|
| RC-1 | **大幅修复**。`_kanban_followup_pending_decision` 改为先查 `kanban task message-status` 回执：`delivery_failed` → 按 reason 的处置决定是否重投（`wait_for_external_condition` / `requires_human_confirmation` 两类一律不自动重投）；`delivered_*` 且 pending 未过自身 TTL → 明确不重投；非终态且在契约承诺的 196.5s 收敛预算内 → 等待。**残留**：查不到回执（接口不存在 / 硬超时 / 老 marker 无 key）时整体退回纯 TTL 猜测，`degraded_reason` 会落进事件 | `stop_review_validate_fix.py:2543` |
| RC-2 | **未变**。`DEFAULT_TTL_SECONDS = 300` 与 `DEFAULT_PENDING_TTL_SECONDS = 900` 倒挂依旧。同期 in-progress 锁 TTL 已由 1h 提到 2h，prep 没有跟上 | `rvf_prep_file.py:15`、`kanban_followup_lock.py:1119`、`:70` |
| RC-3 | **未变，且可救回窗口更窄**。`sweep_stale()` 仍无 protected-token 概念，仍删一切非 `valid`；复活侧新增 `_dispatch_prep_revive_staleness_veto`，进一步收窄 | `rvf_prep_file.py:305`、`rvf_user_prompt_submit.py:1610` 起 |
| RC-4 | **部分修复**。新增的 `withdrawn` 分支已做到「模型可见 `additionalContext` + 用户可见 `systemMessage`」双管；但 `dispatch_no_prep` 的两个出口仍只写 user-only `systemMessage`（注释明写「不进模型上下文」）→ 模型侧仍是一条裸 `$review-validate-fix` | 已修：`rvf_user_prompt_submit.py:1350`；未修：`:1641`、`:1675` |
| RC-5 | **主路径被前置封堵，结构缺口未补**。`arm_kanban_followup_lock_on_delivery` 仍无条件 `write_marker` 覆盖；但上游新增 withdrawal tombstone + token CAS，迟到落地的投递在 UPS 侧被硬中和，双跑主路径已切断 | `rvf_user_prompt_submit.py:614`、`rvf_withdrawn_dispatch_tombstone.py` |
| 观测缺口 | **未变**。`rvf_agent_decision_cli.py:1330` 的 `sweep_stale()` 仍是裸调、无 ledger —— 这正是 07-28 那个 prep「凭空消失、无记录」的元凶所在；stop hook 侧 `:1437` 有 `dispatch_prep_file_sweep_completed` | 同左 |

### 5.1 同一事故若发生在 `90a6160` 上会怎样

1. 17:10 的 Stop 先查回执。排队未提交对应非终态，已远超 196.5s 收敛预算 → 走 `never_converged`；
   回执查不到则走降级 TTL 路径。**两条路都会正式撤回**（tombstone + 物理取消 + 放 lease + 清锁），
   而不是像当初那样「删掉 pending 假装没发生过」。
2. 17:11 旧 prompt 迟到落地 → UPS 命中 tombstone → **硬中和**，并用 `additionalContext` 明确告诉模型
   「这条迟到落地的触发不应被执行」。主 agent 不会再自行开跑。
3. 仍然会多派一轮 —— 这是**有意**的：对方无法确认送达时，宁可重投。

即：**「双跑」与「模型被裸 trigger 误导」这两条已被堵上；「迟到落地的那一轮必然作废」（RC-2+RC-3）
与「arm 无守卫」（RC-5 结构缺口）仍在。**

## 6. 修复方向（建议，本轮不实施）

- **F1 prep 保活**：`sweep_stale()` 增 `protected_tokens`（在途 pending / in-progress marker 引用的
  token 不删）；prep TTL 与 pending TTL 对齐，消除 300s/900s 倒挂。这能把「落地即废」的 4/4 转成
  「复活后真正跑起来」。**前提**：必须确认 withdrawal tombstone 覆盖所有撤回路径，否则等于把
  「靠 prep 已死而侥幸不双跑」变成真双跑。
- **F2 补齐 arm 守卫**：`arm_kanban_followup_lock_on_delivery` 增「已有 ACTIVE 且 run_id 不同 → 拒绝、
  `STALE` → 允许接管」。tombstone 是止血机制（其自身注释亦如此定位），不该是唯一防线。
- **F3 受众一致性**：`dispatch_no_prep` 的两个出口比照 `withdrawn` 分支补 `additionalContext`。按
  `docs/architecture/stop-hook-output-audience-contract.md` 的口径，「这条 trigger 已作废、不要手动
  开跑」是**需要驱动模型不做某事**的禁令，只给用户看不够。
- **F4 观测**：`rvf_agent_decision_cli.py:1330` 的 sweep 补 ledger 事件；重投决策事件补记「决策 Stop
  是否与 pending 同 session」。

**明确不建议**：
1. 单纯拉长 pending TTL —— 延迟无上界，任何常数都会被穿透，且「必死投递」的窗口同步变长。
2. 「同 session 就不重投」—— 24 例中 22 例的决策 Stop 与 pending 同 session，其中 18 例是真丢投；
   这条判据会把「重复派发」换成「静默漏审」。

## 7. 复核方式（全部只读）

1. 时间线：`~/.claude/rvf/runs/rvf-20260728T{165420Z-stop-hook-4e74ecb0,171007Z-stop-hook-671e13a2,171315Z-stop-hook-0a5fdf79}/{summary.json,events.jsonl}`。
2. 「判丢投→重投」全量：扫 `~/.claude/rvf/runs/*/events.jsonl` 里 `event == "kanban_followup_prior_dispatch_unconfirmed"`（24 条）。
3. 「是否真的落地过」：在 `~/.claude/projects/**/*.jsonl` 中按 token 搜 `type == "user"` 且正文含 `RVF_DISPATCH` 的消息（4/24 落地）。
4. 「谁删了 prep」：把 `dispatch_prep_file_sweep_completed` 事件的 `removed_paths` 与目标 token 对照 —— 不在任何一条里，即无 ledger 的 sweeper 所为。
5. 现状复核：在 local main `90a6160` 上核对第 5 节表格的各处代码锚点。

## 8. 相关工作

- `docs/log/2026-06-07-kanban-followup-terminal-fallback-honest-status-and-pending-reconcile.md` ——
  pending marker 与 15min 窗口的引入记录，即本文 RC-1 的直接来源。
- `docs/architecture/stop-hook-output-audience-contract.md` —— hook 出口受众分级契约，第 6 节 F3 依其口径。
- kanban task `fc0e5`（进行中，`dev_docs_rvf_self_injection_late_arrival.md`）—— 同域不同题：它查的是
  **~60 秒级**的投递延迟与「自注入被 live 用户输入插队」的乱序问题；本文查的是**分钟级**延迟穿透
  在途窗口后的守卫误判。两者互补，不重复。
- 其它同题 task（结论多已落入 base）：`1c4fd`（重复派发频率分析）、`32cad`、`fc980`（等待已派发
  reviewer 时的错误派发）、`7b147`（prep expiration 机制）、`4c388`（跨-task stranded-pending 扫荡设计）。
