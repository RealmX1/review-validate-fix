---
name: rvf-local-deploy
description: 当在 review-validate-fix 仓库中，用户要求 deploy、local install、sync、发布到本机 Codex plugin cache，或从当前 checkout 配置本机 stable RVF plugin/Stop hook 时使用。
---

# RVF Local Deploy

本 skill 用于把本仓库的 canonical RVF plugin 部署到本机 Codex plugin 空间。它只适用于 `review-validate-fix` 仓库。

## Preconditions

- 从仓库根目录工作：`/Users/bominzhang/Documents/GitHub/review-validate-fix`。
- 把 `plugins/review-validate-fix/` 视为唯一 canonical plugin source。
- 不要从 `~/plugins/review-validate-fix` 或 `~/.codex/plugins/cache/...` 部署；这些是安装产物。
- 先检查 `git status --short`。plugin/deployment scope 外的 background WIP 可以保持 dirty；但除非用户明确要求部署 dirty worktree，不要部署未提交的 plugin/runtime 改动。

## Default Deploy

安装前先运行 contract check：

```bash
python3 scripts/check_plugin_contracts.py
```

通过后安装 plugin 并刷新 stable Stop hook：

```bash
python3 scripts/install_to_codex.py --configure-stop-hook
```

这会更新本机 stable channel：

- `~/plugins/review-validate-fix`
- `~/.codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0`
- `~/.agents/plugins/marketplace.json`
- `~/.codex/config.toml`
- `~/.codex/hooks.json`

installer 还会写入部署日志，用于追踪「哪些 plugin 状态已经被部署」以及「部署时对应哪个 RVF trajectory / analysis run」：

- `~/plugins/review-validate-fix/skills/review-validate-fix/state/deployments/deployments.jsonl`
- `~/plugins/review-validate-fix/skills/review-validate-fix/state/deployments/latest-deployment.json`
- `~/.codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0/skills/review-validate-fix/state/deployments/deployments.jsonl`
- `~/.codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0/skills/review-validate-fix/state/deployments/latest-deployment.json`

每条记录应至少包含 source git HEAD/branch/status、runtime hash、安装目标、hook 选项，以及 `CODEX_RVF_RUN_DIR` / latest RVF run pointer 中可解析出的 run summary 和 analysis artifact paths。

安装产物中的每个 `skills/*/SKILL.md` H1 heading 还会带 `deployed <commit-prefix>` stamp；source checkout 内的 canonical `SKILL.md` 不应带该 stamp。

## Post-Deploy Checks

验证 installed runtime，而不是只验证 source checkout：

```bash
test -f /Users/bominzhang/plugins/review-validate-fix/.codex-plugin/plugin.json
test -f /Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/scripts/codex_stop_hook_router.py
python3 -m py_compile /Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/scripts/codex_stop_hook_router.py
test -f /Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/state/deployments/latest-deployment.json
test -f /Users/bominzhang/.codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0/skills/review-validate-fix/state/deployments/latest-deployment.json
rg -n "\\[deployed [0-9a-f]{12}(-dirty)?\\]" /Users/bominzhang/plugins/review-validate-fix/skills/*/SKILL.md
rg -n "\\[deployed [0-9a-f]{12}(-dirty)?\\]" /Users/bominzhang/.codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0/skills/*/SKILL.md
```

如果部署的是具体功能改动，还要检查 installed 版本中的相关文件。示例：

```bash
rg -n "SCHEMA_VERSION|ANALYSIS_SCHEMA_VERSION" \
  /Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/scripts/diff_tracker.py \
  /Users/bominzhang/plugins/review-validate-fix/skills/review-validate-fix/scripts/analysis_artifacts.py
```

## Failure Gates

遇到以下情况时停止，不要安装：

- `scripts/check_plugin_contracts.py` fails.
- 当前 checkout 不是预期 source repo。
- 部署会复制无关的未提交 plugin/runtime 改动。
- 用户要求 stable deployment，但 checkout 不在预期 branch/tag/commit。

最终回复中说明 installer 输出摘要、post-deploy checks、deploy log 路径，以及哪些 dirty paths 被有意保留未动。
