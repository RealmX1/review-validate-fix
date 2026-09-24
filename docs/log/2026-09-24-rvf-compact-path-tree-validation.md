# 任务 2a6da：路径树展示实现与验证

已实现会话中批准的压缩目录树方案。展示契约、覆盖入口和实际示例见
[路径树展示契约](../architecture/compact-path-tree-display-contract.md)。

## 开工前复核

计划位置为本会话上一轮批准的 proposed_plan。修改目标为共享渲染器及 Stop、packet、scope-of-work、
handoff intake、scope diagnosis、bootstrap 确认、analysis scaffold、base intent 的展示函数及对应测试。
与 sibling 的软重叠均限定在展示层，未改变对方任务涉及的候选计算、封窗、验证或 handoff 生命周期。

```text
DRAFTED_PLAN_LOCATION=APPROVED_CONVERSATION_PLAN
MUTATION_ALLOWED=1
BASE_TIP_AT_INSPECT=90a6160d9557b037b7e465bdcf69507fcf45d974
DIVERGENCE_POINT_AT_INSPECT=90a6160d9557b037b7e465bdcf69507fcf45d974
BASE_UNIQUE_COMMITS=0
BASE_CHANGED_PATHS_SINCE_DIVERGENCE=NONE
TASK_BRANCH_AT_PREFLIGHT=DETACHED
TASK_WORKTREE_DIRTY=0
BASE_WORKTREE_DIRTY=1
REMOTE_AVAILABLE=1
REMOTE_BASE_SYNC_STATUS=LOCAL_BASE_CONTAINS_REMOTE
WORKFLOW_INTENT=SYNC_LOCAL_BASE_INTO_TASK_WORKTREE_ONLY
WORKFLOW_SELECTION=EXPLICIT_ONE_WAY
WORKFLOW_MODE=SYNC_LOCAL_BASE_INTO_TASK_ONLY
HAND_BACK_ALLOWED=0
PUSH_ALLOWED=0
SEMANTIC_RECONCILIATION_REQUIRED=0
BASE_TIP_AFTER_ABSORPTION=90a6160d9557b037b7e465bdcf69507fcf45d974
TASK_TIP_AFTER_BASE_ABSORPTION=90a6160d9557b037b7e465bdcf69507fcf45d974
TASK_WIP_FINAL_STATUS=NOT_REQUIRED_TASK_WORKTREE_CLEAN
BASE_REF_LOCK_STATUS=AVAILABLE
BOOTSTRAP_RECEIPT=VERIFIED
BOOTSTRAP_SEQUENCE=VERIFIED_SYNC_BEFORE_TARGET_REPOSITORY_VALIDATION
ONE_WAY=VERIFIED
BASE_UNCHANGED_BY_TASK=VERIFIED
NO_HANDBACK=VERIFIED
NO_PUSH=VERIFIED
SIBLING_TASK_SURVEY_ENTRY=BY_NAME
SIBLING_TASK_SURVEY=VERIFIED
TASK_INVENTORY_SOURCE=KANBAN_CLI
SIBLING_TASK_COUNT=141
SOFT_OVERLAP_TASK_IDS=0272c,21579,26711,979fa,b8962,c1ca5
HARD_COLLISION_TASK_IDS=NONE
NO_SIBLING_WORKTREE_MUTATION=VERIFIED
NO_SIBLING_WORK_MERGED=VERIFIED
LOCAL_BASE_DELTA_INSPECTED=VERIFIED
LOCAL_BASE_ABSORPTION=COMPLETED
PLAN_REVISION=APPLIED
POST_ABSORPTION_PLAN_FRESHNESS=VERIFIED
```

软重叠：review 列 `0272c、21579` 涉及 packet，`26711、b8962、c1ca5` 涉及 Stop；
validation 列 `979fa` 涉及 handoff intake。均无活跃会话编辑碰撞。未修改主 checkout 的 WIP。
同步使用 personal skill 受控脚本，固定 tip 前后相同，锁释放后才开始本次测试。

## 验证结果

