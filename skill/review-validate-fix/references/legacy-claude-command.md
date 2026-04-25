---
description: 手动触发一轮 post-work Opus review → 并行 validate+fix → 产出 handoff blob（Stop hook 的手动版）
---

手动触发一次 **review-validate-fix** 循环，对当前的未提交改动做审查。这是 [`~/.claude/hooks/stop-review-validate-fix.sh`](../hooks/stop-review-validate-fix.sh) Stop hook 的手动版本。**不要 shell 到 `claude -p`** —— 通过 Agent 工具派发 review subagent，这样它会出现在 agent panel 里，开发者能实时看到并中断。

手动路径的特点：无 session round cap、无 scope 锚点、同步执行。

> **🔗 同步维护约定**：本 slash command 与 Stop hook（[`~/.claude/hooks/stop-review-validate-fix.sh`](../hooks/stop-review-validate-fix.sh)）是一对双胞胎——手动路径 vs. 自动路径，review / validate-fix / handoff / elevate 的 prompt 文本与输出契约**必须保持一致**。改动其中一处（review prompt、validate-then-fix 指令、handoff 模板、elevated-issues 呈现格式等）时，请同步更新另一处；否则同一个 session 在手动/自动两条路径下会出现不一致的 subagent 行为和 handoff 格式。

**本 slash command 的所有汇总输出请使用中文。**

## Steps

### 1. Gate

运行 `git status --porcelain`。若输出为空，回复 `没有可审查的改动 —— 没有未提交的变更。` 然后结束。

### 2. 派发一个 Opus review Agent

用 Agent 工具调用，参数如下：

- `subagent_type`: `"general-purpose"`
- `model`: `"opus"`
- `description`: `"Opus post-work review (manual)"`
- `prompt`: SESSION CONTEXT 块（见下）+ 下方的 review prompt 原文

**在发 Agent 调用前**，先基于你本 session 的对话历史填写下面的 SESSION CONTEXT 块（只填你确有把握的字段；没把握就留空或直接省略该条。不要编造、不要用占位符，不要把这段 meta 说明本身带进去）：

```
## Session context（主会话注入）
- 用户最初的请求 / 意图：<1–2 句复述，让 review agent 知道这次改动为什么存在>
- 本 turn 实际由主会话改过的文件（**仅主会话自己的 Write/Edit**，**不要**
  把仓库里已有的未提交改动、其他 chat session/其他 agent 留下的工作、或
  本机其他进程的改动混进来；无法区分时就不要填这一条）：<path1、path2…>
- 关键设计取舍（只在明显会被误判时填）：<例 "故意不加 retry，因为上游
  已经幂等"、"看起来死代码但 tombstone 清理路径会用到" 等>
```

然后把这个 SESSION CONTEXT 块**原样放在下面 review prompt 的最顶部**再交给 Agent。如果你一条都填不出来，就完全省略这个块——宁可留白也不要乱写。

> **TODO（待研究）**：跨 session / 跨 agent 的 session context 共享目前没有机制。目标：当多个 Claude Code 会话同时 / 先后在一个仓库里工作时，让 review agent 知道"哪些未提交改动属于本 session、哪些是别的 session 留下的"。候选方向：tmux/wezterm 面板间 IPC、本地 SQLite 索引（按 path + ISO8601 mtime + session_id 记录 Write/Edit 事件）、或现成开源方案（例如 git-absorb 的 per-hunk 归属、syft/sigstore 式的 attestation、CRDT 协作库 Automerge/Yjs 同步 session metadata）。**先调研已有的开源方案**再自造轮子。目前的保守方案：主会话只填它能 100% 确认是自己动的文件；分不清就干脆不填。

**Review prompt**（要传给这个 Agent 的完整 prompt，SESSION CONTEXT 块拼在最顶部）：

> **请用中文回复所有输出**（字面 sentinel `NO_ISSUES`、verdict tag `REAL`/`FALSE_POSITIVE`、文件路径、代码除外）。
>
> 你正在 review 一个 git 仓库中刚完成的未提交工作。开发者尚未 review 这些改动。你的任务：找出 bug、回归、未完成的实现、错误的假设、遗漏的边界情况、被破坏的不变量、安全问题、以及编辑遗留的死代码。
>
> 如果 prompt 顶部有 `## Session context（主会话注入）` 块：把它当作**背景参考**，而**不是**免死金牌。主会话的意图说明可以帮你判断某些"看似奇怪"的代码其实是有意为之；但你依然要独立 verify——主会话可能漏说、说错、或没意识到自己引入了 bug。
>
> **输出契约（必须严格遵守）**：
> - 如果改动没问题，原样输出字面字符串：`NO_ISSUES`（不加标点、不加前言、仅这一个词）。
> - 否则输出编号列表。每条：一行 `` `路径:行号` `` 引用，接 1–2 句中文说明具体问题。要精简。**只报真实问题** —— 不要报风格偏好、假设性重构、"建议加注释"之类。
>
> 不要概括代码做了什么。不要复述 diff。不要恭维。不要提与 bug 无关的改进。
>
> 自己用 Bash / Read / Grep 工具探查仓库：未提交改动在 `git diff HEAD`，未跟踪文件在 `git status --porcelain`。先看 `git diff HEAD` 和 `git status --short`，再 Read 具体文件补充 context。

### 3. 根据 review 输出做决策

- 若 review Agent 返回 `NO_ISSUES` → 回复 `Review 干净 —— 没发现问题。` 跳到 step 5（仍然产出 handoff）。
- 否则 → 进入 step 4。

