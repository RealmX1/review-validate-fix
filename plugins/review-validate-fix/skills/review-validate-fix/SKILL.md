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

### 统一发起协议：唯一的发起消息是**发起决策点横幅**

自动 RVF 的发起**一律**先给你一份决策点正文，首行是 `=== REVIEW-VALIDATE-FIX 发起决策点：由你自主决定 start 或 decline ===`，正文含候选摘要 + `RVF_DECISION_TOKEN=<token>` + CLI 命令样例。是否发起由你决定，开发者已预先授权，不需要征询开发者：简析后用 `rvf start`（发起）或 `rvf decline`（谢绝）显式表态。**这条规则不分环境**——在不在 Cline Kanban task 内、Claude 还是 Codex，横幅的**正文与决策协议完全一致**。

**谢绝会在本会话 snooze 这片工作范围。** `rvf decline` 之后，同一会话、同一 worktree 的自动横幅会保持静默，直到出现工作边界：提交（HEAD 变化）、范围明显扩大（出现新路径**且**未审行数比基线多出 50% 以上）、同一批文件上的未审行数涨到基线的 3 倍**且**多出至少 150 行，或 12 小时到期。开发者发来新 prompt 不解除 snooze，只把「范围扩大 / 行数翻倍」的对比基线挪到那一刻（后台任务完成通知之类 host 自己合成的回合不算发话）。所以同一批工作谢绝过一次后不会再被反复问；开发者手动触发 `/review-validate-fix` 不受 snooze 限制，此时横幅会明说「用户推翻了你的谢绝」，不要照上次的判断再谢绝一次。

变的只是横幅**怎么送到你面前**（transport，与决策协议正交）：

| 在哪 | transport | 你看到的形态 |
| --- | --- | --- |
| **cline-kanban task 内**（默认） | 终端投递一行**边界标记** user message，正文由 RVF 的 UserPromptSubmit hook 随之注入 | 隔几秒到达并开启一个新回合：先是一行 `RVF 发起决策点 · RVF_DECISION_TOKEN=<token>（决策正文见随附的 hook 上下文；未随附即为重复提交，忽略本行）`，紧随其后的 hook 上下文里就是决策点正文 |
| task 外 / 非 kanban backend / 逃生口 | Stop hook 自注入 | 与 Stop 同一刻直接出现在上下文里 |

带外投递那条路上，正文多一行时效说明，其余逐字相同。五件必须知道的事：

- **Stop 那一刻你什么也看不到是正常的。** 默认短等为 0ms，横幅数秒后才作为 user message 到达。这不是降级——正因为它开启新回合，才有 rewind 边界。
- **只看到边界标记、没有随附正文 = 这是同一枚 token 的重复提交**（例如用户 rewind 后标记被放回输入框又发了出去）。正文只随首次到达注入一次；你还没表态的话，这枚 token 已随之作废，下一次 Stop 会重新征询。此时不要 start、不要 decline、也不要为此询问开发者，照这条消息里用户其余的话继续即可。若这其实是首次到达、只是正文没能随附（例如在你回合中途排队送达），下一次 Stop 会直接补发完整正文，届时照常决策。
- **只有确定失败才回落自注入。** 投递结论未知时本轮静默停、不回落，由投递意图在后续 Stop 对账（宁可晚问一轮，也不要两条通道各问一次）。回落发生时开发者旁注会明说「本轮已回落为 Stop hook 自注入」。
- **投出去却始终没成为 turn 的横幅会被撤回。** 那枚 token 进 `withdrawn` 终态，即便消息迟到落地，`rvf start` 也会硬失败——不要试图绕过它去手动开一轮。
- 逃生口：`RVF_DECISION_POINT_DELIVERY_TRANSPORT=stop_hook_self_injection|cline_kanban_task_chat_session` 可强制指定通道。

**用户 rewind 掉横幅之后**：那枚 token 当轮作废（移出去重索引、**不计** decline 冷却与铸造熔断），下一次 Stop 会重新征询一次。所以看到「同一批改动又被问了一遍」且中间发生过 rewind，那是设计行为，不是重复派发。

接受之后**不会再有第二条注入消息**。执行所需的一切从 `rvf start` 的回执里读：`detail.execution_handoff` 带 `target_repo` / `prep_file` / `review_env` / `review_packet` / `scope_contract` / `scope_of_work` / `review_agent_context` / `session_manifest` / `worktree_bootstrap` / `origin_metadata` / `handoff_path` / `intent_artifact_status_file` 与 handoff 契约；人类可读行与 `RVF_AGENT_RESULT_JSON:` 两种形态都有，回执末尾的 `next:` 行已按顺序排好、参数已填好，照抄即可。完整流程：

