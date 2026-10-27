# 宿主适配

`HostAdapter` 只决定 Stop 与 UserPromptSubmit 的线上输出格式。Claude Code、Codex 和 Kimi 保持各自实际支持的字段；失败、诊断及 skip 不生成 Agent 续跑指令。

外部 reviewer CLI 的 argv 投递、默认模型及结构化结果处理位于 `core/task_snapshot_review/harness_cli_adapters.py`。
