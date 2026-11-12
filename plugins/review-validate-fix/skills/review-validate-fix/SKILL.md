---
name: review-validate-fix
description: 用户明确调用 RVF，或主会话依据任务授权选择执行任务快照双审或显式计划材料审阅时使用。
---

# 任务快照评审

需要审阅计划本身时，使用独立的[显式计划材料 lane](references/plan-material-review-contract.md)：冻结计划、需求和范围，运行独立 reviewer，返回质量事实。它不从 Stop Hook 自动触发，不复用代码快照通过，不授予计划批准。

一次冻结材料启动 → 匿名发现同时供主会话与独立 Validate-Fixer 核对 → 必要修复及完整树验证 → 简短后台结果交回 → 所属工作流审阅并处置结果。保留 SANTA-method 与角色独立上下文；默认一个逻辑匿名 fixer，普通修复最多三轮。

发起决策优先读取 hook 或显式入口提供的共用事实摘要与持久详单。正式 run 的 `decision-facts.json` 绑定冻结候选，供各角色复用；只在内容变化、来源失效或需要进一步语义判断时补查，常规决策不重复查询全量历史或 CLI 帮助。自动分组只提示用途，skill、hook、prompt 和控制行为的配置均属于实际行为改动，仍须核对语义与风险。

若决定跳过 RVF，简述具体行为变化、影响与依据，明确属于首次不发起、已有精确覆盖后不重跑，还是父任务统一评审。任务历史累计优先使用已核验的 Task Card 保留提交统计，和当前 Dirty 分开报告；缺失时注明固定任务窗口逐提交累计的范围。Dirty 按实现／行为配置、测试与 fixtures、文档、其他报告文件数和文本增删行，包含未跟踪文件，二进制与背景 WIP 单列；未声明路径时背景归属保持 unknown。缺证按项保留 unknown，不能解释为零改动或验证成功；来源变化或下一真实用户回合自动补证，诊断不驱动新回合。

**临时三路外审至 2026-11-02 00:00 America/New_York（UTC 05:00）：** 正式创建的新 run（含 prepare-only、Kanban 托管）最低三席，普通席及 fallback 排除 Codex CLI，最后一席只运行独立工作树和上下文中的外部 `codex exec`；主会话是 Codex 也不改用 internal subagent。oh-my-pi 正常参与普通席。`--reviewer-count` 是总数，至少提升为三；显式配置仍须符合各席约束，未提供 Codex 配置时补用已启用的默认 Codex 配置。必选席缺 CLI、认证或额度失败不降级为其他 harness，其他席继续；保存恢复条件并复用原故障重试与语义修复预算。全部席完成后综合所有发现与重复发现的补充证据，再进入现有 validate/fix。截止后创建恢复正常双路及 Codex 普通候选资格；既有 run 保持创建时的数量与约束，到期后接续也不减席。

1. 核对任务需求、有效授权和实际 Git 工作树。选择固定基线及明确任务路径，背景 WIP 保持原样。将用户原话和必要实现说明保存为一份持久需求文件。Kanban 新的一段实现开始前，可用 `rvf record-kanban-task-review-start --repo <工作树> --task-id <task-id> --base-ref <实现前固定 SHA> --scope <路径> ...` 明确保存起点；prompt、换会话和吸收 base 都不会推进它。未记录时仅使用可证明的任务创建/checkpoint 起点，不猜 live main。
2. 准备最终验证命令的 JSON argv 数组列表，一次执行 `rvf review-task-snapshot --repo <工作树> --base-ref <基线> --scope <相对路径> ... --requirements-file <文件> --fix --validation-commands <文件>`。隔离环境需要准备时附 `--preparation-commands <argv JSON>`；可附 `--machine-context-file <JSON>` 和 `--machine-evidence-commands <argv JSON>`。材料冻结到精确 run；`--reviewer-config <JSON>`、`--fixer-config <JSON>` 在启动时选择 CLI。所有分配 reviewer 必须实际完成，缺腿不能降为通过。

   Kanban 可用 `--base-from-kanban-task <task-id>` 替代 `--base-ref`，仍明确提供范围和需求。未知来源返回 `unknown` 与持久恢复条件，不解释为无候选。新一轮默认保留原起点；只有明确使用 `--incremental-after-run <同任务已完成目录>` 才选择增量起点，范围扩大则从原起点重新覆盖。已有 run 始终按精确目录接续。路径内使用完整净 diff，包括同路径外部上下文，不声称 session/hunk 所有权。
   在支持所属执行的 Kanban 实例中附 `--kanban-task-id <任务 ID> --kanban-project-path <主仓库路径>`，由宿主登记 owner、park、恢复和一次结果交回。普通命令由工具等待到结果；主 Agent 不轮询阶段、不重新拼装 fixer/验证命令。机器待证只更新持久进度，故障诊断不驱动 Agent 回合。重启或补证复用 `rvf resume-task-review --run-dir <精确目录>`，已完成 reviewer 不重跑。无 finding 红测也会从真实被测树进入修复。
