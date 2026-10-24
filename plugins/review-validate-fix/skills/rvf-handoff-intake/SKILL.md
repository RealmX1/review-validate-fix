---
name: rvf-handoff-intake
description: Use when the user provides an RVF handoff path or pasted handoff content and asks Codex to analyze it, decide which suggestions or reviewed work to take, validate, stage only relevant files, and commit the main-session worktree.
---

# RVF Handoff Intake

本 skill 用于接收已经完成的 RVF handoff，并在当前主会话 worktree 中完成最终采纳、验证和提交。它不启动新的 RVF review。Claude Code 的 `/rvf-handoff-commit` 命令是同一套流程的另一个入口。

与 `$rvf-land` 的区别只在一处：这里的 RVF worktree 可以是同仓的**另一个** worktree（Cline Kanban task / RVF fork），修复需要你先移植进当前主会话 worktree。机械步骤共用同一个脚本 `rvf_land.py` 的 `--cross-worktree` 模式；脚本是与本 SKILL 同级的 `../review-validate-fix/scripts/rvf_land.py`（部署后是对应 payload 路径），下文记作 `<skill-scripts-dir>/rvf_land.py`。

## 1. prepare

```bash
python3 <skill-scripts-dir>/rvf_land.py prepare --cross-worktree --commit-staged-scope-only-and-leave-unrelated-dirty-paths --handoff <handoff.md> --repo <current-main-session-repo>
```

粘贴正文时改用 `--stdin` 加单引号 heredoc（`<<'RVF_HANDOFF'`），不要让 shell 展开 handoff 内容。prepare 内部跑 `rvf_handoff_intake.py` 同一套 intake，输出 RVF run、reviewed scope、目标 worktree 的 scoped diff（待移植）、当前 worktree 的 scoped diff、fix ledger 里每条 RVF-NNN 的处置、`validation_reusable` 与 commit message 草稿。`rvf_worktree_differs_from_current` 为真在本模式下是预期情形。

**`--commit-staged-scope-only-and-leave-unrelated-dirty-paths` 何时用**：主会话 worktree 里有用户或其他 agent 的并行改动是常态，本入口默认带上它。带上后 scope 外脏文件不再判 exit 12，而是原样留在工作树（不 stage、不 revert、别人预先 stage 的也不会被带进提交），prepare 输出里单列「按开关原样保留」的路径；代价是工作树提交后不 clean，evidence 跳过。读那份列表：其中确属本次工作的路径（测试注册、直接 fallout）用 `--extra-path` 纳入，否则它们就不会进提交。开关写进 prepare plan，commit 自动沿用。

自动 stage 集合只含 scope contract 的 reviewed 主审边界（`primary_files ∪ fix_allowlist ∪ task_change_paths`）与已合回的 fix 补丁路径；contract 的 `background_files` / `protected_files` 与 handoff 标注的 protected 路径不会被自动 stage。

## 2. 判断与采纳

- 不要盲信 handoff。阅读 prepare 给出的 diff 与 `intake_hints`（protected / accepted / rejected 优先于手工猜测同文件内 hunk 归属），判断 reviewer finding 或 fix 是否真实有效。
- 只把你认为有效、且属于 reviewed scope 或其直接 fallout 的变更移植进当前 worktree。
- 如果最终不采纳任何 RVF suggestion，但主会话原本工作仍有效，也继续 commit 主会话已完成工作。
- 保留 unrelated dirty paths，不 revert 用户或其他 agent 的并行改动；scope 外但确属本次工作的路径用 `--extra-path` 纳入。
- 定稿 conventional commit message，body 用中文记录 RVF run id、采纳/未采纳原因；保留 `[[rvf-land-validation-summary]]` 行。

## 3. commit

```bash
python3 <skill-scripts-dir>/rvf_land.py commit --run-dir <run_dir> --message-file - <<'RVF_LAND_COMMIT_MESSAGE'
<定稿的 commit message>
RVF_LAND_COMMIT_MESSAGE
```

