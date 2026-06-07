# RVF 全项目 Master Backlog（重排 · 范围 B）

> 基准：local `main` `75ef235`。生成于 2026-06-07。
> 范围 B = 把 prelude 复核（见 `prelude-waste-analysis.md`）的发现**并入**全项目既有/新增 backlog，统一重排优先级。
> **状态核验已在规划期完成**：凡 main 有明确 land 证据者直接定 ✅Done（见末节「已 Done 对账」），不留到执行期；只有确无闭合证据的 nav-review 老文档项标 Open/Proposal。
> 路径前缀 `…/scripts/` = `plugins/review-validate-fix/skills/review-validate-fix/scripts/`。

## 重排原则

优先级 ≈ (价值 × 每轮命中频率) ÷ 改动成本，再按依赖关系兜底排序：

- **T0** 纯 prompt/模板层的「每轮固定浪费 / 生成-prompt 正确性」止血项（小改、每轮命中）；
- **T1** 结构化交接（消除 451 行 sh + markdown 解析）；
- **T2** scope 正确性 + 验证/观测/信心闭环（价值高、工量大）；
- **T3** 生命周期收尾 / 跨 harness 审计；
- **T4** 文档/导航卫生 + Deferred / Blocked / 待评估。

---

## 总表

状态图例：🔴Open · 🟡Partial · 🟢Done · ⛔Blocked · 💭Proposal

