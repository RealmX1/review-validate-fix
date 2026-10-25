# 固定候选 RVF 与终态分析恢复

## 实现合同

用户已批准 task `53034` 的五期计划，并明确要求实施。能力目标是：候选冻结后不自动吸收 base；准备失败仍有可核验的终态；分析能识别当前 Codex 的真实子代理和编辑事件；阶段与资源统计具有固定边界；交接绑定真实 run。

范围包括 RVF 的执行指引、CLI、run ledger、轨迹采集、分析归档、恢复索引、阶段投影和交接读取。`cline-kanban` 的活跃指令由配套 task `e1368` 交付。当前任务没有改动其工作树。

范围外包括 GitHub 接入、多阶段自动吸收、提交数或活动量阈值、reviewer 路由策略、重新执行历史正式分析、部署与远端发布。后续策略比较记录于 Linear `REA-34`。

验收以隔离 Git、原生转录 fixture、真实子进程和并发执行覆盖固定候选、身份矛盾、归档失败、跨重启重试及不重复副作用。新回归纳入 `scripts/check_skill_contracts.sh`；完整入口为 `python3 -B scripts/check_plugin_contracts.py`。

## 固定候选与最终整合

自动流程为计划、实现、固定候选 RVF、`rvf-land`、最终 `base-branch-sync`。`start` 回执声明 `base_absorption_policy=final_integration_only`；`--local-base-ref` 只指定比较用的本地 ref。

计划后、实现后和 RVF 准备前均不自动吸收 base。允许只读检查本地 `main` 的变化。候选和比较 OID 一旦固定，浮动 `main` 的前移不改变本轮输入。

真实 HEAD 或候选变化继续触发既有快照拒绝，不更新旧 fingerprint 冒充同一轮。已有分界恢复入口准备出新 run 后，旧归档请求的 `successor_review_runs` 追加新身份；不扩大旧 scope，也不豁免未审 unit。

`request-review-start-boundary` 保留“有效准备中的 run 不能被无故替换”的门禁。失败准备被明确替代时先归档；已经通过正式清理终止的准备轮次也可以继续请求新分界。新 token 仍保持 `minted`，不借旧 run 的接受事实绕过新的独立原生回合和 checkpoint 验证。

## 独立生命周期

`core/run_ledger/review_run_lifecycle.py` 保存 `artifacts/review-run-lifecycle.json`。短元数据提交使用内核文件锁和原子替换；锁不占用业务 base 锁，进程退出或机器重启会释放内核锁。

| 字段 | 来源与含义 |
| --- | --- |
| `review_run_started_at` | 接受开审决策的时刻；直接 prepare 入口可使用精确 run 的 `prepare_started` |
| `review_run_start_evidence_source` | 起点所用的原生接受记录或 prepare 事件 |
| `phase_started_at` | 实际进入各阶段的首次观测 |
| `review_run_terminal_status` | `handoff` 或非成功终态；不与组件退出码混用 |
| `review_run_terminal_at` | 首次终止观测；恢复和晚写不得改变 |

阶段为 `prepare → review → merge → validate_fix → verify → handoff → complete`。派发、合并处置、fix prepare、主 worktree 验证及交接入口记录实际阶段。attempt worktree 内的并行验证不提前将主会话推进到 `verify`。没有证据的阶段不补造时间。

直接 prepare 使用实际的 `component=prepare-run`、`event=started`、`reason_code=prepare_started` 事件建立起点；历史事件补证也核对同一合同。时间窗口按带时区的真实 UTC 时刻比较，保留原始文本及小数秒精度。缺失、非法或没有时区的值保持未知，不能取得窗口资格。

组件 summary 仍可报告自己的操作状态，但通过共同提交锁投影生命周期，不能改写已绑定 run，也不能将已到达的阶段退回 `prepare`。终态后的迟到写入保留已归档字段。

## 非成功轮次的持久归档

`review_run_terminal_archive.py` 接收 `aborted_before_review`、`cancelled`、`interrupted`、`superseded`、`declined`。取消和两种 decline 清理入口使用同一归档能力。

