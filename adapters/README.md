# adapters/

host-specific 实现。每个子目录承担一个 harness 的 6 维契约实现（见 `docs/multi-harness-plugin-guideline/05-adapter-contract.md`）。

## 子目录

| 目录 | host | 状态 |
|---|---|---|
| `adapters/claude_code/` | Claude Code（marketplace + plugin） | S3 收尾决策中（默认形态：transcript + subagent stub；hooks 留在 `plugins/review-validate-fix/hooks/` nested 位置） |
| `adapters/codex/` | Codex（marketplace + plugin） | S1/S2 落点；Stop hook chain 仍在 `plugins/review-validate-fix/skills/.../scripts/` 内 |

OpenCode / Cursor / Hermes / OpenClaw 作为 **host adapter**（RVF 寄居的 harness，需实现 6 维契约）在本仓库仍为 Reference-only，不在 `adapters/` 下落实装。

> 注意区分两个「external」概念：上表与本段说的是 **host adapter**。另有一条独立的 **alternative reviewer**（santa-method 外部评审员，由 config 驱动、跑 `prompts/reviewer.md`）——`cursor-agent` 已作为可选 alternative reviewer 受支持（仓库自带 `skills/review-validate-fix/config/alternative-reviewer.cursor.json` 模板），这与它作为 host adapter 的 Reference-only 状态互不影响。

## 6 维契约

| 维度 | 含义 |
|---|---|
| hook entry | host 的 stop / pre-tool / post-tool 入口点格式 |
| subagent | 同栈 subagent 调用形态（Task / `codex exec` / etc.） |
| transcript | 原始 trace 解析为 `NormalizedTranscript` 的路径 |
| permission | 文件/网络/shell 权限传达机制 |
| config | manifest 字段差异（plugin.json/marketplace.json schema） |
| discovery | install / enable 路径与 marketplace 配置 |
