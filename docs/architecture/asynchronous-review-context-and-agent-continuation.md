# RVF 异步审阅、事实补充与简短续接

本合同对应任务 `review-validate-fix/a278b`，依据 2026-10-31 用户明确要求实现的完整方案。此前调查阶段的只读限制已经被本次实施授权替代；来源 run 的持久 Stop 继续有效。本任务不恢复历史取消 run；独立评审、同步、推送与部署分别按有效授权处理。

## 实施合同

- 能力：匿名 reviewer findings 同时供主会话 sanity check 和单个逻辑 Validate-Fixer 使用；主会话独有事实独立登记、验真、去重，迟到补充自动补验。
- 范围：RVF 冻结 run、角色请求及验真结果、审阅摘要、CLI、Kanban 所属执行的通用分阶段通知与主会话打断策略。
- 边界：不恢复旧自动 closeout、Land、rewind 编排；不处理网络故障根因，不改变 reviewer 匿名性或任务权限，不启动被撤销工作。
- 验收：主会话忙碌和 parked 不阻塞 fixer；补充覆盖影响最终资格；默认通知和摘要不加载完整验证脚本；前台打断保留后台，明确 Stop 阻止重启、补丁应用和投递；接受未知先对账，结果修订与接受历史追加保存。
- 验证：隔离无模型 CLI、Python 行为测试、Kanban guarded 测试、类型检查与隔离构建；真实部署实例另登记待验断言。

## 两个并行消费者

reviewer 全部完成后，程序保存不可变 `reviewer-findings.json`，含精确 run 路径、原候选 commit、匿名发现及规范化 JSON 的 SHA-256。`reviewer-findings-summary.md` 提供事实摘要及完整补充命令。该发布不等待主会话接受，Validate-Fixer 随即继续。

```sh
rvf submit-review-context --run-dir <精确run> --supplement-id <稳定唯一ID> \
  --candidate-commit <冻结候选> --findings-sha256 <匿名发现摘要> \
  --context-file <持久事实文件> [--finding-id <匿名发现ID>]
```

登记只取得 `main-agent-context-supplements.lock` 和撤销锁，不争用 controller/workflow 锁，不写 controller 的 `run.json`。每个 ID 绑定完整材料；同 ID 同材料复用，异材料拒绝。原文按 SHA-256 保存，读入时重新验证内容、run、候选及 findings 身份。登记不改变冻结需求、范围或修复授权。

fixer 请求携带 `main_agent_context_supplements`。每份补充必须有 `context_supplement_responses`，包含 ID、`confirmed|refuted|not_applicable|unresolved` 及独立证据。只有分配给该尝试、身份匹配且已明确处置的补充计入覆盖。主会话主张可以被反证，未解决补充不能授予正常最终资格。

迟到补充使已有最终通知失去后续投递资格，但不撤回既有原生接受事实。补验复用同一个逻辑 fixer、此前真实修复树和已完成 reviewer；新补充集合有独立的有界语义预算，重启和配置变化不重置。最终验证期间新登记的补充在发布边界再次核对，先返回机器待证，再恢复原 run。

## 摘要与事件合同

`agent-review-summary.md` 由程序直接生成，包含逐项处置、修复依据、累计补丁指针、补充回应、完整被测树、验证结论和需判断事项。完整 argv、环境、日志和范围机器材料仍在原件，摘要只给必要指针，不要求主会话随后整份读取 `business-result.json`。

发现说明、处置依据和补充回应使用有界摘录，替换预登记验证命令中的完整内联脚本、多行参数及长参数；完整角色原件保留。即使 fixer 把完整验证脚本放进 `reason`，主会话默认读取摘要也不会重新整块注入它。

每个有意义的结果修订保存在 `agent-review-result-revisions/<sha256>/`，旧摘要与结果不覆盖。默认 CLI 返回省略携带完整验证对象的 `closeout`，保留业务状态、简短事件与摘要入口；完整业务结果仍持久保存。

