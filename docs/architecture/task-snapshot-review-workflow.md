# 固定任务快照评审

本文件落实 cdd62 中用户批准的「RVF 工作流解耦、Prompt 精简与 GitHub Issue 收集」计划。

RVF 的流程为：固定任务快照 → 两路独立 reviewer → 来源匿名的合并 validate/fix → 主会话最终验证与提交 → 按既有授权执行 base 同步。

## 组件接口

- `rvf review-task-snapshot --repo <工作树> --base-ref <固定基线或本地 ref> --scope <相对路径> ... --requirements-file <需求文件>` 创建持久快照并启动独立评审，默认两路，`--reviewer-count 3` 可指定三路。数量在创建时保存，接续必须完成全部分配腿；ref 只解析一次，范围必须显式给出。`--prepare-only` 只准备输入。
- `rvf resume-task-review --run-dir <持久目录>` 接续原 run，只执行未完成部分。可附 `--fix` 执行一个合并 fixer，`--validation-commands <JSON 文件>` 执行最终验证。主会话使用普通 Git 提交；该入口也可核对已发生的提交。
- `rvf collect-issues --report <JSON 文件>` 由主 Agent 统一收集问题。`rvf collect-issues --flush` 接续持久待补写材料。失败不阻断评审、提交或同步。
- `rvf analyze --run-dir <目录>` 按需读取结果生成复盘材料，无自动启动、归档门禁或写源码权限。

run 中保留需求原文、固定基线、候选 Git 对象、显式范围、实际拼装的角色 prompt、原始 stdout/stderr 和进度。后台进程由脚本或宿主等待；重启复用同一目录。一次失败最多原路重试一次，之后改用已配置的其他可用 CLI；两路未完成如实保留。

最终验证在独立工作树执行：当前 HEAD 的已提交内容叠加已评审的任务路径，不带入范围外 staged/unstaged/untracked WIP。`validation.json` 保存完整 `tested_tree`、实际 `tested_commit`、工作树与命令；依赖随 HEAD 漂移时自动重跑原验证命令，不重跑已完成 reviewer。只在实际提交的完整树与已通过验证的树一致时登记完成。同一树的中断验证复用原验证工作树和已完成命令，保留构建产物；树发生变化则从头验证。

Validate-Fixer 返回 `incomplete` 仍保留新发现、处置、隔离修复快照与补丁；下次输入包含先前结果，工作树接续已保存修复。新范围内发现先并入匿名分配，未解决时不能进入最终验证；未完成修复不提前覆盖主工作树。明确停止在初始角色派发及等待阶段均取消并回收当前流程拥有的 worker。

补证核验为 `incomplete` 的结果按该有效状态统一读取进度和合并发现，即使原始 `outcome.json` 记录为 `failed`。原始失败事实保持不变，未完成结果不提升为 `completed`。

默认配置选择先跳过已禁用 reviewer，不读取其失效文件。角色 `findings` 与 fixer `new_findings` 的可选 `problem_key` 必须是字符串，格式异常在登记完成前进入现有失败/替代路径。角色文件与 stdout 两种结果来源统一脱敏；按 JSON 语义保留协议字段、数值与嵌套正文结构，成功、失败和原生结果恢复均处理原始报告文件。

Cline Kanban 只提供通用任务、会话、进程等待及恢复。不消费 RVF 的分界、token、阶段资格、rewind、自动 land 或收尾再投递。旧历史仍是历史，不迁移成新流程成功事实。

## Kanban 固定起点与已提交工作发现

`--base-from-kanban-task <task-id>` 与 `--base-ref` 二选一，`--scope` 和需求文件保持必填。
Kanban 适配层核对 task 创建身份、provenance incarnation、真实 worktree、Git common-dir 和目录实体，
优先沿用保存的精确 run/实现起点；未记录时仅接受创建 provenance 或首轮 checkpoint 与 Git 创建记录的等价证明。
core 只接收统一的起点证据并随 `run.json` 冻结，不读取 `~/.cline`。

开始一段新实现时，用 `record-kanban-task-review-start --task-id <id> --repo <工作树> --base-ref <实现前 SHA> --scope <路径>` 明确记录。
普通 prompt、换 session、提交进入 main 或 base 前移都不会推进起点。
`--incremental-after-run <目录>` 必须显式选择同任务、已完成且完整树验证可核对的新版 run；其范围不能盖住新路径，扩大时回到原起点。
旧 tracker、unit、封窗或 session 归属不构成新的覆盖。

任务提交发现使用 Kanban 已观察提交与当前 worktree Git 提交记录，历史改写只接受唯一的一对一 patch-id。
纯 base 同步不能因此变成任务自有提交。这里的归属仅用于发现；评审仍使用显式路径完整净 diff，包含同路径外部 hunk 作为上下文。
净 diff 为零（包括已 revert）不触发决策。

Stop 对有证据的 clean committed 改动提出一次发起决策，提交已进入 base 也不消失；同任务同观察跨会话去重。
当前范围已由关联的新版 run 完成且内容一致时不重复提醒。不会因此启动 reviewer/fixer。
unknown、损坏状态和补证诊断只持久保存，不驱动 Agent。每个来源观察只核验一次，来源变化或下一次有身份的真实 prompt 可重验；
起点文件损坏时尝试从独立持久解析记录恢复，同时保留损坏原件和每次补证结果。来源恢复后无需新增人工审批。
缺证不妨碍独立 dirty 工作走既有入口。运行材料在 RVF 用户数据目录内；`RVF_CLINE_KANBAN_STATE_DIRECTORY` 可指定隔离的 Kanban 状态目录。

base-branch-sync 根据实际 Git 状态、任务授权和必要验证保全 WIP、吸收 base 与交回提交，不读取 RVF 内部 schema、缓存、run 资格或封窗记录。同步后若产生实质新增行为，按实际风险验证；历史评审事实不被改写。

## 问题收集

主 Agent 给问题指定稳定的 `problem_key`（根因/行为身份，不含文件行号），并给本次观察指定 `observation_id`。默认仓库来自问题所属仓库的 GitHub origin；跨组件问题只选择主要修复仓库。文件移动或描述变化沿用该 key，也可显式指定已有 Issue。

查询开放及关闭 Issue，排除 pull requests；相同问题只追加 observation。写前持久保存原始报告及投递意图；结果未知先查询 Issue/评论中的标识，避免盲目重发。同机并发收集使用共享仓库锁，按仓库、问题身份及其别名共享未知创建意图，新 observation 不另建 Issue。断网、缺少凭据或限流保留待补写材料和恢复原因，不改变评审状态。后续 review/resume CLI 活动会按持久恢复时间异步补写，最多每分钟启动一次；也可由宿主通用恢复任务执行 `collect-issues --flush`。不为网络等待唤醒 Agent。

接口依据：[GitHub Issues API](https://docs.github.com/en/rest/issues/issues) 与[评论 API](https://docs.github.com/en/rest/issues/comments)。Issue 正文保存问题、影响、复现及依据，observation 保存任务/run 来源、验证和修复情况。默认不改变 Issue 状态，也不自动派发任务。

## 验收

自动化覆盖固定基线与显式范围、背景 staged/unstaged/untracked WIP 保全、独立双审、单腿失败、异常格式、有界替代、重启接续、匿名合并修复、安全应用、真实红测、提交后登记中断、base 漂移及 GitHub 去重/关闭匹配/并发/断网/未知结果对账。

角色说明以约 2,000 字符为设计目标，实际需求及 diff 不受长度门禁限制。需求、范围、diff、findings 各保存一份，prompt 引用必要材料；不要求重复阅读。部署和诊断说明按需读取。
