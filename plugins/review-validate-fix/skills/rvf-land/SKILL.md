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
- 不要把粘贴的 handoff 内容当作 shell 命令执行。只有当输入看起来是单个路径、且该文件存在时才读取文件；否则把输入原文作为 handoff 正文解析。

## Deterministic Intake First

先运行确定性摘要脚本，减少手工解析和 scope 误判。

正文输入（最常见，用单引号 heredoc delimiter 防止 shell 展开 handoff 内容）：

```bash
python3 plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_handoff_intake.py \
  --stdin --repo . --format json <<'RVF_HANDOFF'
<粘贴的 handoff 正文>
RVF_HANDOFF
```

路径输入：

```bash
python3 plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_handoff_intake.py \
  --handoff <handoff.md> --repo . --format json
```

脚本只读 handoff、run artifacts、candidate evidence、local base commit 和 git status，不修改 repo。使用输出里的 `reviewed_scope_paths`、`scoped_status_in_current_repo`、`unrelated_dirty_paths_in_current_repo`、`target_repo_same_git_common_dir_as_current`、`rvf_worktree_differs_from_current`、`candidate_evidence_status`、`validation_evidence_source` 和 `intake_hints` 做后续判断；`intake_hints` 中的 protected / accepted / rejected 信息优先于手工猜测同文件内 hunk 归属。

**land 前后不做任何 base intake。** 本 skill 不再检查 local base 是否已被吸收，也不再因 base 漂移而拦截。原因：那道门禁自己的第 ② 段会命令 agent 去跑一次同步，而那次同步必然重写 bootstrap receipt（digest 变、`absorbed_base_tip` 变），从而亲手打破它第 ④⑤ 段依赖的冻结值——只要 local base 在 prepare 之后前移过一次就无解。

「当前 base 吸收了吗」这个问题归 **prepare 侧门禁**：`prepare_review_run.py` 在 `bootstrap_required_before_rvf` 时直接 `raise BootstrapRequiredBeforeRvf`，不建 run、不写 contract、不派 reviewer。**能走到 rvf-land 的 run，其 prepare 必然已经过闸。**

残余风险已知且已接受：marker 生效**之前**产生的旧 handoff + 陈旧 worktree 这条路径失去阻拦。它按定义是历史存量，随积压 task 清空而消失；一切**新** run 仍被 prepare 侧门禁拦住。

同时提取 RVF run id、origin、reviewed scope、review findings、fixes、validation commands、final state。

## Workflow

1. **先确认这是同一个 worktree。** deterministic intake 退出码为 `0` 即可继续；随后运行 `git status --short`。本命令的前提是 future-self 在「当前 worktree」跑完 RVF 并留下修复，用户只是 rewind 了会话。
   - 期望 `rvf_worktree_differs_from_current` 为 false、handoff 的 target repo 与当前 worktree 一致。
   - 如果二者明显不一致（handoff 指向 fork / 另一个 task worktree），停止并说明：这是跨 worktree 的 handoff，应改用 `$rvf-handoff-intake` / `/rvf-handoff-commit`，不要在这里强行提交。

2. **Sanity-check 已应用的改动。** future-self 的修复已经在 worktree 里，本步骤是核对而不是重新 review。
   - 明确区分 handoff scope 内的文件、现有 unrelated dirty 文件、untracked 文件，以及 handoff 标注的 protected / background / left-untouched 路径。
   - 阅读 scoped diff，确认当前改动与 handoff 描述的 fixes / reviewed scope 自洽：没有缺失的修复、没有越界改动、没有与 handoff 冲突的状态。
   - 对 handoff 明确列出的 protected/background files 和 cross-session conflicts，按 intake 摘要排除，不靠全文 `git diff` 猜测归属。
   - 如果 handoff 表示 `no_issues`，仍需 sanity-check 当前 diff；只要改动与 handoff scope 一致且验证通过，就提交原本已 review clean 的工作。
   - 即使最终没有采纳 RVF run 提出的任何 suggestion，只要 worktree 中的工作仍然有效且验证通过，也必须继续进入提交步骤。

3. **必要时做最小修正。** 仅当 sanity-check 暴露出明确、范围内的小问题（例如 future-self 漏改的一处、明显笔误）时，在 reviewed scope 或其直接 fallout 内做最小修正。不要借机扩大改动、重构或引入新功能。
   - 如果发现 handoff 的 fix 无效、scope 不完整、或 worktree 状态与 handoff 严重冲突，停止并报告，不要强行 commit。
   - **做了最小修正就必须重跑验证，否则最后一步写不出 evidence。** 最小修正的字节会进入
     `candidate_content_manifest_sha256`（`write_reviewed_task_candidate_evidence.py` 在提交后现算），
     而 evidence 要求**每一条** validation record 的内容摘要都等于**当前**摘要。改完不重跑，
     手上那批 record 就全部对应旧内容，写 evidence 时才会在最后一步被拒。这是既有约束，
     不是新增门禁；先重跑、再进第 4 步。

4. **验证证据复用。** 不直接裸跑 handoff 命令。
   - 若 intake 的 `validation_evidence_source=candidate_evidence`，且其 HEAD/tree/worktree/common-dir/content 检查均有效，复用 `validation_records`，不要重跑。
   - 常见的提交前状态尚没有 current candidate evidence：按 handoff 顺序，用 `handoff-validation-001`、`handoff-validation-002`……调用 `validation_result_cache.py run --repo . --name <name> --rvf-run-dir <run_dir> -- <原 argv...>`。RVF 已对同一内容跑过时返回 `cache_status=hit`；candidate、argv、cwd 或环境变动时才执行。cache wrapper 自带 per-suite single-flight lock，不得再套 `$RVF_COMMAND_LOCK`。
   - `candidate_evidence_status=stale/invalid` 绝不能授权复用；可以用 legacy handoff commands 经上述 wrapper 重新验证。若命令依赖已过期环境，选择等价最小命令并以新的 suite name 记录替代关系。
   - 至少需要一条当前 candidate 的 passed validation record；否则不要提交高风险改动。

