# RVF 会话归属对 Bash 写入与子代理写入失明：历史复盘与系统性判定（2026-10-04）

- Kanban 任务：review-validate-fix `08ac4`（只读调查，未改 RVF 源码、未动 `~/.rvf` 运行态）
- 交接来源：cline-kanban 任务 4e79f，`/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/from-task-4e79f-rvf-ownership-attribution-blind-to-bash-and-subagent-writes-2026-10-03/`
- 统计脚本（只读、可重跑）：本目录 `rvf-session-owned-attribution-coverage-gap-history-readonly-statistics-script.py`
- 脚本全量输出（逐 run、逐漏审路径 JSON + 汇总表）：`/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/from-task-08ac4-rvf-session-owned-attribution-coverage-gap-history-statistics-output-2026-10-04-revised-after-rvf-review/`（`per-run-and-per-missed-path-statistics.json`、`summary-tables.md`）
- 统计生成时刻：**2026-10-04T11:56:47Z**。runs 目录和仍在进行的会话 transcript 都在增长，本文所有数字都是这一时刻的快照，之后重跑会得到略大的样本。
- 修订说明：本版是经 RVF 审查后的修订版。统计脚本改为按命令段只认真实写入目标、补全新版 Codex exec 信封解析、`--only-run-id` 不再提前过滤窗口、回执按 run id 绑定，全部数字已按新输出重算。旧输出目录（同名去掉 `-revised-after-rvf-review` 后缀）只作对照保留，本文不再引用。
- 代码基准：源码 HEAD = 本地 `main` = 部署副本 = `80b65c1`

---

## 结论

1. **这是系统性问题，不是偶发。** 机制上，一个改动文件进入正式审查范围，只能靠「主会话 transcript 里在 watermark 之后有 Edit/Write/MultiEdit/NotebookEdit 碰过它」，或者走几条与写入方式无关的旁路（已提交轮次默认归属、同一路径更早被 Edit 过而登记的路径级归属、无 tracker 时的 manifest 回退）。Bash 写入和子代理写入本身**从不**产生覆盖。历史数据与此吻合：
   - 主会话用 Edit/Write 写过的路径，98.7%（2521/2554）进了覆盖；
   - 只经 Bash 或子代理写过的路径，79%（892/1135）落在覆盖外；进了覆盖的 243 条里，202 条能由上面的旁路解释，剩 41 条集中在 8 个旧 Stop hook run（2026-06-27～08-14），新的 agent 自主发起路径里一条都没有。
2. **规模（启发式估计，误差双向，见 §6）。** 2026-05 至今，410 个能判定写入来源的 session-owned run 中，92 个（22%）有漏审。按 run 计漏审路径 924 条；按（仓库，路径）去重后是 **848 个文件、约 5.6 万行改动、110 个新建文件**，分布在 60 个 Kanban 任务里。其中 710 个文件在之后所有 session-owned run 里都没再进过正式覆盖。对「证据只来自 shell 命令解析」的漏审随机抽查 40 条：36 条可确认是真实写入，4 条存疑，0 条明确误判。
3. **问题在加速恶化，主要推手是写法的变化。** 只经 Bash/子代理写入的路径，在本会话写过的路径里占比从 2026-06 的 6% 涨到 2026-10 的 73%（2026-05 只有 1 条，不计）。这类路径一旦出现，6–9 月就有 61%–78% 落到覆盖外，10 月升到 94%。两者相乘，本会话写过的路径漏审率从 2026-06 的 4% 涨到 2026-10 的 **68%**。agent 自主发起路径最严重：68 个 run 里 46 个有漏审，漏审占比 53%。
4. **除了 Bash 和子代理，还有一种失明。** manifest 用「HEAD 提交时刻」做截断。worktree 被 fast-forward 到 base 上别的任务的提交后，本会话更早写下、从未提交的 Edit/Write 会被整段忽略。历史上有 7 个 run、32 个文件因此漏审，例如 `rvf-20261002T154116Z-agentic-initiation-start-4d6fd366`（task 203f3）漏了 16 个 Edit/Write 文件。另外，用户贴来的 d9ce2 进行中 run 表明，`mv` 搬家和从临时目录 `cp` 进仓库也会失明。
5. **沟通：绝大多数从没提过，少数提了但不显眼，还常被淡化。** 92 个有漏审的 run 里：
   - 67 个（73%）在 handoff 和最终回复里都没提；
   - 18 个只写在 handoff 正文里；
   - 4 个出现在最终回复的两行摘要之外；
   - 3 个出现在两行摘要里，其中只有 2026-10-03 那一次给出了「只正式审了 90 个文件中的 11 个」这样的覆盖数字。
   - 同时有 35 个 run 的 handoff 或最终回复声称审过的文件数超过实际覆盖数，例如 `55174d5f` 的两行摘要写「按 strict 档审了 71 个文件」，实际正式覆盖 22 个。dispatch 回执能绑定到本 run 的 38 个有漏审 run 里，36 个的回执写「primary_files N」且 N 大于覆盖数。
   - 结论：**2026-09 之前基本属于「从没提过」；2026-09 以后属于「提过但不显眼」，而且常被说成「不是漏审」或「reviewer 仍然全部审了」。** 结构化证据不支持后一种说法：reviewer 在漏审文件上提 issue 的比例是 2.1%（19/924），同一批 run 里正式覆盖文件的这一比例是 16.8%（160/950）。
