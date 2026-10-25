# 用户委派 elevated 问题后的 Land 与 base 收尾

任务：`review-validate-fix/1d446`；消费者：`my-ai-setup/7f94e`。来源是 `cline-kanban/7494b`，原问题修复归属 `0030c`。本次未复制 `abc87`、`53034` 或其他任务的 WIP，未修改来源真实 run，未重复来源的 base 同步。

## 交付行为

`rvf elevated-issue-delegation inspect|record|verify|resume` 使用精确 repo/run 和用户原生证据。回执绑定 issue、最新 elevated attempt、冻结 handoff/scope、候选与目标稳定身份，内容寻址、原子持久化；撤销与重新委派追加账本。用户批准前文委派提案后，可以由建卡回执确定目标。角色标签、Agent 建卡和转述本身均不能证明授权。

首次请求持久绑定候选；缺证据时记录指纹、原因、恢复条件和尝试历史，同输入共享三次自动补证预算，恢复状态更新由独占锁串行化。已授权 Land 自动恢复待补证委派，只读 check-evidence 不写状态。既有提交和 handback 的重复恢复使用实际 Git 与结果对账。

Land 分开核算真实修复和委派。示例保持共 2、fixed 1、elevated 1、user_delegated 1、blocking_elevated 0；原问题未解决，目标无需完成。v3 evidence 声明委派能力与结论，携带回执和准确计数；普通候选保持有效 v1/v2，v3 范围扩展完整执行原扩展判据。独立消费者拒绝身份、摘要、候选、最新失败 attempt、撤销与降级伪装等不合法证据。委派不增加任何执行权限。

## 验证与持久证据

持久目录：`/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/review-validate-fix/1d446/`。完整命令、版本、摘要与最终状态汇总在该目录的最终交付报告；失败尝试原样保留。

已实际运行：

- 既有完成事实与候选证据回归 74 项。
- 新委派全量 40 项通过，覆盖真实授权、Agent 假授权、否定、Stop、部分委派、撤销、候选/摘要/身份变化、较新失败、原生附件、查询恢复、预算耗尽与并发恢复，包含跨 run 的同名 issue 授权移用拒绝。最终精确 run token 补强再通过 4 项（含新增前缀碰撞拒绝），用例合计 41 项。
- CLI shim、Land 透传与上下文回归 4 项，两个公开 Skill 的正式 validator 通过。
- 必须显式指定消费者的双仓脚本运行 9 场景：v1/v2/v3、v3 扩展，以及计数、目标、回执、补丁篡改与降级拒绝，无 skip。
- 消费者固定源码的完整 v3 对等测试 88 项通过，无 skip。三条真实协议链覆盖无扩展、有扩展、简短批准：prepare/start/stop/apply、委派、Land、独立 inspector、真实隔离 handback；首次 `LANDED_AND_VERIFIED`，重跑 `ALREADY_PRESENT`。旧消费者均明确拒绝 v3。后续追加身份门禁的最终消费者提交与复验结果由持久报告记录。
- 来源 `7494b` 的 18 份原始材料隔离重演，原件摘要不变；原 handoff SHA256 为 `bb4859c174f0e4265ec446830eff81f07eec092b7e627c70ce9edf77f313148a`。加入真实原生用户委派后 Land 为 `nothing_to_commit`，新消费者为 `VERIFIED`，继续显示未解决 1 并指向 `0030c`。

完整插件契约检查使用仓库正式入口运行并通过，退出码 0，timing report 与定向回归分开保留；最后追加的身份与恢复门禁另经上述最终 40 项验证。一次辅助回归误用 pytest 调用仓库自带脚本，产生缺少 `root` fixture 的入口错误；随后按注册入口运行相关 CLI 用例并通过，原错误日志保留。

本次未加入生产兼容旁路或临时全绿伪装，无需遗留兼容 shim。合法 v1/v2 是明确要求保留的协议，不是临时绕过。未安装、部署或执行真实来源 base 同步；双仓验收使用隔离 Git、HOME、原生会话夹具和本地响应。

协议与用法见 [委派契约](../architecture/user-delegated-elevated-issue-disposition-contract.md) 及 [公开命令说明](../../plugins/review-validate-fix/skills/review-validate-fix/references/user-delegated-elevated-issue-disposition.md)。
