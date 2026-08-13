---
name: review-validate-fix
description: Use only when the user explicitly invokes $review-validate-fix, /review-validate-fix, or :review-validate-fix, or asks for the RVF post-work loop with review_only, validate_fix, skip-review, or no-handoff modes.
---

# Review Validate Fix

本 skill 只处理显式 `$review-validate-fix`、`/review-validate-fix` 或 `:review-validate-fix` 调用。`agents/openai.yaml` 的 `policy.allow_implicit_invocation` 必须保持 `false`。

不要因为上下文提到 review、fix、handoff 或 Stop hook 就自动启动 RVF。

## 正常入口

**Hook-prepared 路径（默认）**：进入会话第一条用户消息若含 `RVF_DISPATCH=token=...` 或显式触发字面量（`/review-validate-fix`、`$review-validate-fix`、`:review-validate-fix`），UserPromptSubmit hook 会自动调用 shared prepare 入口（`prepare_review_run.prepare_run_from_prep_file()`）。开始任何工作前先 `cat $RVF_PREP_FILE`（或在 hook 输出的 `prep_file_path` 上 `cat`），确认 `rvf_run.shared_workflow_state.status == "completed"` 且 `artifacts` 字段齐全；齐全则**跳过**下方第 1 / 5 / 6 步，直接 source `$RVF_REVIEW_ENV` 并按既有模式继续。Hook 自动写过的 `startup-scope-of-work.md` 是最小 stub，主会话仍必须用真实 reasoning 内容覆盖该文件或新写一份 scope-of-work（步骤 3）。

> Manual same-session 触发（`/review-validate-fix` 等字面量在已激活的对话里直接发出）走的也是这条路径，但因为 hook 不修改用户 prompt、也不向 agent 进程导出 `$RVF_PREP_FILE`，hook 会通过 `hookSpecificOutput.additionalContext` 把 `prep_file` 路径与 `shared_workflow_state.status` 注入到主会话上下文里。看到 `RVF dispatch prep (post-user-prompt manual auto-prep): - prep_file: …` 这段提示后再 `cat` 该 prep 文件、source 其中的 `review_env`，然后按既有 review 流程继续。

**Fallback / 无 hook 路径**（hook 失败、`shared_workflow_state.status` 是 `failed` / `timeout` / `pending`，或环境无 hook 注册）：

1. 在目标仓库运行 `git status --porcelain`，或使用 `scripts/review_validate_fix_gate.sh <repo>`。
2. 如果仓库 clean，只有用户本轮明确给出 manual review scope 时才继续；否则中文说明没有可审查改动并结束。
   - **clean worktree ≠ 无可审范围**：若本会话有「已提交但未审」的工作，manual UPS hook 会自动检测 committed-round、把 baseline 写进 prep payload，`prepare_review_run` 据此建 committed-round manifest、packet 用 `git diff <baseline>` 渲染已提交改动（`scope_mode=session-owned`）。**不要再用 `git reset --mixed/--hard` 把已提交工作物化成 dirty**——尤其多个连续 commit 时复原脆弱。bootstrap 残留：仅「该 task 从未跑过 review、首轮跨 prompt」时 baseline 可能解析不到（与 auto 路径同源残留）。
3. 写一份 scope-of-work / session context 文件，说明用户意图、本轮实际完成的工作或 manual scope、需要审查的文件、逐文件编辑明细、已跑验证、关键取舍和不确定点。
4. Scope-of-work 不要只列 created/modified/deleted 文件。
5. 若当前 prompt 或环境已提供冻结的 RVF run artifacts，复用既有 `review-env.sh`、scope contract 和 review packet；不要新建 run。
6. 否则运行 `scripts/prepare_review_run.py --repo <repo> --session-context <file>`；在 transcript 或 manifest 可用时传入对应参数；在已知主修改文件、manual scope 文件或背景 WIP 时，分别传入 `--primary-file` / `--background-file`，避免无 manifest 时降级到 `manual-all-uncommitted` 把背景 WIP 纳入 `primary_files` / `fix_allowlist`。
   - 审**已提交**工作（无父会话自动检测时的显式杠杆）：传 `--base-ref <commit>`（committed-round baseline）+ `--transcript <session.jsonl>`，packet 即用 `git diff <base-ref>` 渲染已提交改动（base-ref 须是 HEAD 祖先，否则自动回退 HEAD）。仍然不需要 `git reset`。