`--run-dir` 必填（取 prepare 输出的 `run_dir`，不回退 `RVF_RUN_DIR`）。依次执行：验证（当前内容与 RVF worktree 字节一致时零执行复用，否则按结构化 argv 经缓存重跑，失败直接打印尾部）→ stage → `git diff --cached --check` → `git commit --only -- <stage 集合>` → 确认没有开关放行之外的脏路径 → 封窗 → evidence。JSON 的 `dirty_paths_changed_since_prepare` 列出 prepare 之后变过的路径：除了你的移植与最小修正，其余都是并发改动。

- **封窗只在内容被证明一致时发生**：`validation.mode=reused_run_validation_index_with_zero_execution`（当前 worktree 全量内容与 RVF 验证过的内容字节一致）才封窗；否则 `round_baseline_seal.sealed=false`、`reason=skipped_cross_worktree_landed_content_not_proven_identical_to_rvf_validated_content`。原因：tracker 的已审单元按 worktree 记账，fork 里审过的内容在主会话 worktree 不算已审；内容不一致时封窗会把移植差异和主会话 fork 之后落在同一文件的改动一并宣称已审。不封窗的代价只是下一次 Stop 可能再问一次是否 review 这段提交。**不要手动封窗。**
- scope contract 绑定的是 RVF worktree，所以跨 worktree 时 evidence 通常是 `refused_fail_closed`（带保留开关且有保留路径时是 `skipped_unrelated_dirty_paths_left_uncommitted_by_explicit_flag`），这是预期结果，不影响提交。

## 退出码与处置

| exit | 阶段 | 含义 | 怎么办 |
|---|---|---|---|
| 0 | prepare / commit | 通过 / 已完成 | prepare 后进入判断；commit 后按输出汇报 |
| 2 | 参数/输入错误，或结果工件持久化失败（`land_commit_result_persistence_failed`） | 参数错误时未 stage、未 commit；持久化失败须检查 JSON 的 `status`、`commit_oid` 和 `candidate_evidence`，保存真实 stdout，不盲目重复提交 |
| 10 | prepare / commit | 目标仓库与当前 worktree 不共享 git common dir，或无法确定目标仓库 | 停止，说明风险 |
| 11 | prepare / commit | reviewed scope 与 dirty 矛盾（给不出 scope 却有改动 / scope 内有冲突文件） | 停止并报告 |
| 12 | prepare / commit | 有 scope 外脏文件且没带保留开关 | 确属本次工作就加 `--extra-path`；是并行改动就加 `--commit-staged-scope-only-and-leave-unrelated-dirty-paths` 重跑 prepare |
| 13 | prepare | 找不到 run 目录 | 用 `--run-dir <dir>` 显式给出后重跑 |
| 20 | commit | 没跑 prepare、plan 损坏，或 HEAD / worktree 在 prepare 之后变了 | 重跑 prepare，再 commit |
| 21 | commit | 验证未通过（尾部在 stderr），或重跑期间内容又变了 | 未 stage、未 commit。scope 内的失败修好后重跑 commit；并发改动看 `dirty_paths_changed_since_prepare`，不是你的就停止并报告 |
| 22 | commit | 内容与验证记录不一致，且没有可重放的结构化 argv | 按提示逐条经 `validation_result_cache.py run … --rvf-run-dir <run_dir>` 跑完再重跑 commit |
| 23 | commit | `git diff --cached --check` 未通过 | 已撤销本次 stage；修掉 scope 内的空白错误后重跑 commit |
| 24 | commit | `git commit` 本身失败（hook 拒绝、签名失败等） | stage 保留；看 `git_output`。原因在 scope 内就修好后重跑 commit，不要手动 `git commit`（会跳过封窗与 evidence）；否则停止并报告 |
| 25 | commit | 已提交，但工作树还有保留开关放行范围之外的脏路径 | evidence 跳过；如实报告 `dirty_paths_remaining_beyond_declared_unrelated`，不要补写 evidence |
| 26 | commit | 已提交，但 commit 期间 hook 改变了提交内容：改写了文件（`reason_code=candidate_content_changed_during_commit`），或把 stage 集合外的路径加进了提交（`reason_code=committed_paths_outside_stage_set`） | 不回滚；**未封窗、未写 evidence**，下一次 Stop 会照常审这些字节。如实报告被改写的文件，**不要手动封窗、不要伪造 evidence** |
| 27 | commit | 提交前拒绝：合并处置缺失 / 不合法 / 过期（`reason_code=merge_dispositions_check_failed`） | 跨 worktree 模式下 contract 指向 RVF worktree、evidence 本就不写，预检放行，正常不会出现；若出现，按 `merge_dispositions_errors` 用 `rvf merge write` 补齐、`rvf merge check` 复核后重跑 commit |
| 28 | 提交前完整 evidence 资格检查拒绝；稳定原因码与明细见 `candidate_evidence_qualification` | 未 stage、未 commit。修正 reviewer 完成事实、最新 attempt/result、handoff 数量矛盾、验证、身份、范围或 receipt 后重跑；不得手改 evidence 或冻结工件 |