| 条目 | 来源 doc | 状态 | 依赖/阻塞 | 层 | 一句话理由 |
|---|---|---|---|---|---|
| 未闭合代码块围栏修复（`cline_kanban_task_prompt()` :3019 起始/:3021 闭合被注释） | nav-infra-review P0 / 新发现 1b | 🔴 | 无 | T0 | 正确性 bug：task prompt 以未闭合 code block 结尾，污染 agent 读「最终回复 contract」 |
| dispatch `## Origin` 指令去重（:1055–1062 + :1812–1821 同消息双注入；:3013–3017 第三处 artifact） | 新发现 1 | 🔴 | 无 | T0 | 每个 followup run 必中 ~850 字符冗余 + 与 `origin.json` 重复；纯模板合并 |
| SKILL.md anti-re-verify 纪律（trust-prep 已有，缺「别为复核重读 contract/packet」「source 不跨 Bash、env.sh 勿整篇 cat」） | 新发现 2 / nav-infra-review P1 | 🔴 | 无 | T0 | 治理 run-variable 的「从零侦察」单次浪费，纯 prompt 纪律 |
| `review-env.json` / 统一 artifact env sidecar（内联 agent 真用的少量字段） | design-system Gap1 / 新发现 3 | 🔴 | 无（归并 Gap1） | T1 | **最大单项浪费**：消除 451 行 sh 载体错配，给 observation/contract/confidence 提供稳定引用层 |
| origin 样板瘦身（dispatch 改读 `RVF_ORIGIN_METADATA`(origin.json) 回填 `## Origin`） | 新发现 1 | 🔴 | 前置核查无下游正则解析 prompt 文本里 `RVF_PARENT_*` | T1 | 值不再 inline 全铺，与 artifact 去重 |
| tracker-scope 与 worktree-bootstrap 范围漂移（bootstrap 可能携 tracker scope 外的 unattributed dirty） | nav-infra-review P0 | 🔴 | 无 | T2 | scope-authority 正确性：下个 agent 可能误把 bootstrap 带入当「可审查/可提交」 |
| Gap3 `validation-contract.json`（per-run：automatic_commands/manual_prompts/post_deploy_observations/retirement_policy） | design-system Gap3 / Slice C | 🔴 | 建议在 Gap1 之后 | T2 | 验证方式应成 artifact，而非 phase report/final response 备注 |
| Gap2 observation event schema（`rvf_logging.observation()` + `expected_post_fix_observation`） | design-system Gap2 / Slice B | 🔴 | 无 | T2 | 在 observation 源头记录「修复后应看到什么」 |
| Gap5 confidence ledger（`state/confidence/problems.jsonl`，3-success retire） | design-system Gap5 / Slice D | 🔴 | 依赖 Gap2 observation | T2 | post-deploy 信心闭环 |
| Gap4 verification UI（tracker dashboard 的 Verification Prompts 面） | design-system Gap4 / Slice E | 🔴 | 依赖 Gap2/Gap5 | T2 | 自动验证不可行时结构化提示人工验证 |
| ~~cross-harness A1/A2/B/C~~ | cross-harness-handoff → S0-S4 log §3 | 🟢 | — | T3 | **规划期已对账闭合**（A1/C→`b1f4530`、A2→`b083f65`、B→S1.5+S2-observe）；仅留记录 |
| Codex-native reviewer lease lifecycle 审计（parent-side refresh/release） | global-tracker #3 | 🔴 | 无 | T3 | 验证 Codex-native reviewer 子代理是否有真实 parent-side lease 路径 |
| Kanban task heartbeat polling（可选 task-status 轮询） | global-tracker #2 | 🟡 | 长任务才需要 | T3 | heartbeat/lease refresh 已 land，仅剩可选轮询 |
| paused/request state events | global-tracker #4 | 🔴 | 仅 validate/fix retry 常见时 | T3 | 当前 request lease 保持 active，`paused` 仅 schema 容量 |
| dispatch-flow live 集成测试 | rvf-dispatch-flow-overhaul-plan | 🔴 | 无 | T3 | Slice A–I 已 land，缺端到端 live 验证 |
| Phase 5 plan doc 清理 | global-tracker #5 | 🔴 | 依赖 #2/#3 收口 | T3 | Phase 5 标「partially landed」直到 reviewer 生命周期审计完成 |
| 新增短 agent-navigation-index（按 role 给「读什么/不读什么/权威源」） | nav-infra-review P1 | 🔴 | 无 | T4 | 防新 agent 从全文搜索误入 `state/`/历史 plan/legacy shim |
| review packet 顶部 scope-authority 措辞统一为 `scope.contract.json` final | nav-infra-review P1 | 🔴 | 无 | T4 | packet 顶部仍说 scope-of-work/manifest 是 anchor，落后于 SKILL/reviewer prompt |
| handoff intake parser 聚合「验证」子节（template 子 heading 被 `parse_sections` 切散漏读） | nav-infra-review P1 | 🔴 | 无 | T4 | `rvf_handoff_intake.py` 只读 `sections["验证"]` body，漏 `### Scoped verification` 子节 |
| `state/` 标非导航入口（含旧 Vibe-Kanban 生成上下文） | nav-infra-review P1 | 🔴 | 无 | T4 | `state/current-rvf-session-context.md` 旧语境与当前 Cline Kanban 冲突，搜索易误用 |
| README 减负（深层 runtime 状态机转向 internals/debug） | nav-infra-review P1 | 🔴 | 无 | T4 | README 同时承担入口/运行时/排障/setup/策略，对普通 agent 过载 |
| 历史设计文档加 `Current Status`/`Live Contract Owner` 标签 | nav-infra-review P2 | 🔴 | 无 | T4 | design/phase doc 混 live contract 与 phase 状态，不能当 live spec |
| compatibility shim 在索引中标明 `prompts/` 才是子代理 prompt 源 | nav-infra-review P2 | 🔴 | 依赖 nav-index | T4 | `references/` 下 moved 文件可能被搜索误当 prompt 入口 |
| scope file-vs-unit 固定措辞（`git diff HEAD` 是证据不是 scope） | nav-infra-review P2 | 🔴 | 依赖 nav-index | T4 | 防把「同文件内容累积变化」误读成 scope 扩大 |
| host-specific schema 继续留在脚本/测试、不散进 prose | nav-infra-review P3 | 💭 | — | T4 | advisory 守则，非一次性任务 |
| workspace-diff-tracker 集成 | doc/phase-b-deferred/… | ⛔ | global tracker Phase 2 | T4 | 被阻塞，待 tracker Phase 2 |
| trivial-fix-accumulation review lane | docs/trivial-fix-accumulation-review-lane-plan.md | 💭 | 待评估 | T4 | proposal，未排期 |
| custom-review-standards-pack | docs/rvf-custom-review-standards-pack-plan.md | 💭 | 待评估 | T4 | proposal，未排期 |
| plan-doc-review-routing-scaffold | docs/plan-doc-review-routing-scaffold.md | 💭 | 待评估 | T4 | proposal，未排期 |
| cline-kanban-bootstrap-full-dirty-overlay | docs/potential-work-cline-kanban-bootstrap-full-dirty-overlay.md | 💭 | 待评估（与 tracker scope 漂移相关） | T4 | potential work，未排期 |

---

## 验证手段（per-item，未来执行依据）

