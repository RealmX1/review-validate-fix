# 固定任务快照评审

本文件落实 cdd62 中用户批准的「RVF 工作流解耦、Prompt 精简与 GitHub Issue 收集」计划。

RVF 的流程为：固定任务快照 → 两路独立 reviewer → 来源匿名的合并 validate/fix → 主会话最终验证与提交 → 按既有授权执行 base 同步。

## 组件接口

- `rvf review-task-snapshot --repo <工作树> --base-ref <固定基线或本地 ref> --scope <相对路径> ... --requirements-file <需求文件>` 创建持久快照并启动两路独立评审。ref 只解析一次；范围必须显式给出。`--prepare-only` 只准备输入。
- `rvf resume-task-review --run-dir <持久目录>` 接续原 run，只执行未完成部分。可附 `--fix` 执行一个合并 fixer，`--validation-commands <JSON 文件>` 执行最终验证。主会话使用普通 Git 提交；该入口也可核对已发生的提交。
- `rvf collect-issues --report <JSON 文件>` 由主 Agent 统一收集问题。`rvf collect-issues --flush` 接续持久待补写材料。失败不阻断评审、提交或同步。
- `rvf analyze --run-dir <目录>` 按需读取结果生成复盘材料，无自动启动、归档门禁或写源码权限。

run 中保留需求原文、固定基线、候选 Git 对象、显式范围、实际拼装的角色 prompt、原始 stdout/stderr 和进度。后台进程由脚本或宿主等待；重启复用同一目录。一次失败最多原路重试一次，之后改用已配置的其他可用 CLI；两路未完成如实保留。

Cline Kanban 只提供通用任务、会话、进程等待及恢复。不消费 RVF 的分界、token、阶段资格、rewind、自动 land 或收尾再投递。旧历史仍是历史，不迁移成新流程成功事实。

base-branch-sync 根据实际 Git 状态、任务授权和必要验证保全 WIP、吸收 base 与交回提交，不读取 RVF 内部 schema、缓存、run 资格或封窗记录。同步后若产生实质新增行为，按实际风险验证；历史评审事实不被改写。

## 问题收集

主 Agent 给问题指定稳定的 `problem_key`（根因/行为身份，不含文件行号），并给本次观察指定 `observation_id`。默认仓库来自问题所属仓库的 GitHub origin；跨组件问题只选择主要修复仓库。文件移动或描述变化沿用该 key，也可显式指定已有 Issue。

查询开放及关闭 Issue，排除 pull requests；相同问题只追加 observation。写前持久保存原始报告及投递意图；结果未知先查询 Issue/评论中的标识，避免盲目重发。同机并发收集使用共享仓库锁。断网、缺少凭据或限流保留待补写材料和恢复原因，不改变评审状态。后续 review/resume CLI 活动会按持久恢复时间异步补写，最多每分钟启动一次；也可由宿主通用恢复任务执行 `collect-issues --flush`。不为网络等待唤醒 Agent。

接口依据：[GitHub Issues API](https://docs.github.com/en/rest/issues/issues) 与[评论 API](https://docs.github.com/en/rest/issues/comments)。Issue 正文保存问题、影响、复现及依据，observation 保存任务/run 来源、验证和修复情况。默认不改变 Issue 状态，也不自动派发任务。

## 验收

自动化覆盖固定基线与显式范围、背景 staged/unstaged/untracked WIP 保全、独立双审、单腿失败、异常格式、有界替代、重启接续、匿名合并修复、安全应用、真实红测、提交后登记中断、base 漂移及 GitHub 去重/关闭匹配/并发/断网/未知结果对账。

角色说明以约 2,000 字符为设计目标，实际需求及 diff 不受长度门禁限制。需求、范围、diff、findings 各保存一份，prompt 引用必要材料；不要求重复阅读。部署和诊断说明按需读取。
