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

## 最终授权与资格写入补强

最终生产者实现提交为 `7d0f3e2ae2c74e1164e1488905eb1f426c8afb12`，包含 `eefb015` 的共享资格锁及动作级授权修复；消费者固定为 `6e3c6c22a917e22e1b79ae65edd3e5fa03fa81bb`。消费者 Task `7f94e` 已接续到该提交；其后续本地 main 同步由该 Task 按用户另行授权独立完成，本 Task 未执行真实 base 同步、push 或部署。

撤销按对应动作及完整 issue 标识符分句核验：否定撤销拒绝，肯定撤销不会被另一条款的委派否定误伤。每条历史事件都核验真实授权，后续合法 delegate 不会洗白较早的非法 revoke。run、issue 与目标 task 的后缀近似身份拒绝；英文句末句号按标点处理。

canonical writer 与消费者 CAS 共用 `artifacts/elevated-issue-delegation/mutation.lock`。实际覆盖 prepare/start/stop/apply、awaiting marker、issue upsert、merge、reviewer 结果及其轮转/plan、scope 创建/定稿、validation index、handoff、summary/finalize 与 evidence 共 19 类入口。Land 在锁外完成验证重放，持锁后重新核验到提交和证据发布结束；锁忙不夺锁，释放后继续，恢复预算也会按锁可用性变化重开。

最终固定版本已实际通过：

- 完整 `scripts/check_plugin_contracts.py --verbose`：66 个步骤全部通过，退出码 0。
- 双仓 v1/v2/v3 联合回归：235 项通过，无 skip，包含三条真实完整生产者/消费者生命周期及幂等 handback。
- 固定完整生产者源码的 writer 锁与恢复：22 项通过，逐项保存 19 类真实 writer 的精确 argv、stdout/stderr、持锁阻断与释放后接续结果。
- 双仓独立协议 9 场景，以及两种真实生产者 prepare/消费者 Git CAS 竞态全部通过。复验前出现新 attempt 时 base 不推进；实际 Git 落地期间 prepare 等待，落地后接续并使旧 v3 失效。
- `7494b` 的第 5 次独立重演通过；18 份原件摘要未变。Land 为 `nothing_to_commit`，消费者为 `VERIFIED`，仍显示共 2、fixed 1、elevated 1、已委派 1、阻断 0，跟进 `0030c`。

完整检查曾暴露独立 `rvf_run_finalize` 进程未先初始化 pyroot 的回归；`7d0f3e2` 修复后，35 项 bridge 和 44 项 finalize/bridge 组合通过，最终完整契约也覆盖并通过原失败场景。另一处 stop 参数报错顺序回归与测试准备阶段误解析空 stdout 均已修正；原失败日志保留。既有 SQLite 迁移用例缺少 `leases` 表的问题已在未改动的 `0671383` 独立复现，本次沿用正式 gate 的既有排除，不添加无关兼容实现。

原生交接消息未确认接收时，保留失败及 Review 列 start 拒绝回执，改用正常 mutation reservation 覆盖的受控 fast-forward 接续已验证消费者提交。只推进该 Task 的干净 detached HEAD，接续当时 main 不变；没有伪造原生接收或把旧 RVF 结果提升为覆盖新候选。最终 manifest、完整源码 archive、原件摘要、失败快照及对等报告均持久保存在本 Task 交接目录。没有遗留临时生产兼容旁路。


## 独立 RVF fe6e0ac4 的修复与验收边界

本轮精确 run 为 `rvf-review-start-fe6e0ac4de3db68e`，开审 HEAD 为 `590385afab2fda84af1468f732145318055c3df3`，正式覆盖 25/25 个候选文件。canonical plan 已完成，两条有效腿的 10 项发现合并为 4 条问题：动作级真实授权、旧请求恢复、历史与当前证据判据、资格锁超时日志降级。全部经独立 validate/fix attempt 处理；历史前缀与原生顺序 helper 的最后接线另走正式 attempt，保留先前成功结果与红测。

新增四份原生链/真实 flock 回归已纳入正式契约检查。最终主工作区验收及固定源码清单写入持久交接根下的 `rvf-fe6e0ac4-*` 产物，本文件不预报这些命令通过。RVF 修复在 handoff 阶段仍待后续已授权 Land 提交；本 Task 不部署、不 push、不真实同步 base。

独立复核固定 `6e3c6c2` 消费者的动作授权函数，10 个边界案例有 9 项不符预期，报告为交接根的 `rvf-fe6e0ac4-consumer-independent-authorization-probe.json`。这是纯原生授权函数核验，不冒充完整 handback 结果。既有双仓成功报告保留当时版本与覆盖；这批新边界修复并复验前，最新端到端交付仍未完成。消费者归属既有 `7f94e`，本轮未发送跨 Task message 或修改其真实 run/main。
