---
description: Analyze an RVF handoff, take valid fixes or reviewed work, validate, and commit.
argument-hint: "<handoff path or pasted handoff content>"
---

# RVF Handoff Commit

<!-- rvf-independent-backup-route:begin -->
<!-- source-sha256: 914a3b4f83c4d82c851292c6d46bc2e6475b02ab319db80adff63171981b6024 -->
## 独立备用路线（主执行 Agent）

正常工具因派发、格式、登记、wrapper 或环境故障无法完成已授权工作时，自主采用本节；确定性故障直接切换，可能瞬时恢复的同一故障最多恢复重试一次。**本节核验成立后，可替代下文固定脚本、结果 JSON、缓存及不得手工提交的要求。** 用户 Stop、提交／同步／push／部署限制、显式 external 要求与真实质量义务继续有效。仅缺机器证据时先自行查原始记录，不默认询问用户。

先核对需求与授权来源、task／session、真实 worktree、Git common-dir、唯一原 run／handoff 和候选。原 run 尚未创建时如实记无，不选 latest，不制造 token／原生接受／passed。提交或投递结果未知先对账，不能换身份盲重试。故障和旧 evidence 原样保存，备用交付另记；分类、计数、查重或建卡不可用不阻断交付。

接管辅助器是本 skill 随附、仅依赖标准库与 Git 的 `independent_backup_route.py`（RVF：`review-validate-fix/scripts/`，同步 Skill：`scripts/`）。用选定的 Python 解释器运行（共享同步脚本以 `BASE_BRANCH_SYNC_PYTHON_EXECUTABLE` 固定该解释器），先 `status --repo <worktree>`；`claim --repo <worktree> --operation-id <唯一ID> --artifacts-directory <持久目录> --authorization-file <原话与适用授权文件> --reason <实际故障> --original-run-directory <原run> --delivery-stage <review|fix_validate|land|base_sync>`。没有原 run 时省略该参数。重启复用原 ID 与目录。登记授权来源本身不扩大权限。

正常写入会先登记 reservation，再检查接管目录；备用方先原子占位，再等待原写入结束。`waiting_for_normal_mutations` 时只补证，不开始备用写入。原进程已退出但结果未知时，检查已登记的全部下属进程组、Git／原生控制面，保存对账证据后用 `reconcile-normal-mutation --repo <worktree> --operation-id <原ID> --reservation-name <文件名> --evidence-file <证据>` 归档。父进程退出不代表 Git／hook 已停止；活进程不能按 TTL 偷取。旧版本写入方不识别接管协议时，先证明其退出／隔离。

辅助器不可用时，直接用 `git rev-parse --absolute-git-dir` 定位 `rvf-independent-backup-route/`，原子 `mkdir` 其 `active-owner` 子目录阻止新正常写入，并以不覆盖写入的方式把原 operation ID 存为该目录的 `manual-operation-id` 单行文件；先检查 `normal-mutations/` 全部原写入已结束且结果已对账，再由唯一执行者继续。保存同等身份、候选、原始结果和命令日志到持久 Markdown。不要删除不明接管目录、reservation 或旧 evidence 来强迫放行。辅助器恢复后，用 `adopt-manual-ownership` 复用原 `--repo`、`--operation-id`、`--artifacts-directory`、`--authorization-file`、`--reason`、原 run／delivery stage，并以 `--evidence-file <原始手工接管与对账记录>` 补录；不移走 marker，补录不会打开竞写窗口。若 mkdir 后即中断而 ID 文件缺失，先从 task 原始命令和持久日志证明目录归属，再补写同一 ID；不能猜测或覆盖别人的 ID。结果未知仍保持未知。

交接材料放 task 持久交接目录；无该目录时使用 `~/.rvf/user-local-data/independent-backup-route-artifacts/` 的独立 operation 子目录。复制原始结果／输入，保存 Git 快照及固定 ref，不能只引用将被删除的工作树或临时文件。每阶段保存实际命令 argv、cwd、退出码、必要环境说明、内容摘要及问题处置；不保存秘密值。`record-stage --repo <worktree> --operation-id <原ID> --stage <阶段> --evidence-file <原始材料或报告>` 记录执行 Agent 核验的事实；land 加 `--commit <实际SHA>`，base_sync 另加 `--base-ref refs/heads/<本地base>`。