```
rvf start --token <decision-token> [--transcript-path <session.jsonl>] --reason "<一句话理由>"
rvf scope exclude|include|annotate --token <decision-token> …     # 可选
rvf scope rerender-scope-of-work --token <decision-token>        # 覆盖 scope-of-work 之后
rvf dispatch --token <decision-token> --strictness <light|standard|strict>
```

`rvf` 是部署装到 PATH 上的快捷入口（`~/.rvf/bin/rvf`，并链接到 `~/.local/bin/rvf`）。PATH 上找不到它时，横幅与回执会直接渲染它的绝对路径，照抄即可，不要 `--help` 摸索。

`--transcript-path` 直接照抄横幅里 `start` 那一行给出的值即可（横幅只在能解析出该路径时才渲染它）；你不需要自己去找 transcript 落在哪。省略也可以——铸 token 那一刻的路径已随候选摘要存进 token，CLI 会自行回退读回来。它决定本轮 scope 的口径：拿得到 ⇒ `scope_mode=session-owned` + 真 tracker lease + `primary_units`；拿不到 ⇒ 退回 `manual-all-uncommitted`（会把别的会话的背景 dirty 一并纳入，且**审完无法把这批单元标成 reviewed**，同一批改动可能被下一次 Stop 再提议一次）。`start` 回执的 `message` 与 `detail.tracker_scope_allocation` 会明说本轮是哪一种；看到降级就以 scope contract 的 `fix_allowlist` 为准，别按候选路径全量改。

`dispatch` 之后由**你自己**在当前会话调用 `$review-validate-fix` skill，按本文件就地走 review → merge → validate/fix → handoff。不要新建 Kanban task，也不要 fork 新会话；上面那些路径就是本轮 run 的全部输入，**不需要重新 prepare**。

`dispatch` 的退出码：`0` = 已派发（detached，接着按下面「Review」节等待）；`5` = 真派发失败，token 仍在 `acknowledged`，可修复后重试；`9` = scope-of-work 仍是 stub（先覆盖再 `rvf scope rerender-scope-of-work`）。`kind: request` 终态**不会**体现在 `dispatch` 的退出码上——detached 派发在 reviewer 跑完之前就返回了，它由 `rvf wait` 的摘要（`plan_status=blocked_on_reviewer_request`）报出。

> 例外只有一个：操作者显式设置 `CODEX_RVF_FORK_MODE=kanban-followup` 时，Stop hook 仍走旧的终端投递（注入一条 `$review-validate-fix` 开头的祈使句）。那是保留的逃生口/兼容入口，不是默认路径。

### 谢绝一轮**已派发**的自动 RVF

上面那条逃生口投递下来的祈使句（正文带 `RVF_DISPATCH=token=`）若你判断这一轮不该审（本轮改动与你正在做的事无关、用户已明确让你转做别的工作），**不要硬跑、也不要干停**，走谢绝出口：

```
rvf decline-dispatched-review --token <dispatch-token> --reason <一句话理由>
```

`<dispatch-token>` 取注入消息里 `RVF_DISPATCH=token=` 后面那串（**dispatch token，不是 S3 决策 token**；后者用的是 `rvf decline`）。它清掉 kanban-followup 锁、释放本轮 lease、把理由写进 rewind 抹不掉的磁盘 ledger。默认语义是「这一轮不审」：scope 退回可审池，后续 Stop 仍可能重新提议同一批改动。

确定这批 diff **根本不需要审核**时再加 `--mark-declined-scope-units-review-not-required`：后续 RVF 不再纳入范围。该标记是 **diff（hunk）级而非文件级**——被豁免的 diff 内容一旦被改动，会作为新改动重新进入审核范围。默认不加。

**退出码必须看**：`0` 才表示谢绝完全生效。非 0（`7 = decline_incomplete`）表示副作用只做成了一部分——例如锁属于**另一轮** dispatch 而被保留、lease 未释放、或请求了豁免却没声明成。此时**不要当作已谢绝**：读输出里的 `failed_steps` 与 `next_steps` 按提示补救，或如实告诉开发者哪一步没成。尤其是带豁免时，只要 exit 非 0，那批 diff 就**没有**被声明为「不需审核」，下一轮仍会被纳入范围。