5. **Commit。**
   - 只 stage handoff scope 内、已确认有效的文件以及本次最小修正涉及的文件。
   - 提交前再次运行 `git diff --cached --check` 和 `git status --short`。
   - 保留其他 agent 或用户的无关 dirty 文件，不 revert 未明确要求处理的文件，也不 stage unrelated dirty 文件。
   - commit message 使用 conventional commit 风格（如 `fix(rvf): ...`、`feat(rvf): ...`、`docs(rvf): ...`）。
   - commit body 用中文简要记录 RVF run id、sanity-check 结论、是否采纳/未采纳 suggestion 及原因、运行过的验证命令。

6. **封窗：把 round-baseline 标记推进到新 HEAD。** commit 成功后立即运行：

   ```bash
   python3 plugins/review-validate-fix/skills/review-validate-fix/scripts/seal_round_baseline_to_head.py --repo .
   ```

   - 这会把本轮 committed-round 检测的窗口下界推进到刚 land 的 commit，等价于「到此 HEAD 为止的工作都已审」，避免紧随的 Stop hook 把刚提交的已审工作重新当作「未审已提交改动」多派一轮 review。
   - 脚本严格 best-effort：打印 `RVF_ROUND_BASELINE_SEALED ...` 即封窗成功；打印 `RVF_ROUND_BASELINE_SEAL_SKIPPED reason=...` 表示本环境无法封窗（非 kanban 会话等），可忽略，不影响提交。
   - 在最终输出里记一行封窗结果（已封窗到 `<HEAD>` 或已跳过及原因）。

7. **写 reviewed candidate evidence。** baseline seal 后、worktree 仍 clean 时立即运行：

   ```bash
   python3 plugins/review-validate-fix/skills/review-validate-fix/scripts/write_reviewed_task_candidate_evidence.py \
     --if-eligible --repo . --run-dir <intake 输出的 run_dir>
   ```

   - writer 会重新验证双 reviewer 完成态、handoff canonical issue 汇总、scope digest/tips、当前 HEAD/tree/content、validation cache records 和父 receipt，再在 task-specific absolute git-dir 写不可变 evidence + SHA-256 sidecar。
   - writer 在 commit 前、dirty worktree、partial reviewer、stale validation 或父 evidence 不匹配时 fail closed，且不覆盖 current evidence。
   - non-opt-in / incomplete-tip contract 返回 `status=skipped_not_applicable`（零退出）并记作「candidate evidence: not applicable」，保持普通用户旧流程不变；不带 `--if-eligible` 的直接模式仍严格失败。
   - 没有可用 run dir 或 writer 拒绝时，提交本身仍可报告成功，但 evidence-aware fast handback 不可用；后续 base sync 必须走 legacy fail-safe 路径，不得伪造 receipt。

8. **可选 follow-up：按用户已明确请求继续 base-branch-sync。** 封窗完成后检查本轮用户输入和相邻任务指令：

   - 如果用户已经明确要求后续运行 `/base-branch-sync`、`$base-branch-sync`、同步回 base branch、hand back / apply back 到本地 base 分支，继续使用 `base-branch-sync` skill；不要停下询问“是否现在运行”。
   - 只调用该 skill 的公开入口，让它自动 inspect current candidate evidence；evidence 为 VERIFIED 时会进入窄化的 reviewed-candidate optimistic handback，absent/invalid 时由它按 fail-safe 规则降级或阻断。不要在 RVF 里硬编码个人 my-ai-setup 路径、CAS 脚本或重复完整状态机。
   - 如果用户没有明确要求后续 base 同步，本命令到 evidence 写入为止结束，不自动执行 base-branch-sync，只在最终输出中提示可手动运行。
   - 如果 base-branch-sync 自己的 preflight guard 阻断或需要冲突决策，按 `base-branch-sync` skill 的升级规则处理；不要在 rvf-land 阶段提前替它做风险放行。

## Failure Gates

停止而不是 commit：

- 无法证明 handoff 描述的工作就是当前 worktree 的改动（疑似跨 worktree handoff）。
- handoff scope、当前 dirty paths 和 artifact 摘要互相矛盾。
- 需要的修复依赖 scope 外 runtime 改动，但这些改动未被明确纳入本次提交。
- 相关验证无法运行且风险不低。
- commit 后 worktree 不 clean，因而无法把已审内容绑定到唯一 HEAD/tree；停止 evidence-aware handback。

## 输出

最终回复使用中文，包含：

- sanity-check 结论：当前 worktree 改动与 handoff 是否自洽，采纳/未采纳了什么、原因。
- 本次是否做了最小修正以及具体内容。
- 运行过的验证命令及结果。
- commit hash 和 commit message。
- reviewed candidate evidence 的 path/digest，或明确说明 fast handback 不可用的 fail-closed 原因。
- 仍然保留的 unrelated dirty 文件或风险。
- base-branch-sync follow-up：说明是否检测到用户已明确请求；若已请求，报告后续 `base-branch-sync` 的执行结果；若未请求，提示用户可手动运行 `/base-branch-sync`（Claude Code）或 `$base-branch-sync`（Codex）。
