---
name: rvf-handoff-intake
description: Use when the user provides an RVF handoff path or pasted handoff content and asks Codex to analyze it, decide which suggestions or reviewed work to take, validate, stage only relevant files, and commit the main-session worktree.
---

# RVF Handoff Intake

本 skill 用于接收已经完成的 RVF handoff，并在当前主会话 worktree 中完成最终采纳、验证和提交。它不启动新的 RVF review。Claude Code 的 `/rvf-handoff-commit` 命令是同一套流程的另一个入口。

与 `$rvf-land` 的区别只在一处：这里的 RVF worktree 可以是同仓的**另一个** worktree（Cline Kanban task / RVF fork），修复需要你先移植进当前主会话 worktree。机械步骤共用同一个脚本 `rvf_land.py` 的 `--cross-worktree` 模式；脚本是与本 SKILL 同级的 `../review-validate-fix/scripts/rvf_land.py`（部署后是对应 payload 路径），下文记作 `<skill-scripts-dir>/rvf_land.py`。

## 1. prepare

```bash
python3 <skill-scripts-dir>/rvf_land.py prepare --cross-worktree --handoff <handoff.md> --repo <current-main-session-repo>
```

粘贴正文时改用 `--stdin` 加单引号 heredoc（`<<'RVF_HANDOFF'`），不要让 shell 展开 handoff 内容。prepare 内部跑 `rvf_handoff_intake.py` 同一套 intake，输出 RVF run、reviewed scope、目标 worktree 的 scoped diff（待移植）、当前 worktree 的 scoped diff、fix ledger 里每条 RVF-NNN 的处置、`validation_reusable` 与 commit message 草稿。`rvf_worktree_differs_from_current` 为真在本模式下是预期情形；目标仓库不共享 git common dir 或无法确定时 exit 10，停止说明风险。exit 11 / 12 / 13 的含义与 `$rvf-land` 相同。

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

验证（内容与 RVF worktree 字节一致时零执行复用，否则按结构化 argv 经缓存重跑，失败直接打印尾部）→ stage → `git diff --cached --check` → commit → 确认 clean → 封窗 → evidence。scope contract 绑定的是 RVF worktree，所以跨 worktree 时 evidence 通常是 `refused_fail_closed`，这是预期结果，不影响提交。

## Failure Gates

停止而不是 commit：

- 当前 repo 不是 handoff 目标对应的主会话 repo，且无法证明只是 RVF worktree 差异（exit 10）。
- handoff scope、当前 dirty paths 和 artifact 摘要互相矛盾（exit 11）。
- 需要采纳的 fix 依赖 scope 外 runtime 改动，但这些改动未被明确纳入本次提交。
- 相关验证无法运行或未通过（exit 21 / 22）。