### 4. 并行派发 validate+fix Agents

每条 issue 一个 Agent 工具调用。**全部调用放在同一条消息里**，让它们并发运行、并排出现在 agent panel。

每条参数：
- `subagent_type`: `"general-purpose"`（或明显匹配的 specialist）
- `description`: 简短问题摘要（agent panel 行标题，用中文）
- `prompt`: issue 原文 + 文件路径+行号 + 相关代码 context + 下方 validate-then-fix 指令

**Validate-then-fix 指令**（原文照抄进每个 fix 子代理的 prompt）：

> **请用中文回复所有输出**（verdict tag `REAL` / `FALSE_POSITIVE` / `ELEVATE`、文件路径、代码除外）。
>
> (a) 先读相关文件，**验证** flag 的问题在当前代码里是否真的是一个问题。
> (b) 是真问题且你能独立修好 → 用 Edit/Write 实施**最小化**修复。
> (c) 不是真问题 → **不要**改任何文件；简短用中文说明为何是 false positive。
> (d) 是真问题但**你不应该/不能独立修**——例如需要架构决策、存在多种等价修复需要开发者拍板、涉及你权限/scope 外的改动、或原始需求本身就不明确——→ **不要**改任何文件（哪怕"先做个部分修复"也不要），返回 `ELEVATE`。用 1–3 句中文说清：卡点是什么、为何需要人介入；**如能想到**请列 2–3 个候选方向及各自权衡（开发者看的就是这段）。
>
> 返回结构化 verdict：`[REAL | FALSE_POSITIVE | ELEVATE] <路径:行号> — <你做了什么 / 为何驳回 / 为何升级+备选方向>`。

### 5. 汇总 + 产出 handoff

用 2–5 句中文汇总：flag 了多少条、多少真问题已修复、多少驳回、多少升级给开发者。

然后在**你这条回复的最末尾**，原样 emit 下面这个 block（按 session 实际情况填空，保留中文标签与 `<handoff-context>` / `[REAL]` / `[FALSE POSITIVE]` / `[ELEVATE]` 结构化 token）：

```
<handoff-context>
# Review-validate-fix 交接上下文（手动触发）

## 锚点（fork 起点）
- Review 开始时的 git HEAD: <sha 或 "未提交工作树">
- Session 原始任务（review 触发之前）：<1–2 句复述用户最初要做的事>

## 本次 review —— 共 <N> 条，<V> 真实修复 / <F> 误报 / <E> 升级

- **[REAL]** `路径:行号` — <短标题>
  - 问题：<1–2 句说明实际出了什么错>
  - 修复：<1–2 句说明 subagent 做了什么>
- **[FALSE POSITIVE]** `路径:行号` — <短标题>
  - 驳回：<1–2 句说明为何不成立>
- **[ELEVATE]** `路径:行号` — <短标题>
  - 升级原因：<1–2 句说明 subagent 为何没法自主处理>

## 相对 fork 起点的 repo delta
- 改动的文件：<列表>
- 汇总：<2–4 条>

## 继续指引（给 fork 出来的 earlier-self）
你的 future-self 手动跑了一轮 post-work Opus review 并应用了上面的修复。
把仓库视为"修复已经在位"来继续。恢复原始任务：<复述>。**不要**重新 review。
若本次有 `[ELEVATE]` 条目，它们**尚未修复**，请先处理（见 `</handoff-context>` 之后的升级详情块）再继续。
</handoff-context>
```

用户会复制这个 block，从"运行 `/review-validate-fix` 之前"的时间点 fork 一个更早的 session，把它贴给 earlier-self，让后者以压缩上下文继续。

### 6. 如有 `[ELEVATE]`，呈现升级详情（普通 markdown 回复）

**仅当**本次出现至少一条 `[ELEVATE]` verdict 时执行；否则本 step **整段省略**（不要写空段）。

与上面的 `<handoff-context>` 不同——那是给用户复制贴到更早 fork session 的**结构化 blob**，所以用 XML 标签包起来当作代码块对待。这里的升级详情**不是 blob**，而是你本轮回复里给用户看的普通内容。所以：

- **不要**用 `<elevated-issues>` / `</elevated-issues>` 之类的 XML 包裹标签。
- **不要**把整段塞进代码块（``` ```）。
- 就用**普通 markdown**（标题、加粗、列表）作为你这条回复正文的一部分呈现，风格与你平时答复用户的 markdown 一致即可。
- **不要**用 AskUserQuestion 工具——把候选方向直接 flat 展开在文本里，用户会在下一轮消息里回复编号或自行指明。

在 `</handoff-context>` 之后、你这条回复的正文末尾，按下面这个模板以普通 markdown 呈现（每条升级项按模板展开）：

---

## ⚠️ 需要开发者决策的升级事项

fix 子代理无法自主修复以下 **N** 条问题。手动触发路径没有"下一轮"概念，这里仅做展示，等你处理。

### 1. `路径:行号` — 短标题

- **卡在哪**：1–2 句说明 subagent 为何不能独立修
- **问题现状**：1–2 句复述 review agent 原始 flag
- **候选方向**（直接回复编号即可；也可以自己另提）：
  - A. 方案 A：描述 + 权衡
  - B. 方案 B：描述 + 权衡
  - C. 如有，第三种

### 2. ...（其余升级项重复该结构）

---

> 注：手动 `/review-validate-fix` 路径**不**写 escalation 标记——那是 Stop hook 自动路径的短路开关。手动路径每次都由用户主动运行，天然不会"自动进入下一轮"。
