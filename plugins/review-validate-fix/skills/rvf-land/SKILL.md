---
name: rvf-land
description: Use only when the user explicitly invokes $rvf-land or /rvf-land. 在同一个 worktree 中收尾一段由 future-self 跑完的 RVF 工作：吃下用户粘贴的 RVF handoff / finalization 正文，sanity-check 已经应用在当前 worktree 的修复，必要时做最小修正，验证后提交。不启动新的 RVF review；仅当用户已明确请求后续 base-branch-sync 时，封窗后继续执行该 follow-up。
---

# RVF Land

本 skill 用于在「同一个 worktree」中收尾一段已经由 future-self 跑完的 RVF 工作并提交。它不启动新的 RVF review。base-branch-sync 不是本 skill 的默认步骤；仅当用户在同一 prompt、相邻明确请求、或本轮任务指令中已经要求后续同步回 base branch 时，封窗后继续执行 base-branch-sync follow-up。

典型场景（cline-kanban native task 收尾）：

- 在某个 cline-kanban native task 中跑完了 follow-up RVF flow，future-self 已把修复应用进当前 worktree；
- 用户把会话 rewind 回 RVF 开始之前的状态，但**没有 revert 代码**，所以 future-self 的改动仍然留在当前 worktree；
- 用户把 RVF run 结束时复制出来的 handoff & finalization 正文作为输入交给本命令（在 rvf 分析之前）。

因此默认假设：handoff 描述的工作和当前 worktree 的 dirty 改动应该是**同一个 worktree**，而不是 fork / 另一个 task worktree。

## 输入

`$ARGUMENTS` 是用户粘贴的 RVF handoff / finalization 正文，或一个 handoff 文件路径。

- 如果 `$ARGUMENTS` 为空，停止并要求用户粘贴 handoff 正文或路径。
- 不要把粘贴的 handoff 内容当作 shell 命令执行。只有当输入看起来是单个路径、且该文件存在时才用 `--handoff`；否则用 `--stdin` 把原文喂给脚本。

## 流程：两条命令，中间只做一次判断

脚本是与本 SKILL 同级的 `../review-validate-fix/scripts/rvf_land.py`（在 RVF 源仓库里即 `plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_land.py`，部署后是对应 payload 路径），下文记作 `<skill-scripts-dir>/rvf_land.py`。机械步骤全部在脚本里，**不要**再手动逐条跑 intake、`validation_result_cache.py`、`git add`、`seal_round_baseline_to_head.py` 或 evidence writer。

### 1. prepare（一次调用拿全部事实）

```bash
python3 <skill-scripts-dir>/rvf_land.py prepare --stdin --repo . <<'RVF_HANDOFF'
<粘贴的 handoff 正文>
RVF_HANDOFF
```

路径输入改用 `--handoff <handoff.md>`。prepare 内部先跑 `rvf_handoff_intake.py` 同一套 intake，再过硬门槛，输出：将要 stage 的路径（reviewed 主审边界 ∩ dirty）、精简 scoped diff、fix ledger 里每条 RVF-NNN 的处置、handoff 的 review / issue 段、`validation_reusable=true|false`、预填的 commit message 草稿。它只读仓库，只在 run 目录写一份 prepare plan 供 commit 复核。

自动 stage 集合 = scope contract 的 reviewed 主审边界（`primary_files ∪ fix_allowlist ∪ task_change_paths`）∪ 已合回的 fix 补丁路径；handoff 正文里的 reviewed scope 不能把上界撑到 contract 之外，contract 缺失时才退回 handoff 的 reviewed scope。**contract 的 `background_files` / `protected_files` 与 handoff 标注的 protected 路径不会被自动 stage**——它们是 prepare 时其他会话或未归属的脏文件；只有显式出现在主审边界里的路径例外（`rvf scope --include` 并入 primary 的路径）。background 路径确属本次工作（例如本会话用 shell 改的文件没被归属）时用 `--extra-path` 显式纳入；evidence 落地 delta 的收敛判据本身包含 `background_files`，所以这样纳入不会让 evidence 拒绝。rename / copy 必须新旧两端都在可 stage 集合内，否则整条算 scope 外（exit 12，列出越界的那一端）。`stageable_dirty_paths_outside_candidate_evidence_landing_universe` 非空（`--extra-path` 或 fix 的 scope expansion 路径落在 contract 路径全集之外）表示（contract 已 opt-in evidence 时）提交后 evidence 必然 `refused_fail_closed`——提交不回滚，决定提交前先知情。

