# 01 · 术语与范围

本指南的术语在不同文档/报告中常被混用，先固定本指南内部的含义。

---

## 术语

### Host（宿主 / harness）

运行编码代理的具体客户端进程。每个 host 自己规定：
- 插件清单格式（manifest 的字段名、目录布局）。
- skill / command / hook / agent 的发现路径。
- 运行时上下文（transcript / event stream 的结构、subagent 调用方式、tool 接口）。
- 权限/能力声明（allowed-tools、permission scopes、env 注入方式）。

本指南覆盖的 host：
- **Claude Code**（CLI + IDE 扩展，主线 host）。
- **Codex**（OpenAI 的 CLI `codex` + GUI fork）。
- **OpenCode**。
- **Cursor**（IDE，插件接口最受限）。
- **Gemini / Hermes / OpenClaw**（次级支持，多以 Reference-only 方式声明）。

> ⚠ "harness" 在 RVF 内部还有一个**第二义**：指 RVF Stop hook 调度的运行栈（Codex / Claude Code / Manual）。本指南统一用 host 指**外部宿主**，用 "RVF harness path" 或 "stop-hook host" 指 RVF 内部的栈选择，避免歧义。

### Plugin（插件）

可以被某个 host 安装、启用、禁用的一组功能包。一个 plugin 通常包含：
- manifest（host 特定）。
- 一个或多个 skill / command / agent / hook 入口。
- 自身的版本、license、依赖声明。

"跨 harness 插件" = 同一个仓库同时面向多 host 分发的 plugin。

### Framework

"为多 host 提供统一开发体验"的工具层。例：
- `agentskills.io` 提供 skill frontmatter 标准，多个 client 解析它。
- 某些 marketplace（plugin index）也算 framework 层。

framework 不等于 host，也不等于 adapter。**framework 解决"语法标准化"，adapter 解决"运行时差异"**。

### Protocol

更下层、跨 host 共享的格式约定。`agentskills.io` 是 skill 文档协议；MCP（Model Context Protocol）是工具调用协议。多 host 之所以能"共享同一份 skill 文档"，是因为这些 protocol 帮各 host 把语法对齐。

### Adapter（适配器）

把"host-agnostic 的 core 逻辑"接到具体 host 的薄层代码。adapter 的职责包括：
- 把 host 的 hook event 翻译成 core 能消费的统一调用。
- 把 host 的 transcript / tool result 翻译成 core 能消费的统一数据结构。
- 把 core 的输出（subagent 调用、stop 决策、permission ask）翻译回 host 原生 API。
- host 特定的安装、目录布局、权限声明。

**adapter 不应包含业务逻辑**，业务逻辑全在 core。判断方法：把 adapter 整个删了之后，core 在另一个 host 上重新写 adapter 应能立刻接通。

### Core

host-agnostic 的业务实现。例：reviewer 的 system prompt、validate/fix 的判定规则、报告生成模板、配置文件 schema、共享 utility。core 必须能在不 import 任何 host SDK 的前提下被单元测试。

---

## "协议层 vs 适配层"

3 号报告提出了一个有用的双层框架：
- **协议层** = framework / protocol（skill 标准、MCP 等）：解决"同一份文档/工具描述被多 host 解析"。这一层不能解决 hook 运行时差异。
- **适配层** = adapter：解决"core 与 host 私有运行时绑定"。

这两层互不替代。`agentskills.io` 把 skill 语法标准化之后，hook 入口、transcript 解析、subagent 调用方式仍然必须各 host 单独写 adapter。这是本指南反复强调的点。

---

## 范围限定

本指南**覆盖**：
- 多 host 的 plugin 仓库布局。
- skill / command / hook / agent 在多 host 下的分发策略。
- core/adapter 边界的契约设计。
- Codex 当前 plugin-local hook 限制及 fallback。
- RVF 应用层建议（[`06-rvf-application.md`](06-rvf-application.md)）。

本指南**不覆盖**：
- 单 host 内部的 skill 编写规范（见各 host 官方文档）。
- MCP server 实现细节。
- 具体 host 的安装 / 鉴权流程。
- 商业插件市场的发布、计费、签名机制。
