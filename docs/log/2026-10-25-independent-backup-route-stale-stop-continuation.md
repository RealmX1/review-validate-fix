# 备用路线完成后的过期 Stop 续跑修复

本次交付源码、契约文档与自动回归证据。原 run 的历史消费与当前候选覆盖分别判断：提交改变 HEAD 不再复活旧 run，新内容仍进入现有候选判断。没有刷新本机插件；真实宿主运行时效果仍需后续部署验证。

## 需求与诊断来源

- 当前任务：`review-validate-fix/e5080`。持久初始需求为“backup route 会错误地触发 blocked-by-hook”，附 task `cline-kanban/1dfb2` 的 `kanban_followup_in_progress_reengage` 原始轨迹。
- 本次实施依据：用户明确要求执行上一 Agent 提供的《修复 backup route 的过期续跑阻塞》计划；计划范围为当前仓库源码、文档与自动验证。此前 Agent 的诊断内容作为待核验来源，未扩展为部署／同步授权。
- 精确原 run：`/Users/bominzhang/.rvf/user-local-data/run-logs/runs/rvf-review-start-da3bef86162f5dd0`，没有使用 `latest`。
- 原 operation：`task-1dfb2-t4-original-rvf-da3bef86162f5dd0-backup-continuation`；独立 worktree Git 目录为 `/Users/bominzhang/Documents/GitHub/cline-kanban/.git/worktrees/cline-kanban369`。
- 只读核对的 `completed-operations` 与 `consumed-original-runs` 两份原记录 SHA256 均为 `425cd3b726fe4e1a681bbe96cae60b648e96db8dd5152e82315eab548ca09395`。观测时历史完成 HEAD 为 `3407a443edb8b4141735dc408e1e5e80bde31a5e`，task 当前 HEAD 为 `dcc7dcbde14e8488d0073b6c0d00b0b4d8fa7f10`。
- 新查询对原生产记录的只读结果为 `consumed`／`not_covered`，分别描述历史消费与观测时的新候选。未清理该 task 的凭证、未改写其失败或完成记录。
- 已通过 `work-unit assert-reference-relation` 登记对 `cline-kanban/1dfb2` 的 `diagnoses_failure_of` 关系，附精确 run 路径。

## 修复内容

1. 辅助器统一从真实 worktree 根读取候选路径、HEAD、index 和原始字节，根目录／子目录／符号链接入口一致，独立 worktree 的 Git 控制目录继续隔离。
2. 分开只读的历史消费查询与当前覆盖查询。消费索引缺失或损坏时沿同 operation 的持久完成原件补证；写入中断后恢复消费及归档，损坏索引原字节保留。
3. Stop 在 nudge、过期续期与 pending 重投之前核验消费。只清同 run 的凭证；删除失败不再催旧 run，下次 Stop 重试；另一轮 run 保留。dispatcher 将“候选已覆盖、接管已归档”的分支交核心对账，避免早退遮住另一轮续跑。
4. 补证读取有界，游标、缺口和重验条件持久化；跨进程接续，跨页保留未解决缺口。仍为 `unknown` 时只暂停依赖它的续跑动作，诊断保持 `developer_only`。HostAdapter 通道、正常 finalize、token 和 receipt 契约不变。
5. 更新共享备用指引并渲染三个仓内入口，将备用路线回归加入 `check_skill_contracts.sh`。本次没有引入旧版本兼容分支。

## 实施阶段自动验证证据

持久证据目录：`/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/task-e5080-backup-stop-reconciliation/`。每次验证的 JSON 保存命令、退出码、耗时及日志 SHA256，早先失败日志保留。

