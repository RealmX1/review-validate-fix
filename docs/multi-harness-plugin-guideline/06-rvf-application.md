# 06 · 把指南套到 Review-Validate-Fix

> 本节给出"如果按本指南的原则推进 RVF 的多 host 支持，最小但完整的落地形态"。
>
> 不是任务清单 —— 任务级别的拆分见 [`07-implementation-slices.md`](07-implementation-slices.md)；本文专注**形状**与**取舍**。

---

## 当前 RVF 现状（事实描述）

- 仓库内已存在 `plugins/review-validate-fix/`，主要面向 Claude Code 侧的 plugin 形态。
- 仓库内**已存在单份嵌套 Codex manifest**：`plugins/review-validate-fix/.codex-plugin/plugin.json`，其 `name` 字段为 `"rvf"`（**不是** `"review-validate-fix"`），且嵌套在 `plugins/review-validate-fix/` 下而非 repo-root。**尚无** repo-root 级别的 `.claude-plugin/plugin.json`。
- Stop hook 调度链：`codex_stop_hook_router.py → codex_stop_hook_dispatcher.py → codex_stop_review_validate_fix.py`。它能同时面向 Codex 与 Claude Code 两栈进行 transcript 解析与 reviewer 触发。
- transcript 解析按 `HOST_CODEX="codex"` / `HOST_CLAUDE="claude_code"` 两栈分流。
- skill 文档：`review-validate-fix:review-validate-fix` 已经是 Claude Code 一等公民；Codex 侧通过 skill 文档 + Stop hook 间接绑定。
- 仍有 backward compatibility / dev-only 改动残留（按 AGENTS.md 要求，commit 前应清理至 `dev_backward_compatibility/`）。

结论：RVF 已经**局部触及 Pattern A 思路**（已有单份 Codex manifest，多 host 共存的 shape 已具雏形），但尚未完成 —— core ↔ adapter 边界未显式化，且当前的嵌套 manifest 形态与本指南推荐的"repo-root 双 manifest + 统一 plugin id"形态尚有差距。具体来说，本仓库当前同时正落在 [`04-anti-patterns.md`](04-anti-patterns.md) **反模式 ②（Plugin-id 漂移：Codex manifest 叫 `rvf`，但 Claude Code 侧设计 id 为 `review-validate-fix`）** 的样本上；对齐路径见 [`07-implementation-slices.md`](07-implementation-slices.md) 的 **S0**（统一 plugin id + 决定 manifest 位置）。

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

### 当前形态 vs 建议形态：迁移取舍

仓库当前形态：`plugins/review-validate-fix/.codex-plugin/plugin.json (name="rvf")`，**嵌套**在 `plugins/review-validate-fix/` 下，且**无** repo-root `.claude-plugin/plugin.json`。

与上面建议形态的差距，至少包含：
1. **manifest 位置**：嵌套在 `plugins/review-validate-fix/` 下 vs 建议位于 repo-root。
2. **plugin id 漂移**：Codex manifest `name="rvf"` vs 建议统一为 `review-validate-fix`（参见 [`04`](04-anti-patterns.md) 反模式 ②）。
3. **缺失 Claude Code manifest**：当前没有 `.claude-plugin/plugin.json`，需要补齐才能形成"双 manifest + 同 id"的 Pattern A 完整形态。

这三步迁移工作归入 [`07-implementation-slices.md`](07-implementation-slices.md) 的 **S0**（统一 plugin id + 双 manifest 位置）。本指南其余章节中"建议形态"的描述都以 S0 完成后的形态为前提。

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
