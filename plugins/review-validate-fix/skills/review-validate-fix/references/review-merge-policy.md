# Review Merge Policy

## Reviewer Roles

每轮 review 必须有两个独立来源，且**默认两路都是 `dispatch_mode: external_cli` 的 external reviewer**。路由、probe、派发全部由 `scripts/dispatch_reviewers.py` 完成；主会话不自行选择 harness、不拼装 CLI、也不把 in-harness subagent 当作 double-review 的一腿。

- `alternative-reviewer:<harness-id>`：由 `dispatch_reviewers.py` 按 `reviewer-registry.json` 与路由规则选出的两路 external reviewer，例如 `alternative-reviewer:cursor-cli`、`alternative-reviewer:claude-code`、`alternative-reviewer:codex-cli`。默认路由 = cursor + 非主 dispatch harness（主=Claude → cursor+codex；主=Codex → cursor+claude）。每路的精确 label / config / reviewer_id 以本轮 `artifacts/reviewers/reviewer-plan.json` 为准。
- 同 harness 双实例（仅一个 harness 可用时）：label 形如 `alternative-reviewer:cursor-cli#a` / `#b`，写入各自独立的 `artifacts/reviewers/<reviewer-id>/`。
- `codex-mimic-reviewer-a` / `codex-mimic-reviewer-b`：**绝对最后兜底**，仅当 `dispatch_reviewers.py` 报告 0 个 external reviewer 可用（plan `routing_rule: R3`、`needs_last_resort_fallback: true`）时才使用。setup 与触发条件见 `references/zero-external-reviewer-last-resort-in-harness-fallback.md`；正常路径不读该文件。

两个 reviewer 应并行运行，使用 clean context、同一个主会话 scope-of-work / session context 文件路径、同一份 `scope.contract.json`、同一份 review packet 和 `prompts/reviewer.md` 生成的 self-contained prompt，且不要互相读取对方输出。主会话不要把同一大段 scope 文本分别粘贴给两个 reviewer；把 `artifacts/inputs/` 下的文件路径交给它们读取即可。scope-of-work 必须说明主会话本 turn 实际完成的工作和逐文件编辑明细，不能只列 created/modified/deleted 文件。`scope.contract.json` 是机器可读范围合同；`primary_units` 非空时，reviewer 以 tracker unit scope 为主，session manifest 只作为 ownership evidence 和 tracker 审计来源。reviewer 的默认假设是另一个 reviewer 可能正并行工作；可能冲突的命令按锁规则处理。review packet/diff/status 用来核实和补足证据，不应成为 reviewer 推断 scope 的唯一来源；除非主会话明确要求 full diff review，否则 reviewer 不应把整个 `git diff HEAD` 当作 full-scope analysis 来源。

每个 reviewer 的 artifact 必须写到 `artifacts/reviewers/<reviewer-id>/`，至少包含 prompt、stdout、stderr、normalized、canonical `review-result.json`、`review-result.summary.json` 和 `reviewer.summary.json`。summary 只记录来源标签、输入路径、scope hash、pid/session id、return code、timeout/signal 和 probe history 路径等运行事实；不要做重型来源审计。合并前 reviewer A 的 output 不得进入 reviewer B 的 prompt 或上下文；若 prompt 已直接包含另一路 reviewer 的 finding/summary 或 `<subagent_notification>`，该 reviewer 应通过 result artifact 写 `context_request`，reason 使用 `need-clean-review-context`，让主会话用 clean context 重试。

reviewer 使用 `references/review-standards/reviewer.md` 作为 RVF 定制审查标准入口；复杂度、安全和性能风险可按需引用 `references/review-standards/simplification-subset.md`、`references/review-standards/security-subset.md`、`references/review-standards/performance-subset.md`。这些 standards 只影响问题判断，不改变 reviewer result artifact 契约。

