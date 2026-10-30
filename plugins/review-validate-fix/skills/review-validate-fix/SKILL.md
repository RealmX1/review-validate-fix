---
name: review-validate-fix
description: 用户明确调用 RVF，或主会话依据任务授权选择执行任务快照双审时使用。
---

# 任务快照评审

一次冻结材料启动 → 工具完成独立双审、必要修复及完整树验证 → 一次结果交回 → 主 Agent 审阅、提交并展示收尾。保留 SANTA-method 与角色独立上下文；默认一个匿名 fixer，最多三轮语义修复。

**临时三路外审至 2026-11-02 00:00 America/New_York（UTC 05:00）：** 正式创建的新 run（含 prepare-only、Kanban 托管）最低三席，普通席及 fallback 排除 Codex CLI，最后一席只运行独立工作树和上下文中的外部 `codex exec`；主会话是 Codex 也不改用 internal subagent。oh-my-pi 正常参与普通席。`--reviewer-count` 是总数，至少提升为三；显式配置仍须符合各席约束，未提供 Codex 配置时补用已启用的默认 Codex 配置。必选席缺 CLI、认证或额度失败不降级为其他 harness，其他席继续；保存恢复条件并复用原故障重试与语义修复预算。全部席完成后综合所有发现与重复发现的补充证据，再进入现有 validate/fix。截止后创建恢复正常双路及 Codex 普通候选资格；既有 run 保持创建时的数量与约束，到期后接续也不减席。

1. 核对任务需求、有效授权和实际 Git 工作树。选择固定基线及明确任务路径，背景 WIP 保持原样。将用户原话和必要实现说明保存为一份持久需求文件。Kanban 新的一段实现开始前，可用 `rvf record-kanban-task-review-start --repo <工作树> --task-id <task-id> --base-ref <实现前固定 SHA> --scope <路径> ...` 明确保存起点；prompt、换会话和吸收 base 都不会推进它。未记录时仅使用可证明的任务创建/checkpoint 起点，不猜 live main。
2. 准备最终验证命令的 JSON argv 数组列表，一次执行 `rvf review-task-snapshot --repo <工作树> --base-ref <基线> --scope <相对路径> ... --requirements-file <文件> --fix --validation-commands <文件>`。隔离环境需要准备时附 `--preparation-commands <argv JSON>`；可附 `--machine-context-file <JSON>`、`--machine-evidence-commands <argv JSON>` 和 `--implementation-plan-file <条目字符串列表 JSON>`。材料冻结到精确 run；`--reviewer-config <JSON>`、`--fixer-config <JSON>` 在启动时选择 CLI。所有分配 reviewer 必须实际完成，缺腿不能降为通过。

   Kanban 可用 `--base-from-kanban-task <task-id>` 替代 `--base-ref`，仍明确提供范围和需求。未知来源返回 `unknown` 与持久恢复条件，不解释为无候选。新一轮默认保留原起点；只有明确使用 `--incremental-after-run <同任务已完成目录>` 才选择增量起点，范围扩大则从原起点重新覆盖。已有 run 始终按精确目录接续。路径内使用完整净 diff，包括同路径外部上下文，不声称 session/hunk 所有权。
   在支持所属执行的 Kanban 实例中附 `--kanban-task-id <任务 ID> --kanban-project-path <主仓库路径>`，由宿主登记 owner、park、恢复和一次结果交回。普通命令由工具等待到结果；主 Agent 不轮询阶段、不重新拼装 fixer/验证命令。机器待证只更新持久进度，故障诊断不驱动 Agent 回合。重启或补证复用 `rvf resume-task-review --run-dir <精确目录>`，已完成 reviewer 不重跑。无 finding 红测也会从真实被测树进入修复。
3. 收到 `agent_review_ready` 后主 Agent 核对实际改动、需求与计划实施情况。本工作流默认在 RVF 顺利完成且最终审阅通过后自主使用普通 Git 精确 commit 收尾，无需再索取一次提交确认；用户明确的只读、不得提交等限制继续优先。普通 elevation 不单独阻止提交，只有会造成相关工作回滚或大范围重写的 elevation 阻止自动提交并交具体取舍。`agent_judgment_required` 先处理明确语义问题，`machine_evidence_pending` 按机器恢复条件接续，不能把退出码 0 当成完成。

   提交后用 `rvf resume-task-review --run-dir <目录> --closeout-report <JSON>` 核对实际提交并立即展示收尾：计划 implementation 是否全部完成、工作 recap、验证与 commit、遗留事项。报告含 `work_recap` 和 `implementation_items: [{description, status: completed|deferred|not_completed, evidence}]`；有冻结计划时必须逐项覆盖。缺证写 unknown，不把 RVF 完成等同于所有 implementation 完成。这段展示发生在 base-branch-sync 之前；同步仅按既有授权另行执行。

主 Agent 统一收集 reviewers、fixer 与自己发现的有价值问题。显式要求或既有问题收集授权适用时，用 `rvf collect-issues --list-repository <owner/repo>` 查询开放和关闭问题，匹配稳定 `problem_key` 后 `rvf collect-issues --report <JSON>`；格式按需读 `references/github-issue-observation-report.md`。失败保留持久材料，不阻断提交或同步。

重启只接续原 run。瞬时故障同配置最多重试一次；确定性故障转已登记配置。配置错误等配置变化，认证和额度同配置最多在 300 秒后复核一次。语义预算不随重启或切换重置；角色没有总运行时限，准备、fallback 与补验也不继承墙钟期限。Stop 先持久撤销，不能自动恢复已撤销执行；本地进程退出与原生取消接受分别记录。机器信息缺口自主补证，只有真实价值取舍、新权限或人独有信息才需用户决定。用户的提交、同步、push 和部署限制继续有效。

CLI 配置可登记 `credential_evidence_paths`，仅将内容摘要作为新凭据恢复证据，不保存凭据原文；Codex 与 Claude 的常用凭据文件默认登记。语义修复轮数耗尽仍需主 Agent 审阅；运行较久或持续静默只用于观察，不终止角色、不压缩审阅范围。进度原件保存进程身份、最近观察与输出活动、等待原因和恢复条件。历史超时原件保留，新 attempt 以自己的正常结束和有效结果计完成。优先把耗时且可共享的检查登记到 `--machine-evidence-commands`，由宿主单次采集，两个 reviewer 分别读取事实并作出判断。

常规回执只给业务处置、必要下一步与持久目录；详细日志按需读取。`rvf analyze --run-dir <目录> --run-dir <另一目录>` 只读展示原始/有效恢复状态、依据和分项量测，不是交付门禁。磁盘副本、调用次数与 token 收益分别证明；缺少 native usage 时保持未知。
