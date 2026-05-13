# 附录 · 来源与核验

> 所有引用源均在 **2026-05-12** 通过 raw GitHub 内容 / 官网 specification 页 / issue 页直接拉取核验。本附录只记录"指南所依据的真实可见证据"，不充当"项目目录"。
>
> 不收录 star 数、issue 数等流行度指标 —— 这些数字会随时间漂移，且与架构正确性无关。

---

## A. obra/superpowers

| 路径 | 用途 | 核验结果（2026-05-12） |
|---|---|---|
| `github.com/obra/superpowers` | 仓库主页 | 可访问 |
| `raw.githubusercontent.com/obra/superpowers/main/.claude-plugin/plugin.json` | Claude Code 原生 manifest | 存在；`name: "superpowers"` |
| `raw.githubusercontent.com/obra/superpowers/main/.codex-plugin/plugin.json` | Codex 原生 manifest | 存在；`name: "superpowers"`，`skills: "./skills/"`，含 `interface{}` 块 |
| `raw.githubusercontent.com/obra/superpowers/main/scripts/bump-version.sh` | 版本同步脚本 | 存在 |
| `raw.githubusercontent.com/obra/superpowers/main/scripts/sync-to-codex-plugin.sh` | manifest 同步脚本 | 存在 |
| `raw.githubusercontent.com/obra/superpowers/main/plugin.universal.yaml` | 3 号报告声称存在的 universal manifest | **不存在**（404） |
| `raw.githubusercontent.com/obra/superpowers/main/hookbridge/` | 3 号报告声称存在的 compiler 目录 | **不存在** |

### 结论
Superpowers 走的是 Pattern A（多 manifest in-repo + 共享 skills），不是 3 号报告所说的 "Universal Manifest + Compiler"。3 号报告此处错误。

---

## B. affaan-m/everything-claude-code（ECC）

| 路径 | 用途 | 核验结果（2026-05-12） |
|---|---|---|
| `github.com/affaan-m/everything-claude-code` | 仓库主页 | 可访问 |
| `raw.githubusercontent.com/affaan-m/everything-claude-code/main/.claude-plugin/plugin.json` | Claude Code manifest | 存在；`name: "ecc"`，`version: "2.0.0-rc.1"`，`skills: ["./skills/"]`，`commands: ["./commands/"]`，**无** `hooks` 字段 |
| `raw.githubusercontent.com/affaan-m/everything-claude-code/main/install.sh` | installer | 存在；支持 `--profile` 与 `--target` |
| `raw.githubusercontent.com/affaan-m/everything-claude-code/main/docs/architecture/cross-harness.md` | 跨 harness 总览 | 存在，文档化 4 档兼容性矩阵 |
| `raw.githubusercontent.com/affaan-m/everything-claude-code/main/docs/architecture/harness-adapter-compliance.md` | 兼容性细则 | 存在 |

### 结论
ECC 真实存在；plugin id `ecc`；installer 显式 profile/target；4 档矩阵：Claude Code = Native；Codex = Instruction-backed；OpenCode = Adapter-backed；Cursor = Adapter-backed。是 Pattern A + installer 增强（接近 Pattern C 的双轨变体）。

---

## C. EveryInc/compound-engineering-plugin

| 路径 | 用途 | 核验结果（2026-05-12） |
|---|---|---|
| `github.com/EveryInc/compound-engineering-plugin` | 仓库主页 | 可访问 |
| `.claude-plugin/plugin.json` | Claude Code manifest | 存在；结构与 Superpowers 同形 |

### 结论
Pattern A 不是 Superpowers 的孤例。

---

## D. cc-plugin-to-codex 类项目

> 公开仓库具体名字随着时间会变化；这里只记录"该类项目存在并实际可运行"这一事实。

- 已观察到至少一个公开项目以 Claude Code plugin 结构为输入、Codex plugin 结构为输出的翻译器形态存在（Pattern B 实例）。
- 实际使用时应自行核验该项目的最新维护状态与翻译规则。

---

## E. agentskills.io

| 路径 | 用途 | 核验结果（2026-05-12） |
|---|---|---|
| `agentskills.io/` | 协议主页 | 可访问 |
| `agentskills.io/specification` | 规范页 | 公布 frontmatter 字段：`name`、`description`、`license`、`compatibility`、`metadata`、`allowed-tools` |
| `agentskills.io/clients`（或同等页面） | 已知 client 实现 | 列出 30+ 个 client（具体名单随生态更新） |

### 结论
事实标准级的 skill 文档协议；规范 skill 文档语法，**不**规范 hook / subagent / transcript 等 runtime。

---

## F. openai/codex hook 限制

| 路径 | 用途 | 核验结果（2026-05-12） |
|---|---|---|
| `github.com/openai/codex/issues/16430` | 报告 Codex 不扫插件根 hooks 的 issue | **OPEN** |
| `github.com/openai/codex` 上 `~/.codex/hooks.json` 相关文档 | 用户配置层 hook 接入位置 | 存在并被官方文档引用 |

### 结论
Codex 当前的 hook 运行时只扫 `~/.codex/hooks.json`。这是 [`04`](04-anti-patterns.md) ③ 反模式与 [`06`](06-rvf-application.md) 中 Codex adapter 设计的依据。

---

## 对 4 份原始报告的取舍

> 这部分是"为什么本指南选择 / 舍弃报告中的某段"的记账。

### `harness-adapter-research-report.md`
- **采纳**：5-bullet TL;DR 的结构、候选项目事实表、3 主流模式、3 反模式、5 切片、"核验提醒"机制。
- **保留但改名**："统一 plugin id" 的建议被沿用并写进 [`04`](04-anti-patterns.md) ② 与 [`07`](07-implementation-slices.md) S0。

### `research4.md`
- **采纳**：Shared Source + Thin Adapter 的概念、RVF 目录树建议（被改写成 [`06`](06-rvf-application.md) 的"推荐目录形态"）。
- **修正**：报告中曾提到 `core/` 与 `adapters/<host>/` 并列；本指南完全沿用。

### `research3.md`
- **重点反驳**：声称 Superpowers 走"Universal Manifest + Compiler"路径，包含 `plugin.universal.yaml` 与 `hookbridge/`。经 raw GitHub 拉取**不存在**。本指南把 Universal Manifest + Compiler 单列出来作为"理论上的 Pattern B 极端形态"提及，但**不**作为推荐路径。
- **保留**：3 号报告提出的"协议层 vs 适配层"双层框架有价值，已写入 [`01-glossary-and-scope.md`](01-glossary-and-scope.md)。

### `report.md`
- **不纳入**。该文件内容是关于 RVF 内部 Stop hook 的 harness 选型（Cline Kanban / Codex GUI fork / Manual harness）—— 这里的"harness"是 RVF 内部 stop-hook host 概念，与"多 host 编码代理插件研究"是不同问题。归档保留，本指南不引用。

---

## 复核建议

如果在未来的某次复读中发现本附录的某条核验已失效（仓库改名、issue 被关、字段被移除等），按以下顺序处理：

1. 在指南正文相应位置加 ⚠ 提示并附新核验日期。
2. 不要悄悄修改原结论。如确需修订，新写一节说明"截至 YYYY-MM-DD 现状变化"。
3. 仍然不要引入未核验的项目；如有必要扩充候选项目，先做独立调研并写新的 appendix。