主会话通过工具已消费结果时，使用摘要中的 `acknowledge-review-notice` 命令登记精确 ID 的持久消费回执。最终消费再次核对当前结果和全部登记补充；旧消费原件不覆盖，也不能充当新修订的接受。后台清单撤下已消费事件，恢复原 run 不重新发布同一通知。

业务结果的 `agentContinuationNotice` 含 `noticeId`、`stage`、`text`。`agent-continuation-notices.json` 的 `notices` 列表声明候选事件，阶段为 `reviewer_findings_available`、`final_agent_review`。最终通知还声明 `requiresCurrentResultNoticeMatch: true`，只有当前业务原件里的同一事件仍完全匹配才可投递；主会话补充先写出待证状态后，即使进程尚未撤回清单就崩溃，旧最终通知也已经失效。反向窗口中，清单先出现新修订而业务原件尚未发布时，新通知也暂不投递。每个修订使用稳定 ID，同一 ID 的正文不能变化。未解决项、升级处置及验证失败在通知中明确呈现。

RVF 的 Kanban specification 声明 `continuationNoticesFile`、`ownerSessionInterruptPolicy: preserve_execution`，并用 `producerExecutionRevocationFile` 绑定精确 run 的 `execution-revocation.json`。生产方先保存撤销原件后即使崩溃，Kanban 也会独立收回派发和投递资格，接续有界取消补证；不依赖生产方成功清空通知清单。Kanban 只负责通用通知登记、原生投递、接受对账及恢复，不解释 RVF 阶段资格。

`resultDelivery` 保存当前投递；更换事件时把旧记录追加到 `resultDeliveryHistory`，保留原文、尝试与接受事实。`submission_unknown` 先查原生接受，不能直接重新发送，也不能被新修订覆盖。实际写入前重新验证归属、撤销及通知仍有效。同阶段同修订接受后不再发送。没有专用通知的通用所属执行也仅发送状态与原件指针，不序列化整个业务对象。

## 打断与撤销

Esc/Ctrl+C 通过主会话终端打断入口应用所属执行的声明策略。RVF 的后台执行及接续资格保留；普通未声明保留策略的执行继续按既有取消策略处理。直接前台 CLI 的 SIGINT/SIGTERM 也只打断等待，独立角色及监督命令保留；下次接续先核对原进程或监督结果，避免并发重跑。

已发布最终结果在尚未产生新验证证据时被打断，保留原通知。新一轮验证一旦开始，历史通过与接受事实仍保留，但当前资格转为机器待证；打断不能将未知验证恢复成通过，原 run 补齐验证后再发布有效结果。

明确 `cancel-task-review`、`task command-execution cancel` 或覆盖所属执行的任务停止操作才持久撤销，保存来源、范围与请求依据。不根据原生用户消息通道、进程信号或原因字符串推断真人新授权。既有撤销原件不覆盖，来源仍未知时保留未知。

派发、补丁应用和实际投递前继续检查撤销。撤销后的重启只补齐取消证据与清理，不自动恢复。已发生的历史接受与当前后续资格分别保存。

## 自动恢复边界

| 门禁 | 等价证据与恢复动作 | 有界条件 |
| --- | --- | --- |
| 主会话忙碌或 parked | 所属执行持久通知、原生接受记录；等待可投递后继续，fixer 独立推进 | 通用投递写前核验 |
| 补充迟到 | 独立补充登记、请求摘要及原始上下文；同 run 针对性验修 | 补充集合的修复预算不随重启重置 |
| 监督进程缺证 | 进程身份、原生业务结果、boot 身份、生产方单飞承诺 | 沿用已有单飞与恢复预算 |
| 提交接受未知 | 原生转录、注入账本及 transport receipt；先对账 | 同证据有界重查，变化后恢复 |
| 明确 Stop | 持久撤销与本地/原生取消观察 | 仅补证和清理，禁止恢复业务 |

历史样本中 `fbdce` 无最终结果、`97b34` 正常交回、`7002b` 有控制面撤销、`79d32` 有排队接受轨迹、`12fbc` 为认证故障。这些是用户给定调查基线，不因本实现改写；本轮不推断未知撤销发起者。
