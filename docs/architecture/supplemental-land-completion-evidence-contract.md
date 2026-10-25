# 历史 Land 补充完成证明契约（version 1）

生产方：review-validate-fix / `abc87`。消费及恢复对账：cline-kanban / `7c86c`。

此证明追加历史验收，不修改原 handoff、冻结 scope、哈希、失败记录或实际提交。它不能作为普通 candidate evidence，也不能启用 base-branch-sync 快路径。历史验收与当前后续执行资格分别输出；Stop、paused、禁止同步或部署等独立限制继续有效。

## 精确入口

```sh
rvf land supplemental-completion inspect \
  --repo <同仓库的现存checkout> --task-id <task> \
  --decision-token <16位token> --run-id <原run-id> --run-dir <原run绝对路径> \
  --handoff-sha256 <原件SHA256> --target-commit <完整commit-OID>
```

三个动作均要求全部绑定参数，不推断 latest，不接受缩写 OID 或分支作为目标。

| 动作 | 行为 |
| --- | --- |
| `inspect` | 只读重建事实和固定内容差异，输出待补义务及恢复条件；不写原仓库、run 或业务状态 |
| `record` | 消费已完成材料，通过全部门禁后原子追加证明；缺材料则返回机器等待 |
| `record --recover-machine-evidence` | 在既有授权内，在持久隔离目录补齐双审及原结构化命令的验证，再追加证明 |
| `verify --evidence <path> --evidence-sha256 <digest>` | 按消费者传入的全部预期绑定，独立重放证明并核验真实 Git 目标 |

`inspect` / `record` 可提供 `--supplemental-review-run-dir` 与 `--validation-run-dir`，消费已经准备的对应材料。补审目录必须具有下述固定输入及完成回执；验证 run 必须属于同一 Git common directory，内容、命令、环境及上下文均匹配。不能用自由文本声明替代机器材料。

CLI 始终输出一个 JSON object：

| `status` / exit | 意义 | 消费约束 |
| --- | --- | --- |
| `acceptable` / 0 | 全部历史验收门禁已满足；`record` / `verify` 返回证明引用 | 仅登记历史完成事实 |
| `awaiting_machine_evidence` / 3 | 缺失可恢复机器证据、双审未完成或自动恢复预算已用完 | 保存恢复条件，变化后用原精确绑定接续 |
| `invalid_evidence` / 4 | 身份/摘要不符、真实未完成、未处置问题、红测或事实矛盾 | 不接受；保留原失败与本次拒绝 |
| `execution_prohibited` / 5 | 正常写入已被备用路线接管/消费 | 不重复写入或执行 |

`inspect` 未登记时的 `acceptable` 仅表示已具备生产条件；消费者必须持有 `record` 返回的具体证明路径和摘要，并调用 `verify`，不能凭 inspect 的状态直接记账。

`historical_completion.status=verified` 与 `current_execution_eligibility` 分开。后者只返回 `requires_current_authorization_check` 或 `execution_prohibited`，永远不返回后续动作已获授权。即使当前执行被禁止，纯只读 `verify` 仍可证明历史完成。

## 判据

普通 Land 继续要求双 COMPLETED 标记。补充入口允许缺失标记，仍强制复用：两份有效且不同 reviewer 的 canonical 结果、无 request、最新 merge dispositions / source refs、canonical fix-issue、最新 attempt/result、真实 applied 状态及持久 backlog 结果。明确未完成/失败标记、冲突标记、过期处置、最新失败或 fixed 未 applied 均拒绝。

原候选从 scope 中的 `review_candidate_snapshot` 取得。核对 Git commit message 内不可变绑定、token/task/worktree、checkpoint tree、父提交及目标祖先关系。旧 worktree 无需仍存在；现存 checkout 必须属于消费者预期的 Git common directory。

合法范围扩展复用 `applied_scope_expansion_facts` 的声明、应用及主会话确认判据。冻结范围不 restamp。补丁来源必须是绑定 run/attempt 的原始 `fix.patch`。每个补丁先在记录的 baseline 上核验，再按完整 blob/mode 前像拓扑重放，不能按时间推断应用顺序。互相冲突或无法重建的路径整体补审；补丁实际路径与声明矛盾则拒绝。

重建后的 tree 与实际目标 tree 逐路径比较 blob 和 mode。包括同路径改动、删除、rename 两端、symlink 及 gitlink。固定 packet 含 full-index binary diff、80 行上下文、完整路径前后像及无法可靠重建路径的全文件内容；不能只比较路径集合。

补审复用 `dispatch_reviewers.route` / `execute_plan` 及公共 canonical 完成检查。两腿使用同一固定 packet，不共享彼此结果。发现问题必须完整处置；补审不能应用新补丁后继续宣称旧目标已完成。需要修改内容时，另行产生新目标并重新绑定。

补审派发使用独立 `rvf-supplemental-review-dispatch-qualification` version 1 门禁，原件位于 `artifacts/inputs/supplemental-review-dispatch-qualification.json`。它包含 subject、原 run 及 backlog 快照，派发前重新重建并核对全部历史事实、固定 packet、目标 HEAD 和 clean 状态。普通派发继续要求活动开审 token；补审不复活旧 token，也不改写原开审接受事实。该专用入口只由补充生产器调用，不作为普通 dispatcher 的通用绕过开关。

原验证仅在 manifest、结构化命令摘要、环境指纹和执行上下文匹配时复用。否则按原 argv 在指定目标的隔离 checkout 验证；不重新执行 Land、commit、seal 或 base 整合。要求真实 base worktree 上下文的义务不能由隔离 task 上下文冒充；缺少匹配记录时保持机器等待，允许通过 `--validation-run-dir` 提供可独立核验的等价材料。

