# Handoff — Claude Code 适配缺口（交给 cross-harness adaptation framework overhaul）

> - **日期**：2026-05-28
> - **来源**：对最近 3 次 deployment 以来 7 个 RVF run 的跨 run 复盘（报告：`~/.claude/rvf/cross-run-analysis-2026-05-28.md`）。
> - **受众**：正在做 **cross-harness adaptation framework overhaul** 的 agent（把 RVF 推到 `docs/multi-harness-plugin-guideline/` 定义的 core ↔ adapter 形态）。
> - **状态**：未提交，等你 review。只写文件不做 git 操作（本仓库 main 在主 checkout `/Users/bominzhang/Documents/GitHub/review-validate-fix`）。
> - **核对基准**：所有"仍 open"结论已对 **local `main` (`f941ba0`)** 与 worktree `d3fe8ad` 双重核对，main 比 d3fe8ad 多的 16 个 commit 全是 rvf-test 优化，**未触碰下列任一缺口**。

---

## TL;DR

最近一窗口的 RVF run 给 double-review 打了高分（6 条 reviewer issue：5 条 run 内即修、1 条 false_positive 正确驳回，**零缺陷逃逸**）。但 run 自报的 **Follow-up 几乎全部指向同一根因**：

> **RVF 自身的"确定性分析 / 归因层"是 Codex-原生的——它直接消费 Codex 工具名（`apply_patch`/`spawn_agent`）与 Codex rollout 目录布局，系统性低估并漏算 Claude-host run。**

这正是 `05-adapter-contract.md` 反复警告的 **"core 直接消费 host 字段名 / core 里 hardcode Codex 布局"** 反模式，只是它发生在一个指南 **当前没显式纳入 `core/` 的表面**：`analysis_artifacts.py` / `subagent_capture.py` / fix-attempt causality。请在 overhaul 时把这层一并纳入"走 `NormalizedTranscript` / adapter 抽象"的范围。

---

## 你要接手的现状（指针）

- 适配框架：`adapters/`（`adapters/README.md` 6 维契约总表）、`docs/multi-harness-plugin-guideline/05-adapter-contract.md`（核心契约）、`06-rvf-application.md`（建议目录形态：`core/transcript`、`core/decisions`、`adapters/<host>/{transcript,subagent,hooks}`）、`07-implementation-slices.md`（S0–S3）。
- Claude adapter 现状（`adapters/claude_code/README.md`）：**transcript = S1 落点**（`adapters/claude_code/transcript.py`），**subagent = S2 stub**（路径 C：RVF subagent 调用仍走 Codex），hooks 保留在 `plugins/review-validate-fix/hooks/`。
- 直接相关的前序工作：`docs/log/2026-05-10-trajectory-capture-claude-host-support.md`（Claude-host trajectory 捕获支持）。**先读它**——下面 A/B/C 都是它的延伸面。
- 经验证据（本 handoff 的来源）：`~/.claude/rvf/runs/<run_id>/artifacts/analysis/summary.md` 的 `## Follow-up 建议` 段。

---

## 逐项缺口（映射到 6 维契约）

> 严重度按"对 overhaul 的相关度 + run 复现次数"排。`file:line` 已在 `main` 上核对。

### 🔴 A1 — patch 计数绕过 NormalizedTranscript（维度 3 Transcript · 复现 ×3）
- **症状**：Claude-host run 的 summary 恒显示 `apply_patch 事件=0 / 子代理 apply_patch=0`。`rvf-20260523T135334Z` 实有 **143 Edit + 42 Write + 30 Agent**，全记 0，误导复盘者"以为没改动"。
- **当前违约**：`plugins/review-validate-fix/skills/review-validate-fix/scripts/analysis_artifacts.py:361 / :425 / :730` 直接判 `record.get("tool") == "apply_patch"`（Codex 工具名）。
- **契约取向**：维度 3 明确"core 不碰 host 字段名；只消费 `NormalizedTranscript`"。patch 计数应来自归一化的 `AssistantMessage.tool_calls`（host 各自把 `Edit`/`Write`/`MultiEdit` 与 `apply_patch` 归一为同一语义"write op"），而非 host 原生 tool 名。
- **来源 run**：`rvf-20260523T062744Z`、`rvf-20260523T135334Z`（自标"最值得修"）、`rvf-20260527T175943Z`。