- 新增 `test_compact_path_tree_rendering.py`：16 项通过，包含隔离 Git 仓库中的 packet CLI。
- Stop 回归：193 项通过；脚本 runner 使用 `tmp` 参数的另外 39 项以临时 pytest fixture 补跑通过。
  精确排除的 1 项封窗测试见下节，未修改测试源码来跳过它。
- packet / UPS 定向回归：56 项通过；pytest 无法识别原独立 runner 的 `tmp` 参数的 3 项，
  使用原函数和独立临时目录直接补跑通过。
- scope-of-work、base intent、analysis、bootstrap、Stop audience、唯一自注入入口：97 项通过。
- 原完整检查失败的 review-support 第 3/4 分片在展示断言修正后重跑通过。
- 完整插件检查的其余 gate：恢复执行后全部通过，退出 0；包含安装/部署验证、结构契约与后续测试组。
- `git diff --check`：通过。

完整入口为 `python3 scripts/check_plugin_contracts.py`。首轮在并行测试阶段非零退出：
几处旧展示断言已修正并补验；另有下述基线已有失败，不能将完整入口标为全绿。
后续以同一契约脚本的内存副本恢复执行剩余 gate，仅固定仓库根并移除已经分别检查的
`run_parallel_test_steps` 调用；未修改仓库脚本来跳过失败测试。

```text
SCOPED_VALIDATION=PASSED
REMAINING_CONTRACT_GATES=PASSED
FULL_PLUGIN_CONTRACT_CHECK=FAILED_PREEXISTING_ROUND_SEAL_TEST
NEW_BEHAVIOR_REGRESSIONS=NONE_OBSERVED
```

## 基线已有失败

测试：`test_committed_round_seal_after_land_suppresses_redispatch`。
预期封窗后无候选、无 `decision`，实际仍出现候选 1 文件的 `decision=block` 发起横幅。
当前工作区单独运行退出 1；在固定 SHA `90a6160d9557b037b7e465bdcf69507fcf45d974`
的干净临时 worktree 中，同测试在同一 `assert_skip_reason` 断言处退出 1。
临时基线 worktree 已由受控脚本验证移除。本次不改变封窗或候选计算逻辑。

复现命令：

```sh
python3 -m pytest tests/test_stop_review_validate_fix.py::test_committed_round_seal_after_land_suppresses_redispatch -q
```

当次完整记录位于 `/tmp/rvf-2a6da-plugin-contract-check.log`、
`/tmp/rvf-2a6da-plugin-contract-timing.json`、`/tmp/rvf-2a6da-seal-current-test.log`、
`/tmp/rvf-2a6da-seal-baseline-test.log` 和 `/tmp/rvf-2a6da-contract-remaining-gates.log`。

## 交付边界

示例实际候选段从 845 字符降到 403 字符，减少 52.3%。未测量 tokenizer 或真实 agent 理解准确率。
没有新增依赖或旧格式兼容分支，没有部署、handback 或 push。

## 本轮 RVF 边界修复

run `rvf-20260924T094801Z-agentic-initiation-start-f36bb355` 按 strict 发起，实际外部双路审查完成
（`completed`、`effective_reviewers=2/2`）。两路均指出路径标签遗漏冒号/斜线手动触发前缀；
主会话另复现单个 `(none)` 文件与空集合输出相同。独立 validate/fix 子代理按两个隔离 attempt
分别复现、修复，通过 `stop --status fixed` 登记，并由主会话 `apply` 合回；无 scope expansion。

- RVF-001：冒号始终转义，开头或空白后的斜线转义，普通目录分隔符保留；修复前 5 项失败，修复后 26 项通过。
- RVF-002：真实 `(none)` 标签首字符可逆转义，避免误认空集；修复前 2 项失败，修复后目录树测试共 30 项通过。
- 两项修复仅涉及共享渲染器、对应测试和展示契约；不更改 UPS 触发协议。

最终合并后的验证记录由本 run 的 `handoff-validation-*` cache records 及 `artifacts/handoff.md` 承载。
前述实现阶段全套验证与基线封窗失败日志已复制到 run 的 `artifacts/implementation-validation-history/`。
start 归属解析降级为 `manual-all-uncommitted`，此次无法登记 tracker reviewed 单元；候选 20 文件均为本任务改动。
未提交、未部署、未 handback、未 push。
