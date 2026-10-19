# 显式 RVF 遗漏已提交未审工作的根因调查

调查任务：`review-validate-fix/c3b26`。来源：`Trace-of-Thought-Landing-Page/9895b`，native session `01a1515f-0b88-7993-a4b0-cc9606c29b3a`。调查日期：2026-10-19，以下时间均为 UTC。

## 结论与边界

确认这是候选枚举缺陷。两道相互独立的前置筛选使已提交未审工作在进入 `review_state` 和 ownership 判断之前消失：

1. 首次 review 前，真实用户 prompt 会把 round baseline 写成当前 HEAD；作为兜底的 `merge-base(main, HEAD)` 也会随 task→main handback 前移，不能充当稳定的 task 起点。
2. 即使恢复原始 baseline，非 `main` checkout 上的 live `^main` 排除仍会扣掉已回灌 main 的本 task 提交。它把“来自 base 的外部工作”与“本 task 已落地但未审的工作”混为一类。

独立导入来源提交到全新仓库后，重现了 **默认 0、仅恢复 baseline 仍 0、恢复 baseline 并移除 base 排除后 10**。完整 UserPromptSubmit 子进程重现 `manual_decision_no_candidates`，仅输出 `systemMessage`，没有 token 或 `additionalContext`。合成回归探针共完成 29 项现状断言；这包括明确标记的缺陷表现，不表示产品已修复。

未修改 RVF 生产源码、Landing Page 文件、来源仓库 refs、AWS、真实 tracker 或真实 marker；未启动真实 review run、未部署插件。本次仅新增报告、复现探针和结果摘要。原始 hook 的完整 stdin/stdout 未能从保留日志恢复；下文严格区分现场事实、输入重建和隔离重放。

## 证据入口与版本

持久证据根目录：

`/Users/bominzhang/.cline/kanban/task-handoff-artifacts/c3b26/rvf-committed-candidate-investigation-20261019/`

| 产物 | 用途 |
| --- | --- |
| `source-handoff-snapshot/` | 保存来源交接、三组摘要、marker 与脱敏 trajectory 的字节副本 |
| `read-only-source-state-observation.json` | 07:40:38 的来源 refs/checkpoints、tracker 定向查询、transcript 事件索引、部署文件 SHA256 对照 |
| `read-only-source-task-commit-integration-provenance.json` | 仅 9895b 的创建锚点和提交归属记录 |
| `retained-host-log-evidence-limits.json` | host 日志查询范围、计数与原始输入/输出缺失边界 |
| `isolated-reproduction-verified/` | 独立 Git/RVF 状态、每例完整摘要、完整 hook stdout 和 29 项结果 |

RVF task HEAD 为 `851a00d10b62bbdeac859f8eea07dad5004edbf9`。本次比较的四个文件与已部署 payload 字节一致：`committed_round_detection.py`、`reviewable_unit_diff_tracker.py`、`rvf_user_prompt_submit.py`、`stop_decision_candidate_summary.py`。因此本故障路径不能归因于这四个文件的部署版本落后。

以下源码链接相对于本报告，定位以该 revision 为准：

- [窗口与 base resolver](../../../core/session_scope_allocation/committed_round_detection.py)：`_resolve_committed_round_baseline`、`_resolve_committed_round_base_ref`。
- [提交枚举与 review_state](../../../core/session_scope_allocation/reviewable_unit_diff_tracker.py)：`_base_ref_exclude_revs`、`_list_committed_round_changed_paths`、`select_committed_paths_reviewed_by_session`。
- [UserPromptSubmit](../../../plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_user_prompt_submit.py)：`_capture_round_baseline`、`inspect_user_prompt_submit`、`_handle_manual_decision_point`、`_render_hook_payload`。
- [候选摘要](../../../plugins/review-validate-fix/skills/review-validate-fix/scripts/stop_decision_candidate_summary.py)：`build_candidate_summary`、`_read_tracker_ownership`。

## 实际事件与 hook 输入核对

原始 transcript：

`/Users/bominzhang/.codex/sessions/2026/10/18/rollout-2026-10-18T19-35-45-01a1515f-0b88-7993-a4b0-cc9606c29b3a.jsonl`

只按给定 session 定向读取，报告和证据副本不包含凭证、报名信息或 transcript 全文。

