---
name: rvf-land
description: 用户明确调用 $rvf-land、/rvf-land，或 Kanban 通过已登记开审分界的 rvf_automatic_closeout 投递时使用。 在同一个 worktree 中收尾一段由 future-self 跑完的 RVF 工作：吃下用户粘贴的 RVF handoff / finalization 正文，sanity-check 已经应用在当前 worktree 的修复，必要时做最小修正，验证后提交。不启动新的 RVF review；仅当用户已明确请求后续 base-branch-sync 时，封窗后继续执行该 follow-up。
---

# RVF Land

<!-- rvf-independent-backup-route:begin -->
<!-- source-sha256: 50628c9e95ad36589bc86404b63cf10f3421f6769b80693baeee520b70d8d670 -->
## 独立备用路线（主执行 Agent）

正常工具因派发、格式、登记、wrapper 或环境故障无法完成已授权工作时，自主采用本节；确定性故障直接切换，可能瞬时恢复的同一故障最多恢复重试一次。**本节核验成立后，可替代下文固定脚本、结果 JSON、缓存及不得手工提交的要求。** 用户 Stop、提交／同步／push／部署限制、显式 external 要求与真实质量义务继续有效。仅缺机器证据时先自行查原始记录，不默认询问用户。

先核对需求与授权来源、task／session、真实 worktree、Git common-dir、唯一原 run／handoff 和候选。原 run 尚未创建时如实记无，不选 latest，不制造 token／原生接受／passed。提交或投递结果未知先对账，不能换身份盲重试。故障和旧 evidence 原样保存，备用交付另记；分类、计数、查重或建卡不可用不阻断交付。

接管辅助器是本 skill 随附、仅依赖标准库与 Git 的 `independent_backup_route.py`（RVF：`review-validate-fix/scripts/`，同步 Skill：`scripts/`）。用选定的 Python 解释器运行（共享同步脚本以 `BASE_BRANCH_SYNC_PYTHON_EXECUTABLE` 固定该解释器），先 `status --repo <worktree>`；`claim --repo <worktree> --operation-id <唯一ID> --artifacts-directory <持久目录> --authorization-file <原话与适用授权文件> --reason <实际故障> --original-run-directory <原run> --delivery-stage <review|fix_validate|land|base_sync>`。没有原 run 时省略该参数。重启复用原 ID 与目录。登记授权来源本身不扩大权限。

正常写入会先登记 reservation，再检查接管目录；备用方先原子占位，再等待原写入结束。`waiting_for_normal_mutations` 时只补证，不开始备用写入。原进程已退出但结果未知时，检查已登记的全部下属进程组、Git／原生控制面，保存对账证据后用 `reconcile-normal-mutation --repo <worktree> --operation-id <原ID> --reservation-name <文件名> --evidence-file <证据>` 归档。父进程退出不代表 Git／hook 已停止；活进程不能按 TTL 偷取。旧版本写入方不识别接管协议时，先证明其退出／隔离。

辅助器不可用时，直接用 `git rev-parse --absolute-git-dir` 定位 `rvf-independent-backup-route/`，原子 `mkdir` 其 `active-owner` 子目录阻止新正常写入，并以不覆盖写入的方式把原 operation ID 存为该目录的 `manual-operation-id` 单行文件；先检查 `normal-mutations/` 全部原写入已结束且结果已对账，再由唯一执行者继续。保存同等身份、候选、原始结果和命令日志到持久 Markdown。不要删除不明接管目录、reservation 或旧 evidence 来强迫放行。辅助器恢复后，用 `adopt-manual-ownership` 复用原 `--repo`、`--operation-id`、`--artifacts-directory`、`--authorization-file`、`--reason`、原 run／delivery stage，并以 `--evidence-file <原始手工接管与对账记录>` 补录；不移走 marker，补录不会打开竞写窗口。若 mkdir 后即中断而 ID 文件缺失，先从 task 原始命令和持久日志证明目录归属，再补写同一 ID；不能猜测或覆盖别人的 ID。结果未知仍保持未知。