6. **agent 一直知道，但信息留在了它自己的渠道里。** RVF 自己的 auto-memory 自 2026-08-19 起记了 4 次同类发生。2026-09-29 起，agent 在 11 个 run 里用 `rvf scope include` 补救，其中 9 个理由明写「归属对 shell 编辑失明」。但 include 只并进 `primary_files`，**被补的漏审路径无一例外仍不在 fix_allowlist、仍在 protected**。范围异常账本自动记下了 7 条 include 例外，给出的诊断却是「收紧 ownership filter」，方向与真实原因相反，而且只是本地 ledger，没有任何东西会把它报给用户。
7. **最小可行修复（建议）：**
   - 一是如实报告覆盖率（回执、handoff 自动段、两行摘要都带「正式覆盖 X/Y」），覆盖不全时要求显式确认。这一条不救回漏审，但能让 92 个 run 全部被看见。
   - 二是在 Kanban 单任务 worktree 里，以 git diff 为地面真相，把 unattributed 的脏改动默认归给本会话，历史上可救回 894/924 条。
   - 通用的根治方向是 Bash 前后快照（770 条）加读子代理 transcript（147 条）。
8. **兄弟任务冲突（需要你拍板）：** `b8962`（review 列，未提交）要把自动 Stop 决策点的候选集收窄为「仅本会话已证明归属」。按本调查的数据，这会让 Bash/子代理写的文件连横幅里都看不到，并可能拿掉「归属全空时退化成全量审查」这个意外兜底。建议先暂缓或改为只对 Codex「无本会话改动」的场景生效。

---

## 1. 机制复核：交接报告的结论在当前 HEAD 上是否成立

源码 HEAD、本地 `main`、部署副本都是 `80b65c1`。`session_change_manifest.py`、`reviewable_unit_diff_tracker.py`、`prepare_review_run.py` 与部署副本逐字节相同。交接报告 §3 的 21 条 `文件:行号` 断言由只读子代理逐条核对，行号全部在 ±10 行内。与交接报告有出入或需要补充的有以下几条（路径为仓库布局）：

| # | 交接报告的说法 | 当前 HEAD 的实际情况 |
|---|---|---|
| 1 | bootstrap 确认闸（`rvf_bootstrap_confirm.py`）只接在旧 Stop 派发路径，agent 自主 start 路径没有 | **两条路径都不生效**：`plugins/.../scripts/stop_review_validate_fix.py:5278` 的 `maybe_block_dispatch_on_bootstrap_size` 第一句就是无条件 `return`（注释：S5/R22 已废除该闸，把关移交给发起决策点）。`RVF_BOOTSTRAP_CONFIRM_THRESHOLD_*` 在任何路径上都不起作用 |
| 2 | prepare 时 `primary_files` = tracker 覆盖路径 | prepare 阶段如此（`prepare_review_run.py:1615-1670`）。但 agent 自主发起走到 dispatch 时，`finalize_scope_contract`（`rvf_agent_decision_cli.py:2399-2450`，调用于 2772-2773）会把合同的 `primary_files` 改写成「候选摘要纳入路径 − exclude ∪ include」，其中含 unattributed。这正是 dispatch 回执「primary_files 90」高估的来源（回执文本在 `:2897`）。另外，manual-all-uncommitted 的触发条件是既没有 transcript 也没有 `--session-manifest`，而且没有 primary/background/exclude 参数，不只是「不带 transcript」 |
| 3 | 分配只取本会话名下 available 单元 | 另有 `_union_prior_scope_unit_ids_in_txn`（`reviewable_unit_diff_tracker.py:5210-5247`，在 6022 行调用）会把上一轮 scope 里仍 available 的单元并回来，这一步不受会话归属约束 |
| 4 | PostToolUse 只匹配 `Edit\|Write` | matcher 实际是 `Edit\|Write\|MultiEdit\|NotebookEdit`（`plugins/review-validate-fix/hooks/hooks.json:27`），用途结论不变：只用来 park Kanban 卡，不登记 hunk |
| 5 | （补充）land 的边界 | `rvf_land.py` 的 `land_stageable_path_universe`（270-327）有两道排除，豁免规则不同：scope contract 的 `protected_files ∪ background_files` 对 `primary_files ∪ fix_allowlist ∪ task_change_paths` 内的路径豁免（323-326）；handoff intake hints 解析出的 `intake_hints.protected_paths` **没有这条豁免**（313-317、322，docstring 第 288 行原话「handoff 标注的 protected 路径没有例外」）。所以「既在 primary_files 又在 protected」的 unattributed 路径，**只有在 handoff 没把它标为 protected 时**才会在 land 时被自动 stage，那时审查没覆盖到，提交却会带上。实际风险取决于 handoff 怎么写：模板（`references/handoff-template.md:32`）要求把不得自动 stage 的路径列进该段，但这一段由 agent 手写，可能漏列；而且 handoff 这道排除是逐字精确匹配，模板示例的 `<path>: <原因>` 写法（同文件 `:97`）会被 `rvf_handoff_intake.py:421-431` 连同原因存成一整条，对不上路径。也就是说，只有写成裸路径的条目才挡得住 |
| 6 | （新发现）截断点 | `session_change_manifest.py:628-683` 在找不到 git commit 行时按 **HEAD 提交时刻**截断（`mode=head_commit_time`）。HEAD 若是 fast-forward 进来的 base 提交，本会话更早的、未提交的 Edit/Write 会被整段忽略，见 §2.5 |
| 7 | （新发现，来自 d9ce2）路径改名 | Bash `mv` 或 `cp` 落地的新路径从没被 Edit/Write 过，所以没有归属；manifest 的 owned_paths 里反而留着已不存在的旧路径和已删除的文件 |

其余断言（Claude 的 `Bash` 在 `claude_write_path` 返回 None 后直接跳过；`command_path_candidates` 只用于 Codex `exec_command`；`edit_claims` 只为 Codex `apply_patch` 生成；子代理 transcript 不进归属；packet 里背景 untracked 只列路径；reviewer prompt 以 `primary_units` 为准；fix attempt 只叠加 `issue_paths ∪ fix_allowlist`；handoff 模板不报覆盖缺口；`scope include` / `annotate` / `--force-full-rescope` 的局限）在当前 HEAD 上都成立。

**兄弟任务在途改动**（只读查看，没碰它们的 worktree）：

