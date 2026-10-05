# RVF 范围外问题提交 Kanban backlog，及对看板 support agent 的接口期望

> 状态：RVF 侧已实现（task a0880，2026-10-05）；看板 dedicated support agent 尚未实现，属于 cline-kanban 的独立任务。
> 本文是 RVF 与看板之间的接口约定。RVF 侧行为以代码为准，看板侧行为以本文的期望为准，直到看板侧落地后回填。

## 1. 要解决什么

RVF 过去只有一种方式处理「与本轮工作不紧密相关的问题」：丢弃。
- reviewer 被要求不分析、不报告 scope 外的内容；
- 即使报了，合并阶段也只能 `dropped`，最后只剩一个 reason_code 计数。

开发者 2026-10-04 定下的规则：

- **不放跑**：范围外的真实问题必须留在看板上。
- **不当场修**：不扩大本轮改动。
- **作为新 task 提交到 backlog**：照常通过 review，到 validate-fix 阶段只验证、不修，验证属实后由 validator 提交。

## 2. RVF 侧流程（已实现）

| 阶段 | 行为 | 代码 |
|---|---|---|
| review | reviewer 照常报 issue，范围外的标 `--scope-relation outside_current_round_scope`；观察范围不受限 | `write_review_result.py`、`check_review_result.py`、`prompts/reviewer.md` |
| 合并 | 范围外 issue 照常 `adopted` / `merged_into`；排除区 issue 只能 `dropped` | `rvf_merge_dispositions.py` |
| fix prepare | 按来源 reviewer 的标注预填 `fix_issue_handling_mode=validate_only_then_submit_to_kanban_backlog`；主会话改写须写 `handling_mode_override_reason` | `rvf_fix_issue.py` |
| validate | validate-only attempt 不得有任何改动（硬性）；属实则 `rvf fix stop --status validated_and_submitted_to_kanban_backlog --kanban-backlog-finding-file <json>` | `rvf_fix_attempt.py` |
| 附带提交 | fixer 修复途中发现的其他无关问题，任何 status 都可以带 `--kanban-backlog-finding-file` 一起提交 | 同上 |
| rvf-land | 没有 validator，用 `rvf backlog submit --main-session-findings-file <json>` 提交，卡片标「未经 validator 验证」 | `rvf_out_of_scope_findings_kanban_backlog_submission.py` |
| 记账 | tracker 中 issue 终态为 `submitted_to_kanban_backlog`，attempt 终态为 `validated_and_submitted_to_kanban_backlog`；handoff 计数行增加「转 backlog」一项 | `reviewable_unit_diff_tracker.py`、`write_reviewed_task_candidate_evidence.py` |

finding JSON：

```json
{"findings": [{"title": "一句话", "path": "仓内相对路径", "line": 12, "severity": "high|medium|low",
               "problem_statement": "问题是什么、为何属实", "evidence": "复现命令或代码依据",
               "group_id": "同根因多条合成一张卡时填"}]}
```

## 3. 卡片约定（看板侧读取的契约）

| 项 | 值 |
|---|---|
| 建卡方式 | `kanban task create`：只建不 start（落 backlog）、`--start-in-plan-mode`、不开 auto-review |
| `--project-path` | 主仓路径（git common dir 的父目录），不是 task worktree |
| `--parent-session-id` | **不传**。传了会 fork 父会话上下文，而卡片要求自包含 |
| `--agent-id` | `CODEX_RVF_CLINE_KANBAN_AGENT_ID` > 镜像本 run 的主 harness > kanban 默认 |
| `--base-ref` | 合同 `base_ref` > `CODEX_RVF_CLINE_KANBAN_BASE_REF` > 本地 base 推断 |
| 标题 | 前缀 `[RVF 发现]`，同组多条加「（及另 N 条同根因问题）」 |
| prompt 第 1 行 | `KANBAN_TASK_ORIGIN=rvf_validated_out_of_scope_finding` |
| prompt 第 2 行 | `KANBAN_SUPPORT_AGENT_RELATION_TRIAGE_STATUS=not_triaged`：support agent 上线前恒有，表示未经看板查重 |
| prompt 正文 | 问题、证据、验证情况、来源（run id / run 目录 / handoff 路径 / attempt / issue）、开工前步骤：先确认仍成立 → 用 `cline-kanban-survey-sibling-task-work-in-same-workspace` 查重 → 再修 |

