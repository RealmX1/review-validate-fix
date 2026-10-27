# Review Merge Policy

## Reviewer Roles

每轮 review 必须有两个独立来源，且**默认两路都是 `dispatch_mode: external_cli` 的 external reviewer**。路由、probe、派发全部由 `scripts/dispatch_reviewers.py` 完成；主会话不自行选择 harness、不拼装 CLI、也不把 in-harness subagent 当作 double-review 的一腿。

- `alternative-reviewer:<harness-id>`：由 `dispatch_reviewers.py` 按 `reviewer-registry.json` 与路由规则选出的两路 external reviewer，例如 `alternative-reviewer:cursor-cli`、`alternative-reviewer:claude-code`、`alternative-reviewer:codex-cli`、`alternative-reviewer:kimi-code`、`alternative-reviewer:oh-my-pi`。**选腿池 = 可用集 − 主 harness，池内无偏序**：池恰为 2 时受迫配对，>2 时按记录在案的 seed 伪随机选二（R15/R20 之后已无「cursor 必选一腿」这类偏序，旧表述已退役）。唯一的偏序约束是同额度池互斥——`underlying_provider_account_quota_pool_identity` 相同的两条腿（今天是 codex 与 oh-my-pi，共用同一 Codex 订阅）不会被同时选中，除非池内已无异池组合。每路的精确 label / config / reviewer_id 以本轮 `artifacts/reviewers/reviewer-plan.json` 为准。
- 同 harness 双实例（仅一个 harness 可用时）：label 形如 `alternative-reviewer:cursor-cli#a` / `#b`，写入各自独立的 `artifacts/reviewers/<reviewer-id>/`。
- `codex-mimic-reviewer-a` / `codex-mimic-reviewer-b`：**绝对最后兜底**，仅当 `dispatch_reviewers.py` 报告 0 个 external reviewer 可用（plan `routing_rule: R3`、`needs_last_resort_fallback: true`）时才使用。setup 与触发条件见 `references/zero-external-reviewer-last-resort-in-harness-fallback.md`；正常路径不读该文件。

两个 reviewer 应并行运行，使用 clean context、同一个主会话 scope-of-work / session context 文件路径、同一份 `scope.contract.json`、同一份 review packet 和 `prompts/reviewer.md` 生成的 self-contained prompt，且不要互相读取对方输出。主会话不要把同一大段 scope 文本分别粘贴给两个 reviewer；把 `artifacts/inputs/` 下的文件路径交给它们读取即可。scope-of-work 必须说明主会话本 turn 实际完成的工作和逐文件编辑明细，不能只列 created/modified/deleted 文件。`scope.contract.json` 是机器可读范围合同；`primary_units` 非空时，reviewer 以 tracker unit scope 为主，session manifest 只作为 ownership evidence 和 tracker 审计来源。reviewer 的默认假设是另一个 reviewer 可能正并行工作；可能冲突的命令按锁规则处理。review packet/diff/status 用来核实和补足证据，不应成为 reviewer 推断 scope 的唯一来源；除非主会话明确要求 full diff review，否则 reviewer 不应把整个 `git diff HEAD` 当作 full-scope analysis 来源。

每个 reviewer 的 artifact 必须写到 `artifacts/reviewers/<reviewer-id>/`，至少包含 prompt、stdout、stderr、normalized、canonical `review-result.json`、`review-result.summary.json` 和 `reviewer.summary.json`。summary 只记录来源标签、输入路径、scope hash、pid/session id、return code、timeout/signal 和 probe history 路径等运行事实；不要做重型来源审计。合并前 reviewer A 的 output 不得进入 reviewer B 的 prompt 或上下文；若 prompt 已直接包含另一路 reviewer 的 finding/summary 或 `<subagent_notification>`，该 reviewer 应通过 result artifact 写 `context_request`，reason 使用 `need-clean-review-context`，让主会话用 clean context 重试。