- `b8962`（review 列，「无 session-owned change 时误纳入 unattributed dirty」）：在 `stop_decision_candidate_summary.py` 加了 `candidate_ownership_scope`，让自动 Stop 只审 `current_session_owned`。**这与本调查冲突**，见结论 8 与 §5.4。
- `72e76`（review 列）：主张把 fixer 的「可修改边界」和「能看到的当前状态」分开，对应 §5 候选 7。
- `e347a`（backlog）：对比两套归属系统，可作为根治方案的评估框架。

---

## 2. 历史复盘

### 2.1 样本与口径

- 数据源：`~/.rvf/user-local-data/run-logs/runs/*` 共 9005 个 run 目录，其中 622 个有 `artifacts/inputs/scope.contract.json`。按 scope_mode 分：session-owned 491、manual-all-uncommitted 88、custom 43；后两种按合同全量审，不会漏审，只作对照。（统计时刻 2026-10-04T11:56:47Z。）
- 能判定写入来源的：410 个（另有 80 个 transcript 已不存在，1 个是 cursor 宿主）。15 个 Codex run 全部可判定。
- 「候选」= primary ∪ protected ∪ background。「正式覆盖」= tracker scope 路径，也就是 `primary_units` 所在路径，等于 `fix_allowlist`；没有 tracker 时用 `fix_allowlist`。
- 「写入来源窗口」= 同一 transcript 上一个带合同的 run 开始时刻 → 本 run 开始时刻；第一个 run 取整个会话。窗口始终用全部合同历史建立，`--only-run-id` 只筛选输出，单跑结果与全量逐字段一致（已对 `1a6ccb97`、`55174d5f` 核对）。
- 写入来源判定：
  - 主会话从 `Edit`/`Write`/`MultiEdit`/`NotebookEdit` 的 `file_path` 读取；子代理读 `<session>/subagents/agent-*.jsonl`，规则相同。
  - Bash（含子代理 Bash 与 Codex `exec_command`）先按 `&&`、`;`、`||`、`|`、`&`、换行、括号切成命令段。heredoc 正文归属引出它的那一段；引号、`$( )`、heredoc 正文里的 `>`、`;` 不当作 shell 语法。每段只认真实写入目标：重定向和 `tee` 的目标（不含 `2>&1`、`>/dev/null` 和仓库外路径）；`sed -i`、`perl -i`、formatter 写模式、`rm`、`touch`、`git rm`/`restore`/`checkout` 的文件参数；`mv` 的源与目标；`cp`/`install` 的目标；heredoc 补丁正文里的文件头。相对路径按记录里的 cwd 加段内 `cd` 解析。
  - python / node 内联脚本：如果每个写调用的目标都能还原成字面量路径（直接写、`Path(...)`、`ROOT / "..."`、只被赋过字面量的变量、遍历字面量列表），就只认这些目标；还原不了时，退回「正文里以完整单行字符串字面量出现的候选路径」。后者仍是启发式，有 144 条漏审属于这一类。
  - Codex：读 `apply_patch`、item_completed 信封里的 `FileChange`、旧 schema 的 `patch_apply_end`。新版 `custom_tool_call` name=`exec` 的 input 是 JS 文本，先解码 JS 字符串，再抽 `tools.apply_patch` 的文件头和 `tools.exec_command` 的 cmd。cmd 来自数组或循环变量时，取该段 JS 里所有像命令的字符串。抽不出的 Codex run 记为不可判定；本次没有这种 run。
- **漏审** = 不在正式覆盖内，且本会话在窗口内写过；按成因分为「只经 Bash/子代理写入」和「Edit/Write 早于 HEAD 提交时刻截断」。每条 shell 来源的漏审都带触发命令摘录（`shell_write_trigger_command_excerpt`）与 transcript 行号，供抽查。
- 未覆盖、而且整个会话里都找不到本会话写入证据的路径（992 条），一律**不**计入漏审。它们可能是别的会话写的、会话前遗留的，也可能是批量命令（`git diff --name-only | xargs biome check --write`）间接改的。所以漏审数是**启发式估计**，误差双向：这一类让它偏少，内联脚本启发式等让它偏多，见 §6。

### 2.2 「条件 → 结果」对照

**路径级**（410 个可判定 run，窗口内的写入来源 → 是否进入正式覆盖）：

| 写入来源 | 路径数 | 进覆盖 | 未覆盖 | 未覆盖占比 |
|---|---|---|---|---|
| 主会话 Edit/Write | 2554 | 2521 | 33 | 1% |
| 主会话 Edit/Write，但早于 HEAD 提交时刻截断 | 32 | 0 | 32 | 100% |
| 只经主会话 Bash | 959 | 214 | 745 | 78% |
| 只经子代理 | 143 | 21 | 122 | 85% |
| 子代理 + 主会话 Bash | 33 | 8 | 25 | 76% |
| 窗口内没写、会话更早写过 | 1527 | 1210 | 317 | 21% |
| 本会话没写过 | 1234 | 242 | 992 | 80% |

**run 级**：

| 窗口内是否有「只经 Bash/子代理写入」的候选路径 | run 数 | 有漏审 | 无漏审 |
|---|---|---|---|
| 有 | 121 | 92（76%） | 29 |
| 无 | 289 | 0 | 289 |

**反例 A：只经 Bash/子代理写入，却进了覆盖（243 条）**，成因由脚本自动拆解：

| 成因 | 条数 | 说明 |
|---|---|---|
| 同一路径在会话更早时被 Edit/Write 过 | 57 | 路径级归属会展开成该文件当前所有 hunk（`reviewable_unit_diff_tracker.py:1222-1235`），之后的 Bash 改动搭了便车 |
| 已提交轮次默认归属 | 121 | committed-round 的路径全部默认归本会话（`session_change_manifest.py:1238-1271`） |
| 无 tracker scope，fix_allowlist 来自 manifest | 24 | 旧版本 |
| 未解释 | 41 | 全部是旧 Stop hook run（8 个，从 06-27 的 `rvf-20260627T195627Z-stop-hook-b0a97578` 到 08-14 的 `rvf-20260814T174332Z-stop-hook-982cbc88`；单个 run `rvf-20260728T140911Z-stop-hook-7b52be60` 占 26 条）。该 run 的 manifest 本轮 owned_dirty 为 0，但 tracker scope 带着 311 个单元 / 65 个路径，像是早先登记的路径级归属延续下来。清单见汇总表「未解释的 run（A）」 |

