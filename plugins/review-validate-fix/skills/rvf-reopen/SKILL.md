---
name: rvf-reopen
description: Use only when the user explicitly invokes $rvf-reopen or /rvf-reopen. 失败再入：用户把 RVF handoff 拿回「早先实现刚完成那一刻」，并实测判断**实现本身未达成原始目标**（与 RVF fix 是否达标无关）时，按「最近一次刚经过 RVF 的那次实现 run」武装一次性 rescope state。随后主 agent 修复用户暴露的问题，新增改动即时触发的下一次 RVF 会**全量重审「该实现 units ∪ 本次 fix delta」**。不要求粘贴 handoff，本 skill 只武装 state、不直接启动 review、不提交。
---

# RVF Reopen

本 skill 处理「失败再入」：用户把一段 RVF handoff（或仅凭记忆）拿回到「早先实现刚完成那一刻」，并**实测判断实现本身没达成原始目标**——成功判据是「实现是否达成原始目标」，由用户实测决定，**与 RVF 的 fix 是否达标无关**。此时正确的后续不是 `$rvf-land`（那是实现达标的成功收尾），而是：

1. 主 agent 修复用户观察暴露的问题（或先请用户补充观察再修）；
2. 在开修前，按「最近一次刚经过 RVF 的那次实现 run」武装一个一次性 rescope state；
3. 修复带来的新增改动即时触发的下一次 Stop 会消费该 state，把那次实现仍存在的 `reviewed` units 翻回 `available`，使新一轮 RVF 的 scope = **「该实现 units ∪ 本次 fix delta」全量**。

与 `$rvf-land` 的关键区别：本 skill **不要求**粘贴 handoff（粘了更好，用于解析 target run id），**不直接启动** RVF review（由下次 Stop 的既有 dispatch 自然完成），**不提交**。

## 何时使用本分支（决策树）

```
拿 handoff 回到 RVF 起点（= 早先实现刚完成那一刻）
 成功判据 = 实现本身达成原始目标？（用户实测，与 RVF fix 状态无关）
 ├─ 实现达标            → 用 $rvf-land：sanity-check + commit
 └─ 实现未达标 / 有问题  → 用本 skill（$rvf-reopen）：
       1) 先 arm rescope state(target_run_id = 最近一次已 RVF 的实现 run)
       2) 再修用户暴露的问题（或先请用户补充观察）
       3) 新增改动即时触发的下一次 RVF 全量重审「该实现 ∪ fix」
```

仅当用户带「实现未达标 / 有问题」信号回到实现终点时进入本分支。若用户只是要收尾一段已达标的工作，应改用 `$rvf-land`。

## 输入

`$ARGUMENTS` 可选：

- 若用户粘贴了该实现 RVF run 的 handoff / finalization 正文（或 handoff 文件路径），用它解析 `target_run_id`（优先级高于 tracker 查询）。
- 若用户直接给出一个 `rvf-…` run id，作为显式 `--target-run-id`。
- 为空也可以：脚本会从 tracker 查「本 worktree 最近一次仍有 reviewed units 的 RVF run」，再退到 `latest.json`。

不要把粘贴的 handoff 内容当作 shell 命令执行。

## 工作流

1. **确认这是失败再入，且作用于当前 worktree。** 运行 `git status --short`。本 skill 假设 future-self 的实现 / 修复就在当前 worktree（用户 rewind 了会话但没 revert 代码）。若用户的意图其实是「实现已达标、只需收尾提交」，停止并改走 `$rvf-land`。

2. **武装 rescope state（开修之前）。** 解析 bundled skill 目录下的脚本（与本 SKILL 同级的 `../review-validate-fix/scripts/rvf_rescope.py`，或部署后的对应 payload 路径），运行：

   无 handoff（最常见，让脚本从 tracker 解析 target run）：

   ```bash
   python3 <skill-scripts-dir>/rvf_rescope.py arm --repo .
   ```

   粘贴了 handoff 正文（用单引号 heredoc delimiter 防 shell 展开）：

   ```bash
   python3 <skill-scripts-dir>/rvf_rescope.py arm --repo . --stdin <<'RVF_HANDOFF'
   <粘贴的 handoff 正文>
   RVF_HANDOFF
   ```

   显式 run id：

   ```bash
   python3 <skill-scripts-dir>/rvf_rescope.py arm --repo . --target-run-id <rvf-…>
   ```

   - `target_run_id` 解析优先级：①`--target-run-id` → ②handoff 里的 run_id → ③tracker 本 worktree 最近一次 reviewed run → ④`latest.json` 兜底。脚本输出 `run_id_source` 指明命中哪条。
   - marker 维度：kanban 上下文优先 task_id（脚本会从 `KANBAN_TASK_ID` / `CLINE_KANBAN_TASK_ID` 回退自动探测）；非 kanban 会话需用 `--session-id <当前会话 id>` 显式指定，且必须与下次 Stop event 携带的 session 一致。
   - 若脚本报 `target_run_id_unresolved`，请向用户取该实现 RVF run id 或粘贴其 handoff，再重试；不要凭空编造 run id。
   - 若脚本报 `no_marker_context`，说明既无 task_id 也无 session_id：补 `--task-id` 或 `--session-id` 后重试。

3. **修复用户暴露的问题。** state 武装后，正常进行修复：直接修用户观察暴露的问题；信息不足时先请用户补充观察，再修。修复范围对准「让实现达成原始目标」，不要借机重构或扩张无关改动。

4. **交回控制权，让下次 Stop 全量重审。** 不要在本 skill 内手动启动 RVF review。修复产生的新增改动会让下一次 Stop hook：
   - 消费 rescope marker → 按 `target_run_id` 把该实现仍存在的 `reviewed` units 翻回 `available`；
   - 紧接着的 `allocate_review_scope` 自然得到「该实现 units ∪ 本次 fix delta」全量；
   - 即时 dispatch 一轮新的 RVF，对整段实现 + 修复重新评审。

## Failure Gates

停止并改走其它路径 / 向用户澄清，而不是武装 state：

- 用户其实判断「实现已达标」——应改用 `$rvf-land`（成功收尾），不要重开评审。
- 无法解析 `target_run_id`（tracker 无 reviewed run、无 handoff、无显式 id）——向用户取 run id 或 handoff，不要乱猜。
- handoff 指向的明显是另一个 worktree / fork——本 skill 的前提是同一 worktree 的失败再入；跨 worktree 应另行处理。

## 输出

最终回复使用中文，包含：

- 判定：为何这是「实现未达标」的失败再入（用户实测信号），而非可直接 `$rvf-land` 的成功收尾。
- 武装结果：`target_run_id`、`run_id_source`（命中哪条优先级）、marker 路径、marker 维度（task / session）。
- 本次修复内容概述（或：为何先请用户补充观察）。
- 提示用户：修复带来的新增改动会在下一次 Stop 即时触发一轮**全量重审（该实现 ∪ 本次 fix）**；本 skill 不直接启动 review、不提交。