## Failure Gates

停止而不是 commit：

- 当前 repo 不是 handoff 目标对应的主会话 repo，且无法证明只是 RVF worktree 差异（exit 10）。
- handoff scope、当前 dirty paths 和 artifact 摘要互相矛盾（exit 11）。
- 需要采纳的 fix 依赖 scope 外 runtime 改动，但这些改动未被明确纳入本次提交。
- 相关验证无法运行或未通过（exit 21 / 22），或 `git commit` 失败且原因不在 scope 内（exit 24）。

scope 外脏文件（exit 12）本身**不是**停止理由：用保留开关原样保留它们，照常提交。

## Evidence 资格检查与已有提交恢复

`rvf land check-evidence --repo <worktree> --run-dir <run>` 是完整只读 dry-run：不执行验证、不 stage、不写 run、contract 或 evidence。JSON 的 `status` 为 `eligible`、`refused_fail_closed` 或明确的 skip 状态，`reason_code` 稳定；拒绝时 exit 28。prepare 展示同一资格结果；commit 完成必要验证重放后、staging 前重新检查，缺失/过期处置仍为 exit 27，其余资格拒绝为 exit 28。只有真正需等 commit 才能证明的 HEAD/tree、hook 改写和并发变化留在提交后复核。

完成计数以 reviewer results、merge dispositions、canonical fix-issue 和最新 attempt/result 为准。handoff 的统计句与实际结果条目只核对一致性，正文提及结果标签不会计数。R3 仅在明确兜底路由且两份 canonical mimic 结果完整有效时通过。用户全局 ignore 仅影响判脏，不会隐藏 tracked 修改。

合法 scope expansion 必须有声明、fixed/applied 事实及补丁；protected/background 扩展还需主会话确认。`--extra-path` 只表达 stage 意图，不能替代这些资格。含扩展的 evidence 为 schema version 2，并引用 canonical 扩展凭据；旧 base-branch-sync 消费者必须拒绝该版本，需按跨仓契约升级后验收。普通无扩展候选继续使用 version 1。

已有真实提交且内容仍是同一已审候选时，显式绑定原 run 执行 prepare → commit：`nothing_to_commit` 分支会生成 evidence，不增加实现提交。重复运行应返回 `candidate_evidence.status=reused`，保留最初 reviewed tip 和摘要。

commit 的结构化结果持久化在 `artifacts/rvf-land-commit-result.json`（带 SHA256），不可变历史位于 `artifacts/rvf-land-commit-execution-results/<sha256>.json`；stdout 给出 `land_commit_result_path` / `land_commit_result_sha256`。历史记录绑定 run、worktree、提交前后 HEAD 与内容摘要，拒绝结果不代表提交成功或已进入 local main。

结果工件写入失败时 exit 2，JSON 为 `reason_code=land_commit_result_persistence_failed`；此时必须结合 `status`、`commit_oid` 与 `candidate_evidence` 判断实际结果，提交可能已经完成，不能把 exit 2 一概解释成“未 stage、未 commit”。