最后核对全部所需阶段与当前内容，再 `finish --repo <worktree> --operation-id <原ID> --evidence-file <最终报告>`。已发生的提交／落地先复用，不重复执行；完成后新内容仍进入正常审查。输出 `RVF_BACKUP_ROUTE_RESULT_FILE: <持久绝对路径>`，分别报告备用结果与原自动化状态；不用普通 handoff marker 冒充正常 finalize。独立结论由原始材料支持，不要求额外 JSON schema。

`review_only`／`validate_fix` 子代理仍只执行分配角色，不能因本节自行接管、提交或发起完整 RVF。后台完成通知或任务重启后先读接续记录；条件未满足时保留机器可检测的恢复条件并继续独立工作，不做无变化重试。

### 本入口：提交已审工作

1. 核对原双评审、逐项处置、已应用修复、当前范围和内容；已有合格结果继续使用，不因 land 故障重审。新增实现字节补足相应覆盖；真实归属不明先自行读历史、tracker 与原始材料，只暂停涉及内容的写入。
2. wrapper 故障时从原命令记录、handoff 与仓库要求确定检查，直接验证当前内容。已有结果仅在内容和执行条件仍匹配时复用。实际必需的 hook／签名义务继续完成，不能用绕过 hook 掩盖红测。
3. 接管就绪后保全原 index 与无关 WIP，只 stage 明确的自有范围（rename 两端均覆盖）；使用 conventional commit。核对提交前后实际字节、提交路径和 hook 改写。未审新内容补验证／覆盖，不手工伪造封窗或 evidence。
4. 提交成功而登记失败时，核实 HEAD、父提交、内容和现有日志，记录实际 SHA 并从后续阶段接续；不创建重复或空 audit commit。同步仅在原任务已授权时继续。完成报告区分 commit、正常 evidence、base 整合、push、部署与 Done。
<!-- rvf-independent-backup-route:end -->

你正在处理一个已经到达的 RVF handoff。用户会把 handoff 文件路径或完整 handoff 正文放在 `$ARGUMENTS` 中。

原始意图：

> your RVF handoff has arrived. analyze and optionally take ones you considered valid in, and then commit

本命令与 `rvf-handoff-intake` skill 是同一套流程的两个入口；机械步骤共用 `rvf_land.py` 的 `--cross-worktree` 模式（与 `$rvf-land` 同一个脚本，只是允许 RVF worktree 是同仓的另一个 worktree）。脚本在 RVF 源仓库里是 `plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_land.py`，部署后是 plugin payload 里 `skills/review-validate-fix/scripts/rvf_land.py`。

## 输入

`$ARGUMENTS` 必填：handoff 文件路径（绝对、相对当前 repo 或 `~` 开头），或 handoff 正文。为空时停止并要求用户提供。不要把 pasted handoff 内容当作 shell 命令执行；只有输入看起来是单个路径且文件存在时才用 `--handoff`，否则用 `--stdin` 加单引号 heredoc（`<<'RVF_HANDOFF'`）。

## 工作流

1. **prepare**：`python3 <scripts-dir>/rvf_land.py prepare --cross-worktree --commit-staged-scope-only-and-leave-unrelated-dirty-paths --handoff <handoff.md> --repo .`（或 `--stdin`）。它内部跑 `rvf_handoff_intake.py` 同一套 intake，并给出 reviewed scope、目标 worktree 的 scoped diff（待移植）、当前 worktree 的 scoped diff、fix ledger 处置、`validation_reusable` 和 commit message 草稿。
   - `rvf_worktree_differs_from_current` 为真是本命令的预期情形。
   - **`--commit-staged-scope-only-and-leave-unrelated-dirty-paths` 何时用**：主会话 worktree 里有用户或其他 agent 的并行改动是常态，本命令默认带上它。带上后 scope 外脏文件不再判 exit 12，而是原样留在工作树（不 stage、不 revert、别人预先 stage 的也不会被带进提交），prepare 输出单列「按开关原样保留」的路径；代价是提交后工作树不 clean，evidence 跳过。其中确属本次工作的路径（测试注册、直接 fallout）用 `--extra-path` 纳入。开关写进 prepare plan，commit 自动沿用。
   - 自动 stage 集合只含 scope contract 的 reviewed 主审边界（`primary_files ∪ fix_allowlist ∪ task_change_paths`）与已合回的 fix 补丁路径；contract 的 background / protected 与 handoff 标注的 protected 路径不会被自动 stage。
