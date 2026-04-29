# Validate/Fix Standards

本文件是 `pass_type: validate_fix` 子代理使用的标准。它补充 `references/validate-then-fix-prompt.md`，不改变 verdict 契约。

## 验证顺序

1. 读取 assigned canonical issue。
2. 读取相关文件和必要上下文。
3. 按相关 standards subset 验证 issue 是否真实。
4. 不真实则返回 `FALSE_POSITIVE`，不改文件。
5. 真实且可独立最小修复则改最少文件并返回 `REAL`。
6. 真实但需要用户决策、跨 scope 改动、多种等价方案或缺少测量则返回 `ELEVATE`。

## 专项规则

- simplification：必须保持行为，不得为了“更简洁”修改测试期望。
- security：不得通过关闭安全控制解决问题；不能只输出泛化 hardening。
- performance：必须说明具体 bottleneck、anti-pattern 或测量需求；没有证据时请求测量或 `ELEVATE`。

## Request

如果只缺上下文、标准、命令锁、测量或专项调查，可先输出 `RVF_*_REQUEST`。request 是非完成态，不得和 `REAL` / `FALSE_POSITIVE` / `ELEVATE` 混写。
