# Main Agent Standards

主会话是 RVF 的 orchestrator。它可以读取完整 standards pack，但必须把不同子集按角色、scope 和 issue 分配给 reviewer 或 validate/fix 子代理。

## 主会话职责

- 先生成可靠的 scope-of-work / session context。
- 优先使用 session manifest 的 `owned_paths` / `owned_dirty_paths` 锚定审查范围。
- 让 reviewer 读取 `reviewer.md`，并按需要提供专项 subset。
- 合并 reviewer result artifact 时保留来源标签，但不要把来源传给 validate/fix 子代理。
- 处理 `kind: request` artifact，记录决策和结果。
- 维护 processed issue merge table 和 validate/fix grouping audit table。
- 最终 handoff 只写可确认事实，不混入背景 WIP。

## Request 处理

收到 reviewer 或 validate/fix 子代理的 request 后：

1. 校验 request artifact 格式。
2. 确认 request 没有混入 `kind: no_issues`、issue 或 verdict。
3. 判断 request 是否在当前 scope 内。
4. 判断相关命令是否需要 `scripts/command_lock.py`。
5. 决定满足、驳回、spawn 子任务或升级给用户。
6. 把 request、决策、命令、子任务和结果写入 run ledger。
7. 将最小必要上下文传回 requester。
8. 要求 requester 重试并写完成态 result artifact。

request 本身不得进入 review merge table。只有 requester 重试后的 `kind: no_issues` 或 `kind: issues` artifact 才能进入 merge。

## 子任务策略

默认策略：子代理可以请求子任务，但由主会话 spawn。

主会话 spawn 的所有 RVF 子代理都默认使用当前可用的最佳模型，并显式设置 `reasoning_effort=high`。这包括 Codex-native reviewer、Codex-only fallback reviewer、validate/fix 子代理，以及为 `RVF_*_REQUEST` 派生的受控子任务。若当前接口不支持传入 model / reasoning effort，或用户、平台、本轮运行环境明确限制，主会话必须把降级原因写入 run ledger、handoff 和最终汇总。

等待 validate/fix 子代理时，主会话应使用宽松时间窗口：首次等待用数分钟级到平台允许的较长 `wait_agent` / 等价等待，必要时重复等待；不要用短超时把测试、构建或实际修复中的正常静默误判成失败。等待期间可以准备不依赖最终 patch 的 merge table、验证计划，以及 handoff enabled 或 handoff 文件已存在时的 pending 字段，但不得替子代理提前给 verdict 或接管修复。

如果宽松等待到期仍未完成，主会话必须先发非侵入式 progress probe：询问当前阶段、已读文件、已修改或计划修改路径、正在跑的验证、阻塞点和预计下一步。probe 不能扩大 scope、改变验证包、注入 reviewer 来源或要求提前结束；probe 结果必须作为 liveness / audit metadata 写入 run ledger 和 validate/fix 分组审计表；只有 handoff enabled 或 handoff 文件已存在时，才同步写入 handoff，再决定继续等待、补上下文、处理 request 或升级。

这样能保留：

- run ledger 事件。
- 原始 scope 和 manifest。
- reviewer / validate-fix 来源。
- source-agnostic validate/fix boundary。
- command lock 和 workspace snapshot 纪律。

如果未来平台支持安全 nested subagent，只允许 `max_depth=1`，且 child 必须继承 parent 的 `pass_type`、repo、scope-of-work、manifest、packet、exclusions、no-handoff 和 no-review-loop 约束。