| UTC | 原始 JSONL 行号 | 核实事实 |
| --- | ---: | --- |
| 10-18 23:35:55 | 10 | 初始需求指明 main=`fc25ab7`，在本卡独立 worktree 实现 |
| 10-18 23:46:28 | 210 | `rvf start` 退出 2，输出找不到待表态决策 token；这不是 review 完成证据 |
| 10-18 23:49:35 | 269 | 实现提交 `049da2e`，10 个文件 |
| 10-19 00:27:54 | 392 | 用户发送普通后续 prompt `continue` |
| 10-19 00:29:35 | 408 | 记录提交 `55ea63d`，同步前 main 仍为 `fc25ab7` |
| 10-19 00:29:55 | 417 | handback 执行记录退出 0；持久 provenance 和当前 refs 交叉确认 main 已到 `55ea63d` |
| 10-19 07:31:30 | 522 | 用户实际发送 `$review-validate-fix:review-validate-fix` |
| 10-19 07:33:07 | 535 | 用户报告界面显示“未注 Token、无可审候选” |

这里的行号按当前 JSONL 逐行计数，来源 trajectory 的 ordinal 192/209/268 等采用另一套计数，故同时保留事件 ID，不能直接混用 ordinal。

07:33:07 捕获的 baseline marker 为 `55ea63d`，`task_id=9895b`、session 与 worktree 均匹配；prompt 摘录对应用户的诊断要求。它直接证明**诊断 prompt**推进了 baseline；没有 07:31 那次 marker 的历史副本，不能把这份较晚 marker 称为 07:31 原始输入。

07:31 的用户触发字面量来自 transcript，cwd 来自会话/task 身份，session 来自原生会话记录，task_id 来自 Kanban 绑定。上述信息足以构造最小输入，但原始 hook 是否另含其他字段、task_id 当时来自 event 还是 env，均无法逐字恢复。保留的 Codex SQLite 日志在 07:31–07:36 该 thread 下有 144 行，没有恢复到该 hook 的原始 stdin/stdout；transcript 也未提供这份原始回执。用户界面报告、源码分支和隔离重放相互支持，不能将重放产物冒充现场原始回执。

已安装 `~/.codex/hooks.json` 的 RVF UserPromptSubmit 命令直接指向部署脚本，并显式指定统一 `RVF_USER_LOCAL_DATA_ROOT`、`CODEX_RVF_LOG_ROOT`、`CODEX_RVF_STATE_DIR`。本次捕获的 marker 和 tracker 也在这棵 `run-logs` 下，未发现本例需要用“读写落在不同状态根”解释的证据。

## 两个丢失点的因果链

### 1. bootstrap 下界使用了会移动的分支交点

`inspect_user_prompt_submit` 在 manual 分支之前，对不带机器边界 token/marker 的真实 prompt 调 `_capture_round_baseline`。普通 prompt 和显式 manual prompt 都满足此条件。

无有效 high-water 时，Kanban baseline resolver 取 round baseline 与 `merge-base(base_ref, HEAD)` 中祖先更早者；非 Kanban 则仅使用 round baseline。源码称 task 分支点“稳定”，但它实际每次从 live base 重新计算。

本例 handback 后：

```text
原始 task 起点 = fc25ab7
task HEAD      = 55ea63d
local main     = 55ea63d
merge-base     = 55ea63d
新 prompt 写入 round baseline = 55ea63d
结果：baseline..HEAD 为空
```

触发条件是：没有可用的更早 review high-water，且 baseline 与 live 分支交点都已越过未审提交。**prompt 推进单独不足以让普通未回灌的 Kanban task 漏审**：base 保持原位时，分支交点仍能兜住旧提交，探针验证仍有 1 个候选。反过来，仅 handback、保留旧 round baseline 时，窗口可以还在，但会撞到第二道排除。

直接在 `main` 工作的非 task 会话也存在首次 review 前被下一条普通/manual prompt 推过窗口的问题，隔离例已验证；它与 live `^main` 排除无关，因为主分支 checkout 上该排除会关闭。

### 2. live base exclusion 将落地当成外部来源

`_base_ref_exclude_revs` 在 checkout 分支 ref 与 base 分支 ref 不相同时返回 `^<当前 base SHA>`。只有正在 `main` 分支上提交才关闭排除，task 分支或 detached checkout 不关闭。

来源仍站在 `task/9895b-waitlist-automatic-registration`。恢复 baseline=`fc25ab7` 后，`^main` 会把 `049da2e` 和 `55ea63d` 一起排除。commit allowlist 为空，与净 diff 相交后得到 0 个路径。

