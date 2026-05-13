# 多 Harness 编码代理插件设计指南

> 适用范围：为 Claude Code、Codex CLI/GUI、OpenCode、Cursor、Gemini、Hermes、OpenClaw 等多种"编码代理 harness"同时分发同一套 skill / command / hook / agent 的项目。
>
> 本指南由对 4 份外部研究报告的核验、综合与去冲突得出（详见 [`appendix-sources.md`](appendix-sources.md)）。所有引用的项目结构、manifest 字段、issue 编号等关键事实均带有 2026-05-12 的核验时间戳。

---

## TL;DR（五条）

1. **不要重新发明 manifest 格式**。各 host 已经各自定义了自己的插件清单（`.claude-plugin/plugin.json`、`.codex-plugin/plugin.json`、`AGENTS.md`、OpenCode 的 plugin schema 等）；在一个仓库里同时维护多份 host-原生 manifest，再共享 `skills/` `commands/` `agents/` 等内容目录，是当前可观察到的**主流做法**。
2. **核心实现要 host-agnostic**。skill 文档、reviewer prompt、validate/fix 逻辑、配置文件等"业务核心"放在共享根目录；host 特定的接线（hook entry、subagent invoke、transcript 解析、permission 配置）放在 host adapter 里。adapter 必须 thin，core 不得 import host idioms。
3. **Codex 当前没有插件级 hook 运行时**。`openai/codex` 仓库 issue `#16430`（核验日 2026-05-12 仍为 OPEN）说明 Codex 只在 `~/.codex/hooks.json`（用户配置层）扫描 hook，**不扫描已安装的插件根**。任何依赖"插件自带 hook"的跨 harness 设计在 Codex 上都需 fallback：通过 slash command / skill 文档要求用户手动安装 hook，或通过 installer 写入 `~/.codex/hooks.json`。
4. **协议层 vs 适配层要分清**。`agentskills.io` 是面向多 client 的 skill 文档标准（frontmatter 字段、`allowed-tools` 等），属"协议层"；它能让一份 skill 文档同时被多个 client 解析，但**不解决** hook 运行时、transcript 结构、subagent 调用方式这些 host 私有差异。这些只能由 adapter 解决。
5. **对外发布前，profile/installer 比"自动检测"更可靠**。Everything-Claude-Code 等项目通过 `install.sh --profile <host> --target <path>` 显式选目标，避免在用户系统上误装；与此同时维护"4 档兼容性矩阵"（Native / Adapter-backed / Instruction-backed / Reference-only）公开声明每个 host 的支持深度。这是当前可推广的最稳模式。

---

## 阅读顺序

| # | 文件 | 内容 | 何时读 |
|---|---|---|---|
| 0 | 本 README | TL;DR、阅读顺序、与既有报告的关系 | **先读** |
| 1 | [`01-glossary-and-scope.md`](01-glossary-and-scope.md) | host / plugin / framework / protocol / adapter / core 定义 | 名词理不清时 |
| 2 | [`02-verified-landscape.md`](02-verified-landscape.md) | 已核验的 4 个候选项目（Superpowers / ECC / Compound Engineering / cc-plugin-to-codex）+ agentskills.io 标准 | 想看真实证据 |
| 3 | [`03-dominant-patterns.md`](03-dominant-patterns.md) | Pattern A/B/C 三种主流架构对比、适用场景、典型实现 | 设计决策前 |
| 4 | [`04-anti-patterns.md`](04-anti-patterns.md) | 5 个高频反模式（shadow tree、plugin-id 漂移、Codex 插件 hook、host idioms 漏到 core、inline hook 不消费 stdin） | 评审现有方案时 |
| 5 | [`05-adapter-contract.md`](05-adapter-contract.md) | core ↔ adapter 的 6 维契约（hook entry / subagent / transcript / permission / config / discovery） | 准备落地编码时 |
| 6 | [`06-rvf-application.md`](06-rvf-application.md) | 把上述原则套到本仓库 review-validate-fix 的落地建议 | RVF 维护者读 |
| 7 | [`07-implementation-slices.md`](07-implementation-slices.md) | 5 个可独立验证、可回滚的落地切片 + 依赖顺序 | 准备开 PR 时 |
| A | [`appendix-sources.md`](appendix-sources.md) | 全部引用来源 URL + 核验时间戳 + 4 份原始报告的取舍说明 | 复核时 |

---

## 与既有 4 份报告的关系

`/Users/bominzhang/Documents/GitHub/review-validate-fix/docs/multi-harness plugin research/` 下有 4 份原始报告。本指南对它们的处理：

| 报告 | 处理 | 原因 |
|---|---|---|
| `harness-adapter-research-report.md` | **大量采纳** | 提供了 5-bullet TL;DR、候选项目事实表、3 主流模式、3 反模式、5 切片，结构清晰且自带"核验提醒"。 |
| `research4.md` | **大量采纳** | 与 1 号报告结论高度一致（Pattern A），并给出更具体的目录树建议。两份报告交叉印证。 |
| `research3.md` | **重点反驳后选择性采纳** | 主推"Universal Manifest + Compiler"路径（声称 Superpowers 用 `plugin.universal.yaml` + `hookbridge/`）。经 raw GitHub 内容核验，该结构**不存在** —— Superpowers 实际是多 manifest in-repo（Pattern A）。3 号报告的"协议层 vs 适配层"概念框架仍有保留价值，已收录在 [`01-glossary-and-scope.md`](01-glossary-and-scope.md)。 |
| `report.md` | **不纳入** | 内容是关于 RVF 内部 Stop hook 的 harness 选型（Cline Kanban / Codex GUI fork / Manual harness），并非"跨 harness 插件研究"。已在 [`appendix-sources.md`](appendix-sources.md) 注明归档不引用。 |

---

## 本指南**不**做的事

- 不写 core / adapter 的具体代码（指南只到契约层；落地代码见 [`07-implementation-slices.md`](07-implementation-slices.md) 的切片描述）。
- 不修改本仓库 `plugins/review-validate-fix/` 任何文件；本目录只是文档。
- 不为"未核验项目"背书。若 [`02-verified-landscape.md`](02-verified-landscape.md) 里某项目带 ⚠ 提示，说明截至 2026-05-12 仍存疑，使用时需自行复核。
- 不提供 star 数、issue 数、贡献者排名等会随时间漂移的"流行度"指标；这些不影响架构决策。