> 在途锁的状态与你是否真的接受过这一轮有关，两条入口不同：
>
> - **逃生口投递**：UPS 投递落地只落 `delivered_awaiting_agent_decision`（只做互斥去重、**不催收尾**），Stop 侧确认这条 trigger 仍在当前 transcript 祖先链上才升格为 `in_progress`。
> - **默认路径（你自己 `rvf start` 接受）**：直接落 `in_progress` —— 你显式表过态了，不存在「收没收到」这一维。
>
> 两条都受同一套 rewind 对账保护：用户 rewind 掉那一段（trigger 或横幅离开祖先链）则锁被 reconcile 清掉。所以「被拽回去跑一轮自己从没见过的 RVF」不该再发生；若仍发生，是缺陷，请如实上报而不是照跑。

### Opt-in 仓库的预审 base 同步

- `prepare_review_run.py` 从 local base ref 对应提交里的 `AGENTS.md` 读取 `RVF-PRE-REVIEW-BASE-SYNC-SKILL:`，不依赖旧 task worktree 自己是否已有该标记。
- 若返回 `status=bootstrap_required_before_rvf`（退出码 3），先调用标记指定的单向同步 skill，再对同一 scope 重新 prepare；不要在 RVF 内持有 base lock，也不要绕过 gate。
- 这**不是失败，而是一个可恢复的等待态**。经 dispatch prep 进入时（`rvf start` / kanban follow-up），
  prepare 返回 `status=waiting_local_base_bootstrap` 并附一枚 `bootstrap_gate` 子记录（见
  `scripts/rvf_bootstrap_gate.py`）：
  - `rvf start` 以退出码 `6`（`bootstrap_required`）返回，token 保持 `acknowledged` 并挂上 run 锚点；
    跑完同步 skill 后 `rvf start --token <t> --retry` **复用同一 run** 接续，不是第二轮独立 RVF。
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
- 单向同步只提供 bootstrap receipt、base delta 事实，以及（若上游写了）一份 base↔task intent 复核 artifact；同步流程本身不再做一轮完整语义审查。
- 其后的 implementation review **仍是普通实现审查**：base delta 本身不进 finding 范围（那是 `review_kind=base_drift_integration` 轮次的职责）。intent artifact 以 context 形式注入 reviewer/fixer，用于**归因分类**与背景理解，**不**扩大也不缩小 finding 范围。
- intent artifact 缺失或非法时**一律标注放行、绝不阻断**：两侧部署节奏天然不同步，「文件没写出来」不等于「状态不安全」。结局落成机器事实 `intent_artifact_status`（`consumed` / `missing` / `invalid`），写进 run 工件 `base-task-intent-reconciliation-status.json`、handoff `## 状态`，并追加到 `$RVF_HOME/intent-artifact-consumption-outcomes/<repo>.jsonl`。
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

## 执行段速查：每一步一条 `rvf` 命令

token 与 run 目录都可省——`rvf` 按「显式参数 > `RVF_RUN_DIR` > 当前 cwd 所在 fix attempt worktree > 本会话本仓的 in-flight marker > 本 worktree 最近一次已接受的决策 token」自动推断（Claude Code 的 Bash 不保留 shell 状态，所以不要指望 `source review-env.sh` 跨调用生效）。没有决策 token 的 fallback 路径（直接跑 `prepare_review_run.py`）给 `rvf` 显式 `--run-dir <run_dir>`。解析不出当前会话 id 的 host（Kimi 等）上推断一律 fail-closed：回执里给出的命令已经带好 `--run-dir`，照抄即可；自己拼的 `rvf` 命令也带上（`rvf validate` 用 `--rvf-run-dir`）。

| 步骤 | 命令 | 需要你判断的点 |
| --- | --- | --- |
| 发起 / 谢绝 | `rvf start --reason "…"` / `rvf decline --reason "…"` | 要不要审 |
| 写 scope-of-work | 覆盖回执给出的 `scope_of_work` 文件，再 `rvf scope rerender-scope-of-work` | 本轮改了什么、为什么、审时盯哪里 |
| 派发 | `rvf dispatch --strictness <light\|standard\|strict>` | 严格度档 |
| 等 reviewer | `rvf wait`（按 host 的等法见下） | 无 |
| 合并 | 读 `rvf wait` 打印的摘要，按 `references/review-merge-policy.md` 合并 | 去重、分组、request 处置 |
| 建 fix attempt | `rvf fix prepare <issue.json>…` | 怎么分验证包 |
| 子代理收尾 | 子代理照抄派发参数块里的整行 `cd <attempt worktree> && rvf fix stop --attempt-id … --run-dir … --status …` | （子代理判断） |
| 合回 | `rvf apply --all-stopped` | 冲突怎么处置 |
| 最终验证 | `rvf validate --name handoff-validation-NNN -- <argv…>` | 跑哪些 suite |
| 看状态 | `rvf status` / `rvf results` / `rvf handoff` | 无 |