所以反例 A 并不推翻结论：没有任何一条证据表明 Bash 或子代理写入本身能产生覆盖。

**反例 B：主会话 Edit/Write 写过，却没进覆盖（65 条）**：32 条是 HEAD 提交时刻截断（§2.5），1 条是被别的会话认领，32 条未解释。未解释的全部是旧 Stop hook run（6 个，05-14～08-13）。例如 `rvf-20260607T072901Z-stop-hook-a44652a4`：manifest 认出 10 个 owned dirty，但 tracker 只分到 1 个路径，像是单元已审或被别的 lease 占用，与本问题无关。

**判定**：在「会话里存在只经 Bash/子代理写入的候选文件」这一条件下，除非该文件恰好走了上述某条旁路，否则**必然**漏审。run 级 76% 而非 100%，是因为 29 个 run 里这类文件全都走了旁路。

### 2.3 按宿主 × 发起路径

| 宿主 | 发起路径 | run 数 | 有漏审 | 本会话写过的候选路径 | 漏审 | 漏审占比 |
|---|---|---|---|---|---|---|
| Claude Code | 旧 Stop hook 派发 | 287 | 44 | 2143 | 215 | 10% |
| Claude Code | agent 自主 start | 68 | 46 | 1259 | 673 | 53% |
| Claude Code | 手动触发（UPS） | 34 | 0 | 185 | 0 | 0% |
| Claude Code | 手动 prepare_review_run | 6 | 2 | 83 | 36 | 43% |
| Codex | 全部（Stop 7 / UPS 5 / prepare 1 / dispatcher 1 / start 1） | 15 | 0 | 51 | 0 | 0% |

- **Codex 没有漏审，但样本仍小**：15 个 session-owned run 里有 7 个写过候选路径，共 51 条，全部经 `apply_patch` 写入，也全部进了覆盖；没有一条只经 exec shell 写入。其中 39 条来自 `rvf-20260722T073910Z-prepare-run-db5a39c6`：新版 Codex 把 apply_patch 以 JS 字符串内嵌在 exec 信封里，解码后才能认出。机制上 Codex 的 `apply_patch` 有 edit claims，`exec_command` 也有 `command_path_candidates` 正则兜底（虽然对 python heredoc 基本无效），与数据一致。但 7 个 run、51 条路径仍不足以证明 Codex 的 shell 写入不会漏审。
- **手动触发（UPS）的 34 个 run 没有漏审**：没有一条只经 Bash/子代理写入的候选路径落到覆盖外。
- **其他宿主**：session-owned 样本里没有 Kimi，有 1 个 cursor run（无法判定）。

### 2.4 时间维度

| 月份 | run 数 | 有漏审 | 只经 Bash/子代理写入的占比（行为） | 这类路径落到覆盖外的比例（机制） | 本会话写过的路径漏审率 |
|---|---|---|---|---|---|
| 2026-05 | 22 | 1 | 1%（84 条里仅 1 条） | 100%（1/1，样本过小） | 1% |
| 2026-06 | 175 | 14 | 6% | 70% | 4% |
| 2026-07 | 92 | 15 | 14% | 61% | 9% |
| 2026-08 | 72 | 27 | 36% | 78% | 28% |
| 2026-09 | 29 | 20 | 60% | 69% | 41% |
| 2026-10 | 20 | 15 | 73% | 94% | 68% |

- **起点**：session-owned 由 `18c000e`（2026-04-30）引入，tracker 与 tracker_scope 由 `3f62fc1` / `ba4cb5d` / `8ea9a35`（05-03～04）落地。数据里第一个 session-owned run 在 05-11，**从那时起问题就存在**，只是早期 agent 很少用 Bash 改文件（2026-05 只有 1 条只经 Bash/子代理写入的候选路径），所以显得罕见。
- **agent 自主发起路径的转折点是 `0aac3ba`（2026-08-15，「补回发起侧 tracker scope 与 lease」）**。在此之前，该路径（`b154d4f`，07-11 起）的 run 都是 manual-all-uncommitted，按全部脏改动审，不会漏审；之后改成 session-owned，漏审随即出现：8 月 21 个 run 里 12 个、9 月 29 个里 20 个、10 月 19 个里 14 个有漏审（10 月含 1 个 Codex run）。
- **「部分归属比完全没归属更糟」**：
  - 归属完全为空或 allocator 降级时，start 会退回 manual-all-uncommitted，反而全覆盖，例如 RVF memory 里 08-21 的 `fb62ad9e`。08-15 之后有 42 个 agent 自主 run 走了 manual-all-uncommitted（8 月 22 个、9 月 19 个、10 月 1 个）。能从 scope-of-work 识别出原因的有 11 个：`degraded_allocator_reported_empty` 5 个（其中 `a2cab426` 同时记了「归属为空」和 `--force-full-rescope`，`b13d7fae` 同时记了「归属为空」），`degraded_transcript_schema_drift_suspected` 4 个，只记「归属为空」的 2 个。其余 31 个没有记录原因。
  - 只要会话里有一次 Edit/Write，就会走 session-owned，只覆盖那几个文件。
  - allocator 层面同样如此：Step 3b「把观察到的单元认领给本会话」只在本会话**一个已登记单元都没有**时才触发（`reviewable_unit_diff_tracker.py:5984-6005`），而 agent 自主 start 还显式传了 `auto_claim_observed=False`（`rvf_agent_decision_cli.py:877-884, 982`）。
