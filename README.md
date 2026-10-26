# Review Validate Fix

固定任务快照，两路独立评审，匿名验证修复，主会话直接验证与提交。

```sh
rvf review-task-snapshot --repo /path/to/worktree --base-ref main \
  --scope src --scope tests --requirements-file /persistent/task-requirements.md
rvf resume-task-review --run-dir /persistent/exact-run-directory --fix
rvf resume-task-review --run-dir /persistent/exact-run-directory \
  --validation-commands /persistent/final-validation-commands.json
```

`--base-ref` 只解析一次；`--scope` 明确声明任务改动，背景 staged/unstaged/untracked WIP 保持原样。需求文件保留用户原话、有效授权及必要实现上下文。运行材料默认写入 `~/.rvf/user-local-data/task-snapshot-reviews/`；重启接续同一 run。两位 reviewer 各使用独立新进程与工作树，一个 fixer 合并处理匿名发现。每个 CLI 最多原路重试一次，再使用其他可用配置；未完成和真实红测不会计为成功。

最终验证的 JSON 是 argv 数组列表，例如 `[["python3","-m","pytest","-q","tests"]]`。主 Agent 核对结果后用普通 Git 精确暂存并提交，再接续 run 核对实际提交。RVF 不 rewind、不创建独立 land 会话；base-branch-sync 根据实际 Git 状态及既有授权独立运行。

主 Agent 通过 `rvf collect-issues` 统一收集问题，同根因沿用一个 Issue，新观察追加评论，匹配开放与关闭的问题。断网和未知写入结果保留原始材料，`--flush` 查询实际远端结果后接续；问题收集不阻断评审、提交或同步。格式见[问题观察报告](plugins/review-validate-fix/skills/review-validate-fix/references/github-issue-observation-report.md)。

`rvf analyze --run-dir <目录>` 按需复盘，不自动运行或作为完成门禁。Cline Kanban 只提供通用任务、会话及进程恢复。

开发验证：`bash scripts/check_skill_contracts.sh`。本机安装使用 [rvf-local-deploy](plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md)，只从已核对的 local main 构建独立 release，再通过宿主官方插件 CLI 安装。

[流程与组件接口](docs/architecture/task-snapshot-review-workflow.md) · [Hook 受众契约](docs/architecture/stop-hook-output-audience-contract.md)
