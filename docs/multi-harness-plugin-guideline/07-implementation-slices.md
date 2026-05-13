# 07 · 5 个可独立验证、可回滚的落地切片

> 切片设计原则：每个切片有**入口**（明确触发动作）、**出口**（明确验收条件）、**回滚成本** < 该切片本身。
>
> 顺序：切片之间存在依赖；S0 是前置；S1/S2 可并行；S3 依赖 S2；S4 依赖 S0–S3。
>
> ⚠ 本文件仅描述切片**形态**，不替代实际 issue / plan 文档。真要落地时按 RVF 的工作流（capability planning / implementation plan）走。

---

## S0 · 统一 plugin id 与建立 core/adapter 顶层目录

### 入口
- 当前仓库已有 `plugins/review-validate-fix/` 等多处 manifest / skill 入口；plugin id 在不同位置可能不完全一致。

### 动作
- 选定**唯一** plugin id：`review-validate-fix`。
- 在所有 manifest（已有的 + 即将新建的）`name` 字段统一为该 id。
- 在仓库根新建 `core/` 与 `adapters/` 两个顶层目录（即使内部还为空），标记设计意图。
- 写一份 `core/README.md` 说明"任何不依赖 host SDK 的逻辑放这里"，写一份 `adapters/README.md` 说明"host-specific 接线放这里"。

### 出口验证
- `rg '"name"\s*:' --glob '*plugin.json' .` 显示所有 manifest 都是同一个 id。
- `find core/ -name '*.py' | xargs rg '(claude_code_sdk|codex_sdk)'` 为空（如果 core 已有代码）。
- `core/README.md` 与 `adapters/README.md` 存在。

### 回滚
- 改名是 sed 级动作；新建目录可一键删除。

---

## S1 · 抽出 NormalizedTranscript + 把 Claude Code transcript 解析迁过去

### 入口
S0 完成。

### 动作
- 在 `core/transcript/` 定义 `NormalizedTranscript`、`UserMessage`、`AssistantMessage`、`ToolResult`、`SystemNotice` 等 dataclass / pydantic 模型。
- 在 `adapters/claude_code/transcript.py` 写 `parse_transcript(path) -> NormalizedTranscript`。
- 把 RVF 当前 Claude Code 侧的 transcript 读取代码改为调用 adapter，再消费 core 的 `NormalizedTranscript`。
- 提供一份 fixture transcript（已 normalize）放在 `core/transcript/fixtures/`，供下游切片做 mock。

### 出口验证
- core 模块 `import` 时不依赖任何 host SDK（grep 验证）。
- 用 fixture 跑 reviewer 流程能产出和迁移前一致的报告（snapshot 对比）。

### 回滚
- 单独还原 transcript 模块；核心调度链未变。

---

## S2 · 抽出 invoke_subagent 抽象，先支持 Claude Code

### 入口
S0 完成；可与 S1 并行。

### 动作
- 在 `core/decisions/` 定义 `SubagentResult`（含 `status: ok | timeout | aborted | error`、文本输出、tool calls 摘要）。
- 在 `core/` 中提供 `invoke_subagent(role, prompt, ctx)` 抽象函数（实际实现注入由 adapter 提供）。
- 在 `adapters/claude_code/subagent.py` 写基于 `Task` 工具的实现。
- 把 RVF 现有的 reviewer / validator / fixer 子代理调用全部改为 `core.invoke_subagent(...)`。

### 出口验证
- core 的 reviewer / validator / fixer 模块可单元测试（注入一个 mock subagent 即可）。
- e2e 用 Claude Code 真实跑一次 RVF，回归与迁移前一致。

### 回滚
- 切回直接调 `Task` 工具的旧实现。

---

## S3 · 加 Codex adapter（hook + transcript + subagent）

### 入口
S0、S1、S2 完成。