- 恶化主要来自「行为」：agent 越来越多地用 Bash python 脚本、子代理改文件，这类路径在本会话写过的路径里的占比从 6% 涨到 73%。「机制」侧，这类路径落到覆盖外的比例 6–9 月一直在 61%–78% 之间，10 月升到 94%；同一时期降级兜底触发得越来越少（manual-all-uncommitted 退回 8 月 22 个、9 月 19 个、10 月 1 个），dispatch 回执也开始高估覆盖。
- **「看起来全审了、实际上没有」的 run**：dispatch 回执高估自 `b154d4f`（07-11）起存在。回执按 run 绑定：先认回执文本里本 run 的 run id（`run_id:` 行或 run_dir 路径）；回执里没有 run id 时，只认「本 run 开始 → 同 transcript 下一个 run 开始」之间的；都没有就算无回执。按这个口径，能绑定到回执的 run 最早在 08-15，有漏审的 run 里最早是 08-19 的 `6e7d1fe1`。能绑定回执的 38 个有漏审 run 中，36 个的「primary_files N」大于正式覆盖数；另外 54 个有漏审 run 没有可绑定的回执，多数是不走 dispatch 的旧 Stop hook run。

### 2.5 第三种失明：HEAD 提交时刻截断

- 例子：`rvf-20261002T154116Z-agentic-initiation-start-4d6fd366`（cline-kanban task 203f3）。
  - 会话在 14:24–14:51Z 用 Edit/Write 写了 16 个文件，没有提交。
  - 15:44Z worktree 被 fast-forward 到 base 上别的任务的提交 `2237024e`，提交时刻 14:57:11Z（reflog：`merge 2237024e…: Fast-forward`）。
  - manifest 的 `ownership_baseline.mode=head_commit_time`，14:57:11 之前的 Edit/Write 全被忽略。
  - 结果：40 个候选只覆盖 2 个。这 16 个 Edit/Write 文件和 22 个 Bash 文件一起落进了 background。
- 历史合计 7 个 run、32 个文件、4832 行。
- 意味着：**按规矩先做 base 同步（`sync-local-base-into-task-worktree-only`）再发起 RVF，会主动触发这个缺陷**。

### 2.6 已知事故核实

| 事故 | run | 候选 / 覆盖 / 漏审 | 漏审行数 | 沟通情况 |
|---|---|---|---|---|
| cline-kanban be4d6（10-02） | `rvf-20261002T145528Z-…-6257e1dd` | 17 / 10 / 7（均为 Bash） | 613 | 两行摘要提到「被误标为 protected」，但只当 fixer 障碍讲，没说没审 |
| 4e79f 第一轮（10-02） | `rvf-20261002T182707Z-…-55174d5f` | 71 / 22 / 39（另有 10 个是同 worktree 另一会话 `6fc34fc4` 写的，不计） | 3077 | 两行摘要原文「按 strict 档**审了 71 个文件**」；handoff 写「reviewed scope paths: scope contract 的 71 个 primary_files」 |
| 4e79f 第二轮（10-03） | `rvf-20261003T093058Z-…-1a6ccb97` | 90 / 11 / 79（主会话 Bash 49、子代理相关 30） | 8728 | 唯一在两行摘要里给出覆盖数字的一次：「只正式审了 90 个文件中的 11 个」 |
| RVF memory 08-19 | `…-6e7d1fe1`（task 1e5f7） | 18 / 11 / 5（另 2 个未覆盖文件在会话里找不到写入证据，不计） | 913 | 最终回复里写成「不在本轮范围但必须记下的既有缺口」 |
| RVF memory 08-21 | `…-fb62ad9e`（task 01200） | 归属全空 → manual-all-uncommitted，8/8 全覆盖 | 0 | 不是漏审，只是审完标不了 reviewed |
| RVF memory 09-30 | `…-9b76330c`（task cce02） | 10 / 1 / 9 | 329 | 只写在 handoff 正文：「本轮已全量双评审 + 修复」 |
| d9ce2（10-04，进行中） | `rvf-20261004T100434Z-…-72fa3e23` | 7 / 3 / 4（`mv` 搬家的 3 个脚本 + `cp` 进来的 HTML 报告） | 1758 | 见 §3.4 |

---

## 3. 当时怎么处理、怎么沟通的

### 3.1 提及的位置与程度（92 个有漏审的 run）

| 位置 | 点明归属失明 | 只说有文件未审 | 合计 |
|---|---|---|---|
| 两行摘要里 | 2 | 1 | 3 |
| 最终回复其他位置 | 3 | 1 | 4 |
| 只在 handoff 正文里 | 13 | 5 | 18 |
| 都没提 | — | — | **67（73%）** |

按月看，两行摘要里提到的次数是：05–08 月 0 次，09 月 1 次，10 月 2 次；「都没提」逐月（05→10）为 1、12、14、19、12、9 个 run。脚本输出的 `communication_snippets_for_manual_spot_check` 列出了全部 25 条命中片段。人工抽查后需要扣掉的噪声：`0da6ea5b` 是 RVF 仓库会话在讨论 RVF 内部的「未审 committed 单元」；`858a54ab` 说的是二进制素材未审；`36a628fa`、`9f9607af` 说的是本轮没碰过的背景文件。这几条都与本会话的漏审文件无关。

**判定：**

- 2026-09 之前属于「**从没提过**」：两行摘要 0 次，handoff 偶有一句。
- 2026-09 以后属于「**提过但不显眼**」：多写在 handoff 正文的 intake hints 或注意事项里，两行摘要几乎不提。
- 而且常被淡化：
  - `rvf-20260930T023015Z-…-7d2150b2` 的 handoff：「审完后另外 18 个文件的单元在 tracker 眼里仍是『未审』……**这是归属失明的后果，不是漏审**」；
  - `rvf-20260930T011410Z-…-9b76330c` 的 handoff：「若再收到覆盖这些文件的决策点，谢绝……（**本轮已全量双评审 + 修复**）」；
  - `rvf-20260930T023606Z-…-bc28e0b0` 的最终回复：「这 12 个文件都是本轮工作，**reviewer 仍然全部审了**」。
