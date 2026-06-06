# 06 · 把指南套到 Review-Validate-Fix

> 本节给出"如果按本指南的原则推进 RVF 的多 host 支持，最小但完整的落地形态"。
>
> 不是任务清单 —— 任务级别的拆分见 [`07-implementation-slices.md`](07-implementation-slices.md)；本文专注**形状**与**取舍**。

---

## 当前 RVF 现状（事实描述，已对齐 S0 v2–S3）

> 本段早期版本描述的是「Codex manifest 叫 `rvf`、缺 marketplace.json、core↔adapter 未显式化」的旧形态。自 S0 v2 起这些已逐切片闭合；以下为**当前 as-built 形态**。权威的 as-built 矩阵见 [`docs/architecture/cross-harness.md`](../architecture/cross-harness.md)。

- 仓库内 `plugins/review-validate-fix/` 是跨 harness plugin payload，同时被 Claude Code 与 Codex 的本机 marketplace 枚举。
- **两份 nested plugin manifest 的 `name` 已统一为 `"review-validate-fix"`**（S0 v2 消除反模式 ②）：`plugins/review-validate-fix/.claude-plugin/plugin.json` 与 `.codex-plugin/plugin.json` 同 id；版本均 `0.1.0`。
- **采用 (M+N) Marketplace + Nested 变体**：repo-root `.claude-plugin/marketplace.json`（S0 v2 补齐）列出 plugin，`plugins[0].source` 指向 `./plugins/review-validate-fix`；plugin manifest 留在 nested 位置。这是对指南「repo-root 双 manifest」字面形态的有意偏离，理由见下「Pattern A 的 marketplace 变体」节。
- Stop hook 调度链：`codex_stop_hook_router.py → codex_stop_hook_dispatcher.py → codex_stop_review_validate_fix.py`，同时面向 Codex 与 Claude Code 两栈。Claude Code 侧经 `hooks/{stop.py,user_prompt_submit.py}` 薄 shim 转发到该 core（路径 C，trigger-only）；两入口共享单一 host-ownership 契约 `hooks/_claude_hook_entry.py`（S3 守卫单源化）。
- **core ↔ adapter 边界已显式化**：transcript 解析（`core/transcript/` + `adapters/{codex,claude_code}/transcript.py`，S1）、write-op 计数与子区间窗口（S1.5）、子代理捕获与调用向量（`core/subagents/` + 各 adapter，S2）均 host-agnostic，不消费 host 工具名、不硬编码 host 布局。`HOST_CODEX="codex"` / `HOST_CLAUDE="claude_code"` 仍是 adapter 分派常量。
- skill 文档：`review-validate-fix` 是 Claude Code 一等公民（plugin 暴露 1 command + 6 skill；slash 入口统一走 namespaced `/review-validate-fix:<name>` skill 形态，不再保留与同名 skill 重复的薄 shim 命令）；Codex 侧通过 plugin manifest + 安装器注册的 `~/.codex/hooks.json` 绑定。

结论：RVF 已**完成 Pattern A 落地的 (M+N) 变体**——统一 plugin id、补齐 marketplace.json、显式 core↔adapter 边界。已**脱离** [`04-anti-patterns.md`](04-anti-patterns.md) **反模式 ②（Plugin-id 漂移）** 的样本（自 S0 v2 起）。manifest 字段一致性由 `scripts/sync-manifest.sh` fail-fast 守护（S4）。

---

## 推荐目录形态（建议态，非要立刻动土）

```
review-validate-fix/
├── .claude-plugin/
│   └── plugin.json                # name: "review-validate-fix"
├── .codex-plugin/                 # 新增：让 Codex 也以 plugin 形态识别
│   └── plugin.json                # name: "review-validate-fix"（同 id）
├── skills/
│   └── review-validate-fix/SKILL.md   # 共享，按 agentskills.io 兼容写
├── commands/
├── agents/
├── core/                          # 新增：host-agnostic 业务核心
│   ├── reviewer/
│   ├── validator/
│   ├── fixer/
│   ├── transcript/                # NormalizedTranscript 定义
│   ├── decisions/                 # Decision / SubagentResult 等结构
│   └── config/                    # schema + defaults
├── adapters/
│   ├── claude_code/
│   │   ├── hooks/                 # stop hook 入口脚本
│   │   ├── transcript.py          # 解析 .jsonl
│   │   ├── subagent.py            # Task 工具封装
│   │   └── settings/              # 权限 / env 注入
│   ├── codex/
│   │   ├── hooks/                 # 注意：不会被 Codex runtime 自动加载
│   │   ├── transcript.py          # 解析 Codex session log
│   │   ├── subagent.py            # codex exec 封装
│   │   └── install/               # 写入 ~/.codex/hooks.json 的工具
│   └── manual/                    # "Manual" RVF harness path
│       └── ...
├── scripts/
│   ├── sync-manifest.sh           # 同步两份 manifest 的 version/description
│   └── install.sh                 # 可选 --profile <host> --target <path>
├── docs/
│   └── multi-harness-plugin-guideline/  # 本指南
└── dev_backward_compatibility/    # .gitignore；commit 前清理日志
```

