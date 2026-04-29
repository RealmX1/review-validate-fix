# Main Agent Standards

主会话是 RVF 的 orchestrator。它可以读取完整 standards pack，但必须把不同子集按角色、scope 和 issue 分配给 reviewer 或 validate/fix 子代理。

## 主会话职责

- 先生成可靠的 scope-of-work / session context。
- 优先使用 session manifest 的 `owned_paths` / `owned_dirty_paths` 锚定审查范围。
- 让 reviewer 读取 `reviewer.md`，并按需要提供专项 subset。
- 合并 reviewer 输出时保留 provenance，但不要把 provenance 传给 validate/fix 子代理。
- 处理 `RVF_*_REQUEST`，记录决策和结果。
- 维护 processed issue merge table 和 validate/fix grouping audit table。
- 最终 handoff 只写可确认事实，不混入背景 WIP。

## Request 处理

收到 reviewer 或 validate/fix 子代理的 request 后：

1. 校验 request 格式。
2. 确认 request 没有混入 `NO_ISSUES`、issue list 或 verdict。
3. 判断 request 是否在当前 scope 内。
4. 判断相关命令是否需要 `scripts/command_lock.py`。
5. 决定满足、驳回、spawn 子任务或升级给用户。
6. 把 request、决策、命令、子任务和结果写入 run ledger。
7. 将最小必要上下文传回 requester。
8. 要求 requester 重试并输出完成态结果。

request 本身不得进入 review merge table。只有 requester 重试后的 `NO_ISSUES` 或编号 issue list 才能进入 merge。

## 子任务策略

默认策略：子代理可以请求子任务，但由主会话 spawn。

这样能保留：

- run ledger 事件。
- 原始 scope 和 manifest。
- reviewer / validate-fix provenance。
- source-agnostic validate/fix boundary。
- command lock 和 workspace snapshot 纪律。

如果未来平台支持安全 nested subagent，只允许 `max_depth=1`，且 child 必须继承 parent 的 `pass_type`、repo、scope-of-work、manifest、packet、exclusions、no-handoff 和 no-review-loop 约束。