交接材料放 task 持久交接目录；无该目录时使用 `~/.rvf/user-local-data/independent-backup-route-artifacts/` 的独立 operation 子目录。复制原始结果／输入，保存 Git 快照及固定 ref，不能只引用将被删除的工作树或临时文件。每阶段保存实际命令 argv、cwd、退出码、必要环境说明、内容摘要及问题处置；不保存秘密值。`record-stage --repo <worktree> --operation-id <原ID> --stage <阶段> --evidence-file <原始材料或报告>` 记录执行 Agent 核验的事实；land 加 `--commit <实际SHA>`，base_sync 另加 `--base-ref refs/heads/<本地base>`。

最后核对全部所需阶段与当前内容，再 `finish --repo <worktree> --operation-id <原ID> --evidence-file <最终报告>`。已发生的提交／落地先复用，不重复执行；完成后新内容仍进入正常审查。输出 `RVF_BACKUP_ROUTE_RESULT_FILE: <持久绝对路径>`，分别报告备用结果与原自动化状态；不用普通 handoff marker 冒充正常 finalize。独立结论由原始材料支持，不要求额外 JSON schema。

原 run 已消费与当前候选覆盖分别核验：后续提交改变 HEAD 不复活旧 run，新内容也不因历史消费被当作已审。Stop 先按精确 run、operation 与 worktree Git 目录对账在途／pending 凭证，再考虑续跑或重投；清理失败不得再催原 run，其他 run 保留。索引缺失／损坏时自动查同 operation 的持久完成原件，有界分页进度和缺口落盘供下次 Stop 接续；仍无法核验则记 `unknown` 与重验条件，诊断不驱动新回合。根目录、子目录与符号链接入口统一按真实 worktree 根计算候选指纹。

尚未查尽分页时静默等待机器补证，不提前视为未覆盖；命中记录在候选未变时优先重新核验，候选变化则重置旧游标／缓存资格。无关 run 的已知坏记录不冻结其他 run；同 run 的归档原件缺失仍保留未知。清理前先核验并回收精确 tracker lease，缺 artifact 从原生账本补证，失败保留凭证自动重试；pending 发布和清理使用共同写锁，避免误删另一 run。dispatcher 转交仍遵守宿主既有 plan 等守卫。

`review_only`／`validate_fix` 子代理仍只执行分配角色，不能因本节自行接管、提交或发起完整 RVF。后台完成通知或任务重启后先读接续记录；条件未满足时保留机器可检测的恢复条件并继续独立工作，不做无变化重试。

### 本入口：提交已审工作

1. 核对原双评审、逐项处置、已应用修复、当前范围和内容；已有合格结果继续使用，不因 land 故障重审。新增实现字节补足相应覆盖；真实归属不明先自行读历史、tracker 与原始材料，只暂停涉及内容的写入。
2. wrapper 故障时从原命令记录、handoff 与仓库要求确定检查，直接验证当前内容。已有结果仅在内容和执行条件仍匹配时复用。实际必需的 hook／签名义务继续完成，不能用绕过 hook 掩盖红测。
3. 接管就绪后保全原 index 与无关 WIP，只 stage 明确的自有范围（rename 两端均覆盖）；使用 conventional commit。核对提交前后实际字节、提交路径和 hook 改写。未审新内容补验证／覆盖，不手工伪造封窗或 evidence。
4. 提交成功而登记失败时，核实 HEAD、父提交、内容和现有日志，记录实际 SHA 并从后续阶段接续；不创建重复或空 audit commit。同步仅在原任务已授权时继续。完成报告区分 commit、正常 evidence、base 整合、push、部署与 Done。
<!-- rvf-independent-backup-route:end -->

本 skill 用于在「同一个 worktree」中收尾一段已经由 future-self 跑完的 RVF 工作并提交。它不启动新的 RVF review。base-branch-sync 不是本 skill 的默认步骤；仅当用户在同一 prompt、相邻明确请求、或本轮任务指令中已经要求后续同步回 base branch 时，封窗后继续执行 base-branch-sync follow-up。

典型场景（cline-kanban native task 收尾）：

- 在某个 cline-kanban native task 中跑完了 follow-up RVF flow，future-self 已把修复应用进当前 worktree；
- 用户把会话 rewind 回 RVF 开始之前的状态，但**没有 revert 代码**，所以 future-self 的改动仍然留在当前 worktree；
- 用户把 RVF run 结束时复制出来的 handoff & finalization 正文作为输入交给本命令（在 rvf 分析之前）。