## 4. 查重分两层

1. **RVF 侧本地指纹去重（已实现）**：只拦完全重复。
   - 指纹 = sha256(仓身份 + 规范化路径 + 去空白后的标题与问题描述)；
   - 仓身份用 git common dir，同仓所有 worktree 共享一份 ledger；
   - ledger 放在 `$RVF_HOME/out-of-scope-findings-submitted-to-kanban-backlog/<仓身份>.jsonl`；
   - Kanban 不可用时，卡片暂存到同目录 `pending-task-prompts-awaiting-kanban-runtime/<仓身份>/`，下次任何一次提交都会先重试；也可以手动跑 `rvf backlog retry-pending`。
2. **看板 dedicated support agent 关系分诊（未实现）**：判断「是否已有 task 在修同一问题」以及两者是什么关系。RVF 侧调用缝：`INTEGRATION-POINT(kanban dedicated support agent finding-relation triage)`。

## 5. 对看板 support agent 的期望（grilling 定稿，2026-10-04/05）

| # | 决定 |
|---|---|
| 职责 | 第一版只做「新工作项与现有 task 的关系分诊」。prompt 分层按看板通用知识设计，以后加职责只换指令与查询 |
| 位置 | cline-kanban runtime 功能，新增 `kanban support triage-incoming-work-item --project-path … --work-item-file …` |
| 运行形态 | 无状态调用：runtime 每次按层拼 prompt、调一次 headless agent。没有对话记忆 |
| prompt 分层 | L0 固定指令 → L1 看板层（全看板 task 的极简索引，只在重建时更新）→ L2 项目快照 → L3 追加尾巴（看板级与项目级的日常变化都写在这里）→ L4 本次查询。L0–L2 必须逐字节稳定：不得含时间戳、session id、绝对 worktree 路径 |
| 范围 | 主查本项目；跨项目只给候选，不下最终判定（与收敛契约首版只看同一 project 的口径一致） |
| recap 来源 | 拼 harness 已有材料：标题 + prompt 首段 + 最近通知 `summaryText` + `latestHookActivity.finalMessage` + Claude away summary。非 Claude agent 的 recap 以后派 Task Force 分支生成 |
| 重建时机 | 距上次调用已超过该模型缓存保留时长的实测下限（缓存已冷，重建不额外花钱）；或缓存仍热但 L3 超过 L2 的 25%、过期条目超过阈值、L0 版本变化 |
| 执行模型 | 默认 `claude -p --safe-mode --system-prompt <L0–L2> --tools "" --strict-mcp-config --disable-slash-commands --no-session-persistence`，L3 + L4 经 stdin 传入。`--bare` 不读订阅 OAuth，走订阅时不用。需实测确认 input tokens ≈ 自家 prompt 长度 |
| 处置权限 | 高置信「已有 task 完全覆盖」时不建新卡，把发现追加为已有卡的备注（需要新增追加备注接口），关系记入账本为「待确认的声称」；其余情况建卡并标「可能与 task X 相关」；永不关闭或改写已有卡 |
| 补查 | 上线后，把带 `not_triaged` 标记的 `[RVF 发现]` 卡当作待分诊工作项补查一遍 |
| 问分身 | `INTEGRATION-POINT(Task Force fork relation consultation)`：Task Force 落地后，在中等置信且候选 task 仍在 in_progress / review 时，用 `fork_existing_session` 起分身询问两者的关系。Claude / Codex 的 fork 首轮全价，所以默认关闭、设每日预算 |

相关的看板侧已有资产：
- dd2db 关系模型（`docs/context-inter-work-unit-relation-modeling-overhaul.md`，刻意没有 duplicate 关系）；
- 收敛契约（`.plan/docs/task-relation-modeling-overhaul-completed-work-reconciliation-contract.md`）；
- L1 Recap 规划（task 9cc01）；
- 缓存 TTL 实测（`src/core/coding-agent-prompt-cache-retention-measured-ttl-lower-bounds.ts`）；
- fork 缓存实测（`docs/agent-session-fork-prompt-cache-measurements.md`）。