- 这些说法的依据是 reviewer 工作树叠了全部脏改动（`bootstrap_kind=full-dirty`），reviewer「读得到」。但 reviewer prompt 明确要求以 `primary_units` 为准、背景不主动分析；packet 对背景新文件只列路径。结构化产物里，reviewer 在漏审文件上提 issue 的比例是 2.1%（19/924），正式覆盖文件是 16.8%（160/950）。三个 run 各自的数字是：7d2150b2 漏审 15 条里 1 条有 issue，9b76330c 漏审 9 条里 0 条，bc28e0b0 漏审 12 条里 1 条。

### 3.2 「看起来全审了」的措辞

- dispatch 回执「已按 strict 档派发 reviewer（primary_files N …）」：能把回执绑定到本 run 的 38 个有漏审 run 中，36 个的 N 大于正式覆盖数（绑定口径见 §2.4）。
- handoff / 最终回复声称审过的文件数大于正式覆盖数：35 个 run。例如 `55174d5f` 写「审了 71 个文件」（覆盖 22），`5b568f9c` handoff 写 142（覆盖 11），`67b977b9` 写 50（覆盖 6）。
- 这些数字直接来自合同的 `primary_files`。agent 在 dispatch 时把它改写成了候选全集，然后 agent 读着它写 handoff。

### 3.3 fixer 被迫 elevated

- session-owned run 里一共 19 个 elevated attempt，其中 14 个的理由是目标文件在 protected 或 fix_allowlist 之外。
- 落在有漏审的 run 里的有 12 个，其中 10 个与 protected 有关，例如 `1a6ccb97` RVF-003、`c67a8ce1` RVF-001、`6257e1dd` RVF-001、`6abe7fb5` RVF-001、`9a5c808c` issue-003。
- 抽查的处理方式都是主会话手工修。`c67a8ce1` 的两行摘要原文：「修复点落在被误标为 protected 的文件里，因此按升级结束；我依据上下文判定该标记是归属误判，直接……修复」。

### 3.4 agent 自己的补救，以及它们为什么只成功了一半

| 补救方式 | 次数 | 实际效果 |
|---|---|---|
| `rvf scope include` | 11 个 run（9 个理由明写归属失明，09-29 起） | 只进 `primary_files`。被补的 58 条漏审路径**全部**仍不在 fix_allowlist、仍在 protected（逐 run 核对，例如 `d687c52c` 6/6、`4d747c2d` 14/14、`a87ebc24` 8/8、`6cd19c67` 13/13、`72fa3e23` 4/4）；packet 也不内联它们的内容 |
| 手改运行态 `scope.contract.json` | 至少 2 个 run（`beb3c652`、`7d2150b2`） | fix_allowlist 扩成 19 / 21、protected 清零，fixer 不再被拦；但 `primary_units` 仍是 4 / 3，reviewer 的正式范围没变 |
| canonical issue 的 `paths` 写进 protected 文件 | RVF memory 记载（09-30） | 只对确属该 issue 的文件有效 |
| `rvf decline`，等用户拍板 | 4 次（09-09 b04a4；10-03、10-03、10-04 4e79f） | 理由原文例如「scope include 只改 primary_files 不改 fix_allowlist/primary_units，补审无效」 |
| 写进 agent 自己的 memory | RVF memory 4 次（08-19 / 08-21 / 08-23 / 09-30），cline-kanban memory 1 条 | 用户不会日常去读这些 memory |
| 范围异常账本（自动） | 7 条 include 例外（09-29 起） | note 写「疑似候选全集构造有系统性偏差，建议……收紧 ownership filter」，方向与真实原因（归属过窄）相反；只是本地 ledger，不上报 |

**d9ce2 进行中 run（用户本轮贴来的）的核对**：主 agent 回复说「我用 rvf scope include 把这 4 个文件补了回来，**现在 7 个文件都在审查和可修改名单里**」。按 10:07Z dispatch 之后的合同，`primary_files`=7，但 `fix_allowlist`=3、`primary_units`=3，被 include 的 4 个文件**仍同时在 protected_files 和 background_files 里**。packet 的「Untracked Files」段只内联了 2 个已归属文件，4 个核心新文件（约 680 / 616 / 269 / 193 行）只有路径。这句话前一半大体成立：重渲的 scope-of-work 写了逐文件审查要点，截至统计时 reviewer 已在其中 3 个文件上提了 issue。后一半（可修改）不成立：fixer 遇到这 4 个文件仍会被要求 elevate。这是「include 回执语义不清」直接导致的误报。

---

## 4. 影响

| 指标 | 值 |
|---|---|
| 漏审（按 run 计） | 924 条路径、60,121 行（Bash/子代理 892 条 / 55,289 行；HEAD 截断 32 条 / 4,832 行） |
| 去重后（仓库，路径） | **848 个文件、56,221 行**（每个文件取最大一次），其中新建文件 110 个 |
| 后来进过正式覆盖的 | 138 个；**710 个在之后所有 session-owned run 里都没再进覆盖** |
| 涉及 | 60 个 Kanban 任务。有漏审的 run 按仓库分：cline-kanban 55、review-validate-fix 23、my-ai-setup 5、ai-analysis 3、doctoral-research-proposal 3，其余 3 个仓库各 1 |
| 漏审行数最多的 run | `1a6ccb97` 8728、`5b568f9c`（ac29f）6490、`67b977b9`（ac29f）5457、`55174d5f` 3077、`4d6fd366` 2971、`56e648b3`（b04a4）2162 |
| 之后被 fix/revert 提交触及（扣掉第一个落地提交） | 漏审文件 409/924（44%），同一批 run 的已覆盖文件 410/950（43%） |

