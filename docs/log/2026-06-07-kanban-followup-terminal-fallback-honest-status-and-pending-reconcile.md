# kanban-followup 静默丢投：诚实上报 + 投递对账自愈（pending marker）

日期：2026-06-07

## 现象

一次 RVF Stop hook 报出
`review-validate-fix: kanban-followup-started; reason=kanban_followup_started; summary=…/rvf-20260606T175002Z-stop-hook-3f13c9ae/summary.json`，
但**后续的 followup self-injection 从未发生**——目标 Kanban task 里没有出现新的 `$review-validate-fix` turn。

## 根因（用该 run 物证确认）

投递走的是外部 Cline Kanban `kanban task message` CLI 的**乐观 terminal 回执**，而 RVF 把它当成了「已注入」：

1. dispatch 时**无可用 app-server socket**（`summary.json` / `events.jsonl`：`parent_thread_name_lookup.error: AppServerSocketSelectionError: no existing app-server socket available`）。
2. 目标 task **bf042 的 session 处于 `awaiting_review`（已停止）**（`events.jsonl` event 6：`"session":{"state":"awaiting_review","pid":55036,"exitCode":null}`）。
3. 外部 CLI 因此走 terminal fallback，返回 `ok:true, status:"started", turn_id:"3", message_id:"terminal:bf042:rvf-20260606T175002Z-stop-hook-3f13c9ae"`。`terminal:` 前缀来自**外部 CLI**（RVF 仓库内 0 处产生；`cline_kanban_client.py:send_task_message` 只透传 stdout）。
4. RVF 把 `status in {started,running,in_progress}` 直接映射成 `kanban-followup-started`、回 `message="…was injected."`、systemMessage 由 `rvf_logging.py` 拼出用户看到那行。
5. 但 prompt **从未成为真实 turn**：该 run 的唯一 token `341413a4ca1df749` 在 bf042 transcript 中出现 **0 次**（transcript 续写到 20:14Z，比 17:50Z dispatch 晚 2.5h），而历史 followup 的 `RVF_KANBAN_FOLLOWUP_TRIGGER`(80×)/`RVF_DISPATCH`(24×) 都在——说明 bf042 以前能接住，唯独这次丢了。
6. 因投递未落地，目标 session 的 `UserPromptSubmit` 从未 fire → `arm_kanban_followup_lock_on_delivery` 未运行 → `kanban_followup_in_progress_marker_path: null`。

结论：87e9338「arm-on-delivery」本身行为正确（丢投不留 squat 锁），但它与一个既有缺陷共存——**RVF 把不可靠的 terminal fallback 回执上报为确信的「injected/started」**。`terminal:` 投递并非必然失败（历史上也有 `terminal:` 成功 arm 的），它的真实语义是「dispatch 时尚未确认落地」。

## 修复（A 诚实上报 + B 投递对账自愈）

RVF 无法强迫一个已停止的 agent 消费 terminal 队列里的消息（注入由外部 CLI 完成），故聚焦 RVF 侧可观测 + 自愈：

- **A 诚实上报**（`codex_stop_review_validate_fix.py`）：新 helper `_kanban_followup_delivery_channel`——`message_id` 以 `terminal:` 开头判为 `terminal`（未确认），否则 `app-server`（可确认）。terminal 时 status 从 `kanban-followup-started` 降级为 `kanban-followup-dispatched-unconfirmed`、message 改为诚实文案（提示打开/恢复 task 让排队消息被消费），并记 `kanban_followup_delivery_channel` / `kanban_followup_delivery_confirmed`。app-server 路径保持 `kanban-followup-started` 不变。
- **B 投递对账自愈**（新 pending marker 家族 + 对账）：
  - `kanban_followup_lock.py` 新增 `dispatched-unconfirmed(pending)` marker（独立子目录 `kanban-followup-dispatched/`，与 in-progress 物理隔离；TTL 默认 15min，`CODEX_RVF_KANBAN_FOLLOWUP_PENDING_TTL_SECONDS` 可配；`clear_pending_marker` 支持 token 防误清）。
  - dispatch 未确认投递时写 pending（含 token / delivery_channel）。
  - UPS `arm_kanban_followup_lock_on_delivery`（权威「已落地」信号）写 in-progress 锁的同时按 token 清掉 pending。
  - 下一次 Stop 对账（`_kanban_followup_pending_decision`，仅在无 in-progress 锁时）：pending 仍 active=在途窗口 → 跳过重复 dispatch（恢复 87e9338 牺牲掉的在途去重）；pending 已 stale=静默丢投 → 上报 `kanban_followup_prior_dispatch_unconfirmed` + 清 pending + 放行重投。
  - handoff 清锁处对称清 pending。

## 测试

- `tests/test_review_support_scripts.py`：`kanban_followup_pending_marker_round_trip`、`rvf_user_prompt_submit_clears_pending_on_delivery`（已登记进 `review_support_test_cases()`，避免手写 runner 静默不跑）。
- `tests/test_codex_stop_review_validate_fix.py`：`*_terminal_fallback_reports_unconfirmed_and_writes_pending`、`*_active_pending_skips_redispatch`、`*_stale_pending_redispatches_and_reports`（已登记进 `main()` 的 `tests` 列表）。
- 全量 `test_review_support_scripts.py` 绿；codex_stop 直接受影响子集（kanban-followup + handoff）绿。该沙箱内仅有的失败是与本改动无关的环境性问题（无 codex 二进制 → `provider_health_failed`；以及 pytest 缺该文件自带的 `tmp` fixture）。

## 兼容性 / 部署

- 新状态串 `kanban-followup-dispatched-unconfirmed` 仅内部使用，无外部 dispatcher/`.mjs` 依赖；可靠路径仍回 `kanban-followup-started`。forward-only，无 backward-compat shim。
- live hook 当前部署在 `a12da1260a2e`；本修复 land 后需经 `rvf-local-deploy` 重部署，真机触发才生效（否则复现 deploy-lag）。