7. 后续使用脚本生成的 `review-agent-context.md` / `review-env.sh` / prompt artifacts；不要手写 `RVF_*` export block，也不要把脚本内部 env/config 当成 agent 需要记忆的状态机。

### Opt-in 仓库的预审 base 同步

- `prepare_review_run.py` 从 local base ref 对应提交里的 `AGENTS.md` 读取 `RVF-PRE-REVIEW-BASE-SYNC-SKILL:`，不依赖旧 task worktree 自己是否已有该标记。
- 若返回 `status=bootstrap_required_before_rvf`（退出码 3），先调用标记指定的单向同步 skill，再对同一 scope 重新 prepare；不要在 RVF 内持有 base lock，也不要绕过 gate。
- 这**不是失败，而是一个可恢复的等待态**。经 dispatch prep 进入时（rvf-agent `start` / kanban follow-up），
  prepare 返回 `status=waiting_local_base_bootstrap` 并附一枚 `bootstrap_gate` 子记录（见
  `scripts/rvf_bootstrap_gate.py`）：
  - `rvf-agent start` 以退出码 `6`（`bootstrap_required`）返回，token 保持 `acknowledged` 并挂上 run 锚点；
    跑完同步 skill 后 `rvf-agent start --token <t> --retry` **复用同一 run** 接续，不是第二轮独立 RVF。
  - kanban follow-up 的 in-progress 锁改记 `state=waiting_bootstrap`；锁 TTL 到期时会被**复活**而不是清掉重开。
  - 恢复前一律重新校验 base 身份 / worktree 与 git common-dir 身份 / receipt digest；证据不可信或冲突未解时
    维持等待（既不进 review 也不清等待态），只有等待期工件确实丢失才安全重来。receipt 首次校验通过后，
    其路径与 digest 会被**写回仍在等待的 gate**（token payload / 锁 marker），使后续每一轮真的能判出
    「receipt 被换过」——所以 bootstrap 完成后**不要**再跑一次同步 skill 或改动 receipt。
  - prepare 重放的结果按类分流，不是一个成功/失败位：`artifact_lost`（prep 文件已过 TTL / 被 sweep）
    立刻清等待态安全重来；`timeout` 与其余失败一样按**可重试等待**处理——重放跑在独立子进程里
    （内层预算到点自己写结果退出，外层硬上限按进程组 SIGKILL），收尾后不留在途 prepare 写者，
    故本轮释放在途占位、下一次 Stop 立刻重试同一个 run 的 prepare。
  - 并发去重靠起重放**之前**那次 `O_EXCL` 占位：占位只在一次重放调用期间存在，
    `RVF_KANBAN_FOLLOWUP_BOOTSTRAP_REPLAY_IN_FLIGHT_WINDOW_SECONDS` 是它的**接手**窗口（默认按
    重放预算 + 子进程强杀余量 + 余量推导），只用于持有者崩在半路时让下一个 Stop 接手。
- 调用同步 skill 时传 `BASE_BRANCH_SYNC_POST_ABSORPTION_REVIEW_OWNER=RVF`，声明吸收后的审查归 RVF。
- 单向同步只提供 bootstrap receipt 和 base delta 事实。其后的 implementation review 统一审查实现与该 delta，不再由同步流程重复做一轮完整语义审查。
- 未 opt-in 的仓库、旧 scope contract 和普通用户维持原行为。

## 模式