派发由 `scripts/dispatch_reviewers.py` 单一入口完成：主会话 `source review-env.sh` 后运行 `scripts/dispatch_reviewers.py --execute --repo <repo> --review-packet <packet> --session-context <file>`，脚本据此解析主 dispatch harness（`RVF_MAIN_HARNESS` / `artifacts/inputs/main-harness.json` / transcript 探测，默认 codex；cursor 仅经显式覆盖）、对 `reviewer-registry.json` 中每个 enabled harness 跑 `run_alternative_reviewer.py --config <path> --preflight` probe、按路由规则 R0–R4 选出**恰好两路 external** reviewer，并并行复用 `run_alternative_reviewer.py` 执行内核派发，产出 `artifacts/reviewers/reviewer-plan.json` 与每路 `artifacts/reviewers/<reviewer-id>/`。各 harness 模板：Cursor 经 `cursor-agent -p --output-format stream-json --force --trust --sandbox disabled` 运行（runner 以 `cursor_stream_json` 提取最终文本，终止事件与 Claude 形状一致）；Claude Code 经 `claude -p --output-format stream-json` 运行（`claude_stream_json`）；Codex CLI 经 `codex --ask-for-approval never exec --json --ephemeral --sandbox workspace-write -` 运行（`codex_json`）。Codex CLI reviewer 需要 workspace-write 是因为 RVF review protocol 必须写 `$RVF_REVIEW_RESULT` artifact；review prompt 和 scope contract 仍禁止源码写回、stage、commit 或 validate/fix。runner 会注入 `RVF_REVIEW_RESULT`、`RVF_WRITE_REVIEW_RESULT`、`RVF_CHECK_REVIEW_RESULT` 和 `RVF_REVIEWER_ID`。reviewer 可以读取仓库并运行测试/lint/build，但不能直接编辑、写入、stage、commit 或执行 validate/fix；写入 result artifact 是 review protocol output，不是 repo writeback。external reviewer 应自行完成审查，不要默认等待开发者手动协助。`run_alternative_reviewer.py` 使用可观测活动空闲超时：默认配置每 5 秒检查一次 stdout/stderr 活动，连续 300 秒没有新活动时返回 `RVF_EXTERNAL_REVIEWER_TIMEOUT` 和 exit code `124`；默认不设置总运行时上限，如果本机配置确实需要总上限，应使用宽松的一小时级别限制。对支持事件流的 CLI，默认配置应使用事件流输出刷新活动时间；stdout/stderr 只作为 diagnostic，canonical review outcome 来自 `review-result.json`。

只有当 `dispatch_reviewers.py` 报告 0 个 external reviewer 可用（plan `routing_rule: R3`、`needs_last_resort_fallback: true`）时，才退到 `references/zero-external-reviewer-last-resort-in-harness-fallback.md` 描述的 in-harness mimic 兜底（两路 `codex-mimic-reviewer-a` / `codex-mimic-reviewer-b`，仍是 double review，不要由主会话单独 review 后伪装成两路结果）。只有用户在本轮明确要求必须使用外部 reviewer、且不接受兜底时（`dispatch_reviewers.py --require-external`），才因无可用 external reviewer 而 fail-close。

### 额度耗尽检测 + 自动回退（cooldown / `fallbacks`）

reviewer 背后的 agentic/coding 订阅额度耗尽（usage limit / quota exhausted）与 auth 失败不同：`--preflight`（`codex login status` 等）只查 auth，额度耗尽时仍返回 0。因此 dispatcher 改为**仅失败签名检测 + 双层回退**：

- **检测**：`run_alternative_reviewer.py` 仅在「无合法 review-result 的失败输出」上识别额度签名（codex_json 走 `error`/`turn.failed`/`thread.error` 事件；其它格式扫 stderr/stdout，绝不扫模型评审正文），命中则以专用退出码 **125 + `RVF_EXTERNAL_REVIEWER_USAGE_LIMIT`** flag 收尾，并在 `reviewer.summary.json` 记 `output_error_reason: usage_limit_exhausted`。
- **轮内 reroute**：`dispatch_reviewers.py --execute` 对「125 且 flag/summary 佐证」的每路，对 `A' = 可用集 − 已冷却 − 已失败` 重调 `route()` 取替换 harness，**in-place 替换失败 slot、保持恰好两路**，并保留失败 leg 的 artifact 目录。替换 leg 的 `reviewer_id` 以 `-fb<slot>` 后缀防碰撞。每次替换记入 plan 的 `fallbacks`（每项 `{slot, from, from_reviewer_id, to, to_reviewer_id, round}`）。
- **跨轮 cooldown**：撞额度的 harness 在 `~/.rvf/harness-limit-cooldown/` 记一条带 TTL 的冷却标记（默认 1h，能从错误文本解析到 provider 重置提示则以它为准；env `RVF_HARNESS_LIMIT_COOLDOWN_TTL_SECONDS` 覆盖默认）。后续轮的真实 probe 会跳过仍在冷却期的 harness 并发 `harness_limit_cooldown_active` warning（`--assume-available` 仅测试用、不应用 cooldown）。
- **fail-close（不伪装 R3）**：若 external 补不上且主/兜底 harness 自身也在冷却/刚撞额度，plan 直接 `status: failed` 并发 `main_harness_usage_limit_exhausted` / `all_reviewers_usage_limit_exhausted`（probe 阶段全被 cooldown 清空则发 `all_harnesses_usage_limited`），**绝不**静默置 `needs_last_resort_fallback` 伪装成 0-external 的正常 R3 in-harness mimic。`fallbacks` 非空只说明发生过额度回退；canonical outcome 仍以 `reviewer-plan.json` 的 `status` 与每路 `review-result.json` 为准。

