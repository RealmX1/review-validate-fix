---
name: rvf-local-deploy
description: 当在 review-validate-fix 仓库中，用户要求 deploy、local install、sync 或从 local main 安装本机 Codex/Claude Code RVF plugin 时使用。
---

# RVF Local-Main Official CLI Deploy

本 skill 是稳定 trampoline。它只委托唯一 checkout `refs/heads/main` 的 local-main
worktree 中的 runner；installed plugin、官方 cache、task worktree 都不是 source checkout。

## Phase Zero

仅在用户明确要求本机部署，且工作已经完成 `rvf-land` 与 `base-branch-sync` handback 后：

1. 用 `git worktree list --porcelain` 找到唯一 `branch refs/heads/main` 的 worktree。
2. 从该 worktree 运行：

```bash
python3 scripts/rvf_local_deploy.py --expected-local-main-commit HEAD --json
```

runner 会自行验证：脚本来自 local main、HEAD 等于本地 `refs/heads/main`、整个 worktree
干净、无进行中的 Git 操作，并串行执行 contract check、生成 commit-addressed marketplace
bundle、Codex/Claude Code 官方 CLI 安装、双 cache/hook/user-local state verifier。

禁止从 task worktree 执行旧 runner；禁止使用 dirty bypass；禁止在 local-main runner 失败后
降级到手写 `~/.codex/plugins/cache`、`~/.claude/plugins/cache` 或 installed payload。此流程不
fetch、不 push，也不要求 commit 存在于 remote。

## Completion

仅当 runner 退出 0 且 JSON `status` 为 `ok` 才报告成功。最终证据必须包含：

- `local_main_worktree`、`local_main_commit` 与生成的 plugin version；
- Codex/Claude marketplace 和 plugin selector；
- 双官方 cache digest 与 active release 一致；
- Stop/UserPromptSubmit hook 与 `RVF_USER_LOCAL_DATA_ROOT` 验证；
- 真实 reviewer routing smoke 及 fail-closed 结果（任务要求时）。

失败时报告 `phase`、`installed_before/after`、`error` 和 receipt path；修复后重新从干净
local main 运行。本 skill 不自动执行 `rvf-land`、`base-branch-sync`、fetch、push 或 Cline
Kanban listener restart。