因此默认假设：handoff 描述的工作和当前 worktree 的 dirty 改动应该是**同一个 worktree**，而不是 fork / 另一个 task worktree。

## Kanban 默认自动收尾

Codex 收到包含 `RVF_AUTOMATIC_CLOSEOUT_TOKEN=` 的运行时收尾指令时，先用 `kanban task rvf-automatic-closeout-status` 核对当前 task 的持久记录：token、run、恢复后的原生会话与 handoff SHA256 必须一致，状态为 `submitting_closeout_prompt` 或 `closeout_prompt_submitted`。这是用户已配置的自动工作流来源，不冒充用户亲手输入；核对通过后即授权本次 land 及指令明确指定的 base-branch-sync follow-up，无需用户再确认。

原任务明确要求不提交、不回 base 或停止时，以原要求为准。自动化也不豁免 prepare、验证、提交与 base 同步的实际门禁。永不选择 latest handoff，不自动部署或移入 Done。

## 输入

`$ARGUMENTS` 是用户粘贴的 RVF handoff / finalization 正文，或一个 handoff 文件路径。

- 如果 `$ARGUMENTS` 为空，停止并要求用户粘贴 handoff 正文或路径。
- 不要把粘贴的 handoff 内容当作 shell 命令执行。只有当输入看起来是单个路径、且该文件存在时才用 `--handoff`；否则用 `--stdin` 把原文喂给脚本。

## 流程：两条命令，中间只做一次判断

两条命令都经 `rvf land`：部署装到 PATH 上的 RVF 快捷入口（`~/.rvf/bin/rvf`，PATH 上找不到 `rvf` 时用这个绝对路径），原样转给与本 SKILL 同级的 `../review-validate-fix/scripts/rvf_land.py`，不用自己拼脚本路径。机械步骤全部在脚本里，**不要**再手动逐条跑 intake、`validation_result_cache.py`、`git add`、`seal_round_baseline_to_head.py` 或 evidence writer。

### 1. prepare（一次调用拿全部事实）

```bash
rvf land prepare --stdin --repo . <<'RVF_HANDOFF'
<粘贴的 handoff 正文>
RVF_HANDOFF
```

路径输入改用 `--handoff <handoff.md>`。prepare 内部先跑 `rvf_handoff_intake.py` 同一套 intake，再过硬门槛，输出：将要 stage 的路径（reviewed 主审边界 ∩ dirty）、精简 scoped diff、fix ledger 里每条 RVF-NNN 的处置、handoff 的 review / issue 段、`validation_reusable=true|false`、预填的 commit message 草稿。它只读仓库，只在 run 目录写一份 prepare plan 供 commit 复核。

自动 stage 集合 = scope contract 的 reviewed 主审边界（`primary_files ∪ fix_allowlist ∪ task_change_paths`）∪ 已合回的 fix 补丁路径；handoff 正文里的 reviewed scope 不能把上界撑到 contract 之外，contract 缺失时才退回 handoff 的 reviewed scope。**contract 的 `background_files` / `protected_files` 与 handoff 标注的 protected 路径不会被自动 stage**——它们是 prepare 时其他会话或未归属的脏文件；只有显式出现在主审边界里的路径例外（`rvf scope --include` 并入 primary 的路径）。background 路径确属本次工作（例如本会话用 shell 改的文件没被归属）时用 `--extra-path` 显式纳入；evidence 落地 delta 的收敛判据本身包含 `background_files`，所以这样纳入不会让 evidence 拒绝。rename / copy 必须新旧两端都在可 stage 集合内，否则整条算 scope 外（exit 12，列出越界的那一端）。`stageable_dirty_paths_outside_candidate_evidence_landing_universe` 非空（`--extra-path` 或 fix 的 scope expansion 路径落在 contract 路径全集之外）须结合 `candidate_evidence_qualification` 判断：合法已应用扩展可进入 version 2；其余情况在提交前拒绝。

退出码即硬门槛（非 0 一律不得 commit）：