## Review

- 默认执行两个独立 review pass，除非用户显式跳过 review。
- 派发由脚本完成，且**与前台 Bash 的 600s 上限解耦**（一次彻底 review 可能 > 10 分钟）：`rvf dispatch`（无 token 的 fallback 路径是 `source review-env.sh` 后运行 `scripts/dispatch_reviewers.py --execute --detached`）把整轮派发（解析主 dispatch harness、probe `reviewer-registry.json` 中的 harness、按路由规则选出**恰好两路 external reviewer**、并行派发、额度 reroute/cooldown）self-fork 进一个 detached tmux 线程并**立即返回**。选路 / reroute / cooldown 全在 detached 本体内完成，主会话不自行选择 harness、不拼装 CLI、也不把 in-harness subagent 当作 double-review 的一腿。
- **等待不轮询，按 host 分流**（派发本身 host-agnostic，只有「怎么等」是 host 专属能力）：
  - **Claude Code**：用 Bash `run_in_background: true` 且 **`timeout: 3000000`** 跑**一次** `rvf wait`，然后结束本回合；进程退出时完成通知会唤醒你，通知里的输出就是合并摘要。不要轮询、不要 `sleep`、不要中途读 status.json。`rvf wait` 在 Claude 下默认等到 detached 派发的总超时（2700s）之后，一次就能等到终态；而后台 Bash 不传 `timeout` 时 host 缺省 30 分钟就砍掉它（上限 ≥ 2 小时），所以 `timeout` 必须显式给。等待期间 `rvf wait` 自己登记 awaiting marker，Stop 不会发续跑提示把你拉起来。等 validate/fix 子代理同理：用后台子代理 + 完成通知，不轮询（`rvf fix prepare` 已按 attempt 登记 awaiting marker，子代理 `fix stop` 或你 `rvf apply` 时清除）。
  - **Codex**：在 exec 会话里跑**一次** `rvf wait`（它一直阻塞到终态）；拿到 session 后用 `write_stdin` 空轮询，`yield_time_ms` 取 300000（Codex 空轮询的上限，见 `adapters/codex/README.md`），直到进程退出。等 validate/fix 子代理用 `wait_agent` 的长超时。
  - **Kimi 等其它 host**：`rvf wait` 单次最多阻塞 480s（< 前台工具 600s 上限），打印 `RVF_DISPATCH_STATE=running` 就再调一次。
- **`rvf wait` 到 done 时直接打印合并摘要**：`plan_status`、有效腿数、`fallbacks`，每条腿的 kind 与 issues（path / line / kind / severity / message）或 `audit_summary`、requests；没有合法结果的腿给出 `reviewer.stderr*.txt` 路径。摘要只读每腿 **canonical** `review-result.json`（不读 `superseded-review-result-artifacts/`），并用与 Contract Gate 同一个校验器 `scripts/check_review_result.py` 判定——runner 已经逐腿校验并落盘过，**不要再逐腿手动跑 check_review_result、也不要再去翻 reviewer 目录**。需要机器可读形态时加 `--json`；不等待、只看结果用 `rvf results`。
- **判「本轮 double-review 是否成立」只看 `plan_status`，不看 `returncode`**（摘要末行会直接给出判定）：
  - `plan_status=completed` 且有效腿 `2/2` → 双评审成立，继续。
  - `plan_status=failed`（含某腿登录过期/认证失败/崩溃而补位不足，reason `reviewer_leg_produced_no_review_result`；或额度耗尽类 `*_usage_limit_*`；两类同时发生则 reason 为 `reviewer_legs_failed_mixed_usage_limit_and_no_review_result`，此时**两类都要处置**，别只等配额恢复）→ **不要拿现有结论当双评审用**。按摘要给出的 stderr 路径排障，处置后 `rvf redispatch-reviewers` 对同一 run 重派。reason 末尾带 `_with_review_request_pending` 后缀时，说明**除失败腿外还夹着一条 `kind: request` 腿**，必须连同下一条一起处置。
  - `plan_status=blocked_on_reviewer_request`（reason `reviewer_review_request_pending`）→ 有腿返回 `kind: request`，这是待处理终态、不是故障也不是完成态 review。按 `references/review-merge-policy.md` 的 Contract Gate / Request Handling 满足、驳回或 spawn 子任务后，让**同一** reviewer 用同一 review packet 重试；request 不得进入 merge table。重派用 `rvf redispatch-reviewers --review-request-legs-only`（即 `dispatch_reviewers.py --execute --detached --redispatch-review-request-legs-only`，自动带当前 run 目录——不带 run 目录时 dispatcher 会静默新建一个 run）：复用上一轮 `reviewer-plan.json`，**只重派停在 `kind: request` 的腿**，健康且已完成的腿保留结果、不重跑也不被覆写。该模式不调用 `route()`，所以**不需要** `--selection-seed`；想先看会重派哪几腿，加 `--dry-run` 预演（不动任何 `review-result.json`）。
  - `plan_status=partial` / `unknown` → 同样不成立，按 failed 处置。
  - `launch_status=launch_failed` → 读 status.json 的 `error` 与同目录 `.dispatch-thread.log`。