### Pattern A 的 marketplace 变体：(M+N) vs (P+R)

上面的"建议目录形态"把 plugin manifest 画在 **repo-root**（`.claude-plugin/plugin.json` + `.codex-plugin/plugin.json`），隐含「**repo = 单个 plugin**」（记作 **(P+R)**：Plugin + Repo 同体）。本仓库**有意不走这个字面形态**，而走 **(M+N)：Marketplace + Nested manifest**：

| | (P+R)：repo = plugin | **(M+N)：marketplace 持 plugin（本仓库）** |
|---|---|---|
| plugin manifest | repo-root `.{claude,codex}-plugin/plugin.json` | nested `plugins/review-validate-fix/.{claude,codex}-plugin/plugin.json` |
| marketplace 文件 | 无 | repo-root `.claude-plugin/marketplace.json` 列出 plugin |
| repo-root 还承载 | 仅 plugin 资产 | plugin payload + `core/` + `adapters/` + 安装器 + 测试/契约套件 |

**何时用哪种**：

- 仓库只发一个 plugin、没有独立工程层 → (P+R) 更简单，manifest 抬到 repo-root 即可。
- 仓库同时是 marketplace、带 host-agnostic 工程代码（`core/`/`adapters/`）、独立安装/测试栈，或未来可能再挂别的 plugin → **(M+N)**，让 plugin payload 与工程层物理分离。

**本仓库为什么选 (M+N)**：repo-root 需要承载 `core/`、`adapters/`、`scripts/install_to_codex.py`、契约/测试套件——这些不属于任何单个 plugin payload。若按 (P+R) 把 plugin manifest 抬到 repo-root，会把「marketplace + 工程层 + plugin」三层身份压在同一目录、污染 plugin 边界。(M+N) 把 plugin 收在 `plugins/review-validate-fix/` 子树、用 repo-root marketplace.json 枚举它，既让两 host 的 marketplace 机制识别到 plugin，又保持工程层干净。

这一变体的对齐工作已在 [`07-implementation-slices.md`](07-implementation-slices.md) 的 **S0 v2** 完成（统一 plugin id + 补 marketplace.json + 立 core/adapters 骨架）。完整 as-built 形态、兼容性矩阵与 manifest 一致性守护（`scripts/sync-manifest.sh`）见 [`docs/architecture/cross-harness.md`](../architecture/cross-harness.md)。

---

## Claude Code 最小集（必须）

- `.claude-plugin/plugin.json`：`name: "review-validate-fix"`，声明 skills/commands/agents/hooks 路径。
- `adapters/claude_code/hooks/stop.py`：脚本入口，**先读 stdin**，把事件 normalize 后调 `core.handle_event("on_stop", ...)`。
- `adapters/claude_code/transcript.py`：把 Claude Code 的 `.jsonl` transcript 解析为 `core.transcript.NormalizedTranscript`。
- `adapters/claude_code/subagent.py`：基于 `Task` 工具封装 `invoke_subagent(role, prompt, ctx)`。
- skill 文档放在 `skills/review-validate-fix/SKILL.md`，按 `agentskills.io` 兼容写 frontmatter。

完成后：Claude Code 安装 → 直接 Native 体验。

---

## Codex 最小集（必须 + fallback）

