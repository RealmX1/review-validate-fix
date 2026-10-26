# Reviewed candidate 的提交前资格与 scope expansion 契约

本契约对应 task `5de70`。普通候选继续使用 `rvf-reviewed-task-candidate-evidence` version 1；包含已应用范围扩展或已确认 protected/background 修改的候选使用 version 2。version 2 的消费者升级属于 `my-ai-setup` / `base-branch-sync`，本任务不修改或部署该仓库。

## 资格入口与状态

```sh
rvf land check-evidence --repo <worktree> --run-dir <run>
```

只读检查不执行验证，不 stage，不写 prepare plan、contract 或 evidence，不选择 latest run。JSON schema 为 `rvf-candidate-evidence-qualification` version 1；必含 `status` / `reason_code`，拒绝时还有 `reason`。`eligible` 与明确 skip 返回 0；资格拒绝 `refused_fail_closed` 返回 28。参数错误仍返回 2。

prepare 在 `candidate_evidence_qualification` 展示相同结果。commit 先处理实际需要的验证重放，再在 staging 前执行同源完整检查。缺失/过期的 merge dispositions 保留 exit 27，其余资格失败为 exit 28。验证自身失败、不可重放等仍使用已有 exit 21/22。显式保留无关 WIP 时 evidence 明确 skip，不强加资格门禁；跨 worktree 的 contract 指向另一 subject 时同样明确 skip。

完成计数来自两条有效 reviewer 结果、merge dispositions 与 source refs 交叉核对、canonical fix-issue、最新 attempt/result。attempt 按 `created_at` 排序；时间缺失或冲突、最新 attempt 未完成、fixed 未 applied、真实 failed/elevated 均拒绝，不退回较旧的 applied attempt。backlog 结论还需有建卡或持久 pending/去重事实。handoff 的双 COMPLETED 保留为必要条件；统计句与实际结果条目仅核对一致性，正文提及标签不算结果。

R3 的 `routing_rule=R3`、`needs_last_resort_fallback=true`、空 reviewers 与 planned/completed 状态只允许目录中的 `codex-mimic-reviewer-a` / `codex-mimic-reviewer-b` 两份 canonical 结果。两份均须通过公共 result protocol 校验；这不是允许单路审查或把 failed/partial plan 伪装为兜底。

## Version 2 扩展凭据

原 `canonical_scope` 的 `primary_files`、`fix_allowlist`、`task_change_paths`、`background_files` 不因扩展而改写。其并集仍是冻结的基础路径全集。合法补充集合只来自 canonical issue 中实际 applied 的 fixed patch；普通外扩须已声明且有理由，protected/background 修改须 fixer acknowledge 且主会话已确认。`--extra-path` 不能替代这些事实。

version 2 的 evidence 在 `scope` 新增：

| 字段 | 内容 |
| --- | --- |
| `applied_scope_expansion_receipt_path` | 原 run 下 `artifacts/applied-scope-expansion-qualification-receipts/<sha256>.json` 的绝对路径 |
| `applied_scope_expansion_receipt_sha256` | 该 canonical JSON 工件的 SHA256，另有同名 `.sha256` sidecar |

receipt 的 schema 为 `rvf-applied-scope-expansion-qualification-receipt` version 1，包含：

| 字段 | 校验要求 |
| --- | --- |
| `run_id` | 与 evidence review 身份相同 |
| `reviewed_scope_hash` / `reviewed_contract_sha256` | 绑定 restamp 前冻结 contract；发生过 restamp 时核验 `scope.contract.reviewed.json` 及其 canonical digest |
| `candidate_content_manifest_sha256` | 与 evidence subject 的内容摘要相同 |
| `expansions` | 非空、逐个可核验的应用事实列表 |

每个 expansion 含 `issue_id`、`attempt_id`、`paths`、`fix_patch_sha256`、`facts`、`facts_sha256`。`facts` 内嵌 canonical issue、attempt、result 快照；digest 按 canonical JSON 计算。需核对 run/issue/attempt 身份，attempt 为 applied、result 为 fixed，实际 patch 位于绑定 run 的该 attempt 目录且摘要相同。路径须确实位于 changed paths，满足声明或确认条件，内置工具托管路径保持拒绝。

消费者必须先校验 receipt 的位置、canonical bytes、sidecar、身份、快照及补丁，再将其 `paths` 加入冻结全集。落地 tip 必须继承最初 reviewed tip；`--no-renames` 的落地 delta 必须落在该并集中，rename 源、目的两端都检查。不得只相信一个扩大后的路径数组，也不得将无法理解的 version 2 按 version 1 接受。

## Canonical issue 主路径与原事件补证

task `72dbd` 将两种边界明确分开：attempt 的 `issue_paths ∪ fix_allowlist` 控制修改权限，冻结合同的四个路径集合控制 evidence 的范围扩展记账。canonical issue 的普通主路径可以直接修复；当它处于冻结范围外时，即使已处于 attempt boundary 内，stop 仍将其记入 `scope_expansion.expanded_paths`。