- `full`：默认完整流程，执行 prepare -> double review -> merge -> validate/fix -> handoff。
- `review_only`：只读 review pass，只写 canonical review result artifact，不修复、不 handoff。
- `validate_fix`：只处理主会话分配的 canonical issue 包，不重新 review、不扩大范围、不 handoff。
- `research_checkpoint_no_handoff`：不是 review loop，只输出用户要求的研究 checkpoint。
- 用户显式 `skip review` 时不启动 reviewer；若同时没有 issue list，则不进入 validate/fix。
- Handoff 默认开启；用户显式 `no handoff`、子 pass 或 research checkpoint 时关闭。

## Agent 边界

- 主会话负责 scope-of-work、模式选择、启动脚本、合并 reviewer artifact、分配 validate/fix 包和最终中文汇总。
- Reviewer 子代理使用 `prompts/reviewer.md` 的 self-contained prompt。它们不读取本 `SKILL.md`，不继承父线程历史，不读取另一路 reviewer 输出。
- Validate/fix 子代理使用 `prompts/validate-fix.md` 的 self-contained prompt。它们只处理分配给自己的 canonical issue 包。
- Review result protocol 的事实源是 `scripts/write_review_result.py` 和 `scripts/check_review_result.py`；自然语言 final prose 只作日志。
- Stop hook、backend selection、Kanban dispatch、GUI fallback、env/config 注入和 run ledger 字段由脚本拥有。正常 agent 不需要读取这些内部状态机，也不应把 env/config 手工传给脚本。

## Review

- 默认执行两个独立 review pass，除非用户显式跳过 review。
- 派发由脚本完成，且**与前台 Bash 的 600s 上限解耦**（一次彻底 review 可能 > 10 分钟）：`source review-env.sh` 后运行 `scripts/dispatch_reviewers.py --execute --detached`。它把整轮派发（解析主 dispatch harness、probe `reviewer-registry.json` 中的 harness、按路由规则选出**恰好两路 external reviewer**、并行派发、额度 reroute/cooldown）self-fork 进一个 detached tmux 线程并**立即返回**，stdout 打印 `RVF_DISPATCH_STATUS=<status.json 路径>` 与 `RVF_DISPATCH_LAUNCH=<launched|already_running|launch_failed>`。这是 host-agnostic 的（不用 Claude 专属 `run_in_background`），Codex / Claude 驱动的 Kanban task 行为一致。
- 随后**循环有界轮询**直到终态：`scripts/dispatch_reviewers.py --wait-status --status-path <上面打印的 status.json> --max-wait 480`，每次最多阻塞 480s（< 600s 上限，永不被砍断）。打印 `RVF_DISPATCH_STATE=running launch_status=<...>` 就再调一次继续等；打印 `RVF_DISPATCH_STATE=done launch_status=<...> returncode=<n> plan_status=<...> effective_reviewers=<k>/<n>` 即派发结束。选路 / reroute / cooldown 全在 detached 的 `--execute` 本体内完成，主会话不自行选择 harness、不拼装 CLI、也不把 in-harness subagent 当作 double-review 的一腿。
- **判「本轮 double-review 是否成立」只看 `plan_status`，不看 `returncode`。** `returncode` 只说 detached 派发进程怎么退出，`plan_status` 才说两腿到底有没有产出评审结论：
  - `plan_status=completed` 且 `effective_reviewers=2/2` → 双评审成立，继续。
  - `plan_status=failed`（含某腿登录过期/认证失败/崩溃而补位不足，reason `reviewer_leg_produced_no_review_result`；或额度耗尽类 `*_usage_limit_*`；两类同时发生则 reason 为 `reviewer_legs_failed_mixed_usage_limit_and_no_review_result`，此时**两类都要处置**，别只等配额恢复）→ **不要拿现有结论当双评审用**。stderr 的 `RVF_DISPATCH_PLAN_REASON=` 给出原因，读对应 reviewer 目录下 `reviewer.stderr*.txt` 排障，处置后重跑。reason 末尾带 `_with_review_request_pending` 后缀时，说明**除失败腿外还夹着一条 `kind: request` 腿**，必须连同下一条一起处置（fail-close 优先，但那条 request 同样得走 Contract Gate）。
  - `plan_status=blocked_on_reviewer_request`（reason `reviewer_review_request_pending`）→ 有腿返回 `kind: request`，这是待处理终态、不是故障也不是完成态 review。按 `references/review-merge-policy.md` 的 Contract Gate / Request Handling 满足、驳回或 spawn 子任务后，让 plan `review_request_pending_reviewer_ids` 里的**同一** reviewer 用同一 review packet 重试；request 不得进入 merge table。重试仍走**同一入口**：对同一 run 再跑一次 `scripts/dispatch_reviewers.py --execute --detached`（该终态以专用退出码 `3` 收尾，detached 幂等锁据此判定本 run **未**干净完成、放行重派，不会卡在 `already_running`），并带上本轮 plan `reviewer_selection.selection_seed` 的 `--selection-seed <seed>` 以复现同一对 reviewer。
  - `plan_status=partial` / `unknown` → 同样不成立，按上一条排障。
  - `returncode` 非 0 或 `launch_status=launch_failed` → 读 status.json 的 `error` 与同目录 `.dispatch-thread.log`。唯一例外是 `returncode=3`：它是 `blocked_on_reviewer_request` 的专用码（非故障、也非完成），按上一条处置即可。
