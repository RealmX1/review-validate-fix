# Session-scoped change tracking plan

## 后续替代方向

本文件描述的是当前已落地的 per-chat session tracking。它仍是现有运行期行为的基础，但不再是长期并发模型。下一阶段设计应迁移到 repo 级 global reviewed-diff tracker：在 repo 下按 branch/worktree 维护 diff units、chat session assignment、reviewer lease、activity probe 和 stale release。详见仓库文档 `docs/global-reviewed-diff-tracker-overhaul-plan.md`。

## 目标

恢复 Claude Code 版本中“只审查当前 chat session 修改”的能力，避免多个 Codex 会话、reviewer 或 validate/fix agent 共用同一个 worktree 时，把其他会话的未提交改动混进本轮 `$review-validate-fix` scope。

## 当前问题

Codex 兼容版目前用 `git status` / `git diff HEAD` 构建 review packet。这个证据很完整，但不是 session-scoped：同一仓库里任何未提交改动都会进入 reviewer 上下文。旧 Claude 版本依赖 `PostToolUse(Write|Edit)` activity hook 做 session 级去重和归属；Codex 当前 hook 覆盖仍不稳定，不能直接假设存在等价的 post-edit hook。

## 实施策略

第一阶段采用 transcript-derived manifest：

- 从 Codex JSONL transcript 中解析 `apply_patch` 调用，提取 add/update/delete/move 的路径。
- 从 `exec_command` 调用中保守提取显式写入候选，例如 shell redirect、`tee`、`touch`、`mkdir`、`rm`、`mv`、`cp`。
- 用当前 `git status --porcelain -z -uall` 把 dirty paths 分成 `owned_dirty_paths` 与 `unattributed_dirty_paths`。
- 生成 `session-manifest.json`，作为 review packet 的 session scope anchor。

第二阶段在 Codex hook 能力稳定后升级为 hook ledger：

- `PostToolUse apply_patch` 直接记录 patch path/hunk。
- `PreToolUse exec_command` 记录 workspace snapshot。
- `PostToolUse exec_command` 比较 snapshot，记录命令造成的 path/hash delta。
- Stop hook 从 ledger 生成同一份 manifest schema。

第三阶段把高并发 validate/fix agent 转为独立 worktree/branch：

- 每个写入 agent 在隔离 worktree 工作。
- RVF 只合并 session-owned patch 或分支，减少 shared dirty worktree 的归属问题。

## Manifest 合约

`session-manifest.json` 是 JSON object，核心字段：

- `owned_paths`：当前 session 明确触碰或高概率触碰的路径。
- `owned_dirty_paths`：当前 workspace 中仍 dirty 且属于 `owned_paths` 的路径。
- `unattributed_dirty_paths`：workspace dirty 但没有归属到当前 session 的路径。
- `apply_patch_operations`：从 transcript 中抽取的 apply_patch 文件操作。
- `command_path_candidates`：从 shell 命令文本中保守推断的写入候选。
- `confidence`：`medium` 表示至少有 apply_patch 证据；`low` 表示只有命令文本等弱证据。

## Review packet 行为

当 `build_review_packet.py` 收到 `--session-manifest`：

- `Session Manifest` section 成为 reviewer 的 scope anchor。
- `Session-Owned Git Diff` 是默认审查 diff。
- `Full Git Diff HEAD (Evidence Only)` 只作为依赖核实时的辅助证据。
- 未归属 untracked 文件只列路径，不内联内容。
- session-owned untracked 文件才内联内容。

## 失败策略

如果 manifest 无法生成或没有可靠 owned path，不应静默退回 whole-repo scope。主会话必须显式说明缺少 session ownership 证据，并让用户选择：手写 scope-of-work、传入 manifest、或明确要求 full diff review。