Node 验证先按目标提交中的 npm lockfile 执行隔离 `npm ci --ignore-scripts`，复用可核验的依赖准备回执，并保留每次失败输出。缺运行时、网络或依赖且未产生真实验证记录时返回机器等待；已产生的真实 failed 记录继续拒绝。依赖恢复与验证均计入同输入的有界恢复预算。

## 持久布局与恢复

```text
<run>/artifacts/supplemental-land-completion-evidence/
  <evidence-sha256>.json
  <evidence-sha256>.json.sha256
  <git-bundle-sha256>.bundle
  <git-bundle-sha256>.bundle.sha256
  recovery-operations/<input-fingerprint>/
    machine-evidence-recovery-state.json
    target-validation-and-review-workspace/
    supplemental-review-run/
    isolated-validation-run/
    isolated-validation-command-outputs/
    completed-evidence-reference.json
  record-command-results/<result-sha256>.json
```

证据 schema 为 `rvf-supplemental-land-completion-evidence`、`schema_version=1`。JSON 使用 UTF-8、排序键、紧凑 separators；文件名和 sidecar 均是实际 canonical bytes 的 SHA256。

| 字段 | 内容 |
| --- | --- |
| `binding` | task、token、run、原 run 目录、原 handoff SHA256、完整目标 commit |
| `repository_git_common_directory` | 消费者期望的真实仓库身份 |
| `subject` | 原 scope hash、checkpoint、重建 tree、真实目标 commit/tree/manifest、固定差异及摘要、历史完成事实 |
| `input_fingerprint` | 本轮原材料、目标、验证义务及执行环境的输入摘要 |
| `source_snapshots` | 所需原 run 工件的相对路径 → 原始 bytes 的 base64；包含 digest sidecar |
| `backlog_source_snapshots` | 持久 backlog 依赖原件的快照；核验不追读失效旧路径 |
| `validation_commands` / `validation_snapshots` | 原验证义务及已核验真实记录、引用、摘要的快照 |
| `supplemental_review_snapshots` | 固定输入、双 reviewer、处置、attempt/result 与实际执行时间回执 |
| `supplemental_review_run_directory` / `supplemental_review_backlog_snapshots` | 补审原身份与外部持久依赖快照 |
| `git_bundle_sha256` | 含原候选、合法可重建 baseline、目标提交的自足 Git bundle 摘要 |
| `supplemental_review_recorded_at` | 本次追加补证时间，不能追认成提交前审查时间 |
| `does_not_attest_precommit_review` / `does_not_authorize_followup_execution` | 必须均为 true |

补审输入位于 `artifacts/inputs/supplemental-review-binding.json`，schema 为 `rvf-supplemental-review-binding` version 1，含 `subject_sha256`、`packet_sha256`。`scope.contract.json` 的 `primary_files` 必须覆盖完整固定差异，run 身份为 `supplemental-<subject-sha256>`。scope 和执行回执均须绑定 `target_commit` / `target_tree`；生产时还读取补审实际仓库的 HEAD 与 clean 状态。执行回执单独记录开始、完成时间及 subject 摘要；重启对账后的完成时间注明 `observed_after_reconciliation`。各腿 stdout/stderr 追加保存于 `artifacts/reviewer-command-outputs/`，不混入公共 CLI 的 JSON。

整轮 plan 在实际派发前持久化。各腿结束后另写 `rvf-reviewer-command-execution-result` version 1 回执，绑定 run、repo、reviewer、packet SHA256 和真实 returncode，并保留内容寻址历史。整轮登记中断后可先从这些原生回执恢复健康腿，只执行缺失腿；不推断或伪造退出码。旧补证子进程未确认退出时先等待精确进程组停止，再接续，避免新旧进程同时执行。

正常写入与备用接管共用 `normal_worktree_mutation` reservation；同 run 再用文件锁串行化。原子写入、fsync 和内容寻址保证发布后可复用。发布前以 bundle 和快照独立重放全部判据；提交后登记回执中断时，下次优先核验已有证明，不重复业务动作。JSON 已落盘但缺 sidecar 时，`record` 先独立重验，再补齐 sidecar；只读 `inspect` 不修复文件。

恢复记录含输入指纹、每阶段 attempt count、boot identity、状态、恢复条件和原始输出位置。同输入每阶段最多三次自动尝试；进程重启不清零。先核对已有双审/验证工件，再决定是否执行缺失步骤；原始材料或执行环境改变后重算输入指纹。预算耗尽保留机器等待，不转成人工授权，也不无限空转。

等待结果中的 `recovery.rvf_argv` 是精确重试命令；`triggers` 列出输入/环境变化、机器材料变化和重启后对账条件。正常等待及拒绝结果在 reservation 内持久化后释放 reservation；真实进程中断或结果持久化失败保留原协议的未知记录，供接管对账。

## 验收与配套方交付

`tests/test_supplemental_land_completion.py` 使用真 Git 及隔离状态根，包含 `8bd3c` / `199e6` 等价夹具及正负例。通过下面命令生成持久的消费方契约夹具：

```sh
python3 tests/test_supplemental_land_completion.py --export-contract-fixtures <持久空目录>
```

生成结果明确标为合成夹具，不用于真实任务记账。每组包含本地 Git 仓库、原 run、已生成证明、正例 verify argv，以及身份/摘要/漏审篡改负例。消费方只调用精确 `verify`，不自行放宽成 candidate evidence，也不以补证解除 Stop。
