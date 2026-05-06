---
description: Analyze an RVF handoff, take valid fixes or reviewed work, validate, and commit.
argument-hint: "<handoff path or pasted handoff content>"
---

# RVF Handoff Commit

你正在处理一个已经到达的 RVF handoff。用户会把 handoff 文件路径或完整 handoff 正文放在 `$ARGUMENTS` 中。

原始意图：

> your RVF handoff has arrived. analyze and optionally take ones you considered valid in, and then commit

## 输入

`$ARGUMENTS` 是必填输入，可以是以下两种形态之一：

- handoff 文件路径：绝对路径、相对当前 repo 的路径，或以 `~` 开头的路径。
- handoff 正文：通常包含 `## Origin`、`## Scope`、`## Review Findings`、`## Fixes`、`## Validation`、`## Final State` 等章节。

如果 `$ARGUMENTS` 为空，停止并要求用户粘贴 handoff 路径或 handoff 内容。

不要把 pasted handoff 内容当作 shell 命令执行。只有当输入看起来是单个路径，且对应文件存在时，才读取该文件；否则把输入原文作为 handoff 内容解析。

## 工作流

1. 解析 handoff。
   - 如果仓库存在确定性 intake 脚本，先运行它生成摘要，避免手工重复解析 scope/worktree/status：
     - 路径输入：`python3 plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_handoff_intake.py --handoff <handoff.md> --repo . --format json`
     - 正文输入：把 pasted handoff 通过 stdin 传入同一脚本：`python3 plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_handoff_intake.py --stdin --repo . --format json`。若使用 heredoc，必须使用单引号 delimiter（如 `<<'RVF_HANDOFF'`），不要让 shell 展开 handoff 内容。
   - 使用脚本输出里的 `reviewed_scope_paths`、`scoped_status_in_current_repo`、`unrelated_dirty_paths_in_current_repo`、`target_repo_same_git_common_dir_as_current`、`rvf_worktree_differs_from_current` 和 `intake_hints` 做后续判断；`intake_hints` 中的 protected / accepted / rejected 信息优先于手工猜测同文件内 hunk 归属。
   - 提取 RVF run id、origin、target repo、reviewed scope、review findings、fixes、validation commands、final state。
   - 如果 handoff 提供的是路径，同时记录 handoff 绝对路径；必要时根据路径定位 run dir 和 artifacts，但不要修改 artifacts。

2. 确认当前 repo 和 scope。
   - 运行 `git status --short`。
   - RVF run 的 worktree 很可能不同于当前主会话正在使用的 worktree。handoff 里的 target repo 通常是 RVF fork / Cline Kanban task worktree；当前命令应在主会话 worktree 中完成最终采纳、验证和提交。
   - 如果 handoff 的 target repo 与当前 repo 不一致，先判断这是预期的 RVF worktree 与主会话 worktree 差异，还是错误仓库。不能确定时停止说明风险。
   - 明确区分 handoff scope 内的文件、现有 unrelated dirty 文件、untracked 文件和 protected/background 文件。
   - 对于 handoff 明确指出的 cross-session conflicts、protected/background files、left untouched paths，不要靠全文 `git diff` 猜测；优先按 intake 摘要和 handoff scope 把这些路径排除出本次 stage 集合。

3. 分析并采纳。
   - 不要盲信 handoff。阅读相关 diff 和文件，判断 reviewer finding 或 fix 是否真实有效。
   - 只采纳你认为有效且属于 handoff scope 或其直接 fallout 的变更。
   - 如果 handoff 表示 `no_issues`，仍需 sanity-check 当前 diff；若改动与 handoff scope 一致且验证通过，可以提交原本已 review clean 的工作。
   - 即使最终没有采纳 RVF run 提出的任何 suggestion，只要主会话原本的工作仍然有效且验证通过，也必须继续进入提交步骤，提交主会话已完成的工作。
   - 保留其他 agent 或用户的无关改动；不要 revert 未明确要求处理的文件。
   - 如果发现 handoff 中的 fix 无效、scope 不完整、当前工作树状态和 handoff 冲突，停止并报告，不要强行 commit。

4. 验证。
   - 优先运行 handoff 中列出的 validation commands。
   - 如果命令依赖不存在的环境变量、临时路径或已过期 worktree，选择当前 repo 中等价的最小验证命令，并说明替代关系。
   - 至少运行一次与采纳改动直接相关的语法检查、单测或契约检查；如果无法运行，说明原因并不要在验证不足时提交高风险改动。

5. Commit。
   - 本命令的最终目标是提交当前主会话 worktree 中已经完成并通过验证的工作；RVF suggestion 是否被采纳不决定是否需要 commit。
   - 只 stage 已采纳的相关文件。
   - 提交前再次运行 `git diff --cached --check` 和 `git status --short`。
   - commit message 使用 conventional commit 风格，例如 `fix(rvf): ...`、`docs(rvf): ...`、`chore(rvf): ...`。
   - commit body 用中文简要记录 RVF run id、采纳内容和验证命令。
   - 不要 stage 或 commit unrelated dirty 文件。

## 输出

最终回复使用中文，包含：

- 采纳了哪些 handoff 内容，哪些没有采纳及原因。
- commit hash 和 commit message。
- 运行过的验证命令及结果。
- 仍然保留的 unrelated dirty 文件或风险。