关键错误假设是函数注释中的“ff implies the branch carries no own commits”。这只适用于**从 base 吸收纯外部提交**的特定前史；task **向 base 交回自己的提交**后也能形成相同的 `HEAD == main`、非 main checkout。仅凭最终拓扑和分支名无法区分这两种来源。

该排除不依赖首次 review：即使已有一个更早有效 high-water，只要未审提交现已可达 base，仍会被扣掉。隔离 `handback-older-highwater` 用例确认这一点。main 通过普通 merge 纳入 task、且 base tip 比 task HEAD 更远时也会漏掉；不局限于 tip 相等或 fast-forward。

`fd54db1` 引入这道排除，是为防止 rebase/ff 吸收的外部 base 工作误入本会话。现有 `test_committed_round_base_ref_excludes_ff_to_base` 覆盖了 base→task，却没有覆盖方向相反的 task→base。其原有防误报目的仍需保留。

### 3. review_state 与 ownership 为何救不回来

候选摘要顺序为：

```text
解析窗口 → Git committed 枚举和 base 排除 → review_state 剔除已审
→ committed ∪ dirty → 路径前缀和 ownership 筛选 → candidate_path_count
```

现场捕获摘要的 `review_state_filtered_committed_path_count=0`、other-session 过滤数 0、workspace guard 未阻断。移除两个前置限制即可得到 10 个候选，已审过滤仍为 0。因此本例的空集不是 tracker 将这 10 个文件判成已审，也不是 ownership 排给其他 session。

`lower_bound_source=tracker_review_state` 是“已审裁决使用哪个机制”的标签，不证明实际查到了已审记录；这个标签也不能证明 Git 已经枚举了完整窗口。

07:40:38 的定向只读查询发现来源 session 已在 07:38:19 创建 tracker 登记，现有 24 个 `committed/available` 单元覆盖这 10 个路径，未发现该 session 的 reviewed 单元。这是**补审开始后的状态**，不能倒推 07:31 的完整 tracker 内容。早期三组摘要能证明那次调用过滤计数为 0，但不能证明当时整库无记录。

隔离测试进一步验证：即使预先有本 session 的 `available` ownership，base 排除仍让摘要为 0；allocator 在同一错误窗口下重观测，还会把未观察到的单元转为 `superseded`，最终 `no_unassigned_review_scope`。反之，真正通过隔离 `allocate_review_scope` / `complete_review_scope` 完成的路径，会被 `review_state` 正常剔除（过滤数 1）。ownership 的强其他会话证据正常过滤、弱时间窗证据正常保留，均为独立控制例。

## 已有可复用状态

比 checkpoint 更直接的来源是 Cline Kanban 已持久化的：

`~/.cline/kanban/workspaces/trace-of-thought-landing-page/task-commit-integration-provenance.json`

9895b 的对应条目已有：

| 字段 | 实际值/含义 |
| --- | --- |
| `baseRef` | `main` |
| `incarnationId` | `fc25ab76176ad34865cbbacfe421b83577a75606:1792366543376` |
| `initialBaseCommit` | `fc25ab76176ad34865cbbacfe421b83577a75606` |
| `captureSource` | `worktree_creation`，不是事后从 live merge-base 补猜 |
| `observedTaskCommits` | 两笔来源提交及各自 `stablePatchId` |
| `taskOwnedNonMergeCommitsReachableFromLastKnownTaskHead` | 两笔提交仍被保留，即使已进入 main |
| `taskCommitsIntegratedIntoBaseRef` | 2，记录“已落地”，不表示“已审” |

canonical Cline Kanban 的 `src/workspace/task-commit-integration-provenance.ts` 定义了上述 schema，且明确避免用 `baseRef..HEAD` 清空已合入 base 的自有提交。该设计正好保留 RVF 当前枚举丢失的信息。

独立交叉证据为 `refs/kanban/checkpoints/OTg5NWI/turn/1`：commit `68e8c459...` 的父提交是 `fc25ab7`，tree 与起点 tree 一致。应取其父提交作为已验证的历史起点，而非把 checkpoint commit 本身误当 HEAD 祖先。checkpoint 有滚动保留上限，不能作为唯一长期来源。