### 🔴 A2 — subagent 捕获 hardcode Codex 布局（维度 2 Sub-agent · 复现 ×3）
- **症状**：Claude 经 `Task`/`Agent` 派发的 reviewer / validate-fix 子代理，`spawn_agent`/`subagent_count`/`subagent_patch_event_count` 全 0，子代理轨迹与其内部 patch 无法归因。
- **当前违约**：`subagent_capture.py:23`「只识别 Codex `spawn_agent` / `collab_agent_spawn_end` / Codex rollout 路径布局」；`:66-70` 直接 glob `~/.codex/sessions/.../rollout-*-<id>.jsonl`。
- **契约取向**：维度 2 的 `invoke_subagent → SubagentResult` 应由 adapter 归一。Claude adapter（`adapters/claude_code/subagent.py`，现为 S2 stub）需要把 `Task` 子代理的 transcript/产物暴露为统一结构，core 的捕获/计数消费该结构而非 Codex glob。**这正是 S2 从 stub 转实装时要补的能力。**
- **来源 run**：`rvf-20260523T062744Z`、`rvf-20260527T175943Z`、`rvf-20260523T135334Z`。

### 🔴 B — causality 归因依赖 host 私有 call_id（维度 3 Transcript · 复现 ×2）
- **症状**：attempt-based 修复在主轨迹无 `apply_patch` call_id，`causality.json` 的 `issues[].candidate_patch_call_ids`、`patches[]` 恒空、`patch_events[].call_id=null`，issue↔patch 关系断链（只能靠 `fix_attempts[].fix_patch_path` 兜底）。
- **当前违约**：`rvf_fix_attempt.py` 全文无 `call_id`/`trajectory_line` 字段（main 上 grep 0 命中）。
- **契约取向**：维度 3 典型陷阱原文点名 **"Claude Code 用 `tool_use_id`，Codex 用 `call_id`"**。core 应消费 `NormalizedTranscript.ToolResult.call_id`（adapter 从各自 host 字段映射），并在 fix-attempt 记录里携带触发它的归一化 call_id，让 analyze 能把 attempt patch 接回 trajectory。
- **来源 run**：`rvf-20260522T091050Z`、`rvf-20260525T094513Z`。

### 🔴 C — same-session-full 口径未切分（维度 3 Transcript / 增量读 · 复现 ×1）
- **症状**：`flow-1-self-rising` 下 "RVF 自身轨迹" = 整段会话（含触发前的实现工作）。`rvf-20260523T135334Z` 的 1359 个 tool_call 主要是会话全程工作量，被当成 RVF 子流程读会严重失真。
- **契约取向**：维度 3 的 `read_transcript` 提到 **`since: offset` 增量读 / 分段**。core/adapter 应能按 phase_marker 或 cut offset 切出 review-validate-fix 子区间再计数（或在 scaffold 显式标注"含触发前会话"）。
- **来源 run**：`rvf-20260523T135334Z`。

---

## 已解决的范式参考（请沿用，别再当 per-host 补丁打）

### ✅ G — Stop / UPS hook 的 Codex-aware no-op（维度 1 Hook entry）
- **背景**：Codex plugin loader 把 plugin-bundled `hooks/hooks.json` 也当 hook 源加载，与 `~/.codex/hooks.json` 注册的 entry 平行执行 → 同一 Codex Stop 事件触发两次 RVF。
- **已修**：`plugins/review-validate-fix/hooks/stop.py:19 _is_codex_invocation`（凭 transcript 落在 `/.codex/sessions/` 判 Codex 即静默退出），引入于 `7236a74`；UPS 同理 `6c6ff47`。
- **给 overhaul 的提醒**：这是维度 1 "hook entry 必须每 host 恰好注册一次、core 永不重复处理" 的活教材。overhaul 应让这个保证 **结构性成立**（adapter 负责 host 的唯一注册 + 跨 host 不串台），而不是继续在脚本里手写 `if is_codex: no-op`——后者正是维度 1/反模式所禁止的"在 hook 脚本里写判断逻辑"。

