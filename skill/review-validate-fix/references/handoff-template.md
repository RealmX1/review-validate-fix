# Handoff Template

Handoff 默认开启，但只适用于 `mode: full` 且 handoff 未关闭的完整流程。除非用户明确要求 `no handoff` / `skip handoff` / `不要 handoff`，最终回复先用 2-5 句中文汇总，然后在末尾输出下面的 fenced markdown code block。opening fence 必须是 ```` ```markdown ````，closing fence 必须是 ```` ``` ````；不要把 `<handoff-context>` 作为未包裹的裸标签输出。按真实情况填空；不要编造 session context、文件列表或修复结果。

如果用户显式关闭 handoff、当前是 `pass_type: review_only` / `pass_type: validate_fix` 子 pass，或当前是 `mode: research_checkpoint_no_handoff` / `no-handoff research checkpoint`，不输出下面的 block，也不要输出空模板。只给当前任务要求的中文结果。

## 兼容性边界

这个模板只能作为“继续工作时的上下文压缩 / 交接说明”。不要声称它能让 Claude Code 或 Codex 回到聊天中的任意内部事件位置。

旧 Stop hook 曾把 hook 触发点当作 fork 锚点，但这与 Claude Code 和 Codex 的原生交互模型不兼容：用户只能回退到自己输入过的位置，不能回退到 Stop hook 在会话内部自动触发的任意位置。因此，从旧 hook 迁移时，只保留 handoff blob 的上下文表达价值，不保留“任意 hook 触发点 time-travel”的操作假设。

```markdown
<handoff-context>
# Review-validate-fix 交接上下文

## 锚点（fork 起点）
- Review 开始时的 git HEAD: <sha 或 "未提交工作树">
- Session 原始任务（review 触发之前）：<1-2 句复述>

## 本次 review

review_status: <COMPLETED / SKIPPED_BY_USER>

共 <N> 条，<V> 真实修复 / <F> 误报 / <E> 升级

## Validate/fix 分组

- `RVF-G1`：<包含的 processed issue id / path:line 列表>
  - 分组理由：<共享根因、同一文件区域、同一测试路径、同一决策前提，或“单独验证，因为...”>
  - 执行：<validate/fix 子代理名；若触发允许本地执行的窄例外，写“本地执行：<原因>”>
  - 结果：<R> REAL / <F> FALSE_POSITIVE / <E> ELEVATE

## Issue 处理结果

- **[REAL]** `路径:行号` — <短标题>
  - 来源：<codex-reviewer / alternative-reviewer:<agent-name> / codex-mimic-reviewer-a / codex-mimic-reviewer-b / user-supplied-skip-review / 多个来源；仅 handoff 审计用，未传给 validate/fix 子代理>
  - 问题：<1-2 句说明实际出了什么错>
  - 修复：<1-2 句说明做了什么>
- **[FALSE POSITIVE]** `路径:行号` — <短标题>
  - 来源：<codex-reviewer / alternative-reviewer:<agent-name> / codex-mimic-reviewer-a / codex-mimic-reviewer-b / user-supplied-skip-review / 多个来源>
  - 驳回：<1-2 句说明为何不成立>
- **[ELEVATE]** `路径:行号` — <短标题>
  - 来源：<codex-reviewer / alternative-reviewer:<agent-name> / codex-mimic-reviewer-a / codex-mimic-reviewer-b / user-supplied-skip-review / 多个来源>
  - 升级原因：<1-2 句说明为何没法自主处理>

## 相对 fork 起点的 repo delta
- 改动的文件：<列表>
- 汇总：<2-4 条>

## 继续指引（给 fork 出来的 earlier-self）
你的 future-self 手动跑了一轮 post-work review 并应用了上面的修复。
把仓库视为“修复已经在位”来继续。恢复原始任务：<复述>。不要重新 review。
若本次有 `[ELEVATE]` 条目，它们尚未修复，请先处理下面的升级详情再继续。
</handoff-context>
```

## 升级详情

如果有 `ELEVATE`，先关闭 handoff 的 fenced code block，再在 `</handoff-context>` 之后用普通 markdown 展示：

```markdown
## 需要开发者决策的升级事项

### 1. `路径:行号` — 短标题

- **卡在哪**：<为什么不能独立修>
- **问题现状**：<复述原始问题>
- **候选方向**：
  - A. <方案 + 权衡>
  - B. <方案 + 权衡>
  - C. <可选>
```
