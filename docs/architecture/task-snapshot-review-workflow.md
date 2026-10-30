# 固定任务快照评审

本文件落实 cdd62 中用户批准的「RVF 工作流解耦、Prompt 精简与 GitHub Issue 收集」计划。

RVF 的流程为：一次固定材料启动 → 两路独立 reviewer → 来源匿名的合并 validate/fix → 完整被测树最终验证 → 一次结果交回 → 主 Agent 审阅、提交并立即展示计划完成情况与 recap。base 同步按既有授权另行执行。

## 组件接口

- `rvf review-task-snapshot --repo <工作树> --base-ref <固定基线或本地 ref> --scope <相对路径> ... --requirements-file <需求文件>` 创建持久快照并启动独立评审，默认两路，`--reviewer-count 3` 可指定三路。数量在创建时保存，接续必须完成全部分配腿；ref 只解析一次，范围必须显式给出。`--prepare-only` 只准备输入。
- `rvf resume-task-review --run-dir <持久目录>` 接续原 run，只执行未完成部分。可附 `--fix` 执行一个合并 fixer，`--validation-commands <JSON 文件>` 执行最终验证。主会话使用普通 Git 提交；该入口也可核对已发生的提交。
- `rvf collect-issues --report <JSON 文件>` 由主 Agent 统一收集问题。`rvf collect-issues --flush` 接续持久待补写材料。失败不阻断评审、提交或同步。
- `rvf analyze --run-dir <目录>` 按需读取结果生成复盘材料，无自动启动、归档门禁或写源码权限。

### 临时 Codex 必选外部席

2026-11-02 00:00 America/New_York（UTC 05:00）前，正式创建入口将总数最低提升为三，`run.json` 冻结 `reviewer_count` 和逐席 `reviewer_harness_constraints`。普通席的约束是 `non_codex_external_cli`，最后一席是 `codex_external_cli`；更大显式数量仍表示总数，其中一席固定 Codex。`--prepare-only` 与 Kanban 所属执行在同一入口冻结分配。 普通席仅接受直接可识别的 Claude、Cursor、Kimi、oh-my-pi 命令；Python、Node、shell 及无法识别的自定义包装器保留为配置缺失，不参与普通席或 fallback。首次派发时若该席没有可用配置，保存等待条件；配置或 CLI 恢复后自动接续，不消耗墙钟预算。

派发和全部 fallback 都先过滤本席允许的实际 CLI 命令，再沿用优先级、额度池偏好和独立进程规则。oh-my-pi 不因使用相同额度池而被排除。显式配置同样受约束；缺少 Codex 配置时追加已启用的默认外部 Codex 配置。Codex 席仅接受 `codex exec`，不会转给 internal subagent 或其他 harness，主会话为 Codex 也一样。缺 CLI 时保存逐席 `reviewer_configuration_recovery`，其余席继续；认证、额度、故障重试与撤销沿用角色执行契约。环境或配置恢复后接续精确 run，已完成席不重复。

所有已分配席完成后，`anonymous_findings` 合并全部报告，保留每份独有发现和重复问题的补充证据；不采用多数票。到期后新 run 使用 `any_external_cli`，恢复正常双路和 Codex 普通候选资格。接续只读创建时约束，不重新计算临时政策；既有分配不变。

自动推进入口在 `review-task-snapshot` 同时接受 `--fix`、`--validation-commands`、`--preparation-commands`、`--machine-context-file`、`--machine-evidence-commands` 和 `--implementation-plan-file`。材料按摘要冻结到 `execution-plan.json`，后续只接续精确 run。默认最多三轮语义修复（包含首次 finding 修复），CLI 传输重试另计；语义修复预算持久保存，角色运行不设总时限。完成 reviewer 的上下文与判断不共享，仅共享绑定完整树、cwd、命令、工具链和依赖身份的机器事实。依赖身份缺证时重新采集。

`findings=[]` 的最终验证失败也会进入 validation repair；fixer request 包含原失败命令、日志和真实 `tested_commit`，补丁从该被测基线计算。隔离准备命令不能改写候选源码，其生成物不混入任务修复。安全的外部 HEAD 前移自动合并已评审任务补丁并重验；新的未评审 hunk 或不能安全合并的内容保留原件并形成具体处置。

