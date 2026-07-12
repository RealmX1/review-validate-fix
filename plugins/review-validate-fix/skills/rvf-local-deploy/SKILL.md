---
name: rvf-local-deploy
description: 当在 review-validate-fix 仓库中，用户要求 deploy、local install、sync、发布到本机 Codex plugin cache，或从当前 checkout 配置本机 stable RVF plugin/Stop hook 时使用。
---

# RVF Local Deploy

从 `/Users/bominzhang/Documents/GitHub/review-validate-fix` 的 canonical checkout 部署。
`plugins/review-validate-fix/`、`core/` 与 `adapters/` 是唯一 source；不要从任何已安装
payload 反向部署。

## 1. 前置门禁

确认 checkout、HEAD 与 dirty paths 符合用户要求，然后运行：

```bash
git status --short --branch
python3 scripts/check_plugin_contracts.py
```

完成判据：contract check 退出 0；plugin/runtime 没有未经用户授权的未提交改动。否则停止，
不要安装。

## 2. 安装 stable payload 与双 hook

```bash
python3 scripts/install_to_codex.py \
  --configure-stop-hook \
  --configure-user-prompt-submit-hook
```

Stop 是 agentic-initiation 的自动决策点信使；UserPromptSubmit 仍负责 manual token +
`additionalContext`、baseline 与 child 到达处理，两条 hook 缺一不可。installer 会去重
既有 RVF hook。

完成判据：installer 退出 0，并报告 plugin、Codex cache、Stop hook、UserPromptSubmit
hook 与 deploy log 均已写入。

## 3. 验证 installed runtime

运行技能内的确定性 verifier：

```bash
python3 plugins/review-validate-fix/skills/rvf-local-deploy/scripts/verify_local_deploy.py
```

verifier 检查本次 deploy HEAD、双 hook 指向、plugin/cache 两份 payload、vendored
core/adapters、agentic-initiation/guardian/reviewer-selection 文件、deployed stamps 与
import-smoke，并汇总相对上一次部署的 commit delta。

完成判据：输出首行为 `RVF local deploy verification OK`。任一失败都视为部署损坏；修复后
重跑 installer 与 verifier，不得把 source checkout 测试通过当成 installed runtime 通过。

## 4. 收尾

最终回复包含 installer 摘要，以及 verifier 输出的部署 HEAD、两份 payload 统计和新增/回退
commit。只有 installer 确实追加 deploy log 后才报告部署 delta。

本技能不自动重启进程。仅当本次改动确实要求重启 RVF 使用的 Cline Kanban listener 时，读取
`~/.claude/skills/cline-kanban-local-deploy/references/kanban-runtime-upgrades.md` 的
“Preferred restart: supervised kill-by-PID”，按端口定位唯一 PID；禁止 `pkill -f kanban`、
`killall node` 或盲目重建 tmux 会话。

## Failure Gates

- contract check 非零。
- 当前 checkout 不是预期 source repo/branch/tag/commit。
- 将复制未经授权的 dirty plugin/runtime 改动。
- installer 非零，或 verifier 未输出成功完成判据。