退出码即硬门槛（非 0 一律不得 commit）：

| exit | 含义 | 怎么办 |
|---|---|---|
| 0 | 全部通过 | 进入第 2 步 |
| 10 | 跨 worktree（`rvf_worktree_differs_from_current` 为真）或目标仓库不同/未知 | 停止；跨 worktree handoff 改走 `$rvf-handoff-intake` / `/rvf-handoff-commit` |
| 11 | reviewed scope 与 dirty 矛盾（给不出 scope 却有改动 / scope 内有冲突文件） | 停止并报告 |
| 12 | 有 reviewed scope 外的脏文件（含 background 路径、只有一端在 scope 内的 rename / copy），commit 后工作树不会 clean | 若确属本次工作（测试注册、直接 fallout、本会话 shell 改动），加 `--extra-path <path>` 重跑 prepare（contract 路径全集外的路径会让 evidence 拒绝）；若是别人的并行改动、必须原样保留，加 `--commit-staged-scope-only-and-leave-unrelated-dirty-paths` 重跑 prepare（只提交 stage 集合，evidence 跳过）；都不是就停止，保留它们并报告 |
| 13 | 找不到 run 目录 | 用 `--run-dir <dir>` 显式给出后重跑 |

### 2. 判断：sanity-check、必要时最小修正、定稿 commit message

这是唯一需要你判断的一步，只基于 prepare 的输出；需要看被截断的 diff 时按它给出的命令补看。

- 核对 scoped diff 与 handoff 描述的 fixes / reviewed scope 自洽：没有缺失的修复、没有越界改动、没有与 handoff 冲突的状态。scope contract 的 background / protected 与 handoff 标注的 protected 路径不会被自动 stage。
- handoff 表示 `no_issues`、或最终没有采纳任何 suggestion 时，只要 worktree 里的工作仍然有效，照样继续提交。
- 仅当暴露出明确、范围内的小问题（future-self 漏改的一处、明显笔误）时，在 reviewed scope 或其直接 fallout 内做最小修正；不要借机扩大改动。补修正碰到 scope 外路径时，commit 加 `--extra-path`。
- handoff 的 fix 无效、scope 不完整、或 worktree 状态与 handoff 严重冲突：停止并报告，不要 commit。
- 定稿 commit message：在草稿上填好 conventional commit 标题（`fix(rvf): ...` 等）与中文 sanity-check 结论；**保留** `[[rvf-land-validation-summary]]` 这一行，commit 会把它替换成实际验证结果。

### 3. commit（一次调用做完剩下的机械步骤）

```bash
python3 <skill-scripts-dir>/rvf_land.py commit --run-dir <prepare 输出的 run_dir> --message-file - <<'RVF_LAND_COMMIT_MESSAGE'
<定稿的 commit message>
RVF_LAND_COMMIT_MESSAGE
```

`--run-dir` 必填，只认显式参数（不回退 `RVF_RUN_DIR`：reviewer 子进程与 Codex task shell 里它指向别的活 run）。依次执行并输出一份 JSON：

