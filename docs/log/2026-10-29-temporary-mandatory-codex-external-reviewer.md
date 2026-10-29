# 临时 Codex 必选外审实施与交接

用户已在 task `248b2` 的当前实施请求中批准方案，按 `PLAN-READY` 实施。原始任务要求额外 Codex External Reviewer；当前批准方案明确截止为 **2026-11-02 00:00 America/New_York（UTC 05:00）**，以该精确时间为准。

## 实施合同

- 能力：截止前正式创建的 run 至少三席，普通席及其 fallback 排除 Codex CLI，最后一席只运行外部 `codex exec`；所有报告统一合并后才能修复和验证。
- 范围：正式 CLI、prepare-only、Kanban 托管、创建时冻结的数量及席位约束、配置选择与恢复、当前帮助与说明、自动验收、真实 CLI 烟测、Backlog 清理任务与持久定时启动。
- 不包含：internal subagent、投票决策、改变既有 run 的分配、改写历史失败、放宽总时限或重试预算、部署或同步主分支。
- 验收：截止边界、到期后接续、显式数量和配置、普通席 fallback、oh-my-pi 可用性、Codex 故障阻断与仅缺席恢复、独立进程和工作树、全部独有发现及补充证据、定时启动的重启补触发与防重复。
- 影响面：`task_snapshot.py`、`workflow.py`、`rvf_command_line_shortcut_entrypoint.py`、相关 tests、README、当前 skill 和流程文档、清理任务定时脚本。
- 风险：必选 Codex 不可用时 run 保持未完成；时限和撤销仍按既有契约执行。LaunchAgent 只能在登录用户运行时可用后补触发。
- 验证：针对性 pytest、`bash scripts/check_skill_contracts.sh`、隔离真实 CLI 烟测和定时器替身测试。

## 最小实施切片

1. 创建与派发：先用截止、prepare-only、托管与受约束 fallback 测试证明失败，再冻结分配并过滤所有候选；普通未受约束的底层快照和既有 run 保持原行为。
2. 恢复与综合：覆盖 Codex 认证/额度/缺 CLI、恢复仅补缺席、报告合并和独立外部上下文；复用现有角色执行器及有界恢复。
3. 清理交接：实现并测试持久定时启动，创建绑定身份的 Backlog task，核验实际默认、安装 LaunchAgent 并保存回执；到期任务删除临时启用逻辑，保留在途约束解释能力。

## 验证与实际交付

持久证据目录：`/Users/bominzhang/.rvf/user-local-data/task-248b2-temporary-codex-policy-evidence/`。实际提交、清理任务身份和 LaunchAgent 回执在安装后追加；受理不等于任务执行完成。

- 新测试先证明原入口仍创建两席、无席位约束以及 Codex 可进入普通 fallback；实现后通过。随后对真实外审发现的包装命令、查询/启动预算、启动后核验失败、未核验会话与取消搁置恢复补充了复现和修复。
- 第一次完整回归：507 通过、1 失败（`test_missing_process_identity_preserves_unknown_without_aborting_authorized_work` 返回 `review_incomplete`）；单独重跑通过。未证明首次失败的原因，不将其改写为通过。随后完整回归 515 项通过，日志 `full-contract-validation.log`；外部 fixer 后的最终完整回归另存 `final-full-contract-validation.log`。
- 首次真实三路：`real-three-reviewer-source-review/`。隔离 HOME、CODEX_HOME 和 CODEX_SQLITE_HOME，使用真实外部 CLI、独立工作树及上下文。Cursor 认证失败后普通席经正常 fallback 使用 oh-my-pi；两路普通席及必选外部 Codex 均实际完成，综合 5 条原始发现并逐项处理。
- 最终真实三路：`final-three-reviewer-source-review/`。两路 oh-my-pi 独立进程和一路 `codex exec`，全部完成；匿名 validate/fix 处理另外 4 条发现，最终完整树验证通过，业务处置为 `agent_review_ready`、状态 `ready_to_commit`。原始报告、处置、命令、原生会话及验证证据均保留。
- 当前普通席只接受可识别的直接 Claude、Cursor、Kimi 或 oh-my-pi 命令，拒绝无法核实 harness 的解释器或自定义启动器；必选席仅接受直接 `codex exec`。缺 CLI 首次派发也会固定原角色总时限，迟到配置不能重置；prepare-only 不开始派发时限。
- 未加入旧宿主、旧入口或接口迁移代码。既有 run 缺席位约束时沿用原分配，用于满足批准方案的在途语义；不迁移、扩席或重新选取截止策略。
- 修复后的最终验证：`bash scripts/check_skill_contracts.sh` 全部通过，**520 passed in 277.95s**；包含插件源码、入口、manifest 检查及全部 pytest。RVF 独立完整树验证另有 49 项针对性测试及插件检查通过。

## 实际提交与到期调度

- 实现提交：`e12a4ee994bd0895c772f46709c0113796c24106`，分支 `task/248b2-temporary-mandatory-codex-external-reviewer`。提交后的精确 RVF run 已核对实际提交，状态 `completed`，计划条目 `complete`；原件为 `source-review-post-commit-closeout-result.json`。
- 清理 Backlog Task：`review-validate-fix/6bb72`，创建身份 `1793286667625`，标题 `[CHORE,AGENT]清理临时 Codex 必选外审策略`。来源关系已记录，持久交接为证据目录中的 `cleanup-handoff.md`。
- 实际默认先 preview，再仅覆盖 `startInPlanMode: true → false`，依据是用户已批准定时执行清理。其余实际默认均核对继承：baseRef `main`、worktreeMode `branch`、Agent `codex`、权限 `bypass_all_permission_prompts`、autoReviewEnabled `false`、autoReviewMode `commit`。真实 create 绑定最终预览指纹，回执保存于 `cleanup-task-create-receipt.json`。
- 已安装持久 LaunchAgent：`com.review-validate-fix.task-248b2.cleanup-6bb72`，plist 为 `/Users/bominzhang/Library/LaunchAgents/com.review-validate-fix.task-248b2.cleanup-6bb72.plist`。脚本与状态在证据目录的 `cleanup-task-6bb72-scheduler/`，独立于 task worktree 存活。
- 日历触发加 `RunAtLoad`、60 秒条件复核和绝对 UTC 门禁，在 **2026-11-02 05:00 UTC** 后发起清理任务；支持关机后下次登录补触发。CLI 与运行时 generation 改变可恢复已耗尽的同条件三次预算，原始启动回执与任务 incarnation、进程/原生身份分别核验。
- `plutil -lint`、`launchctl bootstrap`、`launchctl print` 均成功，首次调度状态 `waiting_for_deadline`，任务仍为 Backlog，未提前启动。安装回执和注册原件分别为 `cleanup-launch-agent-installation-receipt.json`、`cleanup-launch-agent-registration.txt`。到期实际启动与清理尚未发生。
- 本轮未同步本地 main、未推送、未部署 RVF 插件，也未把任务标为 Done。清理任务若尚无实现，可在自己的工作树核对并引入上述提交；不覆盖来源工作树。