### 动作
- 新建 `.codex-plugin/plugin.json`，`name: "review-validate-fix"`（与 Claude Code 同 id）。
- 在 `adapters/codex/` 实装：
  - `hooks/stop.py`（**先读 stdin** → normalize → 调 `core.handle_event`）。
  - `transcript.py`（解析 Codex session log → `NormalizedTranscript`）。
  - `subagent.py`（基于 `codex exec` 子进程 → `SubagentResult`）。
  - `install/register_hooks.py`（把 `stop.py` 路径写入 `~/.codex/hooks.json`，对应 [`04`](04-anti-patterns.md) ③）。
- 在 skill 文档或 README 增加"Codex 用户首次安装后请运行 `register_hooks.py`"提示。
- 兼容性矩阵把 Codex 的 hook 一栏从 "Native" 调整为 "Instruction-backed"。

### 出口验证
- Codex 安装 plugin + 跑 `register_hooks.py` 后，stop event 能触发到 `core.handle_event`，core 调度链与 Claude Code 一致。
- Claude Code 侧无回归（切片不动 core，只新增 adapter）。
- 至少有一条 e2e fixture 证明 "Codex hook 真的收到了 transcript"，覆盖 [`04`](04-anti-patterns.md) ⑤ 的 inline-hook stdin 反模式。

### 回滚
- 删除 `.codex-plugin/` 与 `adapters/codex/`；core 无需变化。

---

## S4 · 文档化 + 兼容性矩阵公开 + manifest sync 工具

### 入口
S0–S3 完成。

### 动作
- 在 `docs/multi-harness-plugin-guideline/` 落地本指南（本切片即是其一部分；后续更新落到这里）。
- 在 RVF README / `docs/architecture/cross-harness.md` 公开兼容性矩阵（参考 ECC，[`02`](02-verified-landscape.md) B 节）：
  | host | skill | hook | subagent | 等级 |
  |---|---|---|---|---|
  - 文字声明：什么是 Native / Adapter-backed / Instruction-backed / Reference-only。
- 加 `scripts/sync-manifest.sh`：校验/同步所有 `*-plugin/plugin.json` 中 `name`、`version`、`description` 字段一致；CI 接入。
- 加 `scripts/install.sh --profile <host> --target <path>`（可选；参考 ECC）。

### 出口验证
- `scripts/sync-manifest.sh --check` 在 CI 通过。
- 矩阵文档 在 README 主入口可达（不是埋在三层子目录里）。
- 指南本身被引用：README 顶部 / CONTRIBUTING 提到 "新增 host 支持前先读 `docs/multi-harness-plugin-guideline/`"。

### 回滚
- 文档可移除；sync 脚本可删除。安全。

---

## 依赖图

```
        S0  (统一 id + 顶层目录)
        ├─→ S1 (NormalizedTranscript)
        └─→ S2 (invoke_subagent)
               ↓
              S3 (Codex adapter)
               ↓
              S4 (文档 + 矩阵 + sync 工具)
```

S1 与 S2 之间无依赖，可并行；S3 等 S2（subagent）也等 S1（transcript）；S4 等前三个。

---

## 每个切片建议的 commit 前缀（conventional commits）

| 切片 | 建议前缀 |
|---|---|
| S0 | `chore(rvf): unify plugin id and scaffold core/adapters` |
| S1 | `refactor(rvf): extract NormalizedTranscript + claude_code adapter` |
| S2 | `refactor(rvf): introduce invoke_subagent + claude_code adapter` |
| S3 | `feat(rvf): add codex adapter (hook + transcript + subagent + install)` |
| S4 | `docs(rvf): publish compatibility matrix + manifest sync tooling` |

---

## 共同验收准则（每切片都要满足）

- 任何 backward compatibility 改动在 commit 前清入 `dev_backward_compatibility/`（按 AGENTS.md）。
- 不引入 `vibe-kanban` 命名；任何 Kanban 上下文按 `cline-kanban` / `kanban` CLI 表述。
- 默认中文文档（含 commit 信息），与全局偏好一致。
- 只更新本地分支，不主动 push 远端，除非显式被要求。