最后一行**没有区分度**：漏审文件与同一批 run 的已覆盖文件，后续被 fix/revert 提交触及的比例几乎相同（44% 对 43%）。何况「触及」不等于「由漏审引入」，热门文件本来就常被改，同一文件也会在多个 run 里重复计数。所以历史数据既不能说明、也不能排除漏审文件里藏着更多 bug。要回答「漏审到底放过了多少真问题」，可行做法是对 710 个从未覆盖的文件挑一批，用 manual-all-uncommitted 补审。这超出只读调查的范围，需要你另行批准。

---

## 5. 建议

### 5.1 改进候选：按历史上能救回多少重新排序

| 排名 | 候选（交接报告 §3.8 编号） | 历史可救回（漏审条数 / run 数） | 代价与风险 |
|---|---|---|---|
| 1 | **覆盖率如实报告 + 确认闸**（2 + 5） | 不直接救回，但能让 924 / 92 **全部被看见** | 便宜，不改归属，可立刻做 |
| 2 | **Kanban 单任务 worktree：以 git diff 为地面真相，unattributed 默认归本会话**（1） | 894 / 83 | 会把同 worktree 里别的会话（往往是同一任务的前一个会话）或 base 同步残留的改动一起纳入：历史上 330 条「本会话没写过」的未覆盖路径。在单任务 worktree 里这多半是想要的行为。落点已有：allocator Step 3b（`reviewable_unit_diff_tracker.py:5984-6005`）的「认领观察到的单元」目前只在本会话零单元时触发，改成「单任务 worktree 里也认领尚无归属的单元」即可，lease 照常拿 |
| 3 | **Bash 前后快照归属**（4） | 770 / 88 | 需要 PreToolUse/PostToolUse 对 Bash 做 `git status` 加内容哈希快照，每次 Bash 调用多一点延迟；子代理的工具调用是否也触发 hook【推测，需实测】 |
| 4 | **归属时读 `subagents/agent-*.jsonl`**（3） | 147 / 8 | 积木已有（`adapters/claude_code/subagent.py:75-153`）；需把子代理写入锚到主 transcript 的 Agent 调用行，才能和 watermark 比较 |
| 5 | **修 HEAD 提交时刻截断**（新） | 32 / 7 | 截断点不应取「fast-forward 进来的 base 提交」的时刻；可改为只认本会话自己产生的提交，或取 merge-base |
| 6 | **`scope include` 真正登记归属**（6） | 58 条（agent 已手动 include 的漏审路径） | 等于给已发现问题的 agent 一个有效的补救按钮 |
| 7 | **fix attempt 叠加全部脏改动、只锁可写边界**（7，对应兄弟任务 72e76） | 不救回审查，但能消掉 10 次 protected 相关 elevated | — |
| 8 | Claude Bash 也跑现有 `command_path_candidates` 正则（8） | 62 / 33 | 对 python heredoc、`biome --write` 基本无效，收益低 |
| 9 | 「归属为空」fail-loud（9） | 0 | 归属全空时已经会降级成全量审查；真正的问题是「部分归属」，见 §2.4 |

### 5.2 最小可行修复

两步，都不碰 tracker 的数据模型：

1. **单任务 worktree 的覆盖率闸**：start 时如果「正式覆盖路径 / 候选路径」< 100%，且 worktree 是 Kanban 单任务 worktree（或未覆盖路径出现在本会话 transcript 的命令文本里），就把尚无归属的单元认领给本会话（候选 2，落点是 allocator Step 3b），或者至少退回已有的 manual-all-uncommitted 分支（与「归属为空」走同一条已存在的降级路径）。后者的代价是这一轮没有 lease、标不了 reviewed——这是已知行为，见 RVF memory。
2. **如实报告**（§5.3）。

### 5.3 沟通层面的改进

- **start 回执**：把「自动 scope 90 路径；23 个可审单元」改成「候选 90 路径；**正式审查覆盖 11 路径（23 单元）**；79 路径未归属：本会话 Bash 写入 / 子代理写入 / 其他」，覆盖不全时加 ⚠。
- **dispatch 回执**：不再报 `primary_files N`，改报「正式审查 X / 候选 Y」；维护「primary ∩ protected = ∅」不变量（现状是 79 个路径同时在两边）。
- **`scope include` 回执**：必须说清「只影响 land 的 stage 边界与 reviewer 的提示，**不扩大 fix_allowlist / primary_units**」，或者干脆让 include 真正登记归属（候选 6）。d9ce2 的误报就源于此。
- **handoff**：「handoff 表头改由脚本渲染」已有在途实现（RVF memory `project_handoff_generated_header_ownership_split` 记载已在任务分支提交、尚未回 main）。在脚本渲染的表头里加「审查覆盖：X/Y 文件（未覆盖 Z 个，原因 …）」，agent 就无法省略。模板里 `reviewed scope paths` 的取值改成「正式覆盖路径」而不是 `primary_files`。
- **两行摘要**：覆盖不全时，`Reviewers：` 行必须带「正式审了 X/Y 个改动文件」。这一点写进 handoff 模板的固定结构。
- **范围异常账本**：note 的诊断方向改为「候选里的本会话改动未被归属（Bash / 子代理 / 改名 / HEAD 截断）」，并在下一次决策点或 handoff 里对用户可见。
- **禁止淡化措辞**：handoff 模板写明「reviewer 工作树能读到不等于审过；未进入 primary_units 的文件一律记为未审」。

### 5.4 需要你拍板的冲突

`b8962` 的在途改动让自动 Stop 只把 `current_session_owned` 的改动放进候选。按本调查的数据，会有两个后果：

- Bash/子代理写的文件（2026-10 占本会话改动的 73%）从决策横幅里消失；
- 全靠 Bash 写入的会话可能连决策点都不再出现，从而失去「归属全空 → 全量审查」的兜底。

建议在本问题修好之前暂缓 `b8962`，或者把它限定在「Codex 宿主 + 本会话确实没有任何写工具调用」的场景。