`business-result.json` 区分 `agent_review_ready`、`agent_judgment_required`、`machine_evidence_pending` 与 `cancelled`，退出码不授予业务资格。机器等待包含缺证、等价来源、证据路径、触发条件、恢复动作和有界次数。原生错误优先，文本兜底仅扫描失败进程的 stderr；正常正文的额度关键词不是额度错误。认证/额度同配置 300 秒后最多复核一次；新凭据或配置摘要允许恢复，不能无限重试。

fixer 的 completed 表示发现的语义处置和必要的针对性验证完成；执行器应用补丁后仍须统一运行完整最终验证。只有决定处置所必需的具体机器事实无法在既有权限和等价来源下取得、且可由已登记命令补齐时，才使用 `completion_blocker: registered_machine_validation` 的 incomplete；尚未执行整套 final 本身不构成语义未完成。执行器在保留修复树上自动补验，保存完整树、cwd、准备环境及日志，再交回同一 fixer 独立收束，不把原始 incomplete 改成 completed。中断和环境恢复共享每个语义轮最多三次采集的预算；失败后按恢复证据变化或一次定时探测有界接续，环境变化也使既有通过证据失效。所有 attempt 原件保留，全部动作继续受进程归属核验和持久撤销约束，没有角色总时限；语义缺口不触发这一机器补证。

准备、机器补验和权威最终验证均由独立监督者等待所属后代。原生启动身份和继承的随机执行归属覆盖新 session 后代；`ps` 不可用时使用平台原生查询。短暂查询失败只作有界复核，持续缺证保存命令退出事实、后代退出未知与恢复条件，不能把未知写成通过或耗尽整个角色期限后冒充命令失败。

macOS 同时保存监督者的内核 `uniqueid`。新孤儿缺少执行 token 时，只排除原始父版本证明由 launchd 启动、或父 `uniqueid` 非 1 且早于监督者的无关进程，避免并发后台进程把成功准备误报为后代退出未知。`uniqueid` 按进程创建顺序递增；reparent 后 exec 可能将父 `uniqueid` 改为 1，因此不能仅凭当前父为 launchd 排除清空环境的所属后代；缺少归属证据仍保留 unknown。依据：[XNU 创建身份](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_fork.c)、[父身份保存](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_proc.c)。

接续时按 `pid` 与 `start_identity` 核对监督者是否仍存活；附加的 `uniqueid` 证据不改变该启动身份，不能因记录字段增多而将仍存活的监督者写成已退出。

观测缺证不终止尚在执行的已登记命令；有界复核后，以直接子进程退出这一新事实触发再次核验。退出证明仍未知时不写通过，也不派发下一份命令。Stop 可使用尚未回收的 Popen 直接子进程句柄这一独立归属证据；其他后代仍核对原生启动身份。原生 Popen 启动失败单独保存未派发事实及 errno，重采可据此安全恢复，不把缺少监督文件直接解释为退出。

使用 `--kanban-task-id` 与 `--kanban-project-path` 将同一 run 委托给通用 `kanban task command-execution`。执行记录先冻结 incarnation、主会话、工作树、材料、恢复动作及稳定结果 ID，宿主负责等待与原生接受补证。`cancel-task-review` 先写持久撤销，再仅取消匹配启动身份的所属进程；原生共享宿主取消证据未知时保留 unknown。Stop 后不能以自动恢复再次派发。

本工作流默认在 RVF 顺利完成且主 Agent 最终审阅通过后自主 commit 收尾；明确只读、不得提交等限制继续优先。`elevated` 处置必须注明 `elevation_impact: none|rollback|broad_rewrite`；普通 elevation 不单独阻止提交，后两类形成具体判断请求。主 Agent 提交后以 `--closeout-report` 保存逐项计划状态、证据与工作 recap，原 run 核实真实提交并立即输出收尾，先于任何 base-branch-sync。未提供实施证据时计划完成情况是 unknown，而非从 RVF 成功推定。

Analyze 支持重复 `--run-dir`，纯读取原始 outcome 与持久 reconciliation，分别展示 raw/effective 状态及原件指针，不调用会写补证的执行状态读取器。全部 attempt 与独立 reviewer 保留，缺失版本、原生 usage 或主 Agent 往返指标保持 unknown。