reviewer 使用 `references/review-standards/reviewer.md` 作为 RVF 定制审查标准入口；复杂度、安全和性能风险可按需引用 `references/review-standards/simplification-subset.md`、`references/review-standards/security-subset.md`、`references/review-standards/performance-subset.md`。这些 standards 只影响问题判断，不改变 reviewer result artifact 契约。

派发由 `scripts/dispatch_reviewers.py` 单一入口完成：主会话 `source review-env.sh` 后运行 `scripts/dispatch_reviewers.py --execute --repo <repo> --review-packet <packet> --session-context <file>`，脚本据此解析主 dispatch harness（`RVF_MAIN_HARNESS` / `artifacts/inputs/main-harness.json` / transcript 探测，默认 codex；cursor 仅经显式覆盖）、对 `reviewer-registry.json` 中每个 enabled harness 跑 `run_alternative_reviewer.py --config <path> --preflight` probe（该 probe 是一次**真实模型调用**，故结论带 TTL 缓存复用：默认「可用」30 分钟、「不可用」5 分钟，缓存身份绑实际解析模型、有效配置、执行身份、CLI 可执行文件及 probe 模式；选路与派发共享同一有效探针，任一变化即失效；派发阶段某腿失败会当场作废它的缓存。缓存只复用真实 probe 的结论，**不**用更便宜的检查替代它——`cursor-agent status` 退 0 而 `-p` 退 1 那次事故正是后者的形状。`RVF_REVIEWER_HARNESS_PROBE_RESULT_CACHE_DISABLED=1` 可强制每轮重新 probe）、按路由规则 R0–R3 选出**恰好两路 external** reviewer，并并行复用 `run_alternative_reviewer.py` 执行内核派发，产出 `artifacts/reviewers/reviewer-plan.json` 与每路 `artifacts/reviewers/<reviewer-id>/`。各 harness 模板：Cursor 经 `cursor-agent -p --output-format stream-json --force --trust --sandbox disabled` 运行（runner 以 `cursor_stream_json` 提取最终文本，终止事件与 Claude 形状一致）；Claude Code 经 `claude -p --output-format stream-json` 运行（`claude_stream_json`）；Codex CLI 经 `codex --ask-for-approval never exec --json --ephemeral --sandbox workspace-write -` 运行（`codex_json`）；Kimi Code 经 `kimi --output-format stream-json` 运行（`kimi_stream_json`，prompt 经 `-p` 携带一段指针指令、正文落盘，见 `setup/mcp-setup-startup.md`）；oh-my-pi 经 `omp -p --mode json --no-session --no-extensions --auto-approve --model openai-codex/gpt-5.6-sol --thinking xhigh` 运行（`oh_my_pi_json`；`--no-extensions` 是它的递归防护，等价于 codex 的 `--disable hooks`）。Codex CLI reviewer 需要 workspace-write 是因为 RVF review protocol 必须写 `$RVF_REVIEW_RESULT` artifact；review prompt 和 scope contract 仍禁止源码写回、stage、commit 或 validate/fix。runner 会注入 `RVF_REVIEW_RESULT`、`RVF_WRITE_REVIEW_RESULT`、`RVF_CHECK_REVIEW_RESULT` 和 `RVF_REVIEWER_ID`。reviewer 可以读取仓库并运行测试/lint/build，但不能直接编辑、写入、stage、commit 或执行 validate/fix；写入 result artifact 是 review protocol output，不是 repo writeback。external reviewer 应自行完成审查，不要默认等待开发者手动协助。`run_alternative_reviewer.py` 使用可观测活动空闲超时：默认配置每 5 秒检查一次 stdout/stderr 活动，连续 300 秒没有新活动时返回 `RVF_EXTERNAL_REVIEWER_TIMEOUT` 和 exit code `124`；默认不设置总运行时上限，如果本机配置确实需要总上限，应使用宽松的一小时级别限制。对支持事件流的 CLI，默认配置应使用事件流输出刷新活动时间；stdout/stderr 只作为 diagnostic，canonical review outcome 来自 `review-result.json`。