- 只在摘要报告 0 个 external 可用（`needs_last_resort_fallback: true`）时，才按 `references/zero-external-reviewer-last-resort-in-harness-fallback.md` 走最后兜底。
- **lease 过期后重新分配必须走并集，不得静默换口径。** 若 reviewer 报 `lease_not_found` / lease 已过期（典型成因：这一轮被中断、隔了很久才回来），**不要**直接按当时工作树重新 allocate 一次了事——那会把原本圈定的那批改动整个踢出 packet，口径从 committed-round 悄悄翻成纯 dirty，最终审的可能 100% 是另一批东西。正确做法是把上一轮的 `tracker-scope.json` 传回去：

  ```
  python3 core/session_scope_allocation/reviewable_unit_diff_tracker.py allocate-review-scope \
      --repo "$RVF_REPO" --session-id <sid> --run-id <run> --reviewer-id <rid> \
      --union-with-prior-scope <上一轮 tracker-scope.json> --output-scope <新 scope 路径> --print-result
  ```

  它把上一轮 scope 里仍可审的 unit 并回本轮（已被真正审掉的不会复活），并从该 artifact 继承 `committed_baseline` / `committed_base_ref` 以恢复口径。结果里 `unioned_prior_scope_unit_ids` 与 events 里的 `scope_reallocated_after_lease_expiry` 是这次并集的证据；packet 的 `## Tracker Scope` 会标出承接自上一轮的那一段，reviewer 据此分辨两段的成熟度。
- Reviewer 必须以 `scope.contract.json` 为最终范围合同；session manifest 只是 ownership evidence / tracker audit context。
- scope contract v3 的 `review_kind=implementation` 仍审实现整体；`review_kind=base_drift_integration` 只审 `interaction_primary_paths` 所描述的 base/task 交互面，`base_delta_paths` 与 `task_change_paths` 是上下文事实，`candidate_evidence_path` / digest 是必须保持的父证据绑定。v1/v2 contract 继续按既有实现审查读取。
- `git diff HEAD` 是证据来源，不是默认 review scope。除非用户要求 full diff review，否则只审查合同范围及其直接连带影响。
- 每个 reviewer artifact 都经 `scripts/check_review_result.py` 校验（runner 逐腿执行、`rvf wait` 摘要同源复用）；artifact 缺失、schema invalid、excluded path、clean/issues/request 混写都不能当作合格结果。
- 合并 reviewer artifact 时使用 `references/review-merge-policy.md`。

## Validate / Fix

