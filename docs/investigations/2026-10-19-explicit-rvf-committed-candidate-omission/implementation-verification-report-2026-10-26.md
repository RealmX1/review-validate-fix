# Kanban 已落地未审提交的候选修复与验证

## 结果与授权来源

本次修复让已回灌本地 base 的 Kanban task 自有提交继续具有候选资格。事故来源的正常候选发现由 **0 个文件恢复为 10 个文件**，无需覆盖 baseline，也无需关闭 base 排除。session ownership、skip trailer、排除路径及 tracker review_state 继续分别生效。

实施授权来自本轮用户要求实施所附计划。原 task prompt 中的“请只读追查”由创建 Agent 写入；它描述原调查阶段，不是本轮实施禁令。原始报告、29 项历史断言及现场捕获状态保留。本轮不改 Landing Page、AWS、来源 refs、真实 tracker 或插件部署。非 Kanban 首审窗口问题仍在本次范围之外。

## 固定版本与单向吸收

| 输入 | 固定值 |
| --- | --- |
| 原调查提交 | `cc1ad47ced99f5a15cad3d67791b8a0d89cb95b6` |
| 吸收的本地 main | `fa7e2c6c8ccfb3c4f5c592a95fdaf89590ea0ebd` |
| 吸收后的 task HEAD | `8634dfc114f7b26541d6a3cc27cc6f59be38090b` |
| 事故来源窗口 | `fc25ab76176ad34865cbbacfe421b83577a75606..55ea63d9b218449378dd2b1f0a9167fc04287ac7` |

使用 personal/global `sync-local-base-into-task-worktree-only` 入口。matching origin/main fetch 后，本地 main 已包含远端，保持原值。task 原本干净，无需 stash；受控 merge 保留了原调查提交，无文本冲突。吸收前的全部 Git 子进程禁用 hooks，边界核验后立即释放 common-dir + main 锁，之后才运行语义复核和测试。

base 的 61 个新增提交主要涉及开审分界、冻结候选、独立恢复和收尾资格；task 独有提交仅新增调查文档、探针及矩阵，没有两侧同改文件。核对后将新增来源证据接入已有候选快照，不另造 review runner。

bootstrap receipt 已写入并验证：

`/Users/bominzhang/Documents/GitHub/review-validate-fix/.git/worktrees/review-validate-fix147/base-branch-sync/local-base-into-task-worktree-bootstrap-receipt.json`

receipt SHA256：`dc7a91539e5e78d638296557612a57a08fb9299ad8f8479669a35f0d7016c70f`。

```text
ONE_WAY=VERIFIED
BASE_UNCHANGED_BY_TASK=VERIFIED
NO_HANDBACK=VERIFIED
NO_PUSH=VERIFIED
SYNC_VALIDATION_SEQUENCE=VERIFIED_AFTER_ABSORPTION_AND_WIP_RESTORE
BOOTSTRAP_RECEIPT=VERIFIED
```

## 实现

- Kanban 适配器读取持久 sessions、board 和 provenance，核验仓库 common-dir、task、worktree、最新 incarnation 及创建锚点。core 接收 `rvf-committed-candidate-scope-evidence/v1`，不读取 `~/.cline`。
- 有效 review 高水位继续优先，但不得越过当前 incarnation 的创建下界向前扩张。首审使用 `initialBaseCommit`，实际 base ref 及 base SHA 一起固定。
- 保留现有 first-parent、非 merge 和外部 base 排除；将稳定窗口内、当前 HEAD 可达且已证明自有的提交并回。进入 base 不再抹掉来源证据；路径仍须存在净 diff。
- summary、Stop 检测、manifest 和 allocator 接收同一证据。证据写入 token、候选快照的不可变 Git binding、tracker scope 及 prepare 的 manifest 输入。验证及重分配继承冻结输入，不重新依赖 live main。
- task 归属仅恢复候选资格。manifest 认领已恢复的 committed 路径仍需本 session 的工具证据或已有强归属；不会把 task 的所有工作自动认领给新 session。
- 已审过滤同时核对当前净 diff 的 unit identity，防止旧的 reviewed 路径记录盖住新追加的 hunk。
- unborn 分支明确表示当前没有已提交层，保留独立 dirty 候选；不会因读取不到 HEAD 让整个摘要失败。

## 自动补证与恢复

provenance 不足时，依次读取当前 task 的首轮 checkpoint、tracker 强归属的逐单元内容匹配、同一原生会话中具有明确 cwd 的提交工具回执。checkpoint 使用父提交作为创建锚点，不把 checkpoint commit 本身当作 HEAD 祖先。历史改写只接受两侧唯一的 stable patch-id 映射；歧义、错误 incarnation、不可达锚点及身份不匹配保留 unknown。

补证观察、诊断及恢复条件保存到 task absolute git-dir 的 `rvf-committed-candidate-evidence/`。记录采用原子替换并 fsync 文件和父目录。同一来源观察只尝试一次；来源、HEAD、checkpoint、tracker 或原生会话输入变化后重验。真实 prompt 的 round-baseline `captured_at` 也能解除 unknown，因此相同文字的下一条真实 prompt 仍会有界补证。