只有当 `dispatch_reviewers.py` 报告 0 个 external reviewer 可用（plan `routing_rule: R3`、`needs_last_resort_fallback: true`）时，才退到 `references/zero-external-reviewer-last-resort-in-harness-fallback.md` 描述的 in-harness mimic 兜底（两路 `codex-mimic-reviewer-a` / `codex-mimic-reviewer-b`，仍是 double review，不要由主会话单独 review 后伪装成两路结果）。只有用户在本轮明确要求必须使用外部 reviewer、且不接受兜底时（`dispatch_reviewers.py --require-external`），才因无可用 external reviewer 而 fail-close。

### Probe 判据必须与真实评审入口同源

harness 的 `health_command` **必须走与 `command` 相同的入口**（同一子命令、同一组 flag，只把 prompt 换成一句最小指令），不能用旁路的 auth 查询命令代替。2026-08-11 的 run `rvf-20260811T170157Z` 上，cursor 的 probe 是 `cursor-agent status`（exit 0，文本里却写着 `unable to fetch user details`），真实评审入口 `cursor-agent -p …` 则报 `Authentication required` 退出 1——路由层据此把一个跑不起来的 harness 选进两腿，那腿派出去后不产出任何 review-result。**旁路命令的退出码不是真实可用性的充分判据。**

同理，探针**不得精简 flag**：曾试过省掉 cursor 的 `--force --trust --sandbox disabled`，立刻撞上 `⚠ Workspace Trust Required` —— 探针在任何未 trust 的目录都会非零退出，把**健康**的 harness 误判为不可用（RVF 每个 worktree 都是新目录）。少一个 flag 就换一道门，也就不再是同一条路径。

探针成本的量级要求：故障时应当**瞬时**失败（auth/trust 在启动即拒、不进模型），健康时只多烧一次极短会话。`health_timeout_seconds` 必须小于 `dispatch_reviewers.py --probe-timeout`（默认 60s），否则外层先超时、把健康 harness 判死。

注意 probe 覆盖的是 auth/启动层，**不**覆盖工具执行层——工具层坏掉的 build 仍能通过 probe，由评审过程中的 `CursorStreamActivityMonitor` 实时 fail-over 兜底（见 `scripts/cursor_stream_tool_layer_health.py`）。

### 腿失败检测 + 自动回退（reroute / cooldown / `fallbacks`）

**一条腿是否算数，只看它有没有产出合法 review-result**（`reviewer.summary.json` 的 `review_result_complete`，即 review-result 的 `kind` ∈ {`no_issues`, `issues`}），而不是看进程退出码——退出码只说进程怎么退出，不说有没有可用结论。plan 的 `effective_reviewer_count` 是这个判据的汇总；**缺一条合法腿即非法 double-review**，无论失败原因是什么。

失败分两类，处置不同。额度耗尽（usage limit / quota exhausted）与其它失败的区别在于：它跨 run 持续存在，而 auth 失效、启动失败、崩溃、超时之类只在本轮成立。因此 dispatcher 做**失败签名检测 + 双层回退**：