- `.codex-plugin/plugin.json`：`name: "review-validate-fix"`（与 Claude Code 同 id），同样指向共享 skills/commands。
- `adapters/codex/hooks/stop.py`：脚本入口，与 Claude Code adapter 同形（读 stdin → normalize → 调 core）。
- `adapters/codex/transcript.py`：解析 Codex session log。
- `adapters/codex/subagent.py`：通过 `codex exec` 子进程封装。
- `adapters/codex/install/register_hooks.py`：写一个**显式工具**，把 `adapters/codex/hooks/stop.py` 的绝对路径注册到 `~/.codex/hooks.json`。原因见 [`04`](04-anti-patterns.md) ③ —— Codex runtime 不扫插件根。
- skill 文档：与 Claude Code 共用同一份 SKILL.md，但兼容性矩阵里 Codex 的 hook 一栏标 **"Instruction-backed"**（需用户在安装后跑一次 `register_hooks.py`，或在 skill 文档里说明手动注册步骤）。

完成后：Codex 安装 + 一次 register → Native 体验；不 register → skill/command 可用，hook 不可用。

---

## OpenCode / Cursor / Hermes / OpenClaw（可选）

- 截至 2026-05-12，本指南将这四个 host 划入 **Reference-only** 或 **Adapter-backed**（如 ECC 之于 OpenCode / Cursor）。
- 建议先**不实装** adapter，只在 skill 文档里以 `agentskills.io` 兼容形态发布，让支持该协议的 client 自行解析。
- 兼容性矩阵中明确标注 "Reference-only —— skill 文档可解析，hook/subagent 不接线"。
- 若未来要把任一升级到 Adapter-backed，按 [`05`](05-adapter-contract.md) 的 6 维契约新增一个 `adapters/<host>/`，不动 core。

---

## 兼容性矩阵建议（建议态）

| Host | skill / command | hook | subagent | 等级 |
|---|---|---|---|---|
| Claude Code | ✅ Native | ✅ Native | ✅ Native | **Native** |
| Codex CLI | ✅ Native | ⚠ 需 `register_hooks.py` | ✅ Native（via codex exec） | **Instruction-backed**（hook 维度） |
| Codex GUI fork（私有） | ⚠ 未核验；推测同 CLI | ⚠ 未核验；推测同 CLI | ⚠ 未核验；推测同 CLI | **未核验**（见 [`02`](02-verified-landscape.md) "未核验或部分核验"） |
| OpenCode | ✅ via `agentskills.io` | ❌ | ❌ | **Reference-only** |
| Cursor | ✅ via `agentskills.io` | ❌ | ❌ | **Reference-only** |
| Hermes / OpenClaw | ✅ via `agentskills.io` | ❌ | ❌ | **Reference-only** |

矩阵直接放进未来的 `README.md` / `docs/architecture/cross-harness.md`，参考 ECC 的做法（[`02`](02-verified-landscape.md) B 节）。

---

## RVF 的 "Manual" harness 怎么对齐

RVF 内部已有"Manual" harness path（Stop hook 之外的人工触发链）。建议：
- 把 Manual harness 视为 **adapter 之一**：`adapters/manual/`。
- 它的 hook entry 改写为"由用户手动调用的 CLI"，但下游同样调 `core.handle_event(...)`。
- 这样 Manual 路径享受同一份 core 升级，无需独立维护一份业务逻辑。

---

## 与 AGENTS.md 约束的对齐

- **无 backward compatibility 残留**：上述所有"建议态"动作均为 forward-only；如确需保留任何旧入口（如 Claude 旧 review-validate-fix slash command），在 commit 前把改动日志清入 `dev_backward_compatibility/`，正文不留。
- **不混用 cline-kanban / vibe-kanban**：本节涉及的"Kanban 派发"概念仅指 `cline-kanban` / `kanban` CLI；本指南任何位置都不再引入 `vibe-kanban` 设计。
- **conventional commits**：相关动作的 commit 前缀建议 `feat(rvf): ...` / `refactor(rvf): ...` / `docs(rvf): ...`。

---

## 不立刻做的事

- 不要为对 Codex hook 限制提一个 RVF 私有 workaround（如自己写一个 wrapper runtime）。等上游 `openai/codex#16430` 的进展或社区共识，再决定要不要走更深的方案。
- 不要在 core 里写"如果 host 是 codex 就 …"的分支；这种判断必须在 adapter。
- 不要为"未来想接的 host"提前留 stub 文件夹（YAGNI）；真要接时按契约新增一个 adapter 即可。