请求保存在 `artifacts/review-run-terminal-archive-request.json`，包含仓库真实路径、session、run、固定 scope 摘要、原因、首次终止时刻、尝试计数、恢复条件与后续 run 关联。默认 prep 输入位于用户本地数据的 `runtime-state/prepared-review-run-inputs`；正式 run 另存准备快照。TTL 过期不延长执行资格，尚待清理的输入仍保留。

恢复索引位于 `runtime-state/pending-review-run-terminal-archives/<仓库与会话摘要>`。后续合法 Stop 或显式恢复 CLI 按精确归属扫描，每批最多八个索引；持久游标避免前面的等待项饿死后面的项：

```sh
python3 review_run_terminal_archive.py --repo <精确工作树> --session-id <原会话 ID>
```

显式中止可通过 `request --repo <工作树> --session-id <会话> --run-dir <精确 run> --decision-kind aborted_before_review --reason <真实原因>` 登记；`--defer-resume` 只保存请求。直接 prepare 与实际 preparation worker 的 `KeyboardInterrupt` 使用本次执行创建的精确绑定，保存原生中断观察后原样抛出，不在异常清理中启动归档或 Analyze。等待者中断、超时和普通 I/O 错误不表示 worker 已结束；没有请求的 resume 不推造中止。

恢复不发起 review，不驱动主 agent 新回合。hook 的恢复诊断继续遵守 `developer_only`。

| 情形 | 机器动作与恢复条件 |
| --- | --- |
| dispatcher 在尝试启动前失败 | 核验精确绑定的未启动负证明和不可逆启动边界；恢复入口等输入后按每代最多三次重试，保留原失败和旧执行身份 |
| dispatcher/fixer 仍可能写入 | 保留等待；核验实际写入者的交付、原生退出及取消后仍存活的 run 进程；tmux 存活表示仍需等待 |
| dispatcher 状态缺少可信退出回写 | 核验实际 runtime 自登记的精确执行与交付回执，或同机该次实际执行的 boot 已结束；tmux、launcher 或 monitor 消失均不证明实际 writer 已退出 |
| 归档步骤临时失败 | 每个证据代次最多三次；持久保存计数，输入可读性或执行版本变化后重新评估 |
| 部分 finalize 已成功 | 持久阶段记录绑定 run、scope、首次终态和已确认的捕获摘要；复用已确认阶段，只重试未完成阶段；不重复释放已处理 lease |
| finalize 已完成，但请求收尾前中断 | 先只读核验精确缓存、首次终态及 summary，再补记归档；不消耗新执行额度，不重复副作用 |
| 等待取消期间先成立 handoff | 保留首次成功终态，旧请求记录 `already_terminal`；不另生成 cancelled 事实或非成功分析 |
| 曾等待 writer，或延迟恢复 workspace diff | 不以之后的活工作树补造终止时刻快照，明确保留 `unavailable` |
| workspace 快照写出后、阶段确认前退出 | 保留原文件，但不能据此确认阶段完成或重新采集后来工作树；统计与归因明确标为未知 |
| 分析缺少原 transcript | 等待原路径恢复，或使用 session 相同且摘要匹配的已归档副本；归档本身无需重做 |
| 自动执行被现有策略禁止 | 保留恢复条件，不消耗分析预算，不绕过 suppress、Analyze 自抑制或无 task 的 Codex GUI 策略 |
| run、仓库、会话或固定 scope 不符 | 拒绝相应动作，保留原始证据；其他独立工作继续 |

非成功归档不推进 reviewed/highwater，不发布成功收尾通知。归档完成、分析完成、land 和 base 整合分别记录；一个状态不能证明另一个状态。

tracker 仍为 started 时，可由同一 run、issue、attempt、worktree 的一致终态 `attempt.json`、`result.json` 和 `fix.patch` 补证既有交付已经结束。这不宣称原生进程退出；启动者死亡、TTL 和一次进程查询未命中也不构成退出证明。仍缺可信交付或退出证据时，持久保存 unknown、证据缺口与重验条件。归档恢复的每次提交都在锁内读取最新请求，只合并自己的字段，保留并发写入的后继 run 及其他持久事实。

dispatcher 由实际入口登记 PID、入口及 argv 摘要、run/session 和本机 boot 身份；只有实际主程序正常交付后才记录交付结束。专属 monitor 可跨 tmux HUP 等待这一证据。外壳返回码、monitor 被杀或旧状态文件不能替代实际交付，身份矛盾保持未知；交付证据恢复或同机实际执行的 boot 换代后自动重验。后续 Analyze 的可变 usage/analysis 投影不用于推翻已经确认的捕获事实。