unknown 不转换成“无候选”。空观察返回 `committed_candidate_evidence_pending`，不错误 supersede 旧 committed 单元，也不据旧记录分配无法观测的单元；独立 dirty 工作仍能分配。失败和等待只走 developer_only，不驱动 agent 自动再跑一回合。

普通候选快照绑定来源证据；宿主原生消息凭据继续独立证明可选的原生回退能力，不要求另一个仓库修改其消息凭据 schema。

## 验证与已知套件问题

历史产品从固定 `fa7e2c6` 导出，和当前修复分别运行相同 29 项矩阵，使用独立输出目录。来源对象只从原仓库本地 fetch 读取；事故 provenance 使用 10 月 19 日捕获的条目，不读取后来补审形成的真实 tracker。

| 来源断言 | 历史产品 | 修复验收 |
| --- | ---: | ---: |
| 正常发现 | 0 | 10 |
| 仅显式指定原始 baseline | 0 | 10 |
| 原始 baseline + 关闭 base 排除（因果控制） | 10 | 10 |
| 写回原始 marker，仍保留 base 排除 | 0 | 10 |

[机器可读结果](historical-versus-fixed-regression-results-2026-10-26.json)分别保存历史缺陷断言与修复验收断言；非 Kanban 的旧缺陷标为范围外观察，不计为已修复。

正式测试见 `tests/test_kanban_committed_candidate_provenance.py`，并已登记到 `scripts/check_skill_contracts.sh`。覆盖 task→base ff/merge、较早高水位、base→task ff/merge/rebase、唯一 patch-id、非 main base、skip、缺失/损坏/错误 provenance、checkpoint 与 tracker/原生回执恢复、跨重启缓存、相同真实 prompt 补证、跨 session 归属、追加 hunk、冻结后 main 前移、证据篡改、unborn HEAD、Stop detection、manifest、allocator 与正式 packet 一致性。

相关 pytest 分组通过 270 项；旧 runner 的 committed/candidate/allocator 相关 38 项通过。最终新增回归 **26 项通过**，事故矩阵 **29 项断言通过**，4 项 Stop 带外投递回归逐项通过。完整结果随本次交付持久保存。

完整 `check_skill_contracts.sh` 已实际运行，未全绿。固定 `fa7e2c6` 的干净 worktree 对照确认以下 3 项为已有失败：

1. `detached_wrapper_executes_real_guardian_cli_post_exit`：完成计数记录中的 `last_run_dir` 字面路径断言。
2. `alternative_reviewer_cursor_command_not_claude_patched`：其隔离环境下 `Path.home()` 抛出 `Could not determine home directory`。
3. `alternative_reviewer_cursor_autodetects_stream_json`：相同 HOME 解析错误。

完整套件也发现了本次引入的 unborn HEAD 回归；已修复，并将原先失败的 4 项 Stop 带外投递测试逐项重跑通过。未把 baseline 失败写成通过，未修改这些无关测试来消除红测。

## 并行任务边界

初次全仓调查因 Kanban CLI 退出 1 降级到持久 board，覆盖 219 个 sibling task，不能据此证明实时无重叠。随后用实时 task get 补查 `b8962`：session 为 failed、PID 为空；其 4 个 modified 文件包含摘要和 Stop ownership 逻辑，是待保留的历史 WIP，不是本轮的合并来源。未引入其严格 ownership 过滤方案，也未修改该 worktree。

```text
SIBLING_TASK_SURVEY=DEGRADED_KANBAN_CLI_EXIT_1_WITH_TARGETED_LIVENESS_REFRESH
TASK_INVENTORY_SOURCE=DURABLE_BOARD_STATE
SIBLING_TASK_COUNT=219
SOFT_OVERLAP_TASK_IDS=21579,c1ca5,4a84a,f103f,b8962
HARD_COLLISION_TASK_IDS=NONE
NO_SIBLING_WORKTREE_MUTATION=VERIFIED
NO_SIBLING_WORK_MERGED=VERIFIED
```

`NONE` 仅指当前已核实的碰撞对象；其他任务的实时活跃性仍未知，未把降级快照当成全仓无碰撞证明。

## 持久证据

本轮独立证据根目录：

`/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/review-validate-fix/c3b26/kanban-committed-candidate-fix-20261026/`

- `historical-defects-fa7e2c6/`：29 项历史断言及完整 hook stdout。
- `fix-acceptance-matrix-final/`：最终 29 项修复验收及来源正常路径的完整 hook stdout；`fix-acceptance-matrix-02/` 保留此前成功结果。
- `fix-acceptance-matrix/`：首次验收的 token 幂等预期失败，原样保留；修复的是探针预期，未撤销或伪造产品 token 状态。
- `current-full-contract-failures-*.json`、`baseline-full-contract-failures-*.json`：完整套件红测的当前/固定 baseline 对照；其中初次 current Stop 的 4 项失败随后修复，`final-stop-delivery-regression.json` 逐项记录通过。
- `compare_full_contract_failures_on_fixed_revision.py`：对照 runner；baseline worktree 由 personal skill 受控创建及清理，清理已验证。

独立执行真实状态根卫生检查，未发现本次完整套件临时目录标记对应的测试产物。

交付包含本地源码提交及文档，不执行 handback、push 或部署；本地 main 保持吸收时固定的 `fa7e2c6`。
