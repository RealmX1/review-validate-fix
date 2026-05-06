---
name: rvf-handoff-intake
description: Use when the user provides an RVF handoff path or pasted handoff content and asks Codex to analyze it, decide which suggestions or reviewed work to take, validate, stage only relevant files, and commit the main-session worktree.
---

# RVF Handoff Intake

本 skill 用于接收已经完成的 RVF handoff，并在当前主会话 worktree 中完成最终采纳、验证和提交。它不启动新的 RVF review。

## Deterministic Intake First

先运行确定性摘要脚本，减少手工解析和 scope 误判：

```bash
python3 plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_handoff_intake.py \
  --handoff <handoff.md> \
  --repo <current-main-session-repo> \
  --format json
```

如果用户粘贴的是 handoff 正文而不是路径，把正文通过 stdin 传给脚本：

```bash
python3 plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_handoff_intake.py \
  --stdin \
  --repo <current-main-session-repo> \
  --format json
```

脚本只读 handoff、run artifacts 和 git status；不会修改 repo。

## Workflow

1. 用脚本输出确认 RVF run id、run dir、reviewed scope、artifact paths、current repo dirty paths、unrelated dirty paths、`intake_hints`，以及 RVF worktree 是否只是同一 repo 的不同 worktree。
2. RVF handoff 的 target repo 常常是 Cline Kanban task / RVF fork worktree，不等于当前主会话 worktree。最终采纳、验证、stage 和 commit 必须在当前主会话 worktree 完成。
3. 阅读 scoped diff、handoff conflict hints 和 `intake_hints` 中的 protected / accepted / rejected 信息。只采纳你认为有效、且属于 reviewed scope 或直接 fallout 的内容。
4. 如果最终不采纳任何 RVF suggestion，但主会话原本工作仍有效且验证通过，也继续 commit 主会话已完成工作。
5. 只 stage scoped/accepted files。保留 unrelated dirty paths，不 revert 用户或其他 agent 的并行改动。
6. commit 前运行 `git diff --cached --check` 和与改动相关的验证命令。commit message 使用 conventional commit，body 用中文记录 RVF run id、采纳/未采纳原因和验证。

## Failure Gates

停止而不是 commit：

- 当前 repo 不是 handoff 目标对应的主会话 repo，且无法证明只是 RVF worktree 差异。
- handoff scope、当前 dirty paths 和 artifact 摘要互相矛盾。
- 需要采纳的 fix 依赖 scope 外 runtime 改动，但这些改动未被明确纳入本次提交。
- 相关验证无法运行且风险不低。
