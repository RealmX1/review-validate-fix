# 用户委派 elevated 问题的收尾契约

本契约归属 `review-validate-fix/1d446`；消费者归属 `my-ai-setup/7f94e`。委派仅改变问题的阻断性，问题仍未解决，不增加 commit、同步或 push 授权。来源 `7494b`、实际 hooks 修复 `0030c`、委派路线 `e05af` 的关系已登记；`abc87` 的历史补证与 `53034` 的整合恢复保持独立，不复制其 WIP。

公开入口为 `rvf elevated-issue-delegation inspect|record|verify|resume`，始终显式传 `--repo`、`--run-dir`。`record`/`resume` 使用 `--request-file` 读取版本 1 的请求；`inspect`/`verify` 只读。成功、机器等待、证据无效、明确禁止分别使用 `accepted`、`waiting_for_machine_evidence`、`invalid_evidence`、`execution_prohibited`，退出码为 0、10、11、12。Land 保留 exit 27/28。

处置存放于原 run 的 `artifacts/elevated-issue-delegation/`。`receipts/<sha256>.json` 为 canonical JSON 和 `.sha256` sidecar；`ledger.json` 是原子更新的追加索引。每个回执绑定 sequence、previous_receipt_sha256、action（delegate/revoke）、run_id、issue_id、attempt_id、repo、候选内容摘要、冻结 scope/handoff 摘要、issue/attempt/result 快照、原生用户请求和稳定目标身份。撤销及重新委派追加新事件；已发布 evidence 中的引用必须仍是该 issue 在账本中的有效处置。

原生用户授权采用事件发生时的会话前缀快照；验证 session、user-triggered turn、原生 `UserMessage` 的 ID、用户原话以及其前文。普通 `role=user`、hook 注入、Agent 建卡和 handoff 转述均不成立。支持用户批准前文提案，再通过建卡回执绑定稳定目标的链。当前活动 turn、目标标题和目标完成状态不影响历史接受事实。

该前缀还必须实际包含被委派的唯一 run ID（可来自原生工具记录或前文），不能只由新回执自述 run 身份，或将其他 run 中同名 `RVF-002` 的决定移植过来。无需要求用户在批准消息里重新输入机器标识。

`authorization` 保留 `transcript_path` 来源声明；原生快照的 `original_transcript_path` 必须与其一致，后续核验只读归档。Codex 的 `AgentMessage` 可能用 `Text`（大写 T），`UserMessage` 可以包含分段文本和图片；正文抽取保留全部文本。用户简短批准可以绑定紧邻的明确委派提案；否定原话不能因包含“同意”或“委派”子串而被接受。

撤销单独核对撤销动作的肯定授权。`不要撤销 RVF-002 的委派。` 不能登记为 revoke；`不要继续委派，撤销 RVF-002 的委派。` 可以。按动作条款及完整 issue 标识符归属判断否定，不能让前一条款的委派否定误伤后面的肯定撤销。固定历史链的每个事件仍执行同一授权校验，后续合法 delegate 不会洗掉较早的非法 revoke。run、issue 与目标 task 均匹配完整机器标识符；句末英文句号是标点，标识符后接 `.other` 或 `-other` 则是另一身份。

首次 record/resume 将原请求映射到不可变 `pending-requests` 快照，并持久绑定候选、attempt、scope 和 handoff；自动恢复只对原绑定补证，不能在等待中切换候选。输入指纹包括原生记录、回执、最新 attempt、候选和核验实现；同输入补证最多三次。缺失目标回执可只读查询同一稳定 task，已接受事件不依赖后续查询成功。

所有受支持的 canonical 资格写入口与消费者 CAS 共用原 run 的 `artifacts/elevated-issue-delegation/mutation.lock`，以同 inode 的 `flock` 串行化。覆盖 issue upsert、prepare/start/stop/apply、awaiting marker 对 attempt 的更新、merge/source refs、reviewer 结果追加/换 kind/归档轮转、reviewer plan、scope 创建/定稿/扩展落盘、validation index、handoff、summary/finalize 及 candidate evidence 发布。生产者通过可重入的 `core/review_run_qualification_mutation_lock.py` 统一加锁；直接绕过正式 writer 修改冻结工件不属于受支持的发布方式，读者仍须核对摘要与最新事实。

Land 先在资格锁外完成可能启动子进程的验证重放，再持锁重读 HEAD、Git/WIP、canonical 资格和委派账本，直到提交、封窗与 evidence 发布结束。验证 index 发布也取同锁，因而不能在持锁时等待该验证子进程。消费者必须在同锁下最后复验到实际 CAS 落地，不能只锁账本。取得资格锁不替代原有唯一执行者及 Git 门禁。

锁忙时不改资格输入、不抢占原持有者，生产者最多等待 30 秒并在 stderr 报告机器等待。委派恢复把锁可用性纳入输入指纹：同一锁忙输入最多三次自动补证，释放后自动开启新预算；只读检查不创建锁或恢复记录。所有失败、等待与恢复历史保留。进程结束由内核释放 flock；重启后的执行先对账已有结果。

含委派的 candidate evidence 为 version 3，顶层 `required_capabilities` 精确包含 `user_delegated_elevated_issue_disposition_v1`；`review.conclusion` 为 `review_completed_with_user_delegated_issues`。`review.outcome_counts` 保留 `fixed`、`false_positive`、`submitted_to_kanban_backlog`、`elevated`，另加 `user_delegated` 和 `blocking_elevated`。canonical_issue_count 只求前四项之和，user_delegated 为 elevated 的子集，blocking_elevated 必须为零。

`review.user_delegated_elevated_issues` 为非空列表，逐项包含 `issue_id`、`attempt_id`、`receipt_path`、`receipt_sha256`、`follow_up`。`follow_up` 包含 `project_path`、`workspace_id`、`task_id`、`task_created_at`。消费者必须独立核验回执位置、canonical bytes、sidecar、账本、摘要、原生请求、稳定身份、最新 attempt 及候选内容绑定，不能仅信 evidence 中的计数。旧消费者必须拒绝 version 3。

无委派继续使用有效 v1/v2。v3 若带 `scope.applied_scope_expansion_receipt_path`，完整执行现有 v2 扩展判据；无扩展时该 path/digest 均缺席。委派不能放宽范围。

冻结 handoff 的 elevated 计数保持原值。公开结果分别说明 review 已完成、问题仍未解决、Land 实际结果与 base 同步实际结果。恢复先对账已有提交及结果；只读 check-evidence 不写恢复状态。机器补证按输入指纹分阶段最多三次，变化后开启新预算，失败历史保留。