run 中保留需求原文、固定基线、候选 Git 对象、显式范围、实际拼装的角色 prompt、原始 stdout/stderr 和进度。后台进程由脚本或宿主等待；重启复用同一目录。一次失败最多原路重试一次，之后改用已配置的其他可用 CLI；两路未完成如实保留。

最终验证在独立工作树执行：当前 HEAD 的已提交内容叠加已评审的任务路径，不带入范围外 staged/unstaged/untracked WIP。`validation.json` 保存完整 `tested_tree`、实际 `tested_commit`、工作树与命令；依赖随 HEAD 漂移时自动重跑原验证命令，不重跑已完成 reviewer。只在实际提交的完整树与已通过验证的树一致时登记完成。同一树的中断验证保留原工作树和构建产物；依赖身份无法证明时完整重验，不复用旧通过记录或命令前缀。

Validate-Fixer 返回 `incomplete` 仍保留新发现、处置、隔离修复快照与补丁；下次输入包含先前结果，工作树接续已保存修复。新范围内发现先并入匿名分配，未解决时不能进入最终验证；未完成修复不提前覆盖主工作树。明确停止在初始角色派发及等待阶段均取消并回收当前流程拥有的 worker。

补证核验为 `incomplete` 的结果按该有效状态统一读取进度和合并发现，即使原始 `outcome.json` 记录为 `failed`。原始失败事实保持不变，未完成结果不提升为 `completed`。

通用补证使用 `resume-task-review --context-response-file <文件>`。每次响应按内容摘要保存到 `context-response-originals-by-sha256/`，对应 attempt 引用确切原件；后续响应不覆盖前次原文，重复提交相同内容复用同一原件。角色可按需读取历次通用补证，已完成腿不会因补证重跑。当前响应原件缺失或摘要不符时，派发前保留已有尝试并报告具体缺口；恢复匹配摘要的原件后接续，不把缺证写成完成。

正式责任范围由 `scope_paths` 固定，`changed_paths` 只是 Git 的改动摘要，重命名可能只列出新路径。读取重命名和删除时仍可使用范围内旧路径的基线对象。大型文本、超长单行、二进制和生成物按完整 Git 对象保存，大小不缩减责任范围；reviewer 自主选择分段读取、源码审查或产物一致性核验，不要求提交逐文件阅读证明。

默认配置选择先跳过已禁用 reviewer，不读取其失效文件。角色 `findings` 与 fixer `new_findings` 的可选 `problem_key` 必须是字符串，格式异常在登记完成前进入现有失败/替代路径。角色文件与 stdout 两种结果来源统一脱敏；按 JSON 语义保留协议字段、数值与嵌套正文结构，成功、失败和原生结果恢复均处理原始报告文件。

Cline Kanban 只提供通用任务、会话、进程等待及恢复。不消费 RVF 的分界、token、阶段资格、rewind、自动 land 或收尾再投递。旧历史仍是历史，不迁移成新流程成功事实。

## Kanban 固定起点与已提交工作发现

`--base-from-kanban-task <task-id>` 与 `--base-ref` 二选一，`--scope` 和需求文件保持必填。
Kanban 适配层核对 task 创建身份、provenance incarnation、真实 worktree、Git common-dir 和目录实体，
优先沿用保存的精确 run/实现起点；未记录时仅接受创建 provenance 或首轮 checkpoint 与 Git 创建记录的等价证明。
core 只接收统一的起点证据并随 `run.json` 冻结，不读取 `~/.cline`。

开始一段新实现时，用 `record-kanban-task-review-start --task-id <id> --repo <工作树> --base-ref <实现前 SHA> --scope <路径>` 明确记录。
普通 prompt、换 session、提交进入 main 或 base 前移都不会推进起点。
`--incremental-after-run <目录>` 必须显式选择同任务、已完成且完整树验证可核对的新版 run；其范围不能盖住新路径，扩大时回到原起点。
旧 tracker、unit、封窗或 session 归属不构成新的覆盖。

任务提交发现使用 Kanban 已观察提交与当前 worktree Git 提交记录，历史改写只接受唯一的一对一 patch-id。
纯 base 同步不能因此变成任务自有提交。这里的归属仅用于发现；评审仍使用显式路径完整净 diff，包含同路径外部 hunk 作为上下文。
净 diff 为零（包括已 revert）不触发决策。