3. reviewer 完成后读取 `reviewer-findings-summary.md` 做 sanity check；后台 Validate-Fixer 同时继续，不等待主会话。仅需补充独有事实时使用摘要内的 `rvf submit-review-context --run-dir <精确目录> --supplement-id <稳定唯一ID> --candidate-commit <冻结候选> --findings-sha256 <匿名发现摘要> --context-file <持久文件>`。同 ID 同材料不重复执行，异材料拒绝覆盖；可重复 `--finding-id` 指向具体发现。Validate-Fixer 逐份独立验真并可反证。迟到补充保留此前修复，自动追加针对性验修，不重跑 reviewers；每个补充材料集合复用独立的有界修复预算。最终资格须覆盖全部已登记补充。

   收到 `agent_review_ready` 后读取通知指向的 `agent-review-summary.md`，核对实际补丁、逐项处置、补充回应、被测树与验证结论；完整命令、环境及日志仅按需读取机器原件，无需整份加载 `business-result.json`。`task_snapshot_review_result_facts` 绑定精确候选、范围、处置和验证事实；公开成功状态为 `review_and_validation_completed`。提交、PR、合并、部署和结案由所属工作流依据已有授权决定，评审结果不授予这些权限。会造成相关工作回滚或大范围重写的 elevation 交具体取舍；`agent_judgment_required` 先处理明确语义问题，`machine_evidence_pending` 按机器恢复条件接续，不能把退出码 0 当成完成。

   若主会话已通过工具读到某阶段结果，按摘要中的 `rvf acknowledge-review-notice --run-dir <精确目录> --notice-id <精确通知ID>` 登记消费，后台不再补发同阶段同修订。最终消费再次检查全部补充覆盖及最新修订，历史消费回执不授予新结果的资格，不新增提交或部署权限。

   代码快照 lane 不验收整份实施计划的执行完成度或接管任务 recap；所属工作流保存计划进展、发布与结案事实。已发生提交可在原 run 内核对，评审完成不以提交或再次接续为前提。用户的只读、不得提交等明确限制继续有效。

主 Agent 统一收集 reviewers、fixer 与自己发现的有价值问题。显式要求或既有问题收集授权适用时，用 `rvf collect-issues --list-repository <owner/repo>` 查询开放和关闭问题，匹配稳定 `problem_key` 后 `rvf collect-issues --report <JSON>`；格式按需读 `references/github-issue-observation-report.md`。失败保留持久材料，不阻断提交或同步。

未撤销的 run 在重启时接续原目录。瞬时故障同配置最多重试一次；确定性故障转已登记配置。配置错误等配置变化，认证和额度同配置最多在 300 秒后复核一次。语义预算不随重启或切换重置；角色没有总运行时限，准备、fallback 与补验也不继承墙钟期限。Esc/Ctrl+C 或前台 CLI 打断只结束当前等待，后台角色、监督命令及续接资格保留。明确停止 RVF 时执行 `rvf cancel-task-review --run-dir <精确目录> --reason <依据>`，或使用明确覆盖所属执行的任务停止操作；先持久撤销，后续不自动恢复。撤销保存来源、范围与请求依据；本地进程退出与原生取消接受分别记录。机器信息缺口自主补证，只有真实价值取舍、新权限或人独有信息才需用户决定。用户的提交、同步、push 和部署限制继续有效。

用户在中途中断后明确要求继续（如 `continue`）时，先核验先前 command/script 的所属进程、退出结果及实际副作用：仍在运行则接管等待，已完成则复用可核验证据，未撤销且可接续则 resume 原 run。已撤销的 run 保留原记录，按本次继续授权重新冻结候选并新建 run，不删除撤销标记；只重跑已确认可安全重放的命令，不把旧 run 的完成记录冒充新 run 的完成。不可重放或可能重复外部写入的脚本先核对实际结果，补齐幂等或恢复路径；独立的提交、同步、push 和部署限制继续有效。

CLI 配置可登记 `credential_evidence_paths`，仅将内容摘要作为新凭据恢复证据，不保存凭据原文；Codex 与 Claude 的常用凭据文件默认登记。语义修复轮数耗尽仍需主 Agent 审阅；运行较久或持续静默只用于观察，不终止角色、不压缩审阅范围。进度原件保存进程身份、最近观察与输出活动、等待原因和恢复条件。历史超时原件保留，新 attempt 以自己的正常结束和有效结果计完成。优先把耗时且可共享的检查登记到 `--machine-evidence-commands`，由宿主单次采集，两个 reviewer 分别读取事实并作出判断。

常规回执只给业务处置、必要下一步与持久目录；详细日志按需读取。`rvf analyze --run-dir <目录> --run-dir <另一目录>` 只读展示原始/有效恢复状态、依据和分项量测，不是交付门禁。磁盘副本、调用次数与 token 收益分别证明；缺少 native usage 时保持未知。
