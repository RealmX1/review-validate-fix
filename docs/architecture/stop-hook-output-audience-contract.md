# Hook 输出受众契约

失败、降级、skip 和诊断统一为 `developer_only`，不能驱动 Agent 再跑一回合。只有是否发起 RVF 的决策，以及必要评审未完成的接续说明，才可产生 Agent 指令。等待由脚本或宿主承担。

核心声明受众，`HostAdapter` 决定线上字段。Stop：Claude 使用 `hookSpecificOutput.additionalContext`；Codex 使用 `decision:block`；Kimi 使用 `hookSpecificOutput.permissionDecision:deny`。未知或错误不能伪造成这些出口。

UserPromptSubmit 搭用户当前回合注入上下文：Claude/Codex 使用 `additionalContext`，Kimi 使用顶层 `message`。Kimi stdout 会进入模型，因此诊断只写 stderr。

当前轻量 hook 只在绑定 Kanban task、候选内容改变且未提示过时提出一次发起决策；不铸 token、不追踪 session/hunk，不承担派发、原生分界、rewind、阶段资格或自动收尾。无 Kanban task 的 Codex GUI 持续关闭自动发起；手动命令保留。用户 Stop 与其他明确限制优先。

实现：`core/task_snapshot_review/hook_entrypoint.py`、`adapters/__init__.py` 与 `core/stop_hook_output_audience/`。测试覆盖宿主字段、一次性决策、递归抑制和故障静默。
