# Handoff Template

Handoff 默认开启，但只适用于 `mode: full` 且 handoff 未关闭的完整流程。除非用户明确要求 `no handoff` / `skip handoff` / `不要 handoff`，主会话必须在当前 RVF run 的 artifacts 目录创建并持续维护 `handoff.md`，最终回复第一行输出 handoff 路径，随后追加 1-3 句极短中文摘要：

```text
RVF_HANDOFF_FILE: <handoff.md 绝对路径>

Reviewers：<极短说明 reviewers 检查了什么，发现了几项或没有问题>
Validate/fixers：<极短说明 validate/fixers 验证/修复/驳回/升级了什么>
```

不要在最终回复里重复 handoff 文件正文；摘要只服务快速确认，完整细节必须在 `handoff.md`。完整 run 在最终回复前先调用 `scripts/rvf_handoff.py open <handoff.md 绝对路径>` 尝试用默认编辑器打开该 markdown 文件；Stop hook 检测到 `RVF_HANDOFF_FILE` 后仍会作为兜底 advisory 处理。`CODEX_RVF_OPEN_HANDOFF=0` 可关闭自动打开，`CODEX_RVF_IDE_OPEN_CMD` 可指定 coding agent IDE 打开命令。

如果用户显式关闭 handoff、当前是 `pass_type: review_only` / `pass_type: validate_fix` 子 pass，或当前是 `mode: research_checkpoint_no_handoff` / `no-handoff research checkpoint`，不创建 `handoff.md`，不输出 `RVF_HANDOFF_FILE`，也不要输出空模板。只给当前任务要求的中文结果。

## 维护时机

- Prepare/run 初始化后立即创建 `handoff.md`，至少写入 pending 状态、origin、run id、run dir、目标 repo、review scope / scope-of-work 文件路径、review packet / manifest 路径（若有）。如果 prompt 或环境提供 `RVF_PARENT_CONVERSATION_NAME` / `RVF_PARENT_CONVERSATION_REF`、`RVF_PARENT_CONVERSATION_NAME_SOURCE`、`RVF_PARENT_CODEX_URL`、`RVF_PARENT_TRANSCRIPT_PATH`、`RVF_ORIGIN_METADATA`，必须在文件顶部原样保留这些字段，方便从 handoff 反查原始 Codex chat。`RVF_PARENT_SESSION_ID` 只是稳定 id，不是 conversation name source；不得把它写进 `conversation name source`。带双引号的 conversation name 表示原始会话没有设置 name，当前值来自第一条 user prompt 的前缀。
- Origin 区域内的 4 条 Kanban 行（`source Kanban task id` / `source Kanban attempt id` / `source Kanban task title at trigger` / `generated Kanban task`）只在 prompt 或环境真的提供 `RVF_PARENT_KANBAN_TASK_ID` 时出现；提供时必须直接渲染实际值，不要写 `unavailable`。当前 run 不是从 Cline Kanban task 派生时，整段 4 行连同标题一并省略，避免 Origin 区出现一片 `unavailable` 噪音。
- Review 阶段后更新 reviewer 来源、review 状态、发现的问题或 `kind: no_issues`。
- Merge 与 validate/fix 阶段后更新 canonical issue、Validate/fix 分组、每条 attempt 完成状态、真实修复 / 误报 / 升级。
- 最终阶段更新 repo delta、验证命令、继续指引、升级事项和 deterministic intake hints；最终回复只给文件路径和极短 reviewers / validate-fixers 摘要。

## Deterministic intake hints

handoff 应尽量把接收方需要反复推断的信息写成稳定字段，降低 earlier-self 的解析成本。最终版 handoff 必须明确：

- RVF worktree / target repo 是否可能不同于主会话 worktree。
- reviewed scope paths：只列本次 RVF 审查和修复实际覆盖的路径。
- protected / background / cross-session paths：列出已发现但不得自动 stage/commit 的路径和原因。
- accepted changes：列出应带回主会话 worktree 的变更类别。
- rejected / not accepted changes：列出本次 RVF 明确移除、驳回或不应采纳的 suggestion / hunk。
- validation commands：给出可在主会话 worktree 中重跑的命令；若原命令依赖 RVF run 环境变量，写出等价替代命令。

接收方可用只读脚本生成机器可读摘要：

```bash
python3 plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_handoff_intake.py \
  --handoff <handoff.md> \
  --repo <current-main-session-repo> \
  --format json
```

## 兼容性边界

这个文件只能作为“继续工作时的上下文压缩 / 交接说明”。不要声称它能让 Claude Code 或 Codex 回到聊天中的任意内部事件位置。

旧 Stop hook 曾把 hook 触发点当作 fork 锚点，但这与 Claude Code 和 Codex 的原生交互模型不兼容：用户只能回退到自己输入过的位置，不能回退到 Stop hook 在会话内部自动触发的任意位置。因此，从旧 hook 迁移时，只保留 handoff 文件的上下文表达价值，不保留“任意 hook 触发点 time-travel”的操作假设。

## handoff.md 模板

