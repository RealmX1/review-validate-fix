#!/bin/bash
# Post-work review orchestration hook (auto path).
#
# 🔗 KEEP IN SYNC: 本 hook 是 `/review-validate-fix` slash command
# (~/.claude/commands/review-validate-fix.md) 的自动路径孪生版本。
# review prompt、validate-then-fix 指令、handoff 模板、elevated-issues 呈现
# 格式等**必须与 command 文件保持一致**——改一处就同步改另一处，否则同一
# session 在手动 / 自动两条路径下会出现不一致的 subagent 行为和 handoff。
#
# NOTE: This hook no longer runs `claude -p` itself. Doing so would spawn an
# opaque subprocess invisible to the developer for 30–120s. Instead, the
# hook's only job is to gate + book-keep state, then emit a rewake directive
# telling the MAIN session to dispatch an Opus review Agent via the native
# Agent tool. That way the review (and the downstream validate+fix agents)
# all show up in Claude Code's agent panel with `subagentStatusLine` rows —
# the developer can see and interrupt them.
#
# Gates (short-circuit with exit 0 in order):
#   1. stop_hook_active (recursion guard within a Stop chain)
#   2. not a git repo, or empty `git status --porcelain`
#   3. developer-escalation marker present ($SESSION_ID.escalated) — a fix
#      subagent in a prior round flagged an issue as ELEVATE (needs human
#      decision). Auto-review stays quiet until the dev resolves it and
#      removes the marker (or the 7-day cleanup ages it out).
#   4. no new Write/Edit activity ticks since last dispatch (dedup — uses
#      session-local activity file written by mark-activity.sh; immune to
#      parallel external processes changing the working tree)
#   5. session round cap reached (max 3 dispatches)
#
# On pass-through: increment dispatch count, snapshot current activity tick
# count as the new dedup watermark, then emit the rewake directive and exit 2
# to wake the main session.

set -uo pipefail

STATE_DIR="$HOME/.claude/hooks/state"
MAX_ROUNDS=3
mkdir -p "$STATE_DIR"
find "$STATE_DIR" -type f -mtime +7 -delete 2>/dev/null || true

INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty')
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // "default"' | tr -dc 'A-Za-z0-9_-')
STOP_HOOK_ACTIVE=$(printf '%s' "$INPUT" | jq -r '.stop_hook_active // false')

# Compute activity tick state up front — needed by Gate 1's watermark roll-over
# and by Gate 3's dedup comparison.
ACTIVITY_FILE="$STATE_DIR/${SESSION_ID}.activity"
WATERMARK_FILE="$STATE_DIR/${SESSION_ID}.watermark"

if [ -f "$ACTIVITY_FILE" ]; then
  CURRENT_TICKS=$(wc -l < "$ACTIVITY_FILE" | tr -d ' ')
else
  CURRENT_TICKS=0
fi
CURRENT_TICKS=${CURRENT_TICKS:-0}
LAST_TICKS=$(cat "$WATERMARK_FILE" 2>/dev/null || echo 0)
LAST_TICKS=$(printf '%s' "$LAST_TICKS" | tr -cd '0-9')
LAST_TICKS=${LAST_TICKS:-0}

# ---------- Helper: emit JSON systemMessage banner (user-visible UI banner) ----------
# Claude Code renders `hookSpecificOutput.systemMessage` as a visible banner in
# the UI. Only parsed on exit 0. Use this to surface every hook trigger so the
# user can see WHY stop-review-validate-fix did / didn't dispatch.
emit_banner() {
  local msg="$1"
  local suppress="${2:-true}"
  jq -n --arg msg "$msg" --argjson sup "$suppress" \
    '{continue: true, suppressOutput: $sup, hookSpecificOutput: {hookEventName: "Stop", systemMessage: $msg}}'
}

# Prior dispatch count — used below to decide how chatty to be.
COUNT_FILE="$STATE_DIR/${SESSION_ID}.count"
PRIOR_COUNT=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
PRIOR_COUNT=$(printf '%s' "$PRIOR_COUNT" | tr -cd '0-9')
PRIOR_COUNT=${PRIOR_COUNT:-0}

# ---------- Gate 1: recursion ----------
# When Stop fires inside a rewoken chain, any Write/Edit ticks accrued during
# this rewake (fix subagents' edits) belong to the *current* dispatch cycle,
# not a future one. Roll the watermark forward so the next real user turn only
# dispatches if the user introduces new edits beyond what fix agents did.
# Without this, a pure-conversation user turn (e.g. "has our work finished?")
# would falsely look like "new activity since last dispatch" and trigger a
# spurious round.
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  echo "$CURRENT_TICKS" > "$WATERMARK_FILE"
  emit_banner "🔁 stop-review-validate-fix: rewoken chain absorbed (watermark → T=$CURRENT_TICKS, round $PRIOR_COUNT/$MAX_ROUNDS)"
  exit 0