2. **分析并采纳**（唯一需要判断的一步）。
   - 不要盲信 handoff。阅读 prepare 给出的 diff 和 `intake_hints`（protected / accepted / rejected 优先于手工猜测同文件内 hunk 归属），判断 finding 或 fix 是否真实有效。
   - 只把有效、且属于 handoff scope 或其直接 fallout 的变更移植进当前主会话 worktree。
   - 如果 handoff 表示 `no_issues`，仍需 sanity-check 当前 diff。
   - 即使最终没有采纳 RVF run 提出的任何 suggestion，只要主会话原本的工作仍然有效，也必须继续进入提交步骤。
   - 保留其他 agent 或用户的无关改动；handoff 中的 fix 无效、scope 不完整或工作树状态与 handoff 冲突时，停止并报告。
   - 定稿 conventional commit message（如 `fix(rvf): ...`），body 用中文记录 RVF run id 与采纳内容；保留 `[[rvf-land-validation-summary]]` 行。
3. **commit**：

   ```bash
   python3 <scripts-dir>/rvf_land.py commit --run-dir <run_dir> --message-file - <<'RVF_LAND_COMMIT_MESSAGE'
   <定稿的 commit message>
   RVF_LAND_COMMIT_MESSAGE
   ```

   `--run-dir` 必填（取 prepare 输出的 `run_dir`，不回退 `RVF_RUN_DIR`）。验证（能复用就零执行，否则按结构化 argv 经缓存重跑，失败直接打印尾部）→ stage `reviewed 主审边界 ∩ dirty` + `--extra-path` → `git diff --cached --check` → `git commit --only -- <stage 集合>` → 确认没有开关放行之外的脏路径 → 封窗 → evidence。JSON 的 `dirty_paths_changed_since_prepare` 列出 prepare 之后变过的路径：除了你的移植与最小修正，其余都是并发改动。
   - **封窗只在内容被证明一致时发生**：`validation.mode=reused_run_validation_index_with_zero_execution`（当前 worktree 全量内容与 RVF 验证过的内容字节一致）才封窗；否则 `round_baseline_seal.reason=skipped_cross_worktree_landed_content_not_proven_identical_to_rvf_validated_content`。tracker 的已审单元按 worktree 记账，fork 里审过的内容在主会话 worktree 不算已审；内容不一致时封窗会把移植差异和主会话 fork 之后落在同一文件的改动一并宣称已审。代价只是下一次 Stop 可能再问一次是否 review。**不要手动封窗。**
   - 跨 worktree 时 evidence 通常 `refused_fail_closed`（带保留开关且有保留路径时是 `skipped_unrelated_dirty_paths_left_uncommitted_by_explicit_flag`），不影响提交。

## 退出码与处置