board 卡片只有 `baseRef`、创建时间等，没有固定初始 SHA。tracker 的 session/unit 表提供 ownership 与 review_state，没有现成等价的 task 创建锚点；源码也注明 `reviewed_head` 尚未完成向 tracker 的终局迁移。建议优先适配现有 provenance，不另造一份随 prompt 覆写的 task baseline。task 所有权不自动等于当前 session 所有权，跨 session 仍须服从现有归属/接管规则。

## 影响面

| 场景 | 本次判断 |
| --- | --- |
| 显式 manual 触发 | 已复现：空摘要提前返回，不铸 token；manual 绕过冷却/熔断也救不了枚举前已丢失的工作 |
| 自动 Stop / committed route | 共享同一 resolver 和 committed 枚举，源码确认受影响；本次未驱动真实 Stop 或真实 reviewer |
| manifest、scope allocation | 共享该枚举；隔离 allocator 已复现空 scope，不应只修 banner/UPS |
| task→main ff 或 merge 落地 | 已复现；有效旧 high-water 也不能免疫第二道排除 |
| 纯 base→task ff、merge、rebase | 控制例维持正确外部排除；简单全面关闭 live base exclusion 会使这类工作回流 |
| main 直接提交、首次 review 前后续 prompt | 已复现窗口遗漏，是第一道问题的相邻影响 |
| squash 落地但 task 保留原 SHA | 隔离例仍有 1 个候选；main 无原 task SHA 的可达关系，不能宣称所有落地方式都会漏 |
| 无 tracker / 强弱跨会话 ownership / 重写 high-water | 隔离控制例已验证，见矩阵；多会话共享文件和复杂重写仍需扩展验证 |

`autoReviewEnabled=false` 是来源卡片事实，不能视为用户免审授权。它也不是 manual 此处的 no-candidate 条件。本报告没有重现完整的“最初为何允许未审提交/同步/部署”执行纪律问题；后来的显式候选遗漏无法反向解释更早所有执行行为。

## 最小修复建议（尚未实施）

1. **固定窗口来源。** 在 host/adapter 层读取当前 task incarnation 的既有 `initialBaseCommit`、`baseRef` 和自有提交证据，向中性 core 提供统一的 scope-origin 输入。有效 review high-water 继续优先；首次 review 的 bootstrap 使用稳定锚点。普通/manual prompt 仍可更新 turn 观测值，但不得让未审窗口随之缩窄。对旧卡缺失、worktree 重建、初始 SHA 非祖先等情况显式返回来源/可信度，不能无声声称“已无工作”。
2. **把 provenance 与已审状态分开。** 保留“排除真正外部 base 工作”的能力，同时保留当前 task/session 有证据归属、已进入 base 的未审提交。可先将现有 `baseline..HEAD ^base` 集合与“稳定窗口内、当前 HEAD 可达的自有提交”求并集，再统一做 skip、路径/unit 观测与 review_state 过滤。不要只把 `base_ref=None` 当最终修复，也不要仅依据 `HEAD==base` 或 `HEAD` 为 base 祖先关闭排除。
3. **统一所有消费者。** summary、Stop detection、manifest 和 allocator 必须消费同一冻结的窗口、base SHA 和自有提交集合。allocator 的缺失单元 `superseded` 行为也须回归，避免前端显示有候选、后端又分配空 scope。
4. **复用提交身份，但不扩大授权。** provenance 中 task 自有 SHA 已足以修复本例；rebase/cherry-pick 后用现有 `stablePatchId` 做经过唯一性校验的映射，仍遵守 session ownership/接管与显式 scope。不能按路径简单复活旧 reviewed 单元，也不能把跨会话所有 task 工作自动视为当前 session 所有。对歧义映射应保留不确定性和开发者诊断。
5. **增加解释性观测。** 输出稳定窗口来源、固定 base SHA、排除前/后提交及路径数量、已落地自有提交保留数、review_state 过滤数。`manual_decision_no_candidates` 只描述候选结果，缺失来源时不应断言“自上次 review 无改动”。失败/降级诊断继续遵循 `developer_only`；只有有效发起决策点才注入 agent，不能用异常提示自动驱动重跑。

优先实施 1–3，可修复本例并保持 base 吸收控制例；4 中复杂历史改写的精细映射可以独立分阶段实现，但本阶段必须对不支持情形给出明确结果。当前 provenance 文件是 Cline Kanban 私有持久化结构，本次没有确认一个稳定对外 CLI/API 已完整提供这些字段；实现时应在 adapter 契约层解决，避免让 host-neutral core 硬编码 `~/.cline` 路径。此处是实现建议，不是已获批准的源码改动。