fi

# ---------- Gate 2: git repo + uncommitted changes ----------
# Silent — not in a git repo or no uncommitted changes means the hook has
# nothing to reason about; banner would be noise on every non-git turn.
[ -z "$CWD" ] && exit 0
cd "$CWD" 2>/dev/null || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0
STATUS=$(git status --porcelain 2>/dev/null)
[ -z "$STATUS" ] && exit 0

# ---------- Gate 3: developer escalation ----------
# A fix subagent in a prior round hit a problem it couldn't resolve on its
# own (architectural decision, multiple equivalent fixes, out-of-scope,
# unclear requirement) and the main session touched this marker after its
# handoff. Keep auto-review quiet until the dev handles the escalated
# item(s) and deletes the marker (or the 7-day cleanup ages it out).
# Banner so the user can see why dispatch was suppressed.
ESCALATE_FILE="$STATE_DIR/${SESSION_ID}.escalated"
if [ -f "$ESCALATE_FILE" ]; then
  emit_banner "🛑 stop-review-validate-fix: 已升级给开发者 —— 自动 review 已停止（round $PRIOR_COUNT/$MAX_ROUNDS；处理完后 \`rm ${ESCALATE_FILE}\` 可恢复）"
  exit 0
fi

# ---------- Gate 4: dedup via Write/Edit activity ticks ----------
if [ "$CURRENT_TICKS" -le "$LAST_TICKS" ]; then
  # No new Write/Edit in this session since last dispatch. Only banner this if
  # we've already dispatched at least once — on a fresh session with no edits
  # there's nothing meaningful to report.
  if [ "$PRIOR_COUNT" -gt 0 ]; then
    emit_banner "🔁 stop-review-validate-fix: no new Write/Edit since last round — skipping (round $PRIOR_COUNT/$MAX_ROUNDS held)"
  fi
  exit 0
fi

# ---------- Gate 5: session round cap ----------
COUNT=$PRIOR_COUNT

if [ "$COUNT" -ge "$MAX_ROUNDS" ]; then
  # exit 2 + asyncRewake: Claude Code forwards the hook's STDERR (not stdout)
  # to the model as the blocking-Stop system-reminder. Writing to stderr is
  # load-bearing — an earlier revision wrote to stdout and the rewake was
  # silently dropped (Claude only saw "Stop hook feedback: No stderr output").
  cat >&2 <<EOF
🛑 stop-review-validate-fix: hit $MAX_ROUNDS/$MAX_ROUNDS round cap for this session.

本次 session 的 post-work review 已达 $MAX_ROUNDS 轮上限。**不要**再派发
subagent。向用户简短说明：

  "Auto review 已达本 session 的 $MAX_ROUNDS 轮上限。如需继续，运行
   /review-validate-fix 手动触发；或从最近的 handoff block fork 出更早版本继续。"

然后停止。
EOF
  exit 2
fi

# ---------- All gates passed: increment round, persist, dispatch ----------
# ORIGINAL SCOPE 不在 hook 层锁定：round 1 时用 `git diff HEAD` 会把仓库里预
# 先存在的未提交改动（其他 session/外部进程的工作）错误并入本 session scope。
# 改由主会话在 rewake SESSION CONTEXT 块里自己声明改过哪些文件，round 2+ 让
# 主会话从对话历史回忆 round 1 的声明作为 anchor。
ROUND=$((COUNT + 1))
echo "$ROUND" > "$COUNT_FILE"
echo "$CURRENT_TICKS" > "$WATERMARK_FILE"

# ---------- Build scope rules block (round >= 2 only) ----------
SCOPE_RULES=""
SCOPE_DIRECTIVE=""
if [ "$ROUND" -ge 2 ]; then
  SCOPE_RULES="