| exit | 阶段 | 含义 | 怎么办 |
|---|---|---|---|
| 0 | prepare / commit | 通过 / 已完成 | prepare 后进入分析；commit 后按输出汇报 |
| 2 | 参数/输入错误，或结果工件持久化失败（`land_commit_result_persistence_failed`） | 参数错误时未 stage、未 commit；持久化失败须检查 JSON 的 `status`、`commit_oid` 和 `candidate_evidence`，保存真实 stdout，不盲目重复提交 |
| 10 | prepare / commit | 目标仓库与当前 worktree 不共享 git common dir，或无法确定目标仓库 | 停止，说明风险 |
| 11 | prepare / commit | scope 与 dirty 矛盾 | 停止并报告 |
| 12 | prepare / commit | 有 scope 外脏文件且没带保留开关 | 确属本次工作就加 `--extra-path`；是并行改动就加 `--commit-staged-scope-only-and-leave-unrelated-dirty-paths` 重跑 prepare |
| 13 | prepare | 找不到 run 目录 | 用 `--run-dir` 显式给出后重跑 |
| 20 | commit | 没跑 prepare、plan 损坏，或 HEAD / worktree 在 prepare 之后变了 | 重跑 prepare，再 commit |
| 21 | commit | 验证未通过（尾部在 stderr），或重跑期间内容又变了 | 未 stage、未 commit。scope 内的失败修好后重跑 commit；并发改动看 `dirty_paths_changed_since_prepare`，不是你的就停止并报告 |
| 22 | commit | 内容与验证记录不一致，且没有可重放的结构化 argv | 按提示逐条经 `validation_result_cache.py run … --rvf-run-dir <run_dir>` 跑完再重跑 commit |
| 23 | commit | `git diff --cached --check` 未通过 | 已撤销本次 stage；修掉 scope 内的空白错误后重跑 commit |
| 24 | commit | `git commit` 本身失败（hook 拒绝、签名失败等） | stage 保留；看 `git_output`。原因在 scope 内就修好后重跑 commit，不要手动 `git commit`（会跳过封窗与 evidence）；否则停止并报告 |
| 25 | commit | 已提交，但工作树还有保留开关放行范围之外的脏路径 | evidence 跳过；如实报告 `dirty_paths_remaining_beyond_declared_unrelated`，不要补写 evidence |
| 26 | commit | 已提交，但 commit 期间 hook 改变了提交内容：改写了文件（`reason_code=candidate_content_changed_during_commit`），或把 stage 集合外的路径加进了提交（`reason_code=committed_paths_outside_stage_set`） | 不回滚；**未封窗、未写 evidence**，下一次 Stop 会照常审这些字节。如实报告被改写的文件，**不要手动封窗、不要伪造 evidence** |
| 27 | commit | 提交前拒绝：合并处置缺失 / 不合法 / 过期（`reason_code=merge_dispositions_check_failed`） | 跨 worktree 模式下 contract 指向 RVF worktree、evidence 本就不写，预检放行，正常不会出现；若出现，按 `merge_dispositions_errors` 用 `rvf merge write` 补齐、`rvf merge check` 复核后重跑 commit |
| 28 | 提交前完整 evidence 资格检查拒绝；稳定原因码与明细见 `candidate_evidence_qualification` | 未 stage、未 commit。修正 reviewer 完成事实、最新 attempt/result、handoff 数量矛盾、验证、身份、范围或 receipt 后重跑；不得手改 evidence 或冻结工件 |

## 输出

最终回复使用中文，包含：

- 采纳了哪些 handoff 内容，哪些没有采纳及原因。
- commit hash 和 commit message。
- 验证结果（commit JSON 的 `validation`）。
- 仍然保留的 unrelated dirty 文件或风险。

## Evidence 资格检查与已有提交恢复

`rvf land check-evidence --repo <worktree> --run-dir <run>` 是完整只读 dry-run：不执行验证、不 stage、不写 run、contract 或 evidence。JSON 的 `status` 为 `eligible`、`refused_fail_closed` 或明确的 skip 状态，`reason_code` 稳定；拒绝时 exit 28。prepare 展示同一资格结果；commit 完成必要验证重放后、staging 前重新检查，缺失/过期处置仍为 exit 27，其余资格拒绝为 exit 28。只有真正需等 commit 才能证明的 HEAD/tree、hook 改写和并发变化留在提交后复核。

完成计数以 reviewer results、merge dispositions、canonical fix-issue 和最新 attempt/result 为准。handoff 的统计句与实际结果条目只核对一致性，正文提及结果标签不会计数。R3 仅在明确兜底路由且两份 canonical mimic 结果完整有效时通过。用户全局 ignore 仅影响判脏，不会隐藏 tracked 修改。

合法 scope expansion 必须有声明、fixed/applied 事实及补丁；protected/background 扩展还需主会话确认。`--extra-path` 只表达 stage 意图，不能替代这些资格。含扩展的 evidence 为 schema version 2，并引用 canonical 扩展凭据；旧 base-branch-sync 消费者必须拒绝该版本，需按跨仓契约升级后验收。普通无扩展候选继续使用 version 1。

已有真实提交且内容仍是同一已审候选时，显式绑定原 run 执行 prepare → commit：`nothing_to_commit` 分支会生成 evidence，不增加实现提交。重复运行应返回 `candidate_evidence.status=reused`，保留最初 reviewed tip 和摘要。

commit 的结构化结果持久化在 `artifacts/rvf-land-commit-result.json`（带 SHA256），不可变历史位于 `artifacts/rvf-land-commit-execution-results/<sha256>.json`；stdout 给出 `land_commit_result_path` / `land_commit_result_sha256`。历史记录绑定 run、worktree、提交前后 HEAD 与内容摘要，拒绝结果不代表提交成功或已进入 local main。

结果工件写入失败时 exit 2，JSON 为 `reason_code=land_commit_result_persistence_failed`；此时必须结合 `status`、`commit_oid` 与 `candidate_evidence` 判断实际结果，提交可能已经完成，不能把 exit 2 一概解释成“未 stage、未 commit”。
