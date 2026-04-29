# Validate Then Fix Prompt

默认传给 validate/fix 子代理。主会话本地执行 validate/fix 只允许用于 `SKILL.md` 定义的窄例外，并且必须在最终汇总和 handoff 中写明原因。

```markdown
pass_type: validate_fix

请用中文回复所有输出（verdict tag `REAL` / `FALSE_POSITIVE` / `ELEVATE`、文件路径、代码除外）。

给定一个 review issue：

1. 先读相关文件，验证 flag 的问题在当前代码里是否真的是一个问题。
2. 是真问题且你能独立修好：实施最小化修复，返回 `REAL`。
3. 不是真问题：不要改任何文件，返回 `FALSE_POSITIVE`，并简短说明为何不成立。
4. 是真问题但你不应该或不能独立修：不要改任何文件，返回 `ELEVATE`。例如需要架构决策、存在多种等价修复需要开发者拍板、涉及权限或 scope 外改动、原始需求不明确。

只处理主会话分配给你的 issue。不要重新执行 double review，不要主动寻找未分配问题，不要生成 handoff.md，也不要输出 reviewer provenance。`pass_type: validate_fix` 不是 full mode；即使上下文提到 `$review-validate-fix` 或 research checkpoint，也只返回 verdict 和必要的最小修复说明，不输出 `RVF_HANDOFF_FILE`。

返回结构化 verdict：

`[REAL | FALSE_POSITIVE | ELEVATE] <路径:行号> — <你做了什么 / 为何驳回 / 为何升级>`
```

## 子代理分配

- 在默认 `full` 流程中，只要 review merge 后存在可解析 issue list，主会话必须启动至少一个 `pass_type: validate_fix` 子代理处理验证包。
- 不得因为问题看起来简单、修复明显、reviewer 已给出修复方向或主会话已经理解问题，就跳过 validate/fix 子代理。
- 只有当前运行环境确实没有可用子代理接口、用户本轮明确要求主会话本地执行 validate/fix，或某个 validate/fix 子代理已返回 `REAL` 且只剩主会话可安全完成的机械收尾时，才允许本地执行；“为了省时间”或“问题很小”不是例外。
- 不强制一条 issue 一个子代理。
- 如果多条 issue 共享同一根因、同一文件区域、同一测试路径或同一决策前提，应合并为一个验证包交给同一个 validate/fix 子代理。
- 验证包 prompt 必须说明：
  - 包含哪些 issue。
  - 为什么这些 issue 需要合并验证。
  - 输出仍要逐项给出 `REAL` / `FALSE_POSITIVE` / `ELEVATE` verdict。
- 验证包必须 source-agnostic：不要告诉子代理该 issue 来自 Codex、alternative reviewer、两个 reviewer 共同发现，或某个 reviewer 的原始编号。
- 子代理只接收 canonical issue、相关 path/line、必要代码上下文、复现线索和 validate/fix 指令。

## ELEVATE 详情

每个 `ELEVATE` verdict 后必须追加：

````markdown
```elevation-detail
title: <短标题>
stuck_reason: <1-2 句说明为什么需要用户决策>
issue_restate: <1-2 句复述原始问题>
options:
  - A: <方案 + 权衡>
  - B: <方案 + 权衡>
  - C: <可选，第三个方案 + 权衡>
```
````

如果确实给不出候选方案，仍输出 `options:`，并写明 `候选方向缺失，请手动提供`。