---

## 6. 局限

- **漏审数是启发式估计，误差是双向的。**
  - 偏少的来源（该算没算）：
    - 批量命令（`xargs biome check --write`、formatter 对目录、`git stash pop`、按 glob 的脚本）找不到逐路径证据；
    - `python3 scripts/x.py` 这类看不到正文的脚本；
    - 目标由变量拼出、又还原不了的写入；
    - 内联脚本里没被识别成「带目标参数」的写法。

    这些路径都记为「本会话没写过」，不计入漏审。未覆盖的这类路径共 992 条。
  - 偏多的来源（不该算却算了）：
    - 内联脚本在写目标还原不了时，退回「正文里以完整字符串字面量出现的候选路径」。脚本只读了某个候选文件、结果却写到别处时，就会误计。漏审里有 144 条属于这一类。试跑抽查见过 2 例：脚本把带仓库路径字段的 JSON 写进 run 目录，那个路径被误计为写入。
    - formatter 写模式只说明运行过，不保证改了内容。
    - `git checkout --` / `git restore` 是还原，不是新写。
    - 另有 12 条只靠唯一 basename 命中。
  - **抽查**：从 833 条「证据只来自 shell 命令解析」的漏审里，用固定种子随机抽 40 条（`summary.shell_derived_missed_path_spot_check_sample`，汇总表附触发命令摘录与 transcript 行号），逐条看摘录：
    - 36 条可以确认是真实写入：`cat >` / heredoc、内联脚本 `open(p,'w')` 或 `p.write_text` 写回同一个字面量路径、`for p in [...]` 逐个写回、`git mv`；
    - 4 条存疑：3 条是 `biome … --write`，1 条是 `git checkout --`；
    - 0 条明确误判。

    明确误判的点估计是 0/40（Wilson 95% 置信上界约 9%）。把存疑全部算作误判，则是 4/40 = 10%（Wilson 95% 置信上界约 23%）。即使按上界扣减，漏审集中在 agent 自主发起路径、随月份上升这两点不变。
  - 这 833 条按首个触发命令分类：
    - 内联脚本写回字面量目标 526；
    - 内联脚本启发式 144；
    - 重定向 43；
    - `git rm`/`mv`/`restore`/`checkout` 39；
    - formatter 写模式 37；
    - `rm`/`touch` 15；
    - `cp`/`install` 13；
    - `sed -i`/`perl -i` 12；
    - `mv` 4。

    另有 59 条漏审的证据是子代理的 Edit/Write 工具调用，属于精确证据，不在抽查范围内。
- 80 个 run 的 transcript 已不存在，无法判定；Codex 样本只有 15 个 run、51 条本会话写过的路径。
- 「提到覆盖缺口」靠措辞匹配加人工抽查片段，个别边界情况见 §3.1。
- 「后续 fix 提交」只是上界信号，不能确立因果。本次两组比例几乎相同，见 §4。
- 交接材料给出的 4e79f 第二轮窗口起点是 round-1 提交时刻（19:13:58），本脚本用的是上一个 run 的开始时刻（18:27:07）。即便如此，这个 run 的逐文件分类与交接材料一致：79/79 漏审，其中主会话 Bash 49、子代理相关 30。

---

## 7. 复查与重跑

```bash
# 全量（约 70 秒，含 git log 查询）
python3 -B docs/investigations/rvf-session-owned-attribution-blind-to-bash-and-subagent-writes-history-2026-10-04/rvf-session-owned-attribution-coverage-gap-history-readonly-statistics-script.py \
  --output-directory /Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/<新目录名>

# 只看指定 run（验证 / 复查新 run）
python3 -B …/rvf-session-owned-attribution-coverage-gap-history-readonly-statistics-script.py \
  --output-directory <目录> --only-run-id <run_id> [--only-run-id …] --skip-later-fix-commit-git-query
```

- 本文引用的输出：`/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/from-task-08ac4-rvf-session-owned-attribution-coverage-gap-history-statistics-output-2026-10-04-revised-after-rvf-review/`，生成于 2026-10-04T11:56:47Z。
- **只读保证**：
  - 所有输入只读打开；不打开 tracker SQLite；不调用 `rvf` 子命令；git 只跑 `git log`。
  - `--output-directory` 拒绝 `~/.rvf` 内和任何 git 工作树内的路径。
  - 设置了 `sys.dont_write_bytecode`，跑完后仓库工作树没有新增文件。
  - 运行前后核对了 `1a6ccb97`、`db5a39c6` 两个 run 目录下 4461 个工件文件，mtime 全部不变。唯一变化的是 4e79f 的 transcript：那个会话仍在进行，自己在追加记录。
- **确定性**：同一输入隔 7 分钟连跑两次，924 条漏审路径行逐条一致（含触发命令摘录）。
- **单 run 复查与全量一致**：`--only-run-id` 只筛选要分析与输出的 run；窗口起点、下一轮上界与「后续是否覆盖」索引始终用全部合同历史建立。对 `1a6ccb97`（`window_start_utc=2026-10-02T18:27:07`，90 / 11 / 79）和 `55174d5f`（71 / 22 / 39）单跑，逐字段与全量一致。
- **单 run 字段**：
  - 主要看 `missed_session_written_path_count`、`missed_path_count_by_cause`、`uncovered_path_write_origin_counts`、`coverage_gap_communication_prominence`；
  - 不可判定的原因看 `write_origin_not_analyzable_reason`，回执怎么绑定看 `dispatch_receipt_binding_method`；
  - 漏审路径行里的 `shell_write_trigger_kind` / `shell_write_trigger_command_excerpt` / `shell_write_trigger_transcript_line_number` 用于抽查；
  - 两个反例的成因看 `counterexample_*_explanations`。
- 复用了 RVF 自己的 `command_path_candidates` / `command_workdir`（`core/session_scope_allocation/session_change_manifest.py:425-471`），用于评估候选 8。
