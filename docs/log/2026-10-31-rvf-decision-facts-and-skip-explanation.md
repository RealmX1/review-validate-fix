# RVF 共用决策事实与跳过说明

用户批准实施《RVF 自动采集决策事实与跳过说明》。本任务只修改 RVF 仓库，保持公共 CLI 参数；没有扩展为风险评分器、自动跳过门禁、全量历史查询或本地 base 同步。

## 实现合同与切片

能力：在允许的 Stop 发起决策、显式用户入口和正式快照中提供同一份事实结构；Agent 复用事实并独立判断风险，跳过时说明行为、幅度与依据。

1. 统计与可恢复材料：隔离 Git 测试先覆盖混合 Dirty、逐提交累计、缓存和失败，再实现按项证据与持久观察。
2. 入口与候选绑定：接入三宿主 hook、CLI 和正式快照；已有完整工作树观察直接复用，快照副本参与输入完整性校验。
3. 回归与量测：检查三宿主原生进程、内容去重、重启、背景 WIP 保全，回放固定十条转录，分别报告机器采集与历史决策墙钟。

验收采用 Python/pytest、插件源码与 manifest 检查、隔离 CLI/hook 进程及离线转录回放。主要风险是混淆累计与净改动、缓存继承错误身份、将 unknown 计为通过，以及诊断意外唤醒 Agent。

## 事实结构与使用范围

`core/task_snapshot_review/task_review_decision_facts.py` 复用起点解析器的绑定、固定基线、HEAD、完整工作树树对象与精确 run 材料观察。每项都有 `status`、`source`、`observation_id`、`valid_for`；未知项附恢复条件，不清除其他事实。

当前 Dirty 使用 `HEAD → 完整工作树树对象` 的 NUL 分隔 numstat，包含未跟踪文件，staged/unstaged 同一文件按最终净内容计一次；重命名计一文件，二进制只计文件。分组为实现／行为配置、测试与 fixtures、文档、其他；skill、hook、prompt、Agent 规则优先属于行为。用途分类不能代替语义审查。显式路径外的背景 WIP 单列；未声明任务路径时背景归属为 unknown，不擅自归给任务。

任务历史优先复用 Task Card 的 `retainedCommittedLineChangeHistory`，检查当前 HEAD、本地 base tip、必要提交与汇总一致性。失效时复用 `committedLineChangeStatisticsByCommitSha` 或本任务缓存，缺项每批最多读取 64 个精确 commit；不运行全量 log/history。补采只声称原固定任务窗口逐提交累计，排除 merge，不把净零 diff 当成零历史工作，也不猜测缺失的更早 incarnation。

材料保存在既有 runtime state 的 `task-review-decisions/<任务键>/observations/`；原子指针带原件摘要。损坏原件保留，新观察另存。相同来源、内容与事实按项复用；失败项只在自己的来源变化或下一真实用户消息时有界重验。HEAD、暂存状态和来源变化可更新事实，但一次性提示继续使用既有任务绑定、基线、规范化范围及完整内容身份；暂存/提交相同内容不会增加决策回合。

正式 run 保存候选绑定的 `decision-facts.json`，其摘要进入 `review-input.md`，文件摘要进入 `run.json`，接续前核验完整性。背景完整观察与显式候选分开绑定；起点观察后内容已变化时，重新统计冻结观察，并将旧覆盖事实保留为 unknown。

Stop 只扩展原有发起决策消息；已覆盖、重复、失败、恢复和诊断仍不产生 Agent 回合。普通 UserPromptSubmit 只静默补证；显式调用在当前用户回合提供事实。三宿主出口仍由 HostAdapter 决定。没有添加 backward compatibility 分支或旧工作流迁移。

## 验证与量测

回放脚本、固定样本清单和原始转录定位见 [调查目录](../investigations/2026-10-31-rvf-decision-facts/)。历史时间是 hook 到最终说明的墙钟，包含工具、等待和生成。新增采集量测使用隔离 Git/用户环境，不调用模型，也不执行历史命令；可替换查询项只表示新事实契约覆盖该动作，不能换算为实测 Agent 节时。

全量 `PYTEST_ADDOPTS=--maxfail=1 bash scripts/check_skill_contracts.sh`：889 项测试通过，退出码 0；源码、manifest 与三宿主声明检查通过。固定源码摘要、报告引用和 `git diff --check` 均已核对。25 个新增用例包含本方案的统计、恢复及入口边界。

最终逐例回放、量测及本轮风险判断见 [调查报告](../investigations/2026-10-31-rvf-decision-facts/README.md)。采集器首次 25.079–44.849 毫秒，缓存复用 0.616–2.174 毫秒；这些数值不代表真实 Agent 的决策节时，后者仍保留未知。本轮仅在本地任务工作树交付，没有同步或部署。
