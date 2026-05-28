---
name: rvf-land
description: Use only when the user explicitly invokes $rvf-land or /rvf-land. 在同一个 worktree 中收尾一段由 future-self 跑完的 RVF 工作：吃下用户粘贴的 RVF handoff / finalization 正文，sanity-check 已经应用在当前 worktree 的修复，必要时做最小修正，验证后提交。不启动新的 RVF review，也不自动运行 base-branch-sync。
---

# RVF Land

本 skill 用于在「同一个 worktree」中收尾一段已经由 future-self 跑完的 RVF 工作并提交。它不启动新的 RVF review，也不自动运行 base-branch-sync。

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

脚本只读 handoff、run artifacts 和 git status，不修改 repo。使用输出里的 `reviewed_scope_paths`、`scoped_status_in_current_repo`、`unrelated_dirty_paths_in_current_repo`、`target_repo_same_git_common_dir_as_current`、`rvf_worktree_differs_from_current` 和 `intake_hints` 做后续判断；`intake_hints` 中的 protected / accepted / rejected 信息优先于手工猜测同文件内 hunk 归属。

同时提取 RVF run id、origin、reviewed scope、review findings、fixes、validation commands、final state。

## Workflow

1. **确认这是同一个 worktree。** 运行 `git status --short`。本命令的前提是 future-self 在「当前 worktree」跑完 RVF 并留下修复，用户只是 rewind 了会话。
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

4. **验证。** 优先运行 handoff 中列出的 validation commands。
   - 如果命令依赖不存在的环境变量、临时路径或已过期 worktree，选择当前 repo 中等价的最小验证命令，并说明替代关系。
   - 至少运行一次与改动直接相关的语法检查、单测或契约检查；如果无法运行，说明原因，并且不要在验证不足时提交高风险改动。

5. **Commit。**
   - 只 stage handoff scope 内、已确认有效的文件以及本次最小修正涉及的文件。
   - 提交前再次运行 `git diff --cached --check` 和 `git status --short`。
   - 保留其他 agent 或用户的无关 dirty 文件，不 revert 未明确要求处理的文件，也不 stage unrelated dirty 文件。
   - commit message 使用 conventional commit 风格（如 `fix(rvf): ...`、`feat(rvf): ...`、`docs(rvf): ...`）。
   - commit body 用中文简要记录 RVF run id、sanity-check 结论、是否采纳/未采纳 suggestion 及原因、运行过的验证命令。

本命令到提交为止结束，不自动运行 base-branch-sync。

## Failure Gates

停止而不是 commit：

- 无法证明 handoff 描述的工作就是当前 worktree 的改动（疑似跨 worktree handoff）。
- handoff scope、当前 dirty paths 和 artifact 摘要互相矛盾。
- 需要的修复依赖 scope 外 runtime 改动，但这些改动未被明确纳入本次提交。
- 相关验证无法运行且风险不低。

## 输出

最终回复使用中文，包含：

- sanity-check 结论：当前 worktree 改动与 handoff 是否自洽，采纳/未采纳了什么、原因。
- 本次是否做了最小修正以及具体内容。
- 运行过的验证命令及结果。
- commit hash 和 commit message。
- 仍然保留的 unrelated dirty 文件或风险。
- 提示用户：如需把本次改动同步回 task 的 base branch，可手动运行 `/base-branch-sync`（Claude Code）或 `$base-branch-sync`（Codex）；本命令不会自动执行。