启动前的确定失败与尝试启动后的未知分别记录。负证明来自已封存的尚未尝试派发事实，或原生 `Popen` 对精确 `argv0` 的 exec 拒绝；后者分别覆盖 tmux 与 monitor 所选子命令的创建阶段。已经取得子进程句柄后，tmux 非零、duplicate session、超时或没有 runtime 登记仍需真实写入者证据。新 execution 不覆盖旧的未知或成功绑定，避免丢锁或重复派发时丢失既有 writer。

精确恢复索引保存完整请求快照，其原子提交是终态登记的接受点；run 内的请求文件是可重建投影。正式恢复扫描可在请求投影缺失时校验索引并接续，启动门禁也识别这一已接受意图。首次索引未提交时不捏造终态事实，无索引且无请求时不推断取消。登记、归档接受无 writer 的证明并提交归档状态，以及派发越过启动边界，共用同一派发许可锁；先成立的停止意图拒绝新启动，已越过边界的执行则让归档等待。锁顺序为归档执行锁、共同派发许可锁、run 元数据锁；派发按共同许可锁、执行证据锁的顺序运行，长 finalize 和 Analyze 在这些短元数据临界区之外执行。

准备中断后的恢复只在共同许可锁内依据原始记录确认尚未越过启动边界，再撤销或封存未启动的执行；绑定半提交只从原 receipt 补齐投影，不读取后来入口内容冒充历史绑定。已创建但尚未被 status 指向的孤立 receipt 同样计入本代预算。发现同名 tmux 资源已占用时，在创建新尝试前等待，保留原 status、证明与额度；检查后被外部抢占仍保持新尝试的真实未知。

## 后台 Analyze 的有界执行

启动恢复记录为 `artifacts/review-run-analysis-recovery.json`；真实执行预算为 `artifacts/analysis-execution-attempt-budget.json`。控制文件和首次冻结 prompt 均位于 Analyze agent 可写的 `analysis` 目录之外。

自动分析同一证据代次最多执行三次，进程退出码为零且两份正式产物通过现有校验才能记录完成。原始 causality 快照、输出摘要和已完成状态全部重新核验后，重复调用直接复用结果，不重复调用模型或增加完成计数。

执行版本、入口或登录配置变化可以形成新证据代次；改变模型产物、目录 mtime 或普通日志增长不能重置预算。失败历史持续保留。独立执行锁防止并发重复执行，机器重启后仍读取原有预算。启动失败只影响后台分析，不阻断主任务交接。

入口和登录配置使用内容摘要及必要访问条件，原样重写或 touch 不产生新代次；原始轨迹及冻结输入的访问条件恢复后可以重新评估。模型输出的内容变化不授予新额度。已记录进程成功退出时，正式输出恢复可读后先独立重验，不重复执行模型。

权限或实际 I/O 故障会暂停剩余尝试，等待机器读取证据恢复；TODO、非法 JSON 或尚未生成产物仍是未完成分析，可以在原代次最多三次的预算内修复，不能只凭进程退出码为零判断完成。

正式分析完成与 guardian 完成计数分别记录。计数暂时失败保留失败历史和补记条件；再次验证完成产物后，按精确 resolved run 目录幂等补记。唯一性认领和计数增量在同一份计数记录的内核锁内原子提交，避免完成落盘与计数之间中断后重复累计。

正常 claim、advisory 和 pending 扫描也调用不具备 agent 执行入口的完成重验 helper。计数仍 pending 时保留恢复索引，正式分析的 completed 不回退；补记成功后才清除索引。长重验和 guardian 补记在短请求元数据锁之外执行。

分析恢复请求必须是精确普通文件，run、目录、仓库及会话身份全部一致；长操作结束后再读最新绑定。启动认领具有独立 ID，迟到结果或重复回调不能改写后一次认领或重复退款。完成计数的 API/CLI 缺少明确 run 目录时不写计数；显式相对路径按调用时 cwd 正规化为同一绝对身份。

## 子代理和 patch 证据