- 轮询到 `done` 后，照常读取 `artifacts/reviewers/reviewer-plan.json` 与各路 reviewer artifact；**核对每一路都存在合法 `review-result.json`**（plan 的 `effective_reviewer_count` 是同一事实的汇总）。只在该 plan 报告 0 个 external 可用（`needs_last_resort_fallback: true`）时，才按 `references/zero-external-reviewer-last-resort-in-harness-fallback.md` 走最后兜底。
- Reviewer 必须以 `scope.contract.json` 为最终范围合同；session manifest 只是 ownership evidence / tracker audit context。
- scope contract v3 的 `review_kind=implementation` 仍审实现整体；`review_kind=base_drift_integration` 只审 `interaction_primary_paths` 所描述的 base/task 交互面，`base_delta_paths` 与 `task_change_paths` 是上下文事实，`candidate_evidence_path` / digest 是必须保持的父证据绑定。v1/v2 contract 继续按既有实现审查读取。
- `git diff HEAD` 是证据来源，不是默认 review scope。除非用户要求 full diff review，否则只审查合同范围及其直接连带影响。
- 每个 reviewer artifact 必须通过 `scripts/check_review_result.py` 校验；artifact 缺失、schema invalid、excluded path、clean/issues/request 混写都不能当作合格结果。
- 合并 reviewer artifact 时使用 `references/review-merge-policy.md`。

## Validate / Fix

- `kind: no_issues` 进入 clean path。
- `kind: issues` 进入 validate/fix；每条 issue 先验证，再通过 `rvf_fix_attempt.py stop --status fixed|false_positive|elevated|failed` 写入完成状态。
- 默认 `full` 流程中，只要有可解析 issue list，主会话必须至少启动一个 `validate_fix` 子代理，除非平台没有子代理接口、用户明确要求本地执行，或只剩机械收尾。
- 按根因、文件区域、测试路径或决策前提把 issue 分组；最终汇总要说明 validate/fix 分组和结果。
- `--status elevated` 必须通过 `--result-file <elevation-detail.json>` 写入升级详情；自然语言 final 只作日志。
- **elevated 不即时交开发者，先自解析、再延迟到末端。** 收到 reviewer / fixer 标记的 elevated concern 后，主会话先尝试自行决断：① 以自身 context 判断；② 依据不足则派 subagent 在代码库中找分辨/决策依据。只有 context + 代码库都不足以决断时，才真正记为 `elevated`（能决断的就地转 `fixed` / `false_positive`）。即便确属 elevated，也不当场打断流程去问，而是先把所有非 elevated issue 验证/修复完，到 workflow 末端再处理（见 Handoff）。

