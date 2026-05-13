# 02 · 已核验的真实地形

> 本节列出的 4 个候选项目 + 1 个协议层标准，均在 2026-05-12 通过 raw GitHub 内容、issue 页面、官网 specification 页核验。所有引用源见 [`appendix-sources.md`](appendix-sources.md)。
>
> ⚠ 项目的 **star 数、issue 数、贡献者数** 不在本节出现 —— 这些指标随时间漂移，且与架构正确性无关；不同报告之间的"流行度"冲突全部归因于核验日期差异，无需进一步消解。

---

## A. obra/superpowers

**架构**：多 manifest in-repo + 共享 skills 树（Pattern A）。

**核验事实（2026-05-12）**：
- 仓库 `main` 分支同时存在两份原生 manifest：
  - `.claude-plugin/plugin.json` → `name: "superpowers"`。
  - `.codex-plugin/plugin.json` → `name: "superpowers"`，含 `skills: "./skills/"` 与 `interface{}` 块。
- `scripts/` 目录只有 `bump-version.sh` 与 `sync-to-codex-plugin.sh`；**没有** `hookbridge/`、**没有** `plugin.universal.yaml`、**没有** 任何 compiler 风格的中间格式产物。
- 共享 `skills/` 目录被两份 manifest 同时引用。

**含义**：
- 这是当前可观察到的"多 host 共存"最简实现：**复制 manifest，不复制 skills**。
- 同一 plugin id（`superpowers`）在所有 host 上保持稳定，避免用户在不同 host 上看到不同名字。
- 之所以需要 `sync-to-codex-plugin.sh`，是因为某些字段（version、description 等）需要在两份 manifest 之间保持一致；这条 sync 边界正是 "core" 与 "manifest" 的分界。

**对 3 号报告的反驳**：3 号报告称 Superpowers 采用 "Universal Manifest + Compiler" 路径（`plugin.universal.yaml` + `hookbridge/`）。经直接拉取 `raw.githubusercontent.com/obra/superpowers/main/` 下相关路径，**该结构不存在**。3 号报告此处错误，理由可能是它引用的某个 fork 或更早期分支。

---

## B. affaan-m/everything-claude-code（ECC）

**架构**：installer-driven Pattern A + 4 档兼容性矩阵。

**核验事实（2026-05-12）**：
- `.claude-plugin/plugin.json`：`name: "ecc"`，`version: "2.0.0-rc.1"`，含 `skills: ["./skills/"]` 与 `commands: ["./commands/"]`，**无** `hooks` 字段。
- 通过 `install.sh --profile <host> --target <path>` 安装，profile 包括 claude-code / codex / opencode / cursor 等。
- `docs/architecture/cross-harness.md` + `docs/architecture/harness-adapter-compliance.md` 公开声明 4 档兼容性矩阵：
  - **Native**：host 原生支持，无需 adapter。
  - **Adapter-backed**：通过 adapter 包装现有 host 接口提供功能。
  - **Instruction-backed**：靠 skill / prompt 文档让用户/agent 模拟功能，无运行时支持。
  - **Reference-only**：仅作为文档资源被引用，不在该 host 实际执行。
- ECC 自己的矩阵：Claude Code = Native；Codex = Instruction-backed；OpenCode = Adapter-backed；Cursor = Adapter-backed。

**含义**：
- "兼容性"不是 0/1，**4 档显式声明**比"打钩列表"更不容易把用户引到坑里。
- installer 显式指定 `--profile` 与 `--target`，避免对用户系统做隐式探测；和 plugin id 稳定（`ecc`）一致地降低用户认知负担。
- ECC manifest **没有** `hooks` 字段，符合 [`04-anti-patterns.md`](04-anti-patterns.md) 中"Codex 当前不能从插件根加载 hook"的现实约束。

---

## C. EveryInc/compound-engineering-plugin

**架构**：Pattern A，与 Superpowers 同形。

**核验事实（2026-05-12）**：
- 仓库存在并可访问，根目录结构与 Superpowers 同形：`.claude-plugin/plugin.json` + skill/command 子目录。
- 该项目作为"compound engineering"实践的 plugin 化封装，提供一组 skills 与命令。

**含义**：
- Pattern A 不是 Superpowers 的孤例，已经在生态里被复用。
- 对 RVF 这种结构相近的项目（也是 review/validate/fix 流程的封装）有直接参考价值。

---

## D. cc-plugin-to-codex 类项目

**架构**：Pattern B（单源 → 翻译器 → 多 host）。

**核验事实（2026-05-12）**：
- 存在一类将 Claude Code 插件结构翻译为 Codex plugin 结构的转换器项目（具体仓库名见 [`appendix-sources.md`](appendix-sources.md)）。
- 这类项目本身不是一个跨 host 插件，而是 **跨 host 翻译工具**：以 Claude Code plugin 为输入，输出 Codex plugin 目录。

**含义**：
- 验证了 Pattern B 的可行性：可以选一个 host 作为"标准源"，其它 host 通过翻译器派生。
- 副作用：派生产物不一定能直接享受 host 原生体验（例：Codex 的 hook 不会真正生效，因为运行时不扫插件根 —— 见 [`04-anti-patterns.md`](04-anti-patterns.md)）。
- 适合"主 host 是 Claude Code、其它 host 仅需 best-effort 兼容"的项目；不适合"多 host 都要 Native 体验"的项目。

---

## E. agentskills.io（协议层）

**核验事实（2026-05-12）**：
- 官网 `/specification` 页公布 skill 文档标准：frontmatter 必含 `name`、`description`，可选 `license`、`compatibility`、`metadata`、`allowed-tools`。
- 已声明的 client 实现超过 30 个（具体名单见 [`appendix-sources.md`](appendix-sources.md)），含多种 IDE 与 agent 框架。

**含义**：
- 这是当前最接近"事实标准"的 skill 文档协议层。
- **但** 它只规范 skill 文档；不规范 hook、subagent、transcript、permission。也就是说一份 `agentskills.io` 兼容的 skill 文档能被多个 client 解析，但 hook/runtime 仍各 host 各自处理。
- 实务上：建议把 core/`skills/*.md` 直接写成 `agentskills.io` 兼容形态；hook 与 subagent 仍交给 adapter。

---

## ⚠ 截至 2026-05-12 未核验或部分核验

| 项目 | 状态 | 备注 |
|---|---|---|
| `obra/superpowers` 上游 fork 链 | 部分 | 主仓 main 已核验；任何 fork 自行修改的不在本指南范围。 |
| Codex GUI fork（私有） | 未核验 | 无公开仓库可拉取；本指南不引用其内部结构。 |
| Hermes / OpenClaw plugin manifest | 未核验 | 这两个 host 当前仍以 Reference-only 方式被本指南对待；如需深度支持，建议先做一次独立调研。 |
| 任何商业 marketplace 的发布流程 | 不在范围 | 见 [`01-glossary-and-scope.md`](01-glossary-and-scope.md) 的"不覆盖"列表。 |