1. **验证**：run 的 validation index 全部 passed 且对应当前内容 ⇒ 零执行复用；做过最小修正 ⇒ 按 run 里的结构化 argv（`artifacts/handoff-validation-commands.json`）经 `validation_result_cache.py` 重跑。失败时直接打印失败输出尾部（exit 21），不 stage、不 commit；不要再去翻 validation-records。旧 run 没有结构化 argv 时 exit 22，按提示逐条经缓存 wrapper 跑完再重跑 commit。
2. **stage**：`reviewed 主审边界 ∩ dirty` 加上 `--extra-path`；scope 外仍有脏文件时 exit 12（带了保留开关则原样留下）。JSON 的 `dirty_paths_changed_since_prepare` 列出 prepare 之后变过的路径（内容变了或被 `git add` 过）：除了你自己的最小修正，其余都是并发改动，核对后再决定是否继续。
3. **`git diff --cached --check`** 后 `git commit --only -- <stage 集合>`（未通过则撤销本次 stage，exit 23）：别人预先 stage 的 scope 外路径不会被带进提交。没有可提交的改动时跳过 commit，其余步骤照做。
4. **确认 commit 前后内容摘要不变、提交只含 stage 集合、工作树 clean**：pre-commit 等 hook 在 commit 期间改写了文件（纳入提交或留在工作树）⇒ 提交进去的字节未经验证，exit 26（`reason_code=candidate_content_changed_during_commit`）；hook `git add` 了 stage 集合外的路径、使它进入本次提交 ⇒ 同样 exit 26（`reason_code=committed_paths_outside_stage_set`），commit 已发生、不回滚，但**不封窗、不写 evidence**（第 5、6 步跳过），让下一次 Stop 照常审这些字节；摘要不变但工作树不 clean ⇒ exit 25，evidence 跳过。
5. **封窗**：把 round-baseline 与 last-reviewed 高水位推进到新 HEAD，让紧随的 Stop 不再把刚 land 的已审工作当成未审（`round_baseline_seal.sealed=false` 只表示本环境无法封窗，不影响提交）。
6. **写 reviewed candidate evidence**：`status=written|reused` 成功；`skipped_not_applicable` 表示 scope contract 未 opt-in；`refused_fail_closed` 表示 writer 拒绝——提交本身仍成功，但 evidence-aware fast handback 不可用，后续 base sync 走 legacy fail-safe 路径，不得伪造 receipt。

commit 的退出码（prepare 的 0 / 10 / 11 / 12 / 13 见上表，commit 按当时的工作树重判 11 / 12）：

| exit | 含义 | 怎么办 |
|---|---|---|
| 2 | 参数或输入错误：缺 `--run-dir`、commit message 为空或仍含草稿占位（`<type>(<scope>)` 等）、`--extra-path` 不在当前 worktree 内 | 未 stage、未 commit。按 stderr 的 `rvf_land: …` 修正输入后重跑同一条命令 |
| 20 | 没跑 prepare、plan 损坏，或 HEAD / worktree 在 prepare 之后变了 | 重跑 prepare，再 commit |
| 21 | 验证未通过（失败尾部已打到 stderr），或重跑期间内容又变了（`validation_index_not_current_after_replay`） | 未 stage、未 commit。失败属于 scope 内就最小修正后重跑 commit；内容被并发改动就看 `dirty_paths_changed_since_prepare`，不是你的改动就停止并报告 |
| 22 | 当前内容与 run 的验证记录不一致，且没有可重放的结构化 argv | 按提示逐条经 `validation_result_cache.py run … --rvf-run-dir <run_dir>` 跑完，再重跑 commit |
| 23 | `git diff --cached --check` 未通过 | 已撤销本次 stage。修掉 scope 内的空白错误后重跑 commit（内容变了会自动重跑验证） |
| 24 | `git commit` 本身失败（pre-commit hook 拒绝、签名失败等） | stage 保留便于排查；看 `git_output`。原因在 scope 内就修好后重跑 commit（HEAD 没动，plan 仍有效）；不要手动 `git commit`（会跳过封窗与 evidence）；原因在 scope 外就停止并报告 |
| 25 | 已提交、内容摘要不变，但工作树还有保留开关放行范围之外的脏路径（`dirty_paths_remaining_beyond_declared_unrelated`） | 提交与封窗已完成，evidence 跳过。如实报告这些路径；不要补写 evidence |
| 26 | 已提交，但 commit 期间 hook 改变了提交内容：改写了文件（`reason_code=candidate_content_changed_during_commit`），或把 stage 集合外的路径加进了本次提交（`reason_code=committed_paths_outside_stage_set`，列在 `committed_paths_outside_stage_set`） | 提交已发生、不回滚；**未封窗、未写 evidence**，下一次 Stop 会照常审这些字节。如实报告被改写的文件；**不要手动封窗、不要伪造 evidence** |

