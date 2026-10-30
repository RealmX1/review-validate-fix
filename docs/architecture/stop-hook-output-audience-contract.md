# Hook 输出受众契约

失败、降级、skip 和诊断统一为 `developer_only`，不能驱动 Agent 再跑一回合。只有是否发起 RVF 的决策，以及必要评审未完成的接续说明，才可产生 Agent 指令。等待由脚本或宿主承担。

核心声明受众，`HostAdapter` 决定线上字段。Stop：Claude 使用 `hookSpecificOutput.additionalContext`；Codex 使用 `decision:block`；Kimi 使用 `hookSpecificOutput.permissionDecision:deny`。未知或错误不能伪造成这些出口。

UserPromptSubmit 搭用户当前回合注入上下文：Claude/Codex 使用 `additionalContext`，Kimi 使用顶层 `message`。Kimi stdout 会进入模型，因此诊断只写 stderr。

Kimi 原生 UserPromptSubmit 的 `prompt` 是内容块数组；HostAdapter 只提取文本块，显式入口与真实用户回合补证共用该文本，附件和异常块不构造指令。

Kimi hook 未携带消息 ID 时，HostAdapter 从 `KIMI_CODE_HOME/server/events/<session_id>.jsonl`（默认 `~/.kimi-code`）读取同 session、main agent、当前文本匹配的最新运行中 `prompt.submitted.promptId`，只用于补证去重。同文本的新消息可以重验，重复 hook、assistant 输出与 Stop 不增加机会；原生记录缺失、不匹配或不可读时只检查来源变化，不用随机 ID 或文本摘要伪造新回合。显式入口不依赖该记录。

当前轻量 hook 只在绑定 Kanban task、候选内容改变且未提示过时提出一次发起决策；不铸 token、不追踪 session/hunk，不承担派发、原生分界、rewind、阶段资格或自动收尾。无 Kanban task 的 Codex GUI 持续关闭自动发起；手动命令保留。用户 Stop 与其他明确限制优先。

有可靠固定起点和任务提交证据时，干净已提交工作也参与该决策，进入 base 不使其消失。同任务同观察跨 session 去重；关联的新版 run 已完成且当前范围内容相同则不重复提示。来源不足、身份不符、损坏和恢复记录均为 developer_only，只保存诊断与有界重验条件。真实 UserPromptSubmit 可触发补证，不为补证另开 Agent 回合，也不推进固定起点。

实现：`core/task_snapshot_review/hook_entrypoint.py`、`adapters/__init__.py` 与 `core/stop_hook_output_audience/`。测试覆盖宿主字段、一次性决策、递归抑制和故障静默。
