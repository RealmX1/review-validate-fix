# 新版 Kanban 防漏审：方案 B 实施合同

状态：**PLAN-READY，用户已选择 B；实施起点 `05249a7f5f536dec5098f6cd975e26ea5bb63569`。**

用户本轮原话：`take option b`。选择指向本任务持久计划
`post-overhaul-reanalysis-20261027/adaptation-plan-awaiting-user-confirmation.md` 中的 B。
此前的提交授权继续有效；本阶段完成新版实现、双审、完整树验证和本地提交。
最终 base-branch-sync、push 与部署不在本阶段。

## 实现合同

- 能力：Kanban 任务的可靠固定起点可直接进入新版显式快照；已落入 base 的未审任务提交仍可产生一次发起决策。
- 范围：Kanban 适配层读取任务创建、worktree、provenance 与 Git 证据；记录明确的实现起点和范围；CLI 可选自动起点；clean Stop 决策、去重、unknown 恢复；归档四份失效旧执行/测试文件。
- 非目标：不恢复 token、tracker、lease、session/hunk ownership、投影、manifest、rewind、独立 land/reopen；不迁移旧审查状态；不自动启动评审或部署；不修改其他任务工作树。
- 验收：原事故窗口自动得到 10 路径；task→base 后保留，纯 base→task 不认领；冻结后 base 前移不改变原 run；起点和归属未知时可持久恢复、重启接续、有界重验；范围扩大不跳过新增路径；净 diff 为零不提醒；三种宿主只为真实决策输出 Agent 指令；背景 WIP 保全。
- 预计接口：新增 Kanban 起点适配器；CLI 的 `--base-from-kanban-task` 与 `--base-ref` 二选一；明确记录实现起点的命令；显式选择已完成 run 的增量起点；快照保存输入证据；hook 复用起点解析。
- 风险：同 ID/路径重建、来源延迟、历史改写、范围扩大和已完成评审的误用。以绑定身份、唯一 patch-id、保留原起点及明确增量选择控制。
- 验证：两组新隔离行为测试、既有快照/hook/打包测试、全量 `pytest -q tests`、`bash scripts/check_skill_contracts.sh`；正式双 reviewer 与匿名 fixer 后复跑完整树验证。

## 纵向切片

1. 起点解析及显式 CLI：先验证 task 已进 main 的自动发现、scope 和来源冻结，再实现适配器及 CLI。
2. clean committed 决策：先覆盖三宿主、跨会话去重、纯 base、revert、缺证恢复，再接入 hook。
3. 恢复与后续增量：先覆盖身份替换、损坏状态、真实 prompt、唯一/歧义 patch-id、范围扩大与精确 run，再补齐恢复。
4. 整体验证与交付：保留原事故隔离复现，验证打包、WIP、双审、完整 tested_tree，普通 Git 本地提交。

每个切片记录真实 RED/GREEN。当前 local main 仍为 `b1fd6b4b50be23669ecb2f35c404c6ed50103bb0`，本轮不再次吸收。