## 最终验证证据

- reviewer 的探索性命令仍按 `$RVF_COMMAND_LOCK` 协调，但不构成可供 landing 复用的最终验证证据。
- merge / validate-fix 完成后，主会话把最终 landing suites 按 handoff 顺序命名为 `handoff-validation-001`、`handoff-validation-002`……，并通过 `scripts/validation_result_cache.py run --repo "$RVF_REPO" --name <name> --rvf-run-dir "$RVF_RUN_DIR" -- <argv...>` 执行。这个 cache wrapper 自己就是该 suite 的唯一 single-flight lock，**不得**再套 `$RVF_COMMAND_LOCK`。
- handoff 的验证段记录与 cache record 完全相同的命令 argv / cwd；cache record、candidate content、命令或非秘密环境指纹任一变化都会自动失效。日志与 evidence 只保存 hash、状态和脱敏元数据。
- 不要在 reviewer、validate-fix、handoff 与 landing 各跑一遍相同 suite；同一 candidate 的后续阶段应调用同一 wrapper/name，让它返回 `cache_status=hit`。

## Handoff

- Handoff 默认写入当前 RVF run 的 `artifacts/handoff.md` 并持续更新。
- 最终回复第一行输出 `RVF_HANDOFF_FILE: <绝对路径>`，空一行后按 `references/handoff-template.md` 规定的固定分行标签结构追加极短中文摘要（不要挤成一段）；该模板是摘要结构的唯一详述处。Stop hook 会把该 marker 当作完成信号，run 结束时发送 OS 系统通知（不再自动用编辑器打开 handoff）；不要再手动调用任何「打开 handoff」脚本。
- **存在任何 `[elevated]` 时，只在所有非 elevated issue 验证/修复完、handoff 与相关 brief 更新完之后，才于 workflow 末端的最终回复里逐条把升级直接摆给用户（简述卡点 + 候选方向）**，绝不只塞进 handoff 正文靠两行标签摘要带过，也不在 review 一发现就当场打断流程去问——升级即「需用户决策」。当某条升级是只有用户能拍板的方案选择（trade-off）时，用 `AskUserQuestion` 把选项摆出来让其当场选。
- Handoff 只写确认过的事实，不把背景 WIP 或其他 session 的改动混成本轮工作。

## 拿 handoff 回到实现起点后的两条再入分支

用户把 RVF handoff 拿回「早先实现刚完成那一刻」时，成功判据是「**实现本身是否达成原始目标**」（用户实测决定，**与 RVF 的 fix 是否达标无关**），据此分两条互斥分支：

- **实现达标** → `$rvf-land`：在同一 worktree sanity-check future-self 已应用的修复并提交；不启动新的 review。
- **实现未达标 / 有问题**（用户带失败信号回来，无论是否粘贴 handoff）→ `$rvf-reopen`：先按「最近一次刚经过 RVF 的那次实现 run」武装一次性 rescope state（`scripts/rvf_rescope.py arm`），再修用户暴露的问题；修复带来的新增改动会让下一次 Stop 全量重审「该实现 units ∪ 本次 fix delta」（run-scoped 重开，不波及无关已审工作）。详见 `skills/rvf-reopen/SKILL.md`。

## 文档分层

- `prompts/`：脚本直接传给 reviewer 和 validate/fix 子代理的 self-contained prompt。
- `protocols/`：协议事实源索引；优先指向脚本/checker/schema，避免自然语言重复定义。
- `references/`：主会话正常执行时可能按需读取的材料，例如 review merge policy、review standards 和 handoff template。
- `internals/`：Stop hook、backend、Kanban、env/config、run ledger 等内部 workflow 说明；正常 agent 不读。
- `debug/`：脚本失败、用户要求排障或维护 runtime 时才读的 runbook。
- `setup/`：只用于用户明确要求配置或重配 external reviewer / MCP / agent 集成。
