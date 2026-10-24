# 5de70：evidence 资格判定与正式恢复交付

本任务基于本地 `main` 的 `a51f959a47513a522ddb87aaf6444352b05b5019`，在 `task/5de70-reviewed-candidate-evidence-qualification` 实施。只交付本任务本地提交；不 push、不合回 main、不安装或部署，不修改消费 Task 的 worktree、原 run 或自动投递账本。

## 实现结果

| 项目 | 实际行为与验收 |
| --- | --- |
| B1：手写统计与标签 | 完成计数来自 reviewer results、merge dispositions、canonical issue、最新 attempt/result。旧 applied 不遮住更新的失败。六种原文格式、否定标签正文及两个完整原始 handoff 均有回归；真实升级、缺失/过期处置、未应用、矛盾统计仍拒绝 |
| 完整提交前检查 | `rvf land check-evidence` 为无写入入口；prepare 展示资格，commit 在 staging 前执行同源资格检查。保留 exit 27，新增 exit 28。父 evidence、integration receipt、scope、验证与身份都先检查，再做 restamp 或正式写入 |
| B2：合法扩展 | 声明、applied patch、protected/background 的主会话确认构成补充范围；不改原四个冻结路径集合。version 2 evidence 绑定 canonical 扩展凭据，本仓 writer/reader 共用判据。跨仓消费者尚需升级 |
| B3：全局 ignore | writer 判脏与 intake/land 共用用户配置口径；tracked 修改仍拒绝。本地 `.claude/settings.local.json` 不被误报或自动提交，内容清单算法保持原有口径 |
| B4：R3 | 共享取腿与公共结果协议检查；明确 R3 路由下恰好两份有效 mimic 结果可通过。缺腿、额外腿、failed plan 仍拒绝。复算时近期 150 份 plan 中 R3 为 0，线上 R3 验收没有冒用隔离测试结果 |
| 已有提交恢复 | `nothing_to_commit` 可正式写 evidence；重复运行保持最初 reviewed tip 与相同 evidence digest，不新增实现提交 |
| E1：已暂存删除 | 避免对已从 index 删除的缺失路径再次 git add，删除仍纳入实际 stage/commit 集合；真实 Git 回归通过 |
| E6：结果持久化 | run 内保留当前结果与内容寻址历史，绑定 run/worktree/前后 HEAD/内容摘要。即使结果工件写入失败，stdout 也如实保留已提交或 evidence 已写成的状态 |

实现没有从 handoff 文字回退推断完成事实的临时兼容分支。普通 v1 与扩展 v2 是明确的当前契约区分；未将扩展候选伪装成 v1 交给旧消费者。未修改 hook stdout 代码。

跨仓字段、校验和消费者升级要求见 [资格与扩展契约](../architecture/reviewed-candidate-evidence-qualification-and-scope-expansion-contract.md)。

## 失败与成功样本的实际采纳

| 样本 | 本卡实际核验或回归 | 结论边界 |
| --- | --- | --- |
| `66679` / `81a2e` | 原 handoff 字节与 SHA 保持不变；同一已安装旧 writer 均仍报 `handoff canonical issue summary is missing`，本任务完成检查分别返回 4 / 6。隔离 fixture 用完整原文字节经正式 writer 生成 evidence | 没有对两个真实 run 调用 writer、补 evidence 或代提交 |
| `46d51` | 实际读取三条 canonical issue 的 applied 事实，核验两条已声明测试扩展；隔离正式 land 正例和缺声明/应用/确认负例 | 原直接阻塞是 paused；未将其尚未执行的 writer 风险写成已发生拒写 |
| `c4c82` | 实际读取四条 canonical issue、三条已应用 Web UI 扩展；在回归中复用这三条路径，保持 4 个 canonical issue 的形状 | 同样没有改暂停状态或运行生产 writer |
| `199e6` | 采用 `test/runtime/terminal/prepare-agent-launch-asserted-relation-injection-receipt-only-when-relation-text-reaches-launch.test.ts` 为未审追加路径负例；即使 extra-path 且当前验证通过，也在提交前拒绝 | 没有把它视作已声明并 applied 的 fixer 扩展 |
| `38fd3` | 读取并校验成功对照材料；同一旧 writer 对原件返回 `(0, no_issues)`。新增真实 Git 正例：13/13 路径、六条当前验证、零 issue/attempt，正式 prepare/commit 写 v1；重复恢复复用 evidence、不移动 HEAD 或 reviewed tip、不重跑验证 | fixture 复用路径归属和条件，文件内容属于隔离测试；未重复操作成功 Task 的生产 Git、base 或 WIP |
| `03cbbd43` | 原件仍因缺少 T4 merge dispositions 被拒绝；否定标签正文的修复以完整结构化 fixture 验证 | 没有为旧 run 编造缺失的处置 |

