# Reviewer 宿主机器补证与实质恢复输入

`incomplete` 的 reviewer 可以请求宿主执行已冻结的机器命令。执行器补事实，reviewer 独立完成语义审阅；完成席、原始 incomplete、失败日志和撤销事实都保留。

## 请求与归属

`requests` 中的机器请求采用以下结构。`binding` 必须完整复制角色输入的 `machine_evidence_request_binding`；命令索引从零开始。

```json
{
  "kind": "machine_evidence",
  "binding": "完整复制 request.machine_evidence_request_binding 对象",
  "registered_command_references": [
    {"command_collection": "validation_commands", "command_index": 3}
  ]
}
```

可选集合只有 `machine_evidence_commands` 和 `validation_commands`。文本、未知字段、额外 argv、其他 run 或候选树不授予执行资格。绑定覆盖任务/所属执行、精确 run 路径、候选 commit 和完整树、冻结计划及决策事实摘要。新所属执行不能覆盖原撤销路径或执行身份。

宿主将已核验原件放入请求席的 `reviewer_machine_evidence`，并使用该席的恢复摘要参与重试资格。原件只含机器事实，不包含其他席的发现或结论。相同补证不会重复派发该席。

## 隔离、复用与预算

宿主复用预登记准备命令、命令监督器和 `machine-evidence.lock`。命令派发前保存意图；监督者持有单飞锁，并在所有已登记后代退出后保存退出码、日志摘要和执行后完整树。控制器重启先对账这些原生事实，保留原 `execution.json`，只执行可证明尚未派发的缺项。仍未知的命令不重复执行。

`HOME`、Codex 配置/存储、XDG 目录和临时 IPC 均隔离；继承的 Kanban、provider、角色控制面和 Git override 环境被清除。临时 IPC 使用短的本用户私有目录以满足 Unix socket 长度限制，原始 stdout、stderr、意图和结果仍位于持久 run。

缓存必须核对完整树、冻结 argv、解释器文件、环境、依赖来源及日志摘要。`machine_context.dependency_source_paths` 可冻结实际依赖文件/目录的绝对路径，执行器计算内容摘要；缺失和符号链接等无法完整观察的依赖保留未证明状态，标签不能冒充安装内容证明。没有依赖证明时不为新请求复用历史通过；已交付的历史机器事实仍保留其原始适用范围。

缺证身份由绑定和规范化命令引用形成。实质恢复输入相同时最多三个宿主尝试，确定性失败不盲重试；瞬态错误沿用故障分类和等待时间。依赖已证明且环境相同时，重试只执行缺项。共享机器事实同样隔离控制环境并保留三次上限。

## 只读观察与 Kanban

```sh
rvf observe-task-review-recovery-evidence --run-dir /持久/精确/run
```

入口只读返回 `status` 和 `evidenceSha256`；原件损坏、缺失或绑定不可证明时返回 `unknown`，不造成功摘要。摘要覆盖冻结输入、实际机器事实、外部配置/凭据来源及有界重试到期条件；排除心跳、观察时间、controller nonce、路径重写和相同事实的重复原件。

新托管 spec 使用 `recoveryEvidenceObservationMode: semantic_observation_only`。该模式只消费生产方摘要，不混入诊断文件散列。观察不可用时保留预算并轻量重验。按每份证据保存已使用次数，即使重启或出现 A→B→A，也不重新领取 A 的预算。每次异步观察后仍核对撤销、任务/工作树身份及托管归属。

旧 spec 可使用 Kanban 的 `task command-execution register-recovery-evidence-observation --registration-file …`，追加绑定 `expectedMaterialSha256` 的只读观察器。原 spec、执行 ID、撤销和原预算保持；不同观察器不能覆盖已有登记。该登记不替换冻结的业务命令，不复活已撤销执行，也不声明旧生产方已经具备新能力。

## 机器等待与交付

缺少可执行来源、自动补证耗尽或原生结果仍未知时，按缺证身份登记一次 `machine_evidence_waiting` 通知，复用现有阶段投递账本。业务状态继续是机器等待，持久摘要记录缺项和机器可检测的恢复条件；不要求用户替机器查证。未知投递先对账，最终结果继续按精确修订去重交回。

三个旧 run 的格式转换代码及迁移前原件位于 05370 的持久交接目录，主程序没有按旧 task ID 或历史格式特判。迁移只追加元数据；事后重建的决策事实明确标注 posthoc，无法证明的历史身份、环境和采集时刻保持 unknown。两个已撤销 run 拒绝恢复。继任执行、安装、部署、同步及 push 均另行处理。