| exit | 含义 | 怎么办 |
|---|---|---|
| 0 | 全部通过 | 进入第 2 步 |
| 10 | 跨 worktree（`rvf_worktree_differs_from_current` 为真）或目标仓库不同/未知 | 停止；跨 worktree handoff 改走 `$rvf-handoff-intake` / `/rvf-handoff-commit` |
| 11 | reviewed scope 与 dirty 矛盾（给不出 scope 却有改动 / scope 内有冲突文件） | 停止并报告 |
| 12 | 有 reviewed scope 外的脏文件（含 background 路径、只有一端在 scope 内的 rename / copy），commit 后工作树不会 clean | 若确属本次工作（测试注册、直接 fallout、本会话 shell 改动），加 `--extra-path <path>` 重跑 prepare（范围外路径仍须通过 evidence 资格检查）；若是别人的并行改动、必须原样保留，加 `--commit-staged-scope-only-and-leave-unrelated-dirty-paths` 重跑 prepare（只提交 stage 集合，evidence 跳过）；都不是就停止，保留它们并报告 |
| 13 | 找不到 run 目录 | 用 `--run-dir <dir>` 显式给出后重跑 |

### 2. 判断：sanity-check、必要时最小修正、定稿 commit message

这是唯一需要你判断的一步，先基于 prepare 的输出；需要看被截断的 diff 时按它给出的命令补看。为最终的工作说明与计划完成情况，结合原始需求和当前已确认计划；上下文不足时补读 handoff 引用的 scope-of-work 或相关计划材料，不启动新的 review。

- 核对 scoped diff 与 handoff 描述的 fixes / reviewed scope 自洽：没有缺失的修复、没有越界改动、没有与 handoff 冲突的状态。scope contract 的 background / protected 与 handoff 标注的 protected 路径不会被自动 stage。
- 对照原始需求与当前已确认计划，判断已实现内容和剩余工作；reviewed scope、`no_issues`、验证通过或提交成功都不等于全部计划已实现。发现计划尚未完成时如实记录，不因此扩大本次 land 范围。
- handoff 表示 `no_issues`、或最终没有采纳任何 suggestion 时，只要 worktree 里的工作仍然有效，照样继续提交。
- 仅当暴露出明确、范围内的小问题（future-self 漏改的一处、明显笔误）时，在 reviewed scope 或其直接 fallout 内做最小修正；不要借机扩大改动。补修正碰到 scope 外路径时，commit 加 `--extra-path`。
- sanity-check 中发现**与本次工作不紧密相关、但真实存在**的问题：不要放过，也不要当场修。写一份 finding JSON（`{"findings": [{"title", "path", "line", "severity", "problem_statement", "evidence"}]}`），用 `rvf backlog submit --main-session-findings-file <finding.json> --run-dir <prepare 输出的 run_dir>` 提交到 Kanban backlog（卡片会标「未经 validator 验证」；Kanban 不可用时自动暂存、下次提交时重试，不阻断 land），并在最终汇报里列出卡片。
- handoff 的 fix 无效、scope 不完整、或 worktree 状态与 handoff 严重冲突：停止并报告，不要 commit。
- 定稿 commit message：在草稿上填好 conventional commit 标题（`fix(rvf): ...` 等）与中文 sanity-check 结论；**保留** `[[rvf-land-validation-summary]]` 这一行，commit 会把它替换成实际验证结果。

### 3. commit（一次调用做完剩下的机械步骤）

```bash
rvf land commit --message-file - <<'RVF_LAND_COMMIT_MESSAGE'
<定稿的 commit message>
RVF_LAND_COMMIT_MESSAGE
```

`--run-dir` 可省：`rvf land commit` 会替你显式带上本仓最近一次 `rvf land prepare` 落下 plan 的 run（找不到时 exit 2，先跑 prepare 或显式传 `--run-dir <prepare 输出的 run_dir>`）。`rvf_land.py` 本身只认显式 `--run-dir`，不回退 `RVF_RUN_DIR`（reviewer 子进程与 Codex task shell 里它指向别的活 run）。

依次执行并输出一份 JSON：