`38fd3` 与 B1 两个失败样本使用的已安装 writer SHA256 均为 `cbd8f02039311529155b6f563721a97e80a1122f8edd97e75331559f767672a0`。本卡重跑的只读对照表明：标准零 issue 输入成功、两种同义统计句失败，属于输入条件触发的确定性拒绝；这不能证明本卡修复已经上线。

`9c9c1`、`df81c`、`30555` 是 646eb 报告中的背景成功案例。本卡没有对这三份 run 另作独立全链路重放，不将它们计入本次新增验收。实际详细采纳的成功 Session 为 `38fd3`，守住的是本来合法的零账本、v1 evidence、验证复用及恢复幂等行为；投递和 base/WIP 状态机不在本卡实施范围。

## 跨组 Context：compaction 与确认窗口

本卡实际读取了 646eb 的 `compaction-triggered-intermittency-origin-evidence.json` 与可读分析，以下时间来自其中带原生行号和逐记录 SHA 的审计，不冒充本卡重放过原生投递链路。全部为 UTC，日期不能省略：

| Task | task_started | failed / confirmed | compacted | 精确收尾正文记录 |
| --- | --- | --- | --- | --- |
| `c4c82` | 2026-10-23T20:32:34.747Z | failed 2026-10-23T20:32:49.800Z | 2026-10-23T20:34:34.186Z | 2026-10-23T20:34:34.955Z |
| `1c6e4` | 2026-10-23T19:57:30.515Z | failed 2026-10-23T19:57:45.670Z | 2026-10-23T19:59:41.803Z | 2026-10-23T19:59:44.005Z |
| `46d51` | 2026-10-24T07:07:29.248Z | failed 2026-10-24T07:07:44.196Z | 2026-10-24T07:09:33.483Z | 2026-10-24T07:09:34.079Z |
| `38fd3` | 2026-10-24T08:28:40.314Z | confirmed 2026-10-24T08:28:44.353Z | 此窗口没有 compacted | 2026-10-24T08:28:43.262Z |

这将三个迟到样本的状态来源定位到原生压缩阶段与确认预算的错位：确认窗口内没有精确正文记录，failed 之后才见到 compacted 和正文；正常对照在窗口内出现正文并确认。它没有证明 compaction 的触发阈值、token/history/config 前置条件，或 38fd3 为什么没有触发压缩；这些仍为 unknown。也不能仅凭 `task_started` 就认证精确正文已经接受。

首次判定是否识别 `ContextCompaction`、是否将仍在压缩的会话过早判为终态，以及“尚未接受”与“已接受但未落盘/不可观察”的区别，需要 `7e075` 定位实际确认链代码条件并验证。646eb 当前材料说明该卡的补证 API 主要处理事后恢复；没有据此证明首次失败已经被预防。本卡没有改动这条状态机，不宣称它已被修复。

控制验证仍由 `7e075` 主责：同一 payload/绑定下有无压缩、接受或落盘跨预算边界、正常成功及停止/重启；应保留精确正文接受要求，不能用无条件延长等待代替状态识别。本卡独立交付可确定复现的 writer 输入修复，不等待整个系统所有问题修完，也不将它们混成一个随机根因。

跨组来源：`/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/task-646eb-rvf-automatic-closeout-monitoring/successful-task-38fd3-comparison/20261024T083916Z/compaction-triggered-intermittency-origin-evidence.json`。

## 测试与真实限制

测试使用 Python 3.12.12，并隔离 HOME、`RVF_HOME`、`RVF_USER_LOCAL_DATA_ROOT`。