| 条目 | 验证手段 |
|---|---|
| 未闭合围栏修复 | snapshot/contract 测试：检查 generated task prompt code fence 闭合、含 `RVF_HANDOFF_FILE` 与 `rvf_handoff.py open`（nav-infra-review P0 建议） |
| Origin 指令去重 | 抽 handoff instruction renderer 为单源，diff 注入消息字符数；回归测试断言单段 |
| anti-re-verify 纪律 | 文档项；以后续 run 轨迹抽样是否仍出现「source→撞墙→逐文件重读」为验收 |
| review-env sidecar (Gap1) | producer 写出 / consumer source / path no-recompute 三类测试（design-system Slice A 验收：consumer 不从 prose 复制路径） |
| origin 样板瘦身 | 前置 grep 确认无下游正则解析 `RVF_PARENT_*`；改后断言 `## Origin` 由 origin.json 回填 |
| tracker-scope 漂移 | 决定 tracker scope 下是否仍 bootstrap unattributed；保留则断言标 `protected_context_paths`；packet/handoff 区分 replayed context vs review scope |
| Gap2/3/5/4 | 见 `docs/workflow-plugin-design-system.md` Slice B/C/D/E 各自「验收」段 |
| reviewer lease 审计 | 验证 Codex-native reviewer 有 parent-side refresh/release；无则加薄 lease runtime（不依赖 reviewer Stop hook，`CODEX_RVF_SUPPRESS_STOP_HOOK=1`） |
| Kanban heartbeat polling | 仅长任务需要；经 `cline_kanban_client.py` 加可选 task-status 轮询，保留 1h followup TTL override |
| nav-index / packet wording / intake parser / state / README | 各自加 fixture/snapshot；intake parser 用当前 `references/handoff-template.md` 生成回归 fixture |
| 全局 backlog 共用 | `python3 tests/test_review_support_scripts.py --shard-count 4 --shard-index {2,3}`、`test_codex_stop_review_validate_fix.py`、`scripts/check_plugin_contracts.py`、`git diff --check` |

---

## 已 Done 对账（规划期闭合，移出活跃 backlog）

| 已落地项 | 证据 |
|---|---|
| dispatch-flow Slice A–I | `docs/rvf-dispatch-flow-overhaul-plan.md` 标全部 land；prep payload `shared_workflow_state.status` + `artifacts` 路径字典已在用 |
| Stop hook lazy sweep（global-tracker #1） | `docs/global-tracker-finishing-handoff.md`「Concrete slices 1 - landed」 |
| tracker heartbeat / lease refresh 主体（#2 主体） | 同上「2 - partially landed」（仅剩可选轮询） |
| 失败再入 rvf-reopen | `a86dad7`（+1622，新增 `rvf_rescope.py`/`review_reopen_marker.py`） |
| no_issues 必填 audit_summary（Option A） | `556d02b` |
| cross-harness ✅G（Stop/UPS Codex-aware no-op 结构性化） | 2026-05-28 handoff ✅G；引于 `7236a74`/`6c6ff47` |
| **cross-harness A1/A2/B/C** | `docs/log/2026-05-30-cross-harness-plugin-S0-S4-implementation-log.md` §3 闭合对照表：A1→`b1f4530`(host-无关 `_is_write_op`)、C→`b1f4530`(ts 窗口子区间)、A2→`b083f65`(子代理捕获迁 adapter)、B→S1.5 主轨迹 call_id + S2-observe `candidate_patch_call_ids`（DB 列 null 判 moot）；判据 `rg 'host=="codex"' core/` = 0 |

## 本轮新发现 / 归并对照

- **真正新增立项**（未见于既有追踪文档）：未闭合围栏修复（与 nav-review P0 同指）、Origin 指令去重、anti-re-verify 纪律、静态文档每轮重读。
- **归并入既有 gap**（不重复立项）：review-env 结构化 → design-system **Gap1**；observation/validation/confidence/UI → **Gap2–5**。
- **对账后降级为已 Done**（曾疑为活跃）：cross-harness A1/A2/B/C —— 本 backlog 首版误标「存疑·待复核」，经 S0-S4 log §3 对账修正为 🟢Done。
- **新源 doc 全量纳入**：nav-infra-review 的 2×P0（围栏→T0、scope 漂移→T2）+ 5×P1 + 3×P2 + 1×P3（→T4）；cross-harness handoff A1/A2/B/C（→Done）+ 邻接 D/E（原报告标「不在重点」，未纳入活跃排期）。
