# Simplification Subset

RVF 只把复杂度问题作为 bug-finding 标准的一部分，不把 reviewer 变成重构 agent。

## 吸收标准

- Chesterton's Fence：改动、删除或质疑代码前先理解其存在原因。
- Preserve behavior exactly：输入、输出、副作用、错误行为和顺序必须保持。
- Follow project conventions：以本仓库现有模式为准。
- Prefer clarity over cleverness：明确表达优先于压缩行数。
- Scope to what changed：只审查当前 scope 及直接影响。
- Rule of 500：大规模重构需要自动化和单独计划。

## RVF 报告门槛

可以报告：

- 复杂度导致错误分支、遗漏边界或状态不一致。
- 新增抽象没有当前用途且已经干扰正确性或审查。
- 死代码、不可达分支、无用 compatibility shim。
- 命名误导实际行为，可能导致调用错误。

不要报告：

- 纯风格偏好。
- 单纯“可以更短”的写法。
- 与当前 scope 无关的清理。
- 无行为风险的架构品味差异。