---

## 邻接项（host-agnostic，**不在本次 cross-harness 重点**，仅备查）

这两条也出现在同窗口 run 的 Follow-up，但属 core 侧 host 无关问题，列此以免丢失；overhaul 时无需优先处理：

- **D · scope-expansion 对 session-owned 新测试无 override**：本 session 新建的测试被 scope.contract 同标 `protected_files`+`background_files`，`rvf_fix_attempt.py:438 _validate_scope_expansion` 仍一律 block 向 protected/background 扩展，回归测试无法随 `fix.patch` 携带、需手工搬运。来源：`rvf-20260522T091050Z`。
- **E · ledger 一致性缝隙**：validate/fix 子代理首次 `rvf_fix_attempt.py start` 报 `RVF issue does not exist`，需手动 `diff_tracker.rvf_issue_upsert` 补注册——`upsert` 与 attempt `start` 跨 worktree 的 DB 视图/路径不一致（低置信，建议先复核当前路径解析）。来源：`rvf-20260525T094513Z`。

---

## 证据与可复现入口

```sh
# 跨 run 复盘报告（本 handoff 的母文档）
open ~/.claude/rvf/cross-run-analysis-2026-05-28.md

# 单 run 自报 Follow-up（A/B/C 的一手出处）
grep -A20 '^## Follow-up' ~/.claude/rvf/runs/rvf-20260523T135334Z-stop-hook-1fb70b94/artifacts/analysis/summary.md

# 当前违约点（在 main / 任一 checkout 内）
SCR=plugins/review-validate-fix/skills/review-validate-fix/scripts
grep -nE 'tool.*==.*apply_patch|!= "apply_patch"' $SCR/analysis_artifacts.py   # A1
sed -n '1,30p;60,75p' $SCR/subagent_capture.py                                 # A2（Codex-only 声明 + ~/.codex glob）
grep -n call_id $SCR/rvf_fix_attempt.py                                        # B（应为空）
sed -n '12,61p' plugins/review-validate-fix/hooks/stop.py                      # G（范式）
```

---

## 验证建议（沿用契约的 "adapter 互换" 法）

每条缺口的"修好了"判据，建议用 `05-adapter-contract.md` 末尾的 adapter-swap / fixture 验证：

- **A1/B/C**：准备一份 **Claude-host fixture transcript**（已 normalize 或经 Claude adapter 解析）→ 跑 `analysis_artifacts` → patch 计数 / `candidate_patch_call_ids` / 子区间计数应与 Codex fixture 在语义上一致（数值非 0、call_id 接得上、计数只覆盖 RVF 子区间）。
- **A2**：mock 一个 Claude `Task` 子代理产物 → core 的 subagent 捕获/计数应识别并归因，不依赖 `~/.codex/sessions`。
- **通用判据**：契约要求 "把 adapter 删了 core 仍能在新 host 接通"。若上述任一仍需在 core 内判 `if host == codex`，说明缺口未真正闭合。

---

## 约束提醒（AGENTS.md）

- **forward-only / 无 backward-compat 残留**：如确需保留旧入口，改动日志 commit 前清入 `dev_backward_compatibility/`，正文不留。
- **core 内禁止 `if host == codex`** 式分支（`06-rvf-application.md`"不立刻做的事"）——host 判断只能在 adapter。
- **Kanban 语境只用 `cline-kanban` / `kanban` CLI**，不引入 `vibe-kanban`。
- commit 前缀 conventional：`refactor(rvf): …` / `feat(rvf): …` / `docs(rvf): …`。