**SCOPE RULES**（第 $ROUND 轮 / 共 $MAX_ROUNDS 轮）—— 把这一段一起包进 review Agent 的 prompt：
Round 1 锚定的 ORIGINAL SCOPE —— **由主会话填入**。从对话历史里找：
  1. round 1 你自己在 \`SESSION CONTEXT（主会话注入）\` 块里填的「本 turn 实际
     由主会话改过的文件」清单；或
  2. round 1 结尾 \`<handoff-context>\` 里 \`## 相对 fork 起点的 repo delta\`
     下的「改动的文件」列表。
两处都找不到、或 round 1 你当时就留空了——那就**直接省略下面这段文件清单**，
由 review agent 按保守规则自行判断本 session 改动范围。

  [在此处由主会话填入文件清单，每行一个路径；省略时删掉本段连同上一行占位]

当前 diff 可能已延伸到 ORIGINAL SCOPE 之外。按以下规则裁剪你 flag 的内容：
- ORIGINAL SCOPE 内的文件：自由 flag 真实问题。
- 不在 ORIGINAL SCOPE 内的文件：**仅当**问题是由 scope 内改动直接连带造成
  时才 flag（例：scope 内文件改了函数签名，scope 外的调用处必须同步更新）。
  不要 flag non-scope 文件里的历史遗留瑕疵或与本次工作无关的既存问题。
- 不要抱怨 sprawl 本身；只在\"报什么\"上守住界线。
- 如果 ORIGINAL SCOPE 被省略（主会话无法确认）：仍坚持\"只报真问题、不抱怨
  sprawl\"，scope 外的可疑点只在明显是本轮 diff 连带造成时才 flag。"

  SCOPE_DIRECTIVE="

Fix 子代理的 scope 约束（第 $ROUND 轮 / 共 $MAX_ROUNDS 轮）：
  ORIGINAL SCOPE：沿用你传给 review agent 的 SCOPE RULES 里那份文件清单
  （由主会话从 round 1 回忆得到；若 round 1 无声明，则本约束只要求\"不借机
  清理无关瑕疵\"，具体 scope 由 fix 子代理自行判断）。

  每个 fix 子代理必须停留在 ORIGINAL SCOPE 内，**除非**修复本身直接需要碰
  scope 外的文件（例：scope 内改了 API 签名，scope 外的调用处必须同步改）。
  不要借机清理 non-scope 文件里无关的瑕疵。"
fi

# ---------- Emit rewake directive to main session ----------
# asyncRewake + exit 2: Claude Code forwards the hook's STDERR (not stdout)
# to the model as the blocking-Stop system-reminder. Writing to stderr is
# load-bearing — an earlier revision wrote to stdout and the rewake was
# silently dropped (Claude only saw "Stop hook feedback: No stderr output").
cat >&2 <<EOF
🔁 stop-review-validate-fix: dispatching Opus post-work review — 第 $ROUND 轮 / 共 $MAX_ROUNDS 轮

Post-work review loop —— 第 $ROUND 轮 / 共 $MAX_ROUNDS 轮。

在主会话里用 Agent 工具执行以下步骤（**不要**用 shell 子进程——我们要让这些
子代理出现在 agent panel 里，开发者能实时看到并中断）。**本文所有 step 说明
与最终对用户的汇总都用中文。**

=== STEP 1：派发一个 Opus review Agent ===

Agent 工具参数：
  subagent_type: "general-purpose"
  model: "opus"
  description: "Opus post-work review (round $ROUND/$MAX_ROUNDS)"
  prompt: <下面这段 review prompt 的原文，含 SCOPE RULES（如果有）>

**在发 Agent 调用前**，先基于你本 session 的对话历史填写下面的 SESSION
CONTEXT 块（只填你确有把握的字段；没把握就留空或直接省略该条。不要编
造、不要用占位符，不要把这段 meta 说明本身带进去）：

\`\`\`
## Session context（主会话注入）
- 用户最初的请求 / 意图：<1–2 句复述，让 review agent 知道这次改动为什么存在>
- 本 turn 实际由主会话改过的文件（**仅主会话自己的 Write/Edit**，**不要**
  把仓库里已有的未提交改动、其他 chat session/其他 agent 留下的工作、或
  本机其他进程的改动混进来；无法区分时就不要填这一条）：<path1、path2…>
- 关键设计取舍（只在明显会被误判时填）：<例 "故意不加 retry，因为上游
  已经幂等"、"看起来死代码但 tombstone 清理路径会用到" 等>
\`\`\`

然后把这个 SESSION CONTEXT 块**原样放在下面 review prompt 的最顶部**再
交给 Agent。如果你一条都填不出来，就完全省略这个块——宁可留白也不要乱写。

> **TODO（待研究）**：跨 session / 跨 agent 的 session context 共享目前
> 没有机制。目标：当多个 Claude Code 会话同时 / 先后在一个仓库里工作时，
> 让 review agent 知道"哪些未提交改动属于本 session、哪些是别的 session
> 留下的"。候选方向：tmux/wezterm 面板间 IPC、本地 SQLite 索引（按 path
> + ISO8601 mtime + session_id 记录 Write/Edit 事件）、或现成开源方案
> （例如 git-absorb 的 per-hunk 归属、syft/sigstore 式的 attestation、
> CRDT 协作库 Automerge/Yjs 同步 session metadata）。**先调研已有的开源
> 方案**再自造轮子。目前的保守方案：主会话只填它能 100% 确认是自己动的
> 文件；分不清就干脆不填。

---
要传给这个 Agent 的 review prompt：

---
**请用中文回复所有输出**（字面 sentinel \`NO_ISSUES\`、verdict tag \`REAL\`/\`FALSE_POSITIVE\`、文件路径、代码除外）。

你正在 review 一个 git 仓库中刚完成的未提交工作。开发者尚未 review 这些
改动。你的任务：找出 bug、回归、未完成的实现、错误的假设、遗漏的边界情
况、被破坏的不变量、安全问题、以及编辑遗留的死代码。

如果 prompt 顶部有 \`## Session context（主会话注入）\` 块：把它当作**背景
参考**，而**不是**免死金牌。主会话的意图说明可以帮你判断某些"看似奇怪"
的代码其实是有意为之；但你依然要独立 verify——主会话可能漏说、说错、或
没意识到自己引入了 bug。

**输出契约（必须严格遵守）**：
- 如果改动没问题，原样输出字面字符串：\`NO_ISSUES\`（不加标点、不加前言、仅这一个词）。
- 否则输出编号列表。每条：一行 \`路径:行号\` 引用，接 1–2 句中文说明具体问题。
  要精简。**只报真实问题**——不要报风格偏好、不要报假设性重构、不要说"建议加注释"。

不要概括代码做了什么。不要复述 diff。不要恭维。不要提与 bug 无关的改进。

自己用 Bash / Read / Grep 探查仓库：未提交改动在 \`git diff HEAD\`，未跟踪
文件在 \`git status --porcelain\`。先看 \`git diff HEAD\` 和 \`git status --short\`，
再 Read 具体文件补充 context。$SCOPE_RULES
---

=== STEP 2：根据 review 输出做决策 ===

当 review Agent 返回：
- 输出 \`NO_ISSUES\` → 回复用户："第 $ROUND 轮 review 干净，没发现问题。"
  并在末尾 emit handoff block（见 STEP 4）。**不要**派 fix 代理。
- 否则 → 进入 STEP 3。

=== STEP 3：并行派发 validate+fix 子代理 ===

每条 issue 一个 Agent 工具调用，**全部放在同一条消息里**，让它们并发运行、
并排出现在 agent panel。

每条的参数：
  subagent_type: "general-purpose"（或明显匹配的 specialist）
  description: 简短问题摘要（agent panel 的行标题，用中文）
  prompt: <issue 原文 + 文件路径+行号 + 相关代码 context + 下面的 validate-then-fix 指令>

传给每个 fix 子代理的 validate-then-fix 指令（原文照抄进它的 prompt）：

**请用中文回复所有输出**（verdict tag \`REAL\` / \`FALSE_POSITIVE\` / \`ELEVATE\`、文件路径、代码除外）。

(a) 先读相关文件，**验证** flag 的问题在当前代码里是否真的是一个问题。
(b) 是真问题且你能独立修好 → 用 Edit/Write 实施**最小化**修复。
(c) 不是真问题 → **不要**改任何文件，简短用中文说明为何是 false positive。
(d) 是真问题但**你不应该/不能独立修**——例如需要架构决策、存在多种等价修
    复需要开发者拍板、涉及你权限/scope 外的改动、或原始需求本身就不明确
    ——→ **不要**改任何文件（哪怕"先做个部分修复"也不要），返回
    \`ELEVATE\`。用 1–3 句中文说清：卡点是什么、为何需要人介入；**如能想
    到**请列 2–3 个候选方向及各自权衡（开发者看的就是这段）。

返回结构化 verdict：\`[REAL | FALSE_POSITIVE | ELEVATE] <路径:行号> — <你做了什么 / 为何驳回 / 为何升级+备选方向>\`。$SCOPE_DIRECTIVE

=== STEP 4：汇总 + 产出 handoff ===

所有子代理返回后（或 NO_ISSUES 的情况立即），用 2–5 句中文汇总：flag 了
多少条、多少真问题已修复、多少驳回、多少升级给开发者。

然后在**你这条回复的最末尾**，原样 emit 下面这个 block（按本 session 实际
情况填空，保留所有中文标签、保留 \`<handoff-context>\` 和 \`[REAL]\` /
\`[FALSE POSITIVE]\` / \`[ELEVATE]\` 作为结构化 token）。**整段必须用三反
引号代码围栏（\`\`\`）包起来再输出**——这是给用户复制贴到更早 fork session
的结构化 blob，不加围栏时 markdown 渲染器会吃掉 \`<handoff-context>\` 这类
XML 标签，blob 无法完整复制。

\`\`\`
<handoff-context>
# Review-validate-fix 交接上下文

## 锚点（fork 起点）
- Review 开始时的 git HEAD: <sha 或 "未提交工作树">
- Session 原始任务（review 触发之前）：<1–2 句复述用户最初要做的事>

## Review loop 过程

### 第 $ROUND 轮 / 共 $MAX_ROUNDS 轮 —— 共 <N> 条，<V> 真实修复 / <F> 误报 / <E> 升级

- **[REAL]** \`路径:行号\` — <短标题>
  - 问题：<1–2 句说明实际出了什么错>
  - 修复：<1–2 句说明 subagent 做了什么>
- **[FALSE POSITIVE]** \`路径:行号\` — <短标题>
  - 驳回：<1–2 句说明为何不成立>
- **[ELEVATE]** \`路径:行号\` — <短标题>
  - 升级原因：<1–2 句说明 subagent 为何没法自主处理>

（如果本 session 有更早的轮次，在当前轮次上方按同样结构添加
\`### 第 N 轮 / 共 $MAX_ROUNDS 轮\` 小节。）

## 相对 fork 起点的 repo delta
- 改动的文件：<列表>
- 汇总：<2–4 条>

## 继续指引（给 fork 出来的 earlier-self）
你的 future-self 跑了一轮 post-work Opus review 并应用了上面的修复。
把仓库视为"修复已经在位"来继续。恢复原始任务：<复述>。**不要**重新 review
——已经做过了。若本轮有 \`[ELEVATE]\` 条目，它们**尚未修复**，请先处理（见
\`</handoff-context>\` 之后的升级详情块）再继续。
</handoff-context>
\`\`\`

=== STEP 5：如有 \`[ELEVATE]\`，产出升级详情块 + 写 escalation 标记 ===

**仅当**本轮出现至少一条 \`[ELEVATE]\` verdict 时执行下面两步；否则本 step
**整段省略**（不要 emit 空块、不要 touch 标记）。

**(5a)** 在 \`</handoff-context>\` 之后、你这条回复的正文末尾，呈现升级详情。
与上面的 \`<handoff-context>\` 不同——那是给用户复制贴到更早 fork session 的
**结构化 blob**，所以用 XML 标签包起来当作代码块对待。这里的升级详情**不是
blob**，而是你本轮回复里给用户看的普通内容。所以：

- **不要**用 \`<elevated-issues>\` / \`</elevated-issues>\` 之类的 XML 包裹标签。
- **不要**把整段塞进代码块（\`\`\` \`\`\`）。
- 就用**普通 markdown**（标题、加粗、列表）作为你这条回复正文的一部分呈现，
  风格与你平时答复用户的 markdown 一致即可。
- **不要**用 AskUserQuestion 工具——把候选方向直接 flat 展开在文本里，用户
  会在下一轮消息里回复编号或自行指明。

按下面这个模板以普通 markdown 呈现（每条升级项按模板展开）：

---

## ⚠️ 需要开发者决策的升级事项（第 $ROUND 轮）

fix 子代理无法自主修复以下 **N** 条问题。已写入 escalation 标记，**下次
Stop hook 不会再派下一轮 review**——等你处理完（或删除标记文件）再恢复。

### 1. \`路径:行号\` — 短标题

- **卡在哪**：1–2 句说明 subagent 为何不能独立修
- **问题现状**：1–2 句复述 review agent 原始 flag
- **候选方向**（直接回复编号即可；也可以自己另提）：
  - A. 方案 A：描述 + 权衡
  - B. 方案 B：描述 + 权衡
  - C. 如有，第三种

### 2. ...（其余升级项重复该结构）

---

**(5b)** 紧接着，用 Bash 工具执行下面这行（本 session ID 已经就位，复制即
可运行）：

    touch "\$HOME/.claude/hooks/state/${SESSION_ID}.escalated"

这个文件是 Stop hook 的短路开关——只要它存在，后续 Stop 事件就不再派
review 循环。开发者处理完升级项后，运行 \`rm\` 删除它即可恢复自动 review。

---

**不要**自己再 review 一次——下一次 Stop hook 会自动决定是否 dispatch 下
一轮（至多 $MAX_ROUNDS 轮；本轮若无新 Write/Edit 则跳过；若本轮写入了
escalation 标记则直接短路，不再派发）。
EOF

exit 2