Codex adapter 关联派发调用、原生 `SubAgentActivity` 和真实返回值。`task_name` 只表示任务路径，不能代替原生 session ID。定位子轨迹后核对 `session_meta.id`；文件名相似不能证明归属。

当前嵌套编辑使用 `item_completed/FileChange` 的成功结果、变更路径和原生编辑 ID；既有 `patch_apply_end` 的带路径结果也参与归因。解析器不依靠解密外层 `functions.exec` 输入，不猜造外层调用关联。

同一会话、同一原生编辑 ID 和相同变更内容的多个完成事件只计一次；不同 ID 或不同内容的后续编辑保留。没有原生 ID 时不凭路径猜测重复。

子轨迹统计从实际 spawn 调用开始；继承的父会话历史与终止后的调用不计入本轮。原生 ID 缺失时不填造 `candidate_patch_call_ids`。attempt 的 `fix.patch` 必须属于当前 run 的 `artifacts/fix-attempts`，记录独立摘要、字节数、issue 与 attempt 作为等价证据。

spawn 边界缺失、非法或身份冲突时，先从相同原生 session 与 call ID 的父轨迹补证；仍无法证明时，已归属计数为未知，单列捕获到的原始 write-op 观测与覆盖原因。未归属的 patch 保留为原始观测，不进入已归属 patch 因果链；证据恢复后可重新生成分析。

`subagent_capture_status` 和 `patch_call_attribution_status` 使用 `complete`、`partial`、`unavailable`。零捕获不表示零执行；正式分析通过校验也不表示采集覆盖完整。

## 资源口径与状态快照

主流程耗时从不可变开审起点计至固定终态，不能再使用可被组件更新的 `summary.timestamp`。同一原生会话的累计 Token 样本作差，基线、身份或完整计数缺失时输出 `null` 与覆盖不足，不补成零。

仅有开审起点处的单个累计样本不能同时充当终值。逐响应统计按 `(native_session_id, response_id)` 去重，子会话还须具有匹配身份的真实 spawn 下界，避免混入继承历史；缺少边界时单列覆盖原因。

逐响应用量按真实 session/thread 与 `response_id` 去重，并单列冲突。主会话、子代理、外部 reviewer 和后台 Analyze 分开呈现。累计差值和逐响应观测不相加，组件累计并行时长不等于主任务额外等待。

`artifacts/usage/usage-summary.json` 保存阶段墙钟与已观测响应统计，并引用后台 `.analyze-thread.status.json`。usage 生成时 Analyze 尚未执行，其耗时和 Token 保持未知；完成后的真实退出、尝试和进程时长由独立状态及预算账本提供。

分析产物的 `component_states` 是生成时的独立状态快照。reviewer 数分别来自正式计划、实际结果文件和协议有效结果，不由 strict preset 的目标数推断。base 整合没有本 run 的独立回执时保持 `unknown`；完成 RVF 或 land 不能代替 base 整合事实。

交接优先读取正式文件目录、summary 和 scope contract，正文里的历史 run 只作引用。当前 `rvf-review-start-<16hex>` 和时间戳 run ID 均可读取；显式身份矛盾返回失败，不能以 `run_id=null` 绕过归属检查。

candidate evidence 的验证记录只有在本次交接的 run 与 scope 都可核验时才复用。land 的显式 `--run-dir` 必须与 handoff 的目录和身份一致；粘贴摘要可从精确显式目录补证，缺少身份时不写正式 prepare 计划。


## 当前已知恢复边界

本次收尾保留两个已复现、尚未实现的恢复缺口：首次 dispatcher 回执在原子 rename 前硬退出时，完整 pending 文件不能自动补为正式回执（RVF-029）；tmux 查询临时不可用时，返回的等待状态尚未接入标准 wait 的持久自动重验（RVF-030）。前者继续保留未知并拒绝新启动，后者需要资源恢复后显式重新派发；不得据此宣称自动恢复完成。这些限制及原始探针已记入 task 53034 的持久兼容性报告。

用户明确要求停止扩展本轮 RVF，按最小实际 land 与 base 整合继续。因此本提交不宣称覆盖上述两个窗口，也不把历史完整契约失败改写为成功。已通过的定向回归按对应候选保留，最终整合只补交互所需的检查。
