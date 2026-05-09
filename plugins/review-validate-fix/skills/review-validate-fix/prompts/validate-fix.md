# Validate Then Fix Prompt

默认传给 validate/fix 子代理。主会话本地执行 validate/fix 只允许用于以下窄例外，并且必须在最终汇总和 handoff 中写明原因：当前平台没有可用子代理接口、用户本轮明确要求主会话本地执行，或子代理已返回 `REAL` 且只剩主会话可安全完成的机械收尾。

```markdown
pass_type: validate_fix

请用中文回复所有输出（verdict tag `REAL` / `FALSE_POSITIVE` / `ELEVATE`、文件路径、代码除外）。

给定一个 review issue：

1. 先读相关文件，验证 flag 的问题在当前代码里是否真的是一个问题。
2. 是真问题且你能独立修好：实施最小化修复，返回 `REAL`。
3. 不是真问题：不要改任何文件，返回 `FALSE_POSITIVE`，并简短说明为何不成立。
4. 是真问题但你不应该或不能独立修：不要改任何文件，返回 `ELEVATE`。例如需要架构决策、存在多种等价修复需要开发者拍板、涉及权限或 scope 外改动、原始需求不明确。

只处理主会话分配给你的 issue。不要重新执行 double review，不要主动寻找未分配问题，不要生成 handoff.md，也不要输出 reviewer 来源审计。`pass_type: validate_fix` 不是 full mode；即使上下文提到 `$review-validate-fix` 或 research checkpoint，也只返回 verdict 和必要的最小修复说明，不输出 `RVF_HANDOFF_FILE`。

你应收到 `scope.contract.json` 路径和 `scope_hash`。读取合同，把 `fix_allowlist` 当作默认可写范围。若最小真实修复必须修改 allowlist 外文件，先说明原因并让主会话决定是否扩大 scope；不要自行顺手扩大。allowlist 外 dirty changes、并行 agent 新增文件、reviewer liveness/probe artifacts、背景 WIP 或 protected files 都可能是预期存在的并行工作，不得清理、删除、格式化或顺手修复。

如果主会话提供了 RVF fix attempt 信息，你必须只在 attempt worktree 中工作，不要回到主 RVF worktree 修改文件。开始验证/修复前运行 `rvf_fix_attempt.py start --attempt-id <attempt_id>`；完成 verdict 前运行 `rvf_fix_attempt.py stop --attempt-id <attempt_id> --status fixed|false_positive|elevated|failed`。attempt worktree 是本次 issue 的 patch ownership 边界；不要写 handoff，不要清理主 worktree，也不要处理未分配 issue。

按 issue 类型读取 RVF 定制 standards pack 的相关子集：`references/review-standards/validate-fix.md` 是默认标准；复杂度问题读取 `simplification-subset.md`，安全问题读取 `security-subset.md`，性能问题读取 `performance-subset.md`。这些 standards 只用于验证和最小修复，不允许你扩大 scope 或重新 review。

如果当前只缺主会话可提供的专项标准、测量、受控子任务或上下文，可以先只输出 `RVF_STANDARD_REQUEST ...`、`RVF_MEASUREMENT_REQUEST ...`、`RVF_SUBTASK_REQUEST ...` 或 `RVF_CONTEXT_REQUEST ...`。request 是非完成态，不能和 `REAL` / `FALSE_POSITIVE` / `ELEVATE` 混写；主会话处理后会要求你重试。

返回结构化 verdict：

`[REAL | FALSE_POSITIVE | ELEVATE] <路径:行号> — <你做了什么 / 为何驳回 / 为何升级>`
```

## 子代理分配

- 在默认 `full` 流程中，只要 review merge 后存在可解析 issue list，主会话必须启动至少一个 `pass_type: validate_fix` 子代理处理验证包。
- 不得因为问题看起来简单、修复明显、reviewer 已给出修复方向或主会话已经理解问题，就跳过 validate/fix 子代理。
- 只有当前运行环境确实没有可用子代理接口、用户本轮明确要求主会话本地执行 validate/fix，或某个 validate/fix 子代理已返回 `REAL` 且只剩主会话可安全完成的机械收尾时，才允许本地执行；“为了省时间”或“问题很小”不是例外。
- 不强制一条 issue 一个子代理。
- 如果多条 issue 共享同一根因、同一文件区域、同一测试路径或同一决策前提，应合并为一个验证包交给同一个 validate/fix 子代理。
- 主会话等待 validate/fix 子代理完成时，应使用数分钟级到平台允许的较长 `wait_agent` / 等价等待，不用几十秒级短超时把正常验证误判为失败。
- 如果宽松等待到期仍未完成，主会话先发非侵入式进度 probe，询问当前阶段、已读文件、已改或计划修改的路径、正在运行的验证、阻塞点和下一步；probe 不得改变验证包、扩大 scope 或要求子代理提前给未验证 verdict。记录 probe 结果后再继续等待或处理明确阻塞。
- 验证包 prompt 必须说明：
  - 包含哪些 issue。
  - 为什么这些 issue 需要合并验证。
  - 输出仍要逐项给出 `REAL` / `FALSE_POSITIVE` / `ELEVATE` verdict。
- 验证包必须 source-agnostic：不要告诉子代理该 issue 来自 Codex、alternative reviewer、两个 reviewer 共同发现，或某个 reviewer 的原始编号。
- 子代理只接收 canonical issue、相关 path/line、必要代码上下文、复现线索和 validate/fix 指令。
- 子代理还应接收同一份 `scope.contract.json` 路径、`scope_hash` 和 `fix_allowlist`；验证包之外的 dirty changes 视为并行工作，除非主会话明确扩大 scope，否则不处理、不清理。
- 派发子代理前，主会话应把 canonical issue 写成 JSON artifact，运行 `rvf_fix_issue.py upsert --repo "$RVF_REPO" --run-dir "$RVF_RUN_DIR" --issue-file <issue.json>`，再运行 `rvf_fix_attempt.py prepare --repo "$RVF_REPO" --run-dir "$RVF_RUN_DIR" --issue-id <issue_id>`。子代理 prompt 必须包含 `attempt_id`、attempt worktree path、`RVF_RUN_DIR` 和原始主 repo path；子代理 cwd 应切到 attempt worktree。
- 子代理返回后，主会话用 `rvf_fix_attempt.py apply --attempt-id <attempt_id> --target-repo "$RVF_REPO"` 将该 attempt 的 `fix.patch` 合回主 RVF worktree；若返回 `merge_conflict`，记录为该 attempt 的未合并状态，不得手工搬运后伪装为已归因 patch。
- 子代理可以用 `RVF_*_REQUEST` 请求缺失标准、测量、受控子任务或上下文，但 request 本身不是 verdict，不得进入最终结果。

## ELEVATE 详情

每个 `ELEVATE` verdict 后必须追加：

````markdown
```elevation-detail
title: <短标题>
stuck_reason: <1-2 句说明为什么需要用户决策>
issue_restate: <1-2 句复述原始问题>
options:
  - A: <方案 + 权衡>
  - B: <方案 + 权衡>
  - C: <可选，第三个方案 + 权衡>
```
````

如果确实给不出候选方案，仍输出 `options:`，并写明 `候选方向缺失，请手动提供`。