Stop 对有证据的 clean committed 改动提出一次发起决策，提交已进入 base 也不消失；同任务同观察跨会话去重。
当前范围已由关联的新版 run 完成且内容一致时不重复提醒。不会因此启动 reviewer/fixer。
unknown、损坏状态和补证诊断只持久保存，不驱动 Agent。每个来源观察只核验一次，来源变化或下一次有身份的真实 prompt 可重验；
起点文件损坏时尝试从独立持久解析记录恢复，同时保留损坏原件和每次补证结果。来源恢复后无需新增人工审批。
缺证不妨碍独立 dirty 工作走既有入口。运行材料在 RVF 用户数据目录内；`RVF_CLINE_KANBAN_STATE_DIRECTORY` 可指定隔离的 Kanban 状态目录。

base-branch-sync 根据实际 Git 状态、任务授权和必要验证保全 WIP、吸收 base 与交回提交，不读取 RVF 内部 schema、缓存、run 资格或封窗记录。同步后若产生实质新增行为，按实际风险验证；历史评审事实不被改写。

## 问题收集

主 Agent 给问题指定稳定的 `problem_key`（根因/行为身份，不含文件行号），并给本次观察指定 `observation_id`。默认仓库来自问题所属仓库的 GitHub origin；跨组件问题只选择主要修复仓库。文件移动或描述变化沿用该 key，也可显式指定已有 Issue。

查询开放及关闭 Issue，排除 pull requests；相同问题只追加 observation。写前持久保存原始报告及投递意图；结果未知先查询 Issue/评论中的标识，避免盲目重发。同机并发收集使用共享仓库锁，按仓库、问题身份及其别名共享未知创建意图，新 observation 不另建 Issue。断网、缺少凭据或限流保留待补写材料和恢复原因，不改变评审状态。后续 review/resume CLI 活动会按持久恢复时间异步补写，最多每分钟启动一次；也可由宿主通用恢复任务执行 `collect-issues --flush`。不为网络等待唤醒 Agent。

接口依据：[GitHub Issues API](https://docs.github.com/en/rest/issues/issues) 与[评论 API](https://docs.github.com/en/rest/issues/comments)。Issue 正文保存问题、影响、复现及依据，observation 保存任务/run 来源、验证和修复情况。默认不改变 Issue 状态，也不自动派发任务。

## 验收

自动化覆盖固定基线与显式范围、背景 staged/unstaged/untracked WIP 保全、独立双审、单腿失败、异常格式、有界替代、重启接续、匿名合并修复、安全应用、真实红测、提交后登记中断、base 漂移及 GitHub 去重/关闭匹配/并发/断网/未知结果对账。

角色说明以约 2,000 字符为设计目标，实际需求及 diff 不受长度门禁限制。需求、范围、diff、findings 各保存一份，prompt 引用必要材料；不要求重复阅读。部署和诊断说明按需读取。

## Agent 运行、取消与观察

所有内置 reviewer、标准自定义角色与 Validate-Fixer 均没有总墙钟上限。配置中的角色 `timeout_seconds`、角色截止文件、定时收束提示及其向准备命令、补验锁和独立监督者的传递已移除。CLI 模型解析和版本查询仍保留各自的操作超时；取消后的退出等待与信号升级也继续有界。

角色的 `progress.json` 持久保存进程启动身份、最近观察、输出字节数和最近输出活动、等待原因及恢复条件。静默与运行较久只形成观察，不作为故障或终止依据。控制器恢复优先读取执行锁、原生正常结束事件与结果原件；活角色不重复派发，已完成席不重跑。机器缺证仍走等价原生进程与命令日志补验，失败保留未知和有界恢复条件。

历史 `deadline-exhausted.json`、`role-deadline.json` 与失败 outcome 不改写；旧 `deadline_exhausted` 不再消耗新执行资格或把成功结果升级成人工处置。新 attempt 必须独立通过正常结束、结果结构和候选完整树核验。持久 Stop 优先于恢复，身份未知或 PID 已复用不能发送信号。

进度原件属于辅助观察：读取、写入或输出 stat 暂时失败只降级观察，不改变角色 outcome，也不终止健康 CLI。相同证据下最多复核三次；输出活动、存储元数据/可用空间变化或进程退出触发补查。故障恢复条件另存 `progress-observation-recovery.json`，同时保留原生进程登记、stdout/stderr 和真实 outcome 作为等价来源；正常结果发布仍必须通过既有必要门禁。