- `kind: no_issues` 进入 clean path。
- `kind: issues` 进入 validate/fix；每条 issue 先验证，再通过 `rvf fix stop --status fixed|false_positive|elevated|failed`（即 `rvf_fix_attempt.py stop`）写入完成状态。
- 默认 `full` 流程中，只要有可解析 issue list，主会话必须至少启动一个 `validate_fix` 子代理，除非平台没有子代理接口、用户明确要求本地执行，或只剩机械收尾。
- 按根因、文件区域、测试路径或决策前提把 issue 分组；最终汇总要说明 validate/fix 分组和结果。
- **fix 循环三步**：
  1. 把合并后的每条 canonical issue 写成 JSON，`rvf fix prepare <issue.json>…` 一次完成 ledger upsert + attempt prepare + start，并打印每条的**子代理派发参数块**（attempt_id、attempt worktree、run_dir、repo、scope contract、scope_hash、fix_allowlist、整行收尾命令）。把参数块原样放进 `prompts/validate-fix.md` 的子代理 prompt；子代理**不需要**再调 start。
  2. 子代理在 attempt worktree 内改完后，照抄参数块里的整行收尾命令 `cd <attempt worktree> && rvf fix stop --attempt-id … --run-dir … --status …`。不能靠 cwd 反推：Claude 子代理每次 Bash 调用都会重置 cwd。
  3. 全部子代理结束后 `rvf apply --all-stopped`：按 attempt 创建顺序合回所有已 stop 的 fixed attempt，逐条报告 `applied` / `merge_conflict` / `scope_expansion_conflict`，**遇到冲突就停下交给你判断**（merge_conflict 记为该 attempt 未合并，不得手工搬运后伪装成已归因 patch）。
- `--status elevated` 必须通过 `--result-file <elevation-detail.json>` 写入升级详情；自然语言 final 只作日志。
- **elevated 不即时交开发者，先自解析、再延迟到末端。** 收到 reviewer / fixer 标记的 elevated concern 后，主会话先尝试自行决断：① 以自身 context 判断；② 依据不足则派 subagent 在代码库中找分辨/决策依据。只有 context + 代码库都不足以决断时，才真正记为 `elevated`（能决断的就地转 `fixed` / `false_positive`）。即便确属 elevated，也不当场打断流程去问，而是先把所有非 elevated issue 验证/修复完，到 workflow 末端再处理（见 Handoff）。

## 最终验证证据

- reviewer 的探索性命令仍按 `$RVF_COMMAND_LOCK` 协调，但不构成可供 landing 复用的最终验证证据。
- merge / validate-fix 完成后，主会话把最终 landing suites 按 handoff 顺序命名为 `handoff-validation-001`、`handoff-validation-002`……，并在主 worktree 里通过 `rvf validate --name <name> -- <argv...>` 执行（即 `scripts/validation_result_cache.py run`，`--repo` 与 `--rvf-run-dir` 由 `rvf` 从当前 run 补上）。**不要**手写 `--repo "$RVF_REPO"` / `--rvf-run-dir "$RVF_RUN_DIR"`：Claude 的 Bash 里这两个变量是空的，记录进不了 run，land 时 exit 22、evidence 拒写。这个 cache wrapper 自己就是该 suite 的唯一 single-flight lock，**不得**再套 `$RVF_COMMAND_LOCK`。
- `rvf validate` 每次都显式带上当前 run 的 `--rvf-run-dir`（wrapper 只认显式参数，不回退 `RVF_RUN_DIR` 环境变量），并把该 suite 的 `{name, argv, cwd, environment_keys, validation_context_kind}` 按 name upsert 进当前 run 的 `artifacts/handoff-validation-commands.json`（带 `.sha256`）。这是 `$rvf-land` 补修正后重跑验证的唯一事实源，land 按它重放、不解析 handoff markdown，所以 argv 必须能脱离当前 shell 独立复现：工作目录用 `--working-directory`、需要的环境变量用 `--environment-key` 声明，不要写成 `cd x && …` 或 `VAR=1 …` 这类依赖 shell 状态的前缀。
- validate/fix 子代理在自己的 attempt worktree 里跑 `rvf validate`（或 reviewer 等 Stop hook 被抑制的子进程里跑）时，它只验证 cwd 所在 worktree、**不**写主 run 的验证记录——那只是 fixer 自己的检查，不是 landing 证据，land 也不会重放它。
- 失败时 wrapper 直接在 stderr 打印 stdout/stderr 尾部，结束时 JSON 里带 run validation index 摘要；不要为了看失败原因在缓存外再跑一遍，也不要再去翻 `validation-records.json`。
- handoff 的验证段记录与 cache record 完全相同的命令 argv / cwd；cache record、candidate content、命令或非秘密环境指纹任一变化都会自动失效。日志与 evidence 只保存 hash、状态和脱敏元数据。
- 不要在 reviewer、validate-fix、handoff 与 landing 各跑一遍相同 suite；同一 candidate 的后续阶段应调用同一 wrapper/name，让它返回 `cache_status=hit`。

## Handoff

- Handoff 默认写入当前 RVF run 的 `artifacts/handoff.md` 并持续更新（`rvf handoff` 打印落点与 `intent_artifact_status_file`）。
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