- 基线 `806633889d00bc6a38b0d31a4e590844645f047a` 重放：两条新回归准确失败，分别是子目录指纹不一致与提交后旧 run 再次 `reengage`。见 `baseline-regression-red.json`／`.log`。
- 实施阶段备用 helper、Stop 备用对账、三类宿主出口、正常 finalize 与正常完成凭证对账：**182 项通过**，见 `backup-consumption-and-finalize-attempt-2.json`／`.log`。
- 既有 Stop follow-up 与 dispatcher 定向检查：98 项通过；另两项使用仓库自有 runner 的 `tmp` 参数，直接 pytest 发生 fixture 加载错误，按原 runner 的调用方式补跑后 2 项通过。见 `stop-followup-and-dispatcher.json` 与 `stop-followup-native-runner-recovery.json`。合计 **282 项不同的定向用例通过**。
- 分页恢复额外以新 Python 解释器验证持久游标接续；记录损坏、完成写入中断、删除失败、双 run 并存、真实 dispatcher 子进程均有回归覆盖。
- 指引生成一致性检查、shell 语法检查与 `git diff --check` 通过。
- 完整 `bash scripts/check_skill_contracts.sh` **通过**，包含真实 RVF 状态根无测试残留检查；见 `full-skill-contracts-isolated-attempt-2.json`／`.log`。验证在同一 Git common-dir 派生的隔离 worktree 执行，基线和 14 个交付文件逐一核对一致。

首次完整检查在既有安装测试中沿当前 task 的 `state` 符号链接复制约 1.1G 历史运行材料，已主动终止该测试进程并保留退出码 143、堆栈与环境观察。未改安装逻辑或原历史材料；将同一候选完整重放到不含该忽略链接的隔离 worktree 后，整套契约通过。见 `full-contract-initial-attempt-environment-observation.json` 与 `isolated-contract-validation-source-equivalence.json`。隔离 worktree 验证后已清理，完整 `candidate-source.patch` 保存在上述持久证据目录，可在同一基线重放。

## 独立 RVF 与补充修复

随后主 Agent 接受自动发起决策点，以 strict 档完成 `rvf-review-start-a0679d5bb51b58ec`。正式审查覆盖 14/14 文件、41 个单元；canonical 终态为 completed，有效 2/2。7 条发现合并为 5 条 canonical issue，全部通过独立 validate_fix attempt 验证、修复并正式合回：

- RVF-001：补证缺口按精确 run 归属处理，排除已知无关 operation 的损坏，归档的同 run 原件缺失保持 unknown。
- RVF-002：分页未查尽时以 developer_only 等待，不重新发起；覆盖命中优先复核，候选变化使旧分页／缓存资格失效。
- RVF-003：备用覆盖转交保留 dispatcher 既有 plan、suppress、goal、handoff、analyze 守卫。
- RVF-004：pending 发布、身份核验清理和盖戳共用写锁，关闭最后一次读取与删除之间的替换竞态。
- RVF-005：三条入口先核验／回收精确 tracker lease，失败保留凭证自动重试；普通范围扩展 `core/session_scope_allocation/reviewable_unit_diff_tracker.py`，增加原生账本只读补证，避免错放其他 run 的 lease。

RVF-005 首个 attempt 因测试 imports 与先前补丁重叠，正式 apply 记录为 merge_conflict，未手工搬到主工作区。新 attempt `rvf-attempt-rvf-review-start-a0679d5-RVF-005-e33e2b51` 以当前合并候选重新生成补丁、验证并应用；旧冲突历史保留。`rvf merge check` 已通过全部 5 条 fix-issue。

以上实施阶段测试对应 RVF 前候选，不冒充补充修复后的最终验证。RVF 最终验证按 `handoff-validation-001` 起的顺序经 `rvf validate` 登记；实际 argv、cwd、内容／环境绑定和结果以本 run 的 `artifacts/handoff-validation-commands.json`、验证索引及 `artifacts/handoff.md` 为准，缺项不推断通过。run 持久目录：`/Users/bominzhang/.rvf/user-local-data/run-logs/runs/rvf-review-start-a0679d5bb51b58ec/`。

## 交付边界

正常 RVF handoff 与备用交付保持分别记录；备用历史消费不签发正常 receipt。原生产 run／task 的核验只读。源码验证不代表本机 `5371b6eb30d0` 插件已刷新，也不代表已同步到 main、部署或完成真实宿主验证。