1. **验证**：run 的 validation index 全部 passed 且对应当前内容 ⇒ 零执行复用；做过最小修正 ⇒ 按 run 里的结构化 argv（`artifacts/handoff-validation-commands.json`）经 `validation_result_cache.py` 重跑。失败时直接打印失败输出尾部（exit 21），不 stage、不 commit；不要再去翻 validation-records。旧 run 没有结构化 argv 时 exit 22，按提示逐条经缓存 wrapper 跑完再重跑 commit。
2. **stage**：`reviewed 主审边界 ∩ dirty` 加上 `--extra-path`；scope 外仍有脏文件时 exit 12（带了保留开关则原样留下）。JSON 的 `dirty_paths_changed_since_prepare` 列出 prepare 之后变过的路径（内容变了或被 `git add` 过）：除了你自己的最小修正，其余都是并发改动，核对后再决定是否继续。
3. **`git diff --cached --check`** 后 `git commit --only -- <stage 集合>`（未通过则撤销本次 stage，exit 23）：别人预先 stage 的 scope 外路径不会被带进提交。没有可提交的改动时跳过 commit，其余步骤照做。
4. **确认 commit 前后内容摘要不变、提交只含 stage 集合、工作树 clean**：pre-commit 等 hook 在 commit 期间改写了文件（纳入提交或留在工作树）⇒ 提交进去的字节未经验证，exit 26（`reason_code=candidate_content_changed_during_commit`）；hook `git add` 了 stage 集合外的路径、使它进入本次提交 ⇒ 同样 exit 26（`reason_code=committed_paths_outside_stage_set`），commit 已发生、不回滚，但**不封窗、不写 evidence**（第 5、6 步跳过），让下一次 Stop 照常审这些字节；摘要不变但工作树不 clean ⇒ exit 25，evidence 跳过。
5. **封窗**：把 round-baseline 与 last-reviewed 高水位推进到新 HEAD，让紧随的 Stop 不再把刚 land 的已审工作当成未审（`round_baseline_seal.sealed=false` 只表示本环境无法封窗，不影响提交）。
6. **写 reviewed candidate evidence**：`status=written|reused` 成功；`skipped_not_applicable` 表示 scope contract 未 opt-in；`refused_fail_closed` 表示 writer 拒绝——提交本身仍成功，但 evidence-aware fast handback 不可用，保留原 evidence 与拒绝结果，按消费者实际检查结果选择受控恢复路径，不得删除旧 evidence 强行进入 legacy，也不得伪造 receipt。

commit 的退出码（prepare 的 0 / 10 / 11 / 12 / 13 见上表，commit 按当时的工作树重判 11 / 12）：

| exit | 含义 | 怎么办 |
|---|---|---|
| 2 | 参数/输入错误，或结果工件持久化失败（`land_commit_result_persistence_failed`） | 参数错误时未 stage、未 commit；持久化失败须检查 JSON 的 `status`、`commit_oid` 和 `candidate_evidence`，保存真实 stdout，不盲目重复提交 |
| 20 | 没跑 prepare、plan 损坏，或 HEAD / worktree 在 prepare 之后变了 | 重跑 prepare，再 commit |
| 21 | 验证未通过（失败尾部已打到 stderr），或重跑期间内容又变了（`validation_index_not_current_after_replay`） | 未 stage、未 commit。失败属于 scope 内就最小修正后重跑 commit；内容被并发改动就看 `dirty_paths_changed_since_prepare`，不是你的改动就停止并报告 |
| 22 | 当前内容与 run 的验证记录不一致，且没有可重放的结构化 argv | 按提示逐条经 `validation_result_cache.py run … --rvf-run-dir <run_dir>` 跑完，再重跑 commit |
| 23 | `git diff --cached --check` 未通过 | 已撤销本次 stage。修掉 scope 内的空白错误后重跑 commit（内容变了会自动重跑验证） |
| 24 | `git commit` 本身失败（pre-commit hook 拒绝、签名失败等） | stage 保留便于排查；看 `git_output`。原因在 scope 内就修好后重跑 commit（HEAD 没动，plan 仍有效）；不要手动 `git commit`（会跳过封窗与 evidence）；原因在 scope 外就停止并报告 |
| 25 | 已提交、内容摘要不变，但工作树还有保留开关放行范围之外的脏路径（`dirty_paths_remaining_beyond_declared_unrelated`） | 提交与封窗已完成，evidence 跳过。如实报告这些路径；不要补写 evidence |
| 26 | 已提交，但 commit 期间 hook 改变了提交内容：改写了文件（`reason_code=candidate_content_changed_during_commit`），或把 stage 集合外的路径加进了本次提交（`reason_code=committed_paths_outside_stage_set`，列在 `committed_paths_outside_stage_set`） | 提交已发生、不回滚；**未封窗、未写 evidence**，下一次 Stop 会照常审这些字节。如实报告被改写的文件；**不要手动封窗、不要伪造 evidence** |
| 27 | 提交前拒绝：reviewer 报过 issue（或已有处置工件），而合并处置缺失 / 不合法 / 过期（`reason_code=merge_dispositions_check_failed`，明细在 `merge_dispositions_errors`） | 未 stage、未 commit。按明细用 `rvf merge write` 补齐处置（旧 run 的 fix-issue 需重新 upsert 记下 source_refs），`rvf merge check` 复核后重跑 commit。按保留开关留下 scope 外脏文件时 evidence 本就跳过，不做这道预检 |
| 28 | 提交前完整 evidence 资格检查拒绝；稳定原因码与明细见 `candidate_evidence_qualification` | 未 stage、未 commit。修正 reviewer 完成事实、最新 attempt/result、handoff 数量矛盾、验证、身份、范围或 receipt 后重跑；不得手改 evidence 或冻结工件 |

