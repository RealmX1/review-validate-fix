# RVF Review Standards Pack

本目录是 Review-Validate-Fix 内部使用的定制 review standards pack。它提炼自 `agent-skills` 的 Review 类 skills，但已按 RVF 的 session-scoped scope、strict output contract、validate/fix 和 handoff 约束改写。

不要把这里的文档当作通用 review report 模板。它只用于帮助主会话、reviewer 子代理和 validate/fix 子代理判断哪些问题值得进入 RVF 流程。

## 文件职责

- `main-agent.md`：主会话如何选择标准、处理 request、spawn 子任务和维护审计。
- `reviewer.md`：reviewer 子代理的 bug-finding 标准和非完成态 request 协议。
- `validate-fix.md`：validate/fix 子代理如何用标准验证、最小修复或升级。
- `simplification-subset.md`：复杂度 / 可维护性问题的 RVF 报告门槛。
- `security-subset.md`：安全问题的 RVF 报告门槛。
- `performance-subset.md`：性能问题的 RVF 报告门槛。
- `protocol-extensions.md`：`RVF_*_REQUEST` 非完成态协议。

## 关键不变量

- reviewer 完成态仍只能是精确 `NO_ISSUES` 或编号 `路径:行号` issue list。
- validate/fix verdict 仍只能是 `REAL`、`FALSE_POSITIVE` 或 `ELEVATE`。
- request contract 是非完成态，不得和完成态混写。
- 子任务默认由主会话 spawn，以保留 run ledger、scope 和来源标签。
- nested subagent 只有在平台能继承 run id、scope、manifest、packet 和限制时才可启用，且最多一层。