## 隔离回归矩阵与验收标准

完整结果：[regression-matrix-results.json](regression-matrix-results.json)。复现脚本中的 `known_defect=true` 表示当前版本应失败的产品目标；其余为必须保留的控制行为。

| 用例（部分同类合并显示） | 当前结果 | 修复后必须满足 |
| --- | --- | --- |
| 首次 review 前提交；未回灌的普通/manual prompt | 1；1；1 | 持续保留，manual 可铸 token |
| task→main 后，当前 baseline | 0 | 保留 1 个未审路径 |
| task→main 后，仅固定初始 baseline | 0 | 保留自有路径，不能靠 live base 一刀切 |
| 固定 baseline 且禁用 base 排除 | 1 | 仅为因果控制，不可成为全局修复 |
| handback 后已有 available ownership | 0 | 可见且可分配 |
| handback 后 allocator | 0 单元/empty | 自有未审单元不被错误 supersede |
| handback 后有更早 high-water；manual 触发 | 0；0 | 保留水位后的自有未审工作，manual 注入决策点 |
| base→task 纯 ff，具名/detached checkout | 0；0 | 继续排除外部 base 工作 |
| base→task merge/rebase，同时有自己的提交 | 1；1 | 只保留自己的工作 |
| main 直接提交，后续 prompt/manual 前 | 1 | 保留 |
| main 首审前后续普通/manual prompt | 0；0 | 保留未审工作或明确缺失历史来源，不能伪报已无改动 |
| available 单元；真正 complete-review 后；high-water=HEAD | 1；0（已审过滤 1）；0 | 不复活已审单元 |
| 强其他 session ownership；弱时间窗归属 | 0（归属过滤 1）；1 | 保持现有归属边界 |
| rebase/reset 导致旧 high-water 不再是祖先 | 1 | 不错误采纳旧水位 |
| merge handback；squash handback 保留原 task SHA | 0；1 | 前者恢复，后者不回归 |
| 来源真实 commits 重放：默认/固定 baseline/两者覆盖 | 0 / 0 / 10 | 正常路径即可发现 10 个文件 |
| 来源重放：持久写入原始 marker，保留 base 排除 | 0 | 验证仅修 marker 不足 |

还需在修复阶段新增：多次 task incarnation、provenance 缺失/过期/损坏、非 main base、并发 base 前移、混合自有和外部提交已共同进入 base、同路径混合变更、部分 reviewed 后追加新 hunk、跨 session fork 接管、patch-id 歧义、cherry-pick/rebase 后被吸收、merge resolution 自有修改、显式 skip trailer、dirty+committed 同路径。它们本次没有全部执行，不能宣称复杂 merge/rebase 或跨会话行为已全面验证。

## 复现方式与安全隔离

在本 RVF task worktree 运行，输出目录必须尚不存在：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/investigations/2026-10-19-explicit-rvf-committed-candidate-omission/reproduce_committed_candidate_omission.py \
  --output-directory /Users/bominzhang/.cline/kanban/task-handoff-artifacts/c3b26/rvf-committed-candidate-investigation-20261019/isolated-reproduction-new-run \
  --source-repository /Users/bominzhang/.cline/worktrees/9895b/Trace-of-Thought-Landing-Page
```

不传 `--source-repository` 则只运行 25 项合成断言；传入则增加 4 项来源提交摘要断言，并执行完整 hook stdout 和反事实 manual 决策点验证。源仓库仅被本地 `git fetch` 读取对象；全部 checkout/branch/merge/rebase/reset/commit 和 SQLite 写操作发生在新建隔离目录。Git 禁用全局/系统配置与 hooks；`RVF_HOME`、user-local-data、run-logs、runtime-state、session-hook、prep-root 均显式隔离。既不修改 `HOME`，也不读取来源 transcript 驱动产品逻辑；不执行 `rvf-agent start`、reviewer、远端 fetch/push 或部署。

复现脚本直接消费本 worktree 的产品函数，内存 resolver 注入只出现在标明的反事实用例；没有修补产品实现。完整 hook 子进程使用隔离身份重建输入，其身份和路径替换是隔离所需，原始输入缺失限制见上文。

验证结果：29 项现状断言通过；源码文件部署一致性比对通过；净 diff 为来源交接给定的 10 个路径；文档/探针 `git diff --check` 通过。本报告给出根因与建议，产品缺陷仍待后续授权实现。