- **检测**：`run_alternative_reviewer.py` 仅在「无合法 review-result 的失败输出」上识别额度签名（codex_json 走 `error`/`turn.failed`/`thread.error` 事件；其它格式扫 stderr/stdout，绝不扫模型评审正文），命中则以专用退出码 **125 + `RVF_EXTERNAL_REVIEWER_USAGE_LIMIT`** flag 收尾，并在 `reviewer.summary.json` 记 `output_error_reason: usage_limit_exhausted`。
- **轮内 reroute（对任何失败腿，不只额度）**：`dispatch_reviewers.py --execute` 对**每条没产出合法 review-result 的腿**，在 `A' = 可用集 − 已冷却 − 本轮已失败` 上重调 `route()` 取替换 harness，**in-place 替换失败 slot、保持恰好两路**，并保留失败 leg 的 artifact 目录。替换 leg 的 `reviewer_id` 以 `-fb<slot>` 后缀防碰撞。每次替换记入 plan 的 `fallbacks`（每项 `{slot, from, from_reviewer_id, to, to_reviewer_id, round, reason}`，`reason` ∈ {`usage_limit_exhausted`, `no_review_result`}）。「本轮已失败」与「已冷却」是两个独立排除集——前者只在本轮成立，后者跨 run 持久；两者都必须排除，否则刚失败的 harness 会被立刻重选、reroute 空转。烧满 idle timeout 的悬挂腿（rc=124）同样走这条路自动补位，代价是最坏墙钟变长（仍在 `--total-timeout` backstop 内）。
- **跨轮 cooldown（仅额度类）**：撞额度的 harness 在 `~/.rvf/harness-limit-cooldown/` 记一条带 TTL 的冷却标记（默认 1h，能从错误文本解析到 provider 重置提示则以它为准；env `RVF_HARNESS_LIMIT_COOLDOWN_TTL_SECONDS` 覆盖默认）。后续轮的真实 probe 会跳过仍在冷却期的 harness 并发 `harness_limit_cooldown_active` warning（`--assume-available` 仅测试用、不应用 cooldown）。**非额度类失败不写 cooldown**——本轮的环境故障不该把 harness 禁到后续 run。
- **fail-close（不伪装 R3）**：补不上时 plan 一律 `status: failed` + 响亮 warning，**绝不**静默置 `needs_last_resort_fallback` 伪装成 0-external 的正常 R3 in-harness mimic，也绝不停在 `partial` 让主会话拿单路结论当双评审用。reason 码按失败类别：额度类发 `main_harness_usage_limit_exhausted` / `all_reviewers_usage_limit_exhausted`（probe 阶段全被 cooldown 清空则发 `all_harnesses_usage_limited`）；其它类发 `reviewer_leg_produced_no_review_result`（warning 里列出失败 harness 与退出码，指向对应 `reviewer.stderr*.txt`）。`fallbacks` 非空只说明发生过回退；canonical outcome 仍以 `reviewer-plan.json` 的 `status` / `effective_reviewer_count` 与每路 `review-result.json` 为准。
- **混合失败必须自报混合**：`all_reviewers_usage_limit_exhausted` 只在**所有**失败腿都是额度类时才发。一腿撞额度、另一腿认证失败/崩溃/无 artifact 时，reason 发 `reviewer_legs_failed_mixed_usage_limit_and_no_review_result`，且两类 warning（`*_usage_limit_*` 与 `reviewer_leg_produced_no_review_result`）都会出现——否则主会话只会去等配额恢复，同时需要修的非额度故障被整条藏住。
- **合法 `kind: request` 腿不是失败腿**：runner 对它以 rc=0 收尾并在 `reviewer.summary.json` 记 `review_request_pending: true`。dispatcher **不**换腿、**不**排除该 harness、**不**写 cooldown，而是把它作为待处理终态冒泡：plan 记 `review_request_pending_reviewer_ids`，无任何真失败腿时 `status: blocked_on_reviewer_request` + `reason: reviewer_review_request_pending`。若按 `no_review_result` 换腿，替换腿一成功 plan 就会变 `completed`，request 被静默丢弃、主会话再也不会按下面的 Contract Gate / Request Handling 处理它。
- **request 腿也不算「额度失败腿」**：`all_reviewers_usage_limit_exhausted` 的「全部」同样把 request 腿计在内——一腿撞额度、另一腿返回合法 `kind: request` 时该断言为假，不得发这个 reason。此时 fail-close 仍优先（`status: failed`、额度类 warning 照发），但 reason 在失败类基码末尾追加后缀 `_with_review_request_pending`（例如 `reviewer_usage_limit_unfilled_with_review_request_pending`、`reviewer_legs_failed_mixed_usage_limit_and_no_review_result_with_review_request_pending`），并同时发 `reviewer_review_request_pending` warning。reason 只有一个槽位，不自报混合的话主会话会照「等配额恢复」处置完就重跑，那条 request 从头到尾没进过 Contract Gate。
- **未完成不等于正常完成**：dispatcher 完成返回 0、失败返回 1、等待补证返回 3。普通入口自动复用精确 run 的计划并只续跑未完成腿；完成腿不重跑。
- **恢复入口**：`rvf redispatch-reviewers`（或绑定同一 run 的 `dispatch_reviewers.py --execute --detached`）。已有 plan 自动恢复，保持 reviewer 身份、选择 seed、候选和 Scope；重复入口由执行锁去重，恢复前重新核验 Stop、授权、候选绑定和在途状态。
- **`--dry-run` / `--plan-only` 对结果工件是只读的**：使用普通 `--dry-run` 预演未完成腿。真正执行前复制尝试检查点，canonical 原地保留。
- **等待侧不得只看 `returncode`**：`rvf wait` 摘要首行的 `returncode` 是 detached 派发进程自己的退出码，与「两腿是否都产出结论」是两回事。同一行的 `plan_status=` / `有效腿=k/n` 与每腿当前 canonical 结果才是判据，摘要末行直接给出判定（详见 SKILL.md「Review」节）。

