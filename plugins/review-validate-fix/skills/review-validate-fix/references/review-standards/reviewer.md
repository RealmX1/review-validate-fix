# Reviewer Standards

本文件是 `pass_type: review_only` 子代理使用的标准。它补充 `references/review-prompt.md`，但不改变 reviewer result artifact 契约。

## 审查目标

只报告当前 scope 内的真实问题：

- correctness bug。
- 回归。
- 未完成实现。
- 错误假设。
- 遗漏边界条件或错误路径。
- 被破坏的不变量。
- 安全问题。
- 明确性能回归或高风险 anti-pattern。
- 死代码、无用 compatibility shim、误导性命名或复杂度导致的真实 bug 风险。

不要报告：

- 风格偏好。
- 泛泛重构建议。
- 没有证据的性能优化。
- 完整 security hardening plan。
- 与本轮 scope 无关的历史 WIP。

## Standards 子集

- 默认使用本文件和 `code-review-and-quality` 的 RVF 子集。
- 复杂度相关判断读取 `simplification-subset.md`。
- 安全相关判断读取 `security-subset.md`。
- 性能相关判断读取 `performance-subset.md`。
- 如果 prompt 未提供所需 subset，可用 `$RVF_WRITE_REVIEW_RESULT standard-request --out "$RVF_REVIEW_RESULT" ...` 写 request artifact。

## Artifact 契约

完成态只能是：

- `kind: no_issues`。
- `kind: issues`，每条含 `path`、`line`、`message`、`kind`（`REAL`/`NIT`/`ELEVATE`）、`severity`（`high`/`medium`/`low`）。

非完成态只能是 `protocol-extensions.md` 中的 `kind: request` artifact。request 不得和完成态混写。最终自然语言 message 只作为日志，不是机器可读 review 状态。

## Issue 字段语义

- `kind=REAL`：真实 bug / regression / 必须修的 correctness 或安全问题。
- `kind=NIT`：风格 / 命名 / 纯 cosmetic 偏好；按上文 "审查目标" 规则 reviewer 本不应报告，但枚举值保留给 elevations downstream 使用。
- `kind=ELEVATE`：需要主会话或人类做 design 决定，不是 reviewer 可以单独定性的真假问题。
- `severity=high`：阻塞发布 / 必须本轮处理。
- `severity=medium`：应当修，但短期内可容忍。
- `severity=low`：可以推迟到后续 turn。
