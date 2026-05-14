# core/

host-agnostic 业务核心。后续切片（S1/S2 …）落点。

## 设计约束

- **不得 import** host SDK（如 `claude_code_sdk` / Codex internal modules）。
- **不得 import** `subprocess`；任何外部进程调用必须经 `adapters/` 包装后由 `core` 通过抽象接口调用。
- 仅消费抽象类型（`NormalizedTranscript`、`SubagentResult` 等），不识别具体 host。

## 规划落点

| 子模块 | 切片 | 内容 |
|---|---|---|
| `core/transcript/` | S1 | `NormalizedTranscript` dataclass + `to_dict()` / `from_dict()` |
| `core/decisions/subagent.py` | S2 | `invoke_subagent(role, prompt, ctx) -> SubagentResult` 抽象 |
| `core/decisions/` | S2+ | 后续 review/validate/fix 决策的纯函数化 |

## 与 adapters 的关系

`core` 定义抽象与契约；`adapters/<host>/` 提供具体实现并注入到 `core`。具体形态见 `docs/multi-harness-plugin-guideline/05-adapter-contract.md` 六维契约。