- 六组 evidence/land/dispositions/intake/validation-cache 核心回归：112 passed。
- 新增资格簇与三份入口的退出码登记：43 passed；包含修复后的 receipt 判据、false-positive 计数与结果持久化失败状态。
- 额外 backlog/旧 evidence 保全/integration receipt 回归：9 passed（包含重跑的既有用例，不与以上简单相加）。
- 后续显式补入的 `38fd3` 正向正式 land 回归：1 passed。
- 新模块与测试已登记到 `scripts/check_skill_contracts.sh` 的存在、编译和 pytest 执行入口；未在手工注册的 `test_review_support_scripts.py` 添加测试。
- 完整 `bash scripts/check_skill_contracts.sh` 实际运行并返回 1：Stop hook shard 3/4 的缺 task id 用例因隔离环境的 `provider_health_failed` 提前返回，未进入本次测试组。用相同环境对原始 `a51f959` source snapshot 运行该用例，同样失败且输出相同原因。这不是全量通过，也没有扩大范围修改 Stop hook 或伪造健康结果；本次测试组另已真实执行。

首次本地交付时，本卡只完成源码自审与上述实际验证，采用普通 Git 提交。当时把用户允许故障时人工收尾误解成了可以省略 RVF，曾结构性谢绝评审；用户随后明确纠正并显式发起 RVF，已推翻该谢绝。本次独立评审及修复见下节；旧人工交付记录保留为当时事实，不改写历史，也不冒用消费 run 的审查结论。

## 用户纠正后的独立 RVF

本轮 run 为 `rvf-review-start-59fc6292a9490e51`，固定比较 `a51f959a47513a522ddb87aaf6444352b05b5019` 至 `20ddb88d6fa51881d09b1b51a7ed7620f9d4efc3` 的实现，正式覆盖 14/14 个候选文件。通过正式 `start`、scope-of-work 定稿、`dispatch --strictness strict` 和同一 `wait` 等待终态；实际结果为 `plan_status=completed`、有效 reviewer 2/2。

两路独立 external reviewer（oh-my-pi、cursor-cli）各提出两项，四项全部采纳为不同 canonical issue。验证包只接收去来源的 issue，各自在工具创建的 attempt worktree 先复现、再修复，经真实 `fix stop --status fixed`、`rvf apply` 合回；最终 `rvf merge check` 通过，未发生范围扩展。

| Issue | 真实缺陷与最小修复 | 本项隔离回归 |
| --- | --- | --- |
| `RVF-001` | 同 run 已有 evidence、HEAD 尚未前移时，修改内容仍能提交后才拒写；提交前增加已有 evidence 内容摘要检查 | 修前 4 failed / 1 passed；修后专项 5 passed，相关 writer/Land 模块 39 passed / 1 skipped。跳过项因隔离 HOME 内没有本机跨仓消费者 |
| `RVF-002` | 最新 false-positive 结论抹掉旧 attempt 已应用的合法扩展路径；完成状态取最新，stage 路径和确认独立累计真实 applied 补丁 | 真实 prepare/start/stop/apply 生命周期修前 4 failed；修后专项 4 passed、完整 Land 测试文件 24 passed。最新 failed/started 仍拒绝 |
| `RVF-003` | 普通范围外路径可借 guarded 确认通道跳过声明；确认只覆盖确实命中冻结 guarded 范围的路径，普通外扩仍须声明 | 公开 Land 与 receipt reader 两负例修前失败；修后资格文件 49 passed，protected/background/excluded 正例保留 |
| `RVF-004` | 补丁祖先目录别名造成同一真实文件身份误拒；比较 resolve 后真实路径，补丁及期望补丁本身仍不得为符号链接 | 修前真实复现 1 failed / 4 passed；修后资格文件 54 passed，覆盖公开 Land、reader、错 run/attempt 和末级符号链接负例 |

`RVF-004` 最初测试有一次 fixture 父目录未创建错误，原日志保留；实际缺陷复现使用后续 `fixer-RVF-004-before-reproduction`，未把 fixture 错误算作产品缺陷证据。各项测试存在重叠，不简单相加。

本轮持久目录：`/Users/bominzhang/.rvf/user-local-data/run-logs/runs/rvf-review-start-59fc6292a9490e51/artifacts/`。`canonical-review-merge-summary.json`、`merge/merge_dispositions.json` 与四个 `fix-attempts/*/result.json` / `fix.patch` 保存实际评审、处置和修复链；`fixer-RVF-*-result.json` / `.log` 保存修前、修后检查。合并后的最终验证由主会话通过 `rvf validate --name handoff-validation-001` 登记，原始输出与退出码保存为 `final-post-review-regression.log` / `final-post-review-regression-result.json`，最终交接见该 run 的 `handoff.md`。

