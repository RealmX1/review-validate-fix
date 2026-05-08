# Potential work: Cline Kanban bootstrap full dirty overlay

## 背景

当前 `cline-kanban` stop hook 路径创建新 Kanban task/worktree 时，`worktree-bootstrap` 只重放父 Codex chat session 归属的 `session_owned_dirty_paths`。这会把“新 agent 能看到的 worktree 状态”和“本轮 review/fix focus”绑定到同一个 session scope。

理想语义可能应拆成两层：

- worktree state：新 Cline Kanban task 应获得父 worktree 的完整 uncommitted overlay，包括 tracked、staged/unstaged、untracked 的相关文件状态。
- focus/scope：review、validate/fix 和报告仍应只聚焦父 chat session 或 tracker allocation 分配给本轮的 scope。

换句话说，agent 的环境应尽量还原父 worktree，而 reviewer 的职责边界应继续收窄。

## 需要进一步评估

这不是已经定案的实现要求。它需要和 pending global reviewed-diff tracker 设计一起考虑，尤其是：

- full dirty overlay 是否会让 Kanban task worktree 中出现其他 session 的 WIP，从而影响 reviewer 判断。
- `scope.contract.json`、review packet、fix allowlist 和 protected/background files 是否足以防止 agent 把环境依赖误当作 review focus。
- global tracker 落地后，worktree overlay units、session assignment、reviewer lease、manual fork takeover 之间如何表达“环境可见但不可领取为 scope”。
- untracked files、large/binary files、generated files 和 excluded prefixes 是否应该全量 bootstrap，还是按 tracker metadata 分类复制。
- Cline Kanban 独立 worktree 的 bootstrap 是否应成为 tracker refresh 的一类 observation，而不是继续依赖旧 `session_owned_dirty_paths`。
- 跨 agent communication system 是否能减少 full dirty overlay 带来的误判风险：不同 Kanban task、Codex session、reviewer 和 validate/fix agent 之间可能需要共享“哪些 diff 是环境依赖、哪些 diff 是当前 focus、哪些 diff 已被其他 agent lease/review”的状态。优先评估可复用的开源方案，而不是直接自研一套长期运行的 agent bus。

## 相关参考

- 旧版计划参考：[global-reviewed-diff-tracker-overhaul-plan.md](global-reviewed-diff-tracker-overhaul-plan.md)。该文件记录了 tracker overhaul 的一个历史版本，不应被视为最终或最新设计；本 potential work 只引用它作为待协调方向。

## 初步实现方向草案

如果后续确认要做，可以考虑：

1. 在 `prepare_review_run.py` 中把 worktree bootstrap input 从 `session_owned_dirty_paths` 改为 tracker/worktree overlay manifest。
2. 继续让 review packet 和 `scope.contract.json` 使用 tracker allocated scope 或 session-owned scope。
3. 在 bootstrap metadata 中显式区分 `environment_paths`、`primary_scope_paths`、`background_paths`、`protected_paths`。
4. 在 Cline Kanban prompt 中把“先还原环境”与“不得扩大 review/fix scope”写成两个独立约束。
5. 评估是否引入或适配现有开源 cross-agent communication / coordination 组件，用于同步 tracker lease、agent heartbeat、scope ownership 和 handoff 摘要。
6. 增加测试覆盖：父 worktree 有 session-owned diff 和 unrelated dirty diff 时，Kanban worktree 能看到两者，但 scope contract 只允许处理前者。
