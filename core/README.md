# core/

Host-agnostic 业务核心。任何不依赖 host SDK 的逻辑放这里：reviewer / validator / fixer 流程编排、`NormalizedTranscript` 与 `SubagentResult` 等公共数据结构、决策表、config schema、共享 utility。

约束（来自 [`docs/multi-harness-plugin-guideline/05-adapter-contract.md`](../docs/multi-harness-plugin-guideline/05-adapter-contract.md) 与 [`04-anti-patterns.md`](../docs/multi-harness-plugin-guideline/04-anti-patterns.md) ④）：

- 不得 import `claude_code_sdk`、`codex_sdk` 或任何 host 私有库；只允许标准库与共享 utility。
- 不得出现 `if host == "codex"` / `if host == "claude_code"` 形态的分支；host 差异由 [`adapters/`](../adapters/README.md) 隔离。
- 公共 API 通过 dataclass / pydantic 模型暴露，避免向上游泄露 host-specific 字典结构。
- 单元测试只依赖 fixture / mock adapter，不需要真实 host runtime。

当前为 S0 占位骨架；实际模块在 S1（`core/transcript/`）、S2（`core/decisions/`）等切片中陆续落入。