## Contract Gate

合并前先校验每个 reviewer 的输出：

1. 用 `scripts/check_review_result.py "$RVF_REVIEW_RESULT"` 或等价解析器校验 canonical artifact。stdout/stderr 和 reviewer final prose 只作为 diagnostic，不作为 clean/issues/request 判定来源。
2. artifact 缺失、损坏、schema invalid、path 绝对路径或 `..` 逃逸、完成态含未解决 request 的状态时，该 reviewer 标记为 `CONTRACT_VIOLATION`。issue 落在 excluded path **不再**让整条腿作废（护栏而非闸门：那会触发换腿 / 重派，代价远大于一条 issue）：checker 只给 warning 并列出编号（`issue_indices_in_excluded_paths_one_based`），合并时这类 issue 只能 `dropped`（`rvf merge write` 强制）。
3. 如果 artifact 是 `kind: request`，这不是完成态 review；主会话应满足、驳回、spawn 子任务或提供上下文后重试 reviewer。request 不得进入合并处置（`rvf merge write` 拒绝 request 腿）。
4. 如果 reviewer 运行期间 `scripts/workspace_snapshot.py compare` 报告状态变化，标记为 `WORKSPACE_CHANGED_DURING_REVIEW`。这只是状态污染信号，不推断 reviewer 的意图，也不要自动 revert；测试缓存、报告、coverage、临时构建产物等可解释副作用不构成 review 契约违规。
5. `CONTRACT_VIOLATION` 的 artifact 不得进入合格 merge。`WORKSPACE_CHANGED_DURING_REVIEW` 需要主会话先检查污染范围；如果只是可解释测试副作用，仍可保留 reviewer artifact；如果出现未授权源文件、lockfile、snapshot 或文档写入，应 fail-close 或询问用户。

**完成判定读取 canonical 的合法结果、正常执行退出及请求处理状态。** `kind=request` 可以同时携带 findings；追加 issue 不解决请求。补证响应与 reviewer 的处理结果持久保存在 `requests[]` 中，全部 resolved 后才能写完成结果。canonical 保留所有 findings，归档只用于审计和尝试检查点；误报交给 validate/fix 判定。

## Merge Rules

合并两个 reviewer 的结果时，**每条 reviewer issue 都要落一条结构化处置**，经 `rvf merge write`（= `scripts/rvf_merge_dispositions.py write`，自动补当前 run 的 `--run-dir`）写成 `artifacts/merge/merge_dispositions.json`（带 `.sha256` 旁证；它取代旧的自由格式合并表）。主会话只给决定，脚本补全 run_id、每腿 canonical 结果哈希、issue 计数与每条 `issue_sha256`：

