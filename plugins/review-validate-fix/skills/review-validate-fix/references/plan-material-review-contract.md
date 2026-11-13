# 显式计划材料审阅合同

这是独立材料 lane，不从代码快照 lane 或 Stop Hook 自动切换。默认一名独立 reviewer，复用 RVF 的原生 CLI 适配、隔离只读工作树、所属进程身份、持久 Stop 与原始输出恢复；不强制每阶段评审、固定人数或评分。

```sh
rvf review-plan-materials --repo /absolute/project-repository \
  --material-file /persistent/implementation-plan.md \
  --requirements-file /persistent/requirements.md \
  --scope-file /persistent/review-scope.md \
  --run-directory /persistent/exact-plan-material-review
rvf observe-plan-material-review --run-dir /persistent/exact-plan-material-review
rvf resume-plan-material-review --run-dir /persistent/exact-plan-material-review
rvf cancel-plan-material-review --run-dir /persistent/exact-plan-material-review --reason '明确取消依据'
```

可重复 `--material-file`；需求与范围必须显式给出。路径都必须为绝对路径。计划、需求、范围按唯一源路径排序，统一生成 `sourceMaterialManifest: [{path,sha256,sizeBytes}]`。`reviewedMaterialDigest` 是该清单经 UTF-8、键排序、紧凑分隔符、保留非 ASCII 的 canonical JSON 后的 SHA-256。每份原件冻结到 `frozen-material-originals/`，不依赖后来的源文件内容。上下文冻结为仓库当前 `HEAD`，未提交源码不包含在只读上下文里；其身份另见 `repositoryContextCommit`。

`--prepare-only` 只冻结，不派发。可用 `--reviewer-config` 显式选择现有 RVF CLI 配置，首次冻结后不能通过重试换配置。省略时选当前可用默认配置的第一项。没有 `configuration_file_path`、且当前进程环境不能提供同名同值的内联 `env_set`，会在冻结前拒绝，避免密钥或路由静默丢失。运行目录是请求的幂等身份：原请求重放找回同一 `reviewRunId`；同目录换材料、范围、上下文或 reviewer 配置会拒绝覆盖历史。真正修订计划后另选明确的新评审目录，Factory 应重新绑定当前阶段和材料，不能把旧通过扩展到新材料。

调用直接返回当前状态。`observe` 严格只读、不恢复 worker、不派发模型。`resume` 对账原执行，复用已完成结果、尚活跃角色及已冻结但未派发的请求；原生调用已发生而结果未知时先补读原 stdout，不盲目启动替代模型。原 worker 的明确瞬时故障至多额外重试一次，预算和冷却跨重启保存。authentication、usage_limit、reviewer_unavailable 等已被共享执行器记为不可瞬时重试的失败，在原失败保留、剩余派发预算仍在、且既有资格门禁重新允许派发时可以继续有界尝试；这不替代结果未知的原调用。确定性故障、未完成的语义缺口保留原件、`validationGaps` 与恢复条件，调用者可继续独立工作。当前没有后台唤醒服务，需原 Agent 或外部调用者在条件变化后调用精确 `resume`。

停止先核对运行身份并落持久撤销，再取消所属进程；冻结原件或结果 JSON 损坏不能挡住这次撤销。材料仍可读时历史结果继续可读，返回状态为 `stopped`，晚到结果不撤销 Stop。中断前台 CLI 不等于撤销后台角色。

结果包含：

- `schemaVersion: 1`、`kind: "plan_material_review"`、`reviewRunId`、`runDirectory`。
- `reviewedMaterialDigest`、`sourceMaterialManifest`、`requirementsSha256`、`scopeSha256`、`repositoryContextCommit`。
- `status`：`prepared / running / completed / incomplete / stopped / unknown`。只有真实角色正常退出且有效 JSON，或原生结构化正常结束的等价补证，才能为 `completed`。
- `findings`、`dispositions`、`validationGaps`、`auditSummary`。初始材料审阅不替调用者处置发现，通常 `dispositions: []`；不运行自动修改计划的 fixer。
- `sourceResultFiles`、`completedReviewEvidence: [{path,sha256,sizeBytes}]`。后者绑定实际结果、原退出或补证、冻结角色请求、模型调用记录。由原 stdout 补证完成时，首次接受会保存不可变快照 `accepted-original-stdout.txt` 及 `accepted-original-stdout.json`（含快照和当时 `result.json` 的 sha256 与大小）。之后只有当前 stdout 与该快照逐字一致，才报告 `completed`，并把这份快照写入 `completedReviewEvidence`。当前 stdout 丢失、不再是结构化正常结束，或换成另一份仍正常结束但内容不同的事件流时，当前状态为 `incomplete`；`acceptedNativeCompletionEvidence` 仍指向原快照，`currentNativeStdoutMatchesAcceptedCompletion` 为 false，`historicalCompletion` 保留此前的接受记录。该拒绝不改写原 outcome，也不把新事件流当成原接受原件或另派模型。未完成结果保留原 `validation_gaps`、审阅摘要和结果原件路径。消费方必须读取原件并核对摘要、输入和实际结束事实，不能只信状态字符串。
- `sourceMaterialsAreCurrent` 表示当前源文件是否仍逐字匹配；后续变化保留历史完成，但不能获得新材料资格。
- `authority: "quality_facts_only"`。评审返回质量事实，不授予计划批准、实施、提交、合并、部署或结案权限。

`resume` 对账完成时保存 `plan-material-review-result.json`，可由 Factory 的当前阶段评估记录消费。当前 stdout 随后与已接受快照不一致时，再次 `resume` 写回当前的非完成状态，并在 `historicalCompletion` 保留上一份接受记录。Factory 另行绑定自己的 Run、StageAttempt、当前计划摘要和结论；RVF 文件不拥有 Factory 阶段，也不判断整个任务计划是否执行完成。本地文件与调用者属于受信任边界，哈希用于归属与变化核验，不是抵御恶意本地调用者伪造审批的认证服务。