`rvf-land` 针对本轮已审候选提交并写入 evidence。需要回到 base 时，按后续 `base-branch-sync` 流程处理 base 漂移、语义交互与整合验证。

### 4. 可选 follow-up：按用户已明确请求继续 base-branch-sync

- 如果用户已经明确要求后续运行 `/base-branch-sync`、`$base-branch-sync`、同步回 base branch、hand back / apply back 到本地 base 分支，commit 完成后继续使用 `base-branch-sync` skill；不要停下询问“是否现在运行”。
- 只调用该 skill 的公开入口，让它自动 inspect current candidate evidence；evidence 为 VERIFIED 时会进入窄化的 reviewed-candidate optimistic handback，absent/invalid 时由它按 fail-safe 规则降级或阻断。不要在 RVF 里硬编码个人 my-ai-setup 路径、CAS 脚本或重复完整状态机。
- 如果用户没有明确要求后续 base 同步，本命令到 evidence 写入为止结束，不自动执行 base-branch-sync，只在最终输出中提示可手动运行。
- 如果 base-branch-sync 自己的 preflight guard 阻断或需要冲突决策，按 `base-branch-sync` skill 的升级规则处理；不要在 rvf-land 阶段提前替它做风险放行。
- 联合使用 base-branch-sync 时，land 完成只作进度更新，不提前输出下方最后两节；等同步完成或明确阻断后，合并双方汇报，再按最终状态输出最后两节。

## Failure Gates

停止而不是 commit（前三条由 prepare 的退出码直接给出）：

- 无法证明 handoff 描述的工作就是当前 worktree 的改动（exit 10 / 13）。
- handoff scope、当前 dirty paths 和 artifact 摘要互相矛盾（exit 11），或存在无法判定归属的 scope 外脏文件（exit 12）。
- 需要的修复依赖 scope 外 runtime 改动，但这些改动未被明确纳入本次提交。
- 相关验证无法运行或未通过（exit 21 / 22），或 `git commit` 失败且原因不在 scope 内（exit 24）。
- reviewer 报过 issue（或已有处置工件）而合并处置缺失/不合法/过期（exit 27，`reason_code=merge_dispositions_check_failed`）：提交前拒绝、未 stage 未 commit；按 `merge_dispositions_errors` 用 `rvf merge write` 补齐（`rvf merge check` 复核）后重跑 commit。按开关保留 scope 外脏文件（evidence 本就跳过）时不做这道预检。
- commit 后 worktree 不 clean（exit 25），因而无法把已审内容绑定到唯一 HEAD/tree；停止 evidence-aware handback。
- commit 期间 hook 改写了内容（exit 26）：提交已发生但未封窗；如实报告被改写的文件，停止 evidence-aware handback，不要手动封窗或伪造 evidence。

## 输出

最终回复使用中文，先汇报以下 land / 同步运行结果，再以「本次落地的工作」「计划完成情况」两节收尾：