```bash
rvf merge write <<'EOF'
{"dispositions": [
  {"reviewer_id": "cursor-cli", "index": 1, "disposition": "adopted", "issue_id": "RVF-001"},
  {"reviewer_id": "codex-cli", "index": 2, "disposition": "merged_into", "issue_id": "RVF-001"},
  {"reviewer_id": "codex-cli", "index": 1, "disposition": "dropped", "reason_code": "style_or_preference_only"}
 ],
 "non_reviewer_fix_issue_sources": [{"issue_id": "RVF-002", "origin": "main_agent_self_review"}]}
EOF
```

- `reviewer_id` / `index`：`rvf wait` 摘要里的腿名与 issue 编号。**index 从 1 起**（工件里记为 `reviewer_issue_index_base: 1`），指向该腿**当前 canonical** `review-result.json` 的 `issues[index-1]`。
- `disposition`：`adopted`（成为该 fix-issue 的来源）/ `merged_into`（同根因，并入已被 adopted 的那条 fix-issue）/ `dropped`（必须带 `reason_code`）。每条 reviewer issue 恰好一条处置；每个 fix-issue 至少有一条 `adopted` 来源。
- `reason_code`：`style_or_preference_only`、`generic_refactor_suggestion`、`not_traceable_to_current_diff`（证据不足、无法复核；这三类对应规则 6）、`outside_review_scope_protected_background_or_excluded`（落在排除区，或落在其他会话**未提交**的背景 WIP 上）、`other_with_reason`（必须写 `reason`）。
- `non_reviewer_fix_issue_sources`：主会话自审发现、不来自任何 reviewer 的 fix-issue（`origin: main_agent_self_review`）。reviewer 也报了的同一问题记 reviewer 的 `adopted`，不在这里重复登记（两者互斥）。
- **处置绑定当前 canonical**：同腿续跑保留 findings 并可能追加发现、请求及处理历史。处置绑定 canonical 的 sha256；canonical 改变后，`rvf merge check` 与 upsert 拒绝过期处置，需要重新 `rvf merge write`。
- **强制点**：`rvf fix prepare` 与 `rvf_fix_issue.py upsert` 共用的 `upsert_canonical_issue` 在处置缺失、结构不合法、过期或未覆盖该 `issue_id` 时拒绝；candidate evidence writer 在 reviewer 报过 issue 时要求全量 `rvf merge check` 通过（每个处置目标都已 upsert、每个 fix-issue 都有来源），并以处置覆盖的 fix-issue 数核对 handoff 的 canonical 计数。**全部 dropped 时 canonical 计数 0 合法**：走 clean path（handoff 写「共 0 条」），处置照写。
- `rvf merge check` 缺省做全量核对；fix-issue 还没全部 upsert 时用 `--without-fix-issue-cross-reference` 只查结构、完整性与哈希。

规则：

1. 两边 artifact 都是 `kind: no_issues`：进入 clean path，不需要写处置。
2. 一边 `kind: no_issues`、另一边 `kind: issues`：只有报 issue 的那一腿需要处置。
3. 两边报同一根因或同一失败模式：合并为一个 fix-issue——一边 `adopted`、另一边 `merged_into` 同一个 `issue_id`。
4. 同一文件相邻行、同一测试失败、同一 API contract 或同一状态机不变量导致的问题：优先分组为一个 fix-issue（其余条目 `merged_into` 它），并在描述里列出涉及的 path/line。
5. 表面相似但修复方向不同的问题不要强行合并；各自 `adopted` 成不同的 fix-issue。
6. 只保留 bug、回归、遗漏边界、安全问题、错误假设、未完成实现和死代码；风格偏好、泛泛重构建议和证据不足无法复核的问题一律 `dropped` 并选对应 `reason_code`。
7. **与本轮工作不紧密相关、但真实存在的问题不要丢弃**：reviewer 标了 `[范围外]`（`scope_relation: outside_current_round_scope`）、或你判断它与本轮改动无直接连带时，照常 `adopted` / `merged_into` 成 fix-issue。`rvf fix prepare` 按来源 reviewer 的标注把它预填为 `fix_issue_handling_mode: validate_only_then_submit_to_kanban_backlog`：validator 只验证、不修，属实即提交 Kanban backlog 作为新 task。你的判断与 reviewer 标注不一致时，在 canonical issue JSON 里显式写 `fix_issue_handling_mode` 与 `handling_mode_override_reason`。主会话自审发现的范围外问题同样登记为 `non_reviewer_fix_issue_sources` 并显式写 validate-only 模式。

