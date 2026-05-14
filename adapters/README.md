# adapters/

Host-specific 接线层。每个 host 一个子目录（`claude_code/`、`codex/`、`manual/` 等），负责把 host 原生 hook / subagent / transcript 形态接到 [`core/`](../core/README.md) 的统一契约。

按 [`docs/multi-harness-plugin-guideline/05-adapter-contract.md`](../docs/multi-harness-plugin-guideline/05-adapter-contract.md) 6 维契约组织：

- `hooks/`：host 的 hook entry 脚本；第一件事读 stdin（参考反模式 ⑤）。
- `transcript.py`：把 host 的 transcript（`.jsonl` / session log）解析为 `core.transcript.NormalizedTranscript`。
- `subagent.py`：把 host 的 subagent 调用（Task / `codex exec` / API）封装为 `core.decisions.SubagentResult`。
- `install/` 或 `settings/`：host 安装期需要写入的本机配置（如 Codex `~/.codex/hooks.json` 注册脚本）。

约束：

- 只有 adapter 可以 import host SDK 或调用 host 进程。
- adapter 不持有业务逻辑；任何决策必须委托给 `core`。
- 同 plugin id 跨 host 一致：`name="review-validate-fix"`，不允许 host 上 rename（参考反模式 ②）。

当前为 S0 占位骨架；实际 adapter 在 S2（Codex subagent 迁移）、S3（Claude Code 实装）等切片中陆续落入。
