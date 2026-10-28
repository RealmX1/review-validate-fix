# Review Validate Fix

一次启动固定任务快照，两路独立评审、匿名修复和完整树验证自动推进，一次结果交回主 Agent 审阅与提交。

```sh
rvf review-task-snapshot --repo /path/to/worktree --base-ref main \
  --scope src --scope tests --requirements-file /persistent/task-requirements.md \
  --run-directory /persistent/exact-run-directory --fix \
  --validation-commands /persistent/final-validation-commands.json \
  --implementation-plan-file /persistent/implementation-plan-items.json
```

`--base-ref` 只解析一次；`--scope` 明确声明任务改动，背景 staged/unstaged/untracked WIP 保持原样。需求文件保留用户原话、有效授权及必要实现上下文。运行材料默认写入 `~/.rvf/user-local-data/task-snapshot-reviews/`；重启接续同一 run。两位 reviewer 各使用独立新进程与工作树，一个 fixer 合并处理匿名发现。每个 CLI 最多原路重试一次，再使用其他可用配置；未完成和真实红测不会计为成功。

最终验证的 JSON 是 argv 数组列表，例如 `[["python3","-m","pytest","-q","tests"]]`；实施计划是条目字符串列表。准备命令、机器上下文和 Kanban 托管选项也在启动时登记，详见 [skill](plugins/review-validate-fix/skills/review-validate-fix/SKILL.md)。无 finding 的红测仍自动进入修复，默认最多三轮语义修复；重启用 `rvf resume-task-review --run-dir <精确目录>` 接续已冻结材料，已完成 reviewer 不重跑。

RVF 顺利完成并经主 Agent 最终审阅后，默认自主精确暂存并 commit；用户明确的不得提交等限制优先。普通 elevation 不阻止提交，涉及回滚或大范围重写的 elevation 才交具体判断。提交后用 `resume-task-review --run-dir <精确目录> --closeout-report <JSON>` 核对实际提交，立即展示计划 implementation 完成度、工作 recap、验证、commit 和遗留，先于 base-branch-sync。RVF 不 rewind、不创建独立 land 会话；同步根据实际 Git 状态及既有授权独立运行。

主 Agent 通过 `rvf collect-issues` 统一收集问题，同根因沿用一个 Issue，新观察追加评论，匹配开放与关闭的问题。断网和未知写入结果保留原始材料，`--flush` 查询实际远端结果后接续；问题收集不阻断评审、提交或同步。格式见[问题观察报告](plugins/review-validate-fix/skills/review-validate-fix/references/github-issue-observation-report.md)。

`rvf analyze --run-dir <目录> --run-dir <另一目录>` 只读复盘原始状态、恢复依据和分项量测，缺失 usage 保持 unknown，不作为完成门禁。Cline Kanban 只提供通用任务、会话、所属命令执行及可靠结果交回。

开发验证：`bash scripts/check_skill_contracts.sh`。本机安装使用 [rvf-local-deploy](plugins/review-validate-fix/skills/rvf-local-deploy/SKILL.md)，只从已核对的 local main 构建独立 release，再通过宿主官方插件 CLI 安装。

[流程与组件接口](docs/architecture/task-snapshot-review-workflow.md) · [Hook 受众契约](docs/architecture/stop-hook-output-audience-contract.md)
