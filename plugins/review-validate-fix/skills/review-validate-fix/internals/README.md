# RVF Internals

本目录记录 RVF runtime 的维护者说明。正常 `$review-validate-fix` agent 不需要读取这里的文件。

内部文档不是脚本状态机的第二份事实源。每个文件都应指向对应脚本；若文档与脚本冲突，以脚本和测试为准。

## Files

- `stop-hook-workflow.md`：Stop hook 正常调度流程和 agent 边界。
- `runtime-contracts.md`：backend selection、env/config 注入、run ledger、Kanban dispatch 的事实源索引。
