---
description: Analyze an RVF handoff, take valid fixes or reviewed work, validate, and commit.
argument-hint: "<handoff path or pasted handoff content>"
---

# RVF Handoff Commit

你正在处理一个已经到达的 RVF handoff。用户会把 handoff 文件路径或完整 handoff 正文放在 `$ARGUMENTS` 中。

原始意图：

> your RVF handoff has arrived. analyze and optionally take ones you considered valid in, and then commit

本命令与 `rvf-handoff-intake` skill 是同一套流程的两个入口；机械步骤共用 `rvf_land.py` 的 `--cross-worktree` 模式（与 `$rvf-land` 同一个脚本，只是允许 RVF worktree 是同仓的另一个 worktree）。脚本在 RVF 源仓库里是 `plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_land.py`，部署后是 plugin payload 里 `skills/review-validate-fix/scripts/rvf_land.py`。

## 输入

`$ARGUMENTS` 必填：handoff 文件路径（绝对、相对当前 repo 或 `~` 开头），或 handoff 正文。为空时停止并要求用户提供。不要把 pasted handoff 内容当作 shell 命令执行；只有输入看起来是单个路径且文件存在时才用 `--handoff`，否则用 `--stdin` 加单引号 heredoc（`<<'RVF_HANDOFF'`）。

## 工作流

1. **prepare**：`python3 <scripts-dir>/rvf_land.py prepare --cross-worktree --handoff <handoff.md> --repo .`（或 `--stdin`）。它内部跑 `rvf_handoff_intake.py` 同一套 intake，并给出 reviewed scope、目标 worktree 的 scoped diff（待移植）、当前 worktree 的 scoped diff、fix ledger 处置、`validation_reusable` 和 commit message 草稿。
   - `rvf_worktree_differs_from_current` 为真是本命令的预期情形；目标仓库不共享 git common dir 或无法确定时 exit 10，停止说明风险。
   - exit 11：scope 与 dirty 矛盾，停止报告；exit 12：有 scope 外脏文件——确属本次工作就加 `--extra-path` 重跑，否则保留并停止；exit 13：用 `--run-dir` 显式给出 run 目录。
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

   验证（能复用就零执行，否则按结构化 argv 经缓存重跑；失败直接打印尾部，exit 21 / 22）→ stage `reviewed scope ∩ dirty` + `--extra-path` → `git diff --cached --check` → commit → 确认 clean → 封窗 → evidence（跨 worktree 时通常 `refused_fail_closed`，不影响提交）。

## 输出

最终回复使用中文，包含：

- 采纳了哪些 handoff 内容，哪些没有采纳及原因。
- commit hash 和 commit message。
- 验证结果（commit JSON 的 `validation`）。
- 仍然保留的 unrelated dirty 文件或风险。