## Contract Gate

合并前先校验每个 reviewer 的输出：

1. 用 `scripts/check_review_result.py "$RVF_REVIEW_RESULT"` 或等价解析器校验 canonical artifact。stdout/stderr 和 reviewer final prose 只作为 diagnostic，不作为 clean/issues/request 判定来源。
2. artifact 缺失、损坏、schema invalid、path 绝对路径或 `..` 逃逸、excluded path、clean/issues/request 混合状态时，该 reviewer 标记为 `CONTRACT_VIOLATION`。
3. 如果 artifact 是 `kind: request`，这不是完成态 review；主会话应满足、驳回、spawn 子任务或提供上下文后重试 reviewer。request 不得进入 merge table。
4. 如果 reviewer 运行期间 `scripts/workspace_snapshot.py compare` 报告状态变化，标记为 `WORKSPACE_CHANGED_DURING_REVIEW`。这只是状态污染信号，不推断 reviewer 的意图，也不要自动 revert；测试缓存、报告、coverage、临时构建产物等可解释副作用不构成 review 契约违规。
5. `CONTRACT_VIOLATION` 的 artifact 不得进入合格 merge。`WORKSPACE_CHANGED_DURING_REVIEW` 需要主会话先检查污染范围；如果只是可解释测试副作用，仍可保留 reviewer artifact；如果出现未授权源文件、lockfile、snapshot 或文档写入，应 fail-close 或询问用户。

## Merge Rules

合并两个 reviewer 的结果时，先保留一张主会话内部合并表：

| processed_id | canonical_issue | source_reviewers | source_items |
|-------------|-----------------|------------------|--------------|
| RVF-001 | <合并后的 issue 描述> | alternative-reviewer:cursor-cli, alternative-reviewer:codex-cli | A1, B2 |

规则：

1. 两边 artifact 都是 `kind: no_issues`：进入 clean path。
2. 一边 `kind: no_issues`、另一边 `kind: issues`：保留有 issue 的一边，source 只记发现该 issue 的 reviewer。
3. 两边报同一根因或同一失败模式：合并为一个 processed issue，source 记两个 reviewer。
4. 同一文件相邻行、同一测试失败、同一 API contract 或同一状态机不变量导致的问题：优先分组为一个 processed issue，并在描述里列出涉及的 path/line。
5. 表面相似但修复方向不同的问题不要强行合并；保留多个 processed issue。
6. 只保留 bug、回归、遗漏边界、安全问题、错误假设、未完成实现和死代码；过滤风格偏好、泛泛重构建议和无法追溯到当前 diff 的问题。

## Request Handling

`kind: request` 是非完成态协议，不是 finding。主会话处理 request 时应：

1. 校验 request artifact 没有和完成态输出混写。
2. 判断 request 是否仍在 session scope 内。
3. 按需提供 standards subset、命令锁、测量结果、上下文或由主会话 spawn 受控子任务。
4. 把 request、决策、命令或子任务结果记录到 run ledger。
5. 让原 reviewer 用同一 review packet 和新的 reviewer result artifact path 重试，直到返回 `kind: no_issues` 或 `kind: issues`。

默认由主会话 spawn 子任务。只有平台能继承 run id、scope、manifest、packet 和 no-handoff/no-review-loop 约束时，才允许最多一层 nested subagent。

## Source Tracking

主会话必须记住每个 processed issue 的来源：

- `source_reviewers`：本轮 `reviewer-plan.json` 派发的两路 `alternative-reviewer:<harness-id>`（如 `alternative-reviewer:cursor-cli`、`alternative-reviewer:codex-cli`）、最后兜底时的两个 mimic reviewer，或其中多个来源。
- `source_items`：原 reviewer 输出中的编号，例如 `alternative-reviewer:cursor-cli#1`、`alternative-reviewer:codex-cli#2`。

来源可以出现在主会话合并摘要和 handoff 中，方便后续理解；但它不能传给 validate/fix 子代理。

## Validation Prompt Boundary

发给 validate/fix 子代理的 issue context 必须 source-agnostic：

- 不写 `alternative-reviewer:<harness-id>` / `codex-mimic-reviewer-*` 等任何来源标签。
- 不写“两个模型都认为”或“只有某模型发现”。
- 不写来源编号。
- 只给 canonical issue、相关 path/line、必要代码上下文、复现线索和 validate/fix 指令。

这样 validate/fix 子代理只验证问题本身，不受来源模型声望、重复发现或分歧影响。