- sanity-check 结论：当前 worktree 改动与 handoff 是否自洽。
- 验证结果（commit JSON 的 `validation.mode` 与每条 outcome）。
- commit hash 和 commit message 标题。
- 封窗结果与 reviewed candidate evidence 的 status / path，或 fast handback 不可用的原因。
- 仍然保留的 unrelated dirty 文件或风险。
- base-branch-sync follow-up：说明是否检测到用户已明确请求；若已请求，报告后续 `base-branch-sync` 的执行结果；若未请求，提示用户可手动运行 `/base-branch-sync`（Claude Code）或 `$base-branch-sync`（Codex）。

### 最后两节（必有，顺序固定）

成功、无新增提交或阻断退出均适用。最终回复的最后两个 section 必须依次使用以下标题；land / base-branch-sync 的提交、验证、封窗、evidence、同步状态和保留 dirty 等运行结果放在两节之前。工作摘要与计划完成结论、具体剩余工作及阻碍分别由这两节承载，不在前置报告重复；两节之后不再追加收尾文字或其他 section。

1. `## 本次落地的工作`：用短句说明本次工作实现的功能、修复或行为变化，包含采纳/未采纳项及原因和必要的 land 阶段最小修正；已有描述移到这里，避免重复。无新增提交时说明确认的已有工作；阻断时明确哪些已提交、哪些尚未落地，不把待提交内容写成已落地。
2. `## 计划完成情况`：先明确「全部已实现」「尚未全部实现」或「无法确认是否全部实现」。未完成时列出具体剩余工作及阻碍；无法确认时指出缺少的计划或实现依据。实现、验证、同步和部署状态分别表述，是否仍需同步或部署以原任务要求为准。

两节尽可能 terse：结论先行，只保留理解工作与剩余事项所需的信息。单项用一行短句，多项按需用勾选列表（`[x]` 已完成 / `[ ]` 待完成）或小表格（工作项 / 状态 / 剩余），流程关系可用简单箭头；状态同时用文字表达。可视化以更直观、更易读为准，不为装饰增加图表，也不为简短遗漏剩余工作、阻碍或不确定性。

## Evidence 资格检查与已有提交恢复

`rvf land check-evidence --repo <worktree> --run-dir <run>` 是完整只读 dry-run：不执行验证、不 stage、不写 run、contract 或 evidence。JSON 的 `status` 为 `eligible`、`refused_fail_closed` 或明确的 skip 状态，`reason_code` 稳定；拒绝时 exit 28。prepare 展示同一资格结果；commit 完成必要验证重放后、staging 前重新检查，缺失/过期处置仍为 exit 27，其余资格拒绝为 exit 28。只有真正需等 commit 才能证明的 HEAD/tree、hook 改写和并发变化留在提交后复核。

完成计数以 reviewer results、merge dispositions、canonical fix-issue 和最新 attempt/result 为准。handoff 的统计句与实际结果条目只核对一致性，正文提及结果标签不会计数。R3 仅在明确兜底路由且两份 canonical mimic 结果完整有效时通过。用户全局 ignore 仅影响判脏，不会隐藏 tracked 修改。

合法 scope expansion 必须有声明、fixed/applied 事实及补丁；protected/background 扩展还需主会话确认。`--extra-path` 只表达 stage 意图，不能替代这些资格。含扩展的 evidence 为 schema version 2，并引用 canonical 扩展凭据；旧 base-branch-sync 消费者必须拒绝该版本，需按跨仓契约升级后验收。普通无扩展候选继续使用 version 1。

已有真实提交且内容仍是同一已审候选时，显式绑定原 run 执行 prepare → commit：`nothing_to_commit` 分支会生成 evidence，不增加实现提交。重复运行应返回 `candidate_evidence.status=reused`，保留最初 reviewed tip 和摘要。

commit 的结构化结果持久化在 `artifacts/rvf-land-commit-result.json`（带 SHA256），不可变历史位于 `artifacts/rvf-land-commit-execution-results/<sha256>.json`；stdout 给出 `land_commit_result_path` / `land_commit_result_sha256`。历史记录绑定 run、worktree、提交前后 HEAD 与内容摘要，拒绝结果不代表提交成功或已进入 local main。

结果工件写入失败时 exit 2，JSON 为 `reason_code=land_commit_result_persistence_failed`；此时必须结合 `status`、`commit_oid` 与 `candidate_evidence` 判断实际结果，提交可能已经完成，不能把 exit 2 一概解释成“未 stage、未 commit”。