`declared_paths` 包含 CLI 的显式声明和 canonical issue 的有效主路径声明；新增 `canonical_issue_declared_paths` 标明后者。仅允许 `validate_then_fix` issue 的真实改动路径，并核对 canonical issue 与 prepare 时的路径身份。仅主路径自动声明时，理由可来自 canonical issue 的问题描述；其他边界外路径仍须通过 CLI 显式声明理由。protected/background/excluded 的双重确认、validate-only 禁修及工具托管路径拒绝不因主路径身份而豁免。冻结范围内但在 attempt 修改边界外的普通路径，也仍须显式声明。

`declared_paths_outside_attempt_boundary` 单独记录修改权限的外扩，参与补丁导出、告警和 apply 前的 baseline blob 漂移检查；它与 `expanded_paths` 不必相同。已审但不在 fixer 修改边界内的依赖，声明后仍须完整合回，也不能覆盖 prepare 后其他会话的修改。

已经 applied 的 attempt 可能保留相对 attempt boundary 生成的旧 `expanded_paths`。正式 `check-evidence`、prepare/commit 与 receipt reader 使用同一分类逻辑重新核对：遗漏路径必须同时属于 canonical issue、prepare 时登记的 issue 路径、result 的 changed paths 和该 attempt 的实际 patch。不能借此补齐其他未声明路径，也不能把尚未应用、validate-only 或身份不符的修复视为完成。

`rvf land check-evidence` 继续完全只读，资格 JSON 的 `recovered_canonical_issue_scope_expansion_paths` 列出实际补出的主路径。prepare/commit 沿用 version 2 evidence 与内容寻址的扩展资格 receipt，保留原始 issue/attempt/result 快照及 patch 摘要；不会改写原 attempt/result、patch、handoff 或冻结路径集合。正常 landing restamp 仍按原契约归档最初 reviewed contract。重复恢复复用相同凭据，干净且内容匹配时不增加实现提交。此规则按相同机器事实计算，不按工具旧版本设置放宽分支。

生产 run 只能在其所属 Task 按精确 run 恢复。本卡的原始 `rvf-review-start-2af6e3d18859ee08` 样本仅作只读核验；完整写入生命周期在隔离仓库中重放。若原候选后来已变化，其他资格门禁仍可能拒绝，不能把主路径判据修复当作对当前所有候选的通过证明。

外部 `base-branch-sync` reader 需要采用相同的 canonical issue 主路径补证判据。旧 reader 仅凭原快照中的 `expanded_paths` 核验，可能拒绝上述历史补证 receipt；不能修改快照来迁就它，也不能宣称本仓 land 成功意味着外部消费者已升级。本卡只交付本仓实现和对等更新契约，不修改或部署外仓。

本仓 reader 已实现以上 version 2 校验。task `5de70` 最初交付时核对的已安装 `base-branch-sync` 仅支持 version 1，当时必须明确拒绝 version 2；这是原交付时的状态，不代表后续消费者未升级。跨仓后续需要采用相同判据，并覆盖合法扩展、缺声明/应用/确认、receipt/patch 篡改、run/内容/原 scope 身份不符及 rename 越界的正负例。两个仓库正式安装并完成对等验收之前，B2 的跨仓快路径仍未交付。

## Restamp、重复恢复与实际结果

所有可预知的只读检查，包括已有 evidence、父链和 integration receipt，都在 contract restamp 或正式 evidence 写入之前完成。dry-run 仅在内存投影新 tip、contract 与父 evidence 归档路径，不制造临时 commit 或正式 evidence。资格输入在检查期间变化则拒绝；正式写入前再次核对工件与候选。

标准 restamp 保留最初 reviewed contract 与 scope hash provenance；重复写入沿用最初 reviewed tip。工作树干净且内容仍匹配已审候选时，prepare → commit 的 `nothing_to_commit` 分支正常生成 evidence，不增加实现提交；重复执行复用相同 evidence 与摘要。

commit 后仍需检查真实 HEAD/tree、内容摘要与 hook 带入路径。提交期间变更继续使用既有 exit 25/26，不能以提交前资格替代实际检查。提交后意外拒绝保留原提交并如实输出，不回滚或伪造成功。

commit 的结果以 `rvf-land-commit-execution-result` version 1 持久保存：`artifacts/rvf-land-commit-result.json` 为最近结果，同摘要命名的不可变历史位于 `artifacts/rvf-land-commit-execution-results/`。记录包含 run、worktree、提交前后 HEAD、内容摘要和真实结果。stdout 的 `land_commit_result_path` / `land_commit_result_sha256` 指向历史工件。结果工件写入失败时 stdout 保留真实提交/evidence 状态并给出 `land_commit_result_persistence_failed`，不得将该失败等同于“没有发生提交”。

这些结果不表示已合回 local main、已 push、已安装、已部署或已完成 PDV；也不替代 Kanban paused 的补证 API。