用户纠正的是 RVF 必须实际完成；自动 Land、evidence 登记或 base 同步故障时，仍可按既有授权人工交付已审工作。本次不 push、不合 main、不部署，不以自动账本状态代替实际审查，也未签发虚构的 completed、部署或 PDV 回执。前述全量合同检查的基线失败仍保留，不因专项通过而改写为全量通过。

## W1 / W2 复算

按原 `t2_evidence_scan.py` 的范围扫描三个主仓库 `.git` 下的 current/immutable/archived evidence，并结合原统计快照按 run 归类；生产 evidence 没有新增或修改。

| 窗口 | opt-in land | 历史真实写成 | 当前仍可读取 evidence 的 run | 当前只读完成与扩展资格通过 |
| --- | ---: | ---: | ---: | ---: |
| W1 | 9 | 3/9 | 3 | 8/9 |
| W2 | 2 | 1/2 | 1 | 2/2 |

W1 的 `08d3eb91` 最新 `RVF-004` attempt/result 实际仍为 elevated，故保留拒绝。最后一列只验证完成与扩展事实，不包含对历史候选重新做全部 Git/验证身份检查，更不是“修复后真实写成率”；未将它写成 8/9 或 2/2 的新 writer 成功记录。

## 正式消费与安装步骤

本卡源码尚未合回 local main 或安装。先由 646eb 核验实际使用的工具确实包含本提交；B2 还需消费侧支持并验证 version 2。原生产 handoff、冻结通知及 scope 摘要不能手改来绕过这些条件。

工具正式可用后，各消费 Task 在自己的 worktree 显式绑定原 run 执行：

```sh
rvf land check-evidence --repo <消费worktree> --run-dir <原run目录>
rvf land prepare --repo <消费worktree> --run-dir <原run目录> --handoff <原run目录>/artifacts/handoff.md --format json
rvf land commit --repo <消费worktree> --run-dir <原run目录> --message-file <已核对的原commit-message文件>
```

| Task | 原 run | 可核对的既有 message 工件 |
| --- | --- | --- |
| `66679` | `rvf-review-start-5a0e72d65bd5c0c4` | `artifacts/automatic-closeout-commit-message.txt` |
| `81a2e` | `rvf-review-start-780c3041b3ea0ec8` | `artifacts/automatic-closeout-land-commit-message.md` |

两卡已有真实提交，内容仍匹配时应得到 `nothing_to_commit` 与 `candidate_evidence=written/reused`，不得增加重复实现提交。`46d51` / `c4c82` 的正式自动恢复还有 Kanban paused 的独立依赖。646eb 继续协调各卡；其是否采用人工收尾取决于各卡既有授权，不由本卡代操作。

若后续另获 main 整合与部署授权，先将本提交按获准流程整合到本地 main，再从唯一 local-main worktree 使用正式 `rvf-local-deploy` 入口：

```sh
python3 scripts/rvf_local_deploy.py --expected-local-main-commit HEAD --json
```

安装前读取现有官方 installation receipt 的源 commit；安装后必须核验 runner 的实际结果、源 commit、两侧 cache 与真实入口。B2 的 `my-ai-setup` 消费者按跨仓契约另行实施和安装。本次没有执行这些动作。

## 持久证据

本卡交接根目录：`/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/task-5de70-reviewed-candidate-evidence-qualification/`。

- `original-handoffs-and-applied-expansion-facts-verification.json`、`raw-handoff-snapshots/`：原件 SHA 与实际完成/扩展事实；生产原件未改。
- `same-old-writer-success-and-format-failure-control.json`：38fd3 / 66679 / 81a2e 在同一旧 writer 与本任务实现下的只读比较。
- `final-evidence-land-regression-tests*.json` / `.log`、`final-qualification-suite*.json` / `.log`、`additional-completion-and-receipt-regression-tests*.json` / `.log`、`successful-38fd3-formal-land-control-test*.json` / `.log`：实际检查结果。
- `check-skill-contracts-result.json` / `.log`、`stop-hook-missing-task-id-baseline-comparison.json` 及两份 probe log：全量失败与未修改基线的对照。
- `recount-historical-reviewed-candidate-evidence.py`、`historical-evidence-recount.json`：可重跑统计脚本、逐 run 结果与输入摘要。
- `manual-local-delivery-record.json`：普通 Git 收尾的真实提交、明确 stage 文件、检查、边界和来源；它是人工记录，不是 RVF 自动签发 evidence。