```markdown
# Review-validate-fix 交接上下文

## 状态

- handoff_status: PENDING / COMPLETED
- review_status: PENDING / COMPLETED / SKIPPED_BY_USER
- run id: <RVF run id>
- run dir: <state/runs/<run_id>>
- 目标仓库: <绝对路径>
- Review 开始时的 git HEAD: <sha 或 "未提交工作树">

## Origin

- original Codex conversation: <RVF_PARENT_CONVERSATION_NAME 或 RVF_PARENT_CONVERSATION_REF 或 unavailable>
- conversation name source: <RVF_PARENT_CONVERSATION_NAME_SOURCE 或 unavailable>
- original Codex URL: <RVF_PARENT_CODEX_URL 或 unavailable>
- original transcript: <RVF_PARENT_TRANSCRIPT_PATH 或 unavailable>
- origin metadata: <RVF_ORIGIN_METADATA 或 unavailable>
- source Kanban task id: <RVF_PARENT_KANBAN_TASK_ID>
- source Kanban attempt id: <RVF_PARENT_KANBAN_ATTEMPT_ID>
- source Kanban task title at trigger: <RVF_PARENT_KANBAN_TASK_TITLE>
- generated Kanban task: <task title 或 task id>

## 原始任务

<review 触发之前的用户任务，1-2 句>

## Review scope

- scope-of-work: <路径>
- session manifest: <路径或 unavailable>
- review packet: <路径>
- 主审查文件 / 范围: <列表>

## Handoff intake hints

- RVF worktree / target repo: <绝对路径>
- main-session worktree expectation: <same worktree / different worktree expected / unknown>
- reviewed scope paths:
  - <path>
- protected / background / cross-session paths:
  - <path>: <原因；例如 protected background WIP、cross-session conflict、left untouched>
- accepted changes:
  - <应采纳的变更类别或 hunk 描述>
- rejected / not accepted changes:
  - <不应采纳的 suggestion / hunk / 文件范围及原因>
- main-session validation commands:
  - <可在主会话 worktree 重跑的命令>

## 本次 review

共 <N> 条，<V> 真实修复 / <F> 误报 / <E> 升级

## Validate/fix 分组

- `RVF-G1`：<包含的 processed issue id / path:line 列表>
  - 分组理由：<共享根因、同一文件区域、同一测试路径、同一决策前提，或“单独验证，因为...”>
  - 执行：<validate/fix 子代理名；若触发允许本地执行的窄例外，写“本地执行：<原因>”>
  - 结果：<fixed / false_positive / elevated / failed；来自 `rvf_fix_attempt.py stop` ledger>
  - scope expansion：<无，或列出 allowlist 外实际修复路径 + `--scope-expansion-reason`；不得把 protected/background/excluded 路径写成已修复>

## Issue 处理结果

- **[fixed]** `路径:行号` - <短标题>
  - 来源：<codex-reviewer / alternative-reviewer:<agent-name> / codex-mimic-reviewer-a / codex-mimic-reviewer-b / user-supplied-skip-review / 多个来源；仅 handoff 审计用，未传给 validate/fix 子代理>
  - 问题：<1-2 句说明实际出了什么错>
  - 修复：<1-2 句说明做了什么>
  - scope expansion：<若修复修改了 fix_allowlist 外路径，列路径和原因；否则写“无”>
- **[false_positive]** `路径:行号` - <短标题>
  - 来源：<codex-reviewer / alternative-reviewer:<agent-name> / codex-mimic-reviewer-a / codex-mimic-reviewer-b / user-supplied-skip-review / 多个来源>
  - 驳回：<1-2 句说明为何不成立>
- **[elevated]** `路径:行号` - <短标题>
  - 来源：<codex-reviewer / alternative-reviewer:<agent-name> / codex-mimic-reviewer-a / codex-mimic-reviewer-b / user-supplied-skip-review / 多个来源>
  - 升级原因：<1-2 句说明为何没法自主处理>

## 相对 fork 起点的 repo delta

- 改动的文件：<列表>
- 汇总：<2-4 条>

## 验证

按"是否在本轮 scope contract 内"拆两段，避免读者把无关的全量套件失败误判为本轮修复未通过。

### Scoped verification (in-contract)

- <命令>: <结果>

### Full-suite results (unrelated)

- <命令>: <结果（说明该失败/通过与本轮 fix_allowlist 路径无直接因果）>

## 继续指引（给 fork 出来的 earlier-self）

你的 future-self 手动跑了一轮 post-work review 并应用了上面的修复。
把仓库视为“修复已经在位”来继续。恢复原始任务：<复述>。不要重新 review。
若本次有 `[elevated]` 条目，它们尚未修复，请先处理下面的升级详情再继续。

## 需要开发者决策的升级事项

### 1. `路径:行号` - 短标题

- **卡在哪**：<为什么不能独立修>
- **问题现状**：<复述原始问题>
- **候选方向**：
  - A. <方案 + 权衡>
  - B. <方案 + 权衡>
  - C. <可选>
```