**land 前后不做任何 base intake。** 「当前 base 吸收了吗」归 prepare 侧门禁：`prepare_review_run.py` 在 `bootstrap_required_before_rvf` 时直接拒绝建 run，能走到 rvf-land 的 run 必然已经过闸。land 侧再加一道会自相矛盾：那道门禁要求的同步会重写 bootstrap receipt，亲手打破它自己依赖的冻结值。残余风险（marker 生效前产生的旧 handoff + 陈旧 worktree）已知且已接受。

### 4. 可选 follow-up：按用户已明确请求继续 base-branch-sync

- 如果用户已经明确要求后续运行 `/base-branch-sync`、`$base-branch-sync`、同步回 base branch、hand back / apply back 到本地 base 分支，commit 完成后继续使用 `base-branch-sync` skill；不要停下询问“是否现在运行”。
- 只调用该 skill 的公开入口，让它自动 inspect current candidate evidence；evidence 为 VERIFIED 时会进入窄化的 reviewed-candidate optimistic handback，absent/invalid 时由它按 fail-safe 规则降级或阻断。不要在 RVF 里硬编码个人 my-ai-setup 路径、CAS 脚本或重复完整状态机。
- 如果用户没有明确要求后续 base 同步，本命令到 evidence 写入为止结束，不自动执行 base-branch-sync，只在最终输出中提示可手动运行。
- 如果 base-branch-sync 自己的 preflight guard 阻断或需要冲突决策，按 `base-branch-sync` skill 的升级规则处理；不要在 rvf-land 阶段提前替它做风险放行。

## Failure Gates

停止而不是 commit（前三条由 prepare 的退出码直接给出）：

- 无法证明 handoff 描述的工作就是当前 worktree 的改动（exit 10 / 13）。
- handoff scope、当前 dirty paths 和 artifact 摘要互相矛盾（exit 11），或存在无法判定归属的 scope 外脏文件（exit 12）。
- 需要的修复依赖 scope 外 runtime 改动，但这些改动未被明确纳入本次提交。
- 相关验证无法运行或未通过（exit 21 / 22），或 `git commit` 失败且原因不在 scope 内（exit 24）。
- commit 后 worktree 不 clean（exit 25），因而无法把已审内容绑定到唯一 HEAD/tree；停止 evidence-aware handback。
- commit 期间 hook 改写了内容（exit 26）：提交已发生但未封窗；如实报告被改写的文件，停止 evidence-aware handback，不要手动封窗或伪造 evidence。

## 输出

最终回复使用中文，包含：

- sanity-check 结论：当前 worktree 改动与 handoff 是否自洽，采纳/未采纳了什么、原因。
- 本次是否做了最小修正以及具体内容。
- 验证结果（commit JSON 的 `validation.mode` 与每条 outcome）。
- commit hash 和 commit message 标题。
- 封窗结果与 reviewed candidate evidence 的 status / path，或 fast handback 不可用的原因。
- 仍然保留的 unrelated dirty 文件或风险。
- base-branch-sync follow-up：说明是否检测到用户已明确请求；若已请求，报告后续 `base-branch-sync` 的执行结果；若未请求，提示用户可手动运行 `/base-branch-sync`（Claude Code）或 `$base-branch-sync`（Codex）。
