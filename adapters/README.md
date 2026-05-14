# adapters/

host-specific 实现。每个子目录承担一个 harness 的 6 维契约实现（见 `docs/multi-harness-plugin-guideline/05-adapter-contract.md`）。

## 子目录

| 目录 | host | 状态 |
|---|---|---|
| `adapters/claude_code/` | Claude Code（marketplace + plugin） | S3 收尾决策中（默认形态：transcript + subagent stub；hooks 留在 `plugins/review-validate-fix/hooks/` nested 位置） |
| `adapters/codex/` | Codex（marketplace + plugin） | S1/S2 落点；Stop hook chain 仍在 `plugins/review-validate-fix/skills/.../scripts/` 内 |

OpenCode / Cursor / Hermes / OpenClaw 在本仓库为 Reference-only，不在 `adapters/` 下落实装。

## 6 维契约

| 维度 | 含义 |
|---|---|
| hook entry | host 的 stop / pre-tool / post-tool 入口点格式 |
| subagent | 同栈 subagent 调用形态（Task / `codex exec` / etc.） |
| transcript | 原始 trace 解析为 `NormalizedTranscript` 的路径 |
| permission | 文件/网络/shell 权限传达机制 |
| config | manifest 字段差异（plugin.json/marketplace.json schema） |
| discovery | install / enable 路径与 marketplace 配置 |