## Request Handling

`kind: request` 是非完成态，可同时携带 `issues[]`。展示端必须同时呈现发现和补证；完整双审完成前不得写 merge dispositions、candidate coverage 或推进 review_highwater。

1. runner 优先读取材料、标准子集和已有锁，继续不依赖缺口的工作；所需文件用 `context-request --target <路径>` 指定。
2. `respond-request --request-id ... --response ...` 保存响应；reviewer 处理后用 `resolve-request --request-id ... --resolution ...` 确认解决。响应本身不代表解决。
3. reviewer 用 `complete --audit-summary ...` 完成评审，保留全部 findings 和已解决的请求历史。
4. 普通恢复入口自动只重跑未完成腿。持久 `recovery` 记录等待条件、已尝试动作和每种条件的次数；同一条件最多自动重试一次，条件变化后重新接续。输入超限先改为文件引用，再以同腿持久检查点进行一次有界尝试；仍失败走现有替补路径。
5. 只有新增权限、真实价值取舍或人独有信息需要人工决定。失败、降级和诊断遵循 `developer_only` 契约。

## Source Tracking

来源只由 `artifacts/merge/merge_dispositions.json` 记录。fix-issue 的 `source_refs`（tracker `rvf_issues.source_refs` 列）由它派生，统一 schema：

- reviewer 来源：`{source_origin: reviewer_result_issue, reviewer_id, index, issue_sha256, disposition}`，`disposition` ∈ {`adopted`, `merged_into`}，`index` 从 1 起；
- 非 reviewer 来源：`{source_origin: main_agent_self_review}`。

canonical issue JSON 里写的 `source_refs` / `reviewer_id` / `source_reviewers` / `source_items` 等来源字段一律被 upsert 剥掉、不被信任。按 reviewer 统计采纳 / 丢弃 / 最终修复（Reviewer 效能评估）直接读处置工件与 `$rvf-analyze` 生成的 `causality.json::merge_dispositions`，不需要再从 handoff 文本反推。

来源可以出现在主会话合并摘要和 handoff 中，方便后续理解；但它不能传给 validate/fix 子代理。

## Validation Prompt Boundary

发给 validate/fix 子代理的 issue context 必须 source-agnostic：

- 不写 `alternative-reviewer:<harness-id>` / `codex-mimic-reviewer-*` 等任何来源标签。
- 不写“两个模型都认为”或“只有某模型发现”。
- 不写来源编号。
- 只给 canonical issue、相关 path/line、必要代码上下文、复现线索和 validate/fix 指令。

脚本侧的隔离：处置工件放在 fixer prompt 从不引用的 `artifacts/merge/` 下；给 fixer 读的 `artifacts/fix-issues/<issue_id>.json` 镜像不含任何来源字段；canonical issue 正文若提到本轮 reviewer 的精确来源标签（例如本轮 plan 里的 `alternative-reviewer:<harness-id>` label，或 mimic 兜底腿身份），upsert 直接拒绝。

这样 validate/fix 子代理只验证问题本身，不受来源模型声望、重复发现或分歧影响。
