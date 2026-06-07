# RVF Prelude 浪费复核

> 分析基准：local `main` `75ef235`（2026-06-04）。生成于 2026-06-07。
> 路径约定：本文 file:line 一律用仓库根相对完整路径，可直接 grep 复算。
> 脚本目录前缀 = `plugins/review-validate-fix/skills/review-validate-fix/scripts/`（下文记为 `…/scripts/`）。

## 背景：什么是 "prelude"

"Prelude" = 一次 RVF 在 **dispatch 之后、派出 reviewer 子代理之前**，主会话所做的全部准备动作：读 prep 文件、确认 workflow 状态、source 环境、写 scope-of-work、理解协议。本复核基于两条真实轨迹：

- **轨迹 A（manual 触发）**：用户在已激活对话里直接发 `/review-validate-fix` 字面量。
- **轨迹 B（Stop-hook Kanban-followup）**：Stop hook 注入 followup 消息派发的 RVF run，review 对象 = my-kanban M0/M1 代码。

核心问题（用户原始两侧提问）：(1) prelude 里哪些浪费是**跨所有 run 不变、更适合程序化**的确定性 plumbing？(2) 哪些是**不可替代的 agent reasoning**，不应被程序化？本文两侧都正面回答（见第五、六节）。

---

## 〇、main 最新工作核对（关键前提）

分析最初所在 worktree 的 HEAD（`0b3b2af`, 2026-05-28）比 local `main`（`75ef235`, 2026-06-04）**落后 15 个 commit**，且 `0b3b2af..main` 全是 main 单向领先（`git rev-list --count 0b3b2af..main` = 15；反向 = 0）。先前 explore 读的全是旧文件态，其中三份脚本/文档被 main 后续 commit 重写。**所有 file:line 已重新对 main 核对刷新。**

| commit | 主题 | 对本分析的影响 |
|---|---|---|
| `75ef235` | manual 触发结构化检测改用 vendored `codex_invoked_skill.py`（结构化优先 + 正则回退） | 「触发器过宽」结论需限定：Codex 经 rollout `text_elements` 可**结构化**命中命名空间形态；但判定为 `结构化 OR 正则`，**Claude 无 rollout → 回落 `RVF_MANUAL_TRIGGER_RE`**，故「粘贴字面量空跑 prep」在 Claude 路径仍在 |
| `2f29aec` | UPS hook 每触发都发用户可见 `systemMessage` | 框架补注：hook 现每触发回一条用户可见消息（已 land，非浪费项） |
| `87e9338` | kanban-followup 锁改投递确认时 arm；重写 dispatch 文件 ~198 行 | 第二节 file:line 全位移，已按 main 刷新；重叠 Origin 指令冗余仍在 |
| `a86dad7` | 失败再入 rvf-reopen（+1622；新增 `rvf_rescope.py`/`review_reopen_marker.py`，又改该文件 +135） | 同上位移；rvf-reopen 已 land，不入活跃 backlog |
| `556d02b` | no_issues review result 必填 `audit_summary`（Option A） | 已 land |
| `84f1617`（2026-06-01） | 收编积压文档：新增两份含 backlog 的 doc | 新 backlog 来源（见第三节），0b3b2af-based survey 未覆盖 |

> 旁证：核心三份 backlog doc（`docs/global-tracker-finishing-handoff.md` / `docs/workflow-plugin-design-system.md` / `docs/rvf-dispatch-flow-overhaul-plan.md`）在 `0b3b2af..main` 间 **UNCHANGED**，旧 survey 对它们仍有效。

---

## 一、旧结论复核（manual → 是否也适用于 Stop-hook 轨迹）

| 旧结论 | 复核 | 判定 |
|---|---|---|
| prep 已由 hook 预置，agent 应跳过 prepare（cat prep → 确认 `status=completed` → source env） | 两条轨迹都在确认 `status=completed` 后跳过了 prepare | **仍成立** |
| 用 `source review-env.sh` 而非结构化 JSON，载体错配 | 轨迹 B 里 review-env.sh 达 **451 行**、被整篇 `cat` | **仍成立，且被放大** |
| scope-of-work 的 reasoning 正文是不可程序化的核心产出 | 两条轨迹都写了大段真实 scope-of-work（Kanban 那段尤其详尽） | **仍成立** |
| agent 过度「重新自证」prep 已保证的事实 | 轨迹 A 很严重（source→撞墙→逐文件重读）；**轨迹 B 更克制**（批量 cat、确认即跳过） | **部分修正**：这类浪费是 run-variable 的单次 agent 行为，不是每轮固定成本 |
| 触发器过宽（字面量误触发空跑 prep） | 仅对 manual 字面量路径成立；Kanban 是 token 派发不受影响。`75ef235` 后 Codex 走结构化更精准，但 Claude 仍回落同一正则 | **仍成立（限 Claude/manual 路径）** |

**结论**：旧分析主轴（"agent 把轻量交接做成从零侦察" + "结构化数据被塞进 shell 文件"）整体仍成立；需修正两点：(a)「agent 过度自证」是 run-variable 行为浪费而非每轮固定成本；(b) 触发器结论按 `75ef235` 限定到 Claude/正则回退路径。

---

## 二、新增发现（主要在 Stop-hook / Kanban-followup 轨迹出现）

### 新发现 1：注入式 dispatch prompt 自带大块 origin 样板 + 重叠指令（每轮固定）

源码已按 main 直接核对（`…/scripts/codex_stop_review_validate_fix.py`，main 共 7460 行）：

- `kanban_followup_review_validate_fix_prompt()`（main **:1786–1833**）一次性拼装整条注入消息。
- 其中 `parent_origin_prompt_block()`（main **:1019–1063**）已含一段「维护 handoff.md / 逐字保留 `## Origin`」指令（**:1055–1062**，"维护 handoff.md" 在 :1057）。
- 紧接着 main **:1812–1821** 又有一段语义高度重叠的「维护 handoff.md / 保留 `## Origin`」指令。
- 这两段**落在同一条 kanban-followup 注入消息**里，合计 ~850 字符，每个 Kanban-followup run 必然双注入，是确凿的代码级冗余。`87e9338`/`a86dad7` 重写该文件后冗余依旧，仅行号位移。
- 全部 `RVF_PARENT_*` 值同时已落在 `origin.json`（prompt 里 `RVF_ORIGIN_METADATA` 即指向它）→ inline 再铺一遍属与 artifact 重复。

**精确口径**：「双注入」特指上面两处落在同一条消息；此外 `cline_kanban_task_prompt()`（main **:3013–3017**）还有**第三处**同类指令，但它属于**另一个生成 artifact**（写进 Kanban task 的 prompt），不与前两处同消息。去重时三处都要纳入考量，但不可笼统说「同一消息出现三次」。

> 轨迹粘贴里整块"看似出现两次"：源码只拼一次（`kanban_followup_..._prompt()` 对 origin_block 单次插值）→ 极可能是 **TUI 滚动回显/粘贴渲染产物**，并非二次注入（已读码排除）。

### 新发现 1b：generated Cline Kanban task prompt 以未闭合代码块围栏结尾（正确性 bug）

- `cline_kanban_task_prompt()`（main 起 **:2909**）在 **:3019** 写入 sh 代码块**起始围栏**（三反引号 + `sh`），但 **:3021** 的**闭合围栏被注释掉**（该行整行以 `#` 注释），其后「原始 fork prompt」块（:3022–3025）也整段注释。
- 结果：生成的 task prompt **以未闭合 code block 结尾**，后续内容可能被 Markdown 当代码块吞掉，污染 agent 对「最终回复 contract」（`RVF_HANDOFF_FILE` 等）的解读。
- 这是 `docs/agent-codebase-navigation-infrastructure-review.md` 独立标的 **P0**（原报告 2026-05-19 引旧行号 `:2644-2646`/`:2647`，main 现 :3019/:3021）。与新发现 1 同文件、同属「生成 prompt 质量」。**这是本复核里唯一的正确性 bug，应最先止血。**

### 新发现 2：一批"跨所有 run 不变"的静态 skill 文档，主会话每轮重读

- `SKILL.md`、`references/review-merge-policy.md`、`references/handoff-template.md`、`…/scripts/write_review_result.py --help`。
- 这些内容 per-run 永不变，却每轮被重读/重新理解一遍。
- **重要校正**：`prompts/reviewer.md` **不**属此类——它由脚本作为 self-contained prompt 直接喂给 reviewer 子代理，主会话无需读它来"理解协议"。

### 新发现 3（量级佐证）：review-env.sh 451 行被整篇 cat

- 印证"载体错配"：真正被 agent 用到的只是其中少量值（run 路径、scope 合同路径、几个脚本路径），却 source/cat 了 451 行 shell。

---

## 三、与 local `main` 已追踪清单的对齐（避免重复造轮子）

主 backlog 文档：`docs/global-tracker-finishing-handoff.md`（tracker/lease 收尾）、`docs/rvf-dispatch-flow-overhaul-plan.md`（dispatch/prep，Slice A–I 已全部 land）、`docs/workflow-plugin-design-system.md`（5 个 gap）。

**本轮新纳入的两份 backlog 来源**（`84f1617`/2026-06-01 收编，0b3b2af-based survey 未覆盖）：

- `docs/agent-codebase-navigation-infrastructure-review.md`（2026-05-19 导航基础设施现状报告，含 **2×P0 + 5×P1 + 3×P2 + 1×P3** findings；上面新发现 1b 即其 P0 之一）。
- `docs/log/2026-05-28-claude-code-cross-harness-adaptation-handoff.md`（cross-harness 适配缺口 **A1/A2/B/C**，baseline `f941ba0`，🔴 open + ✅G 已解）。**对账结果见下：A1/A2/B/C 在 main 上已全部闭合。**

| 前序/本轮候选 | 对齐结果 |
|---|---|
| "把 review-env 改成结构化 JSON" | ≈ `workflow-plugin-design-system.md` **Gap 1**（统一 `artifact.env.json` sidecar）。已记为 gap、未落地 → **归并引用，不重复立项** |
| "让 agent 信任 prep、别重新侦察" | 底层机制**已存在**（dispatch Slice I 已 land：prep payload `shared_workflow_state.status=completed` + `artifacts` 路径字典）；generated prompt 也已含「跳过手动 prepare、source env」（`…/scripts/codex_stop_review_validate_fix.py:3008–3011`）。缺口在 **anti-re-verify 纪律**，不是缺机制 |
| dispatch 样板去重 + 改读 origin.json（新发现 1） | **未见于已追踪文档 → 真正新增、可立项** |
| 未闭合代码块围栏（新发现 1b） | = nav-infra-review **P0**，并入 T0 |
| 削减主会话每轮重读静态文档（新发现 2） | 与 SKILL.md「文档分层」相关，但无条目专治"主会话重读" → 可新增 |
| **[新] cross-harness 缺口 A1/A2/B/C** | **✅已闭合**：`docs/log/2026-05-30-cross-harness-plugin-S0-S4-implementation-log.md` §3 闭合对照表确证 A1→`b1f4530`(S1.5)、C→`b1f4530`、A2→`b083f65`(S2-observe)、B→S1.5 主轨迹 call_id + S2-observe `candidate_patch_call_ids`，判据 `rg 'host=="codex"' core/` = 0。移出活跃 backlog，仅留已闭合记录 |
| **[新] nav-infra-review P1–P3（导航/文档卫生）** | 多为文档与生成-prompt 措辞项 → 归 T4 文档卫生线 |
| 其余 backlog（tracker lease/heartbeat、confidence ledger、validation-contract、verification UI） | 范围 B 下并入 master 表统一重排（见 `master-backlog.md`） |

---

## 四、每轮固定浪费的量级排序（正面回答"浪费在哪/最大单项"）

按**相对体量 × 每轮命中频率**排（仅计每轮固定注入/重读的确定性成本，不含 run-variable 的单次 agent 行为）：

| 名次 | 项 | 体量 | 频率 | 性质 |
|---|---|---|---|---|
| **① 最大单项** | review-env.sh 整篇 cat/source | **451 行 shell** | 每个 hook-prepared run | 载体错配——agent 实际只用其中 < 10 个值 |
| ② | 三处重叠 `## Origin` 指令 | ~850 字符（同消息双注入）+ 第三处 artifact | 每个 Kanban-followup run | 代码级冗余 + 与 `origin.json` 重复 |
| ③ | origin 样板 inline 全铺 `RVF_PARENT_*` | 一段元数据 | 每个 followup run | 与 `origin.json` 重复 |
| ④ | 静态 skill 文档每轮重读 | SKILL.md + 2 references + 1 --help | 每个 run | per-run 不变却重新理解 |

**结论**：单项最大浪费是 **451 行 review-env.sh 的载体错配**（新发现 3）——它一项就盖过 origin 样板与文档重读之和，且修复路径明确（= Gap 1 结构化 env sidecar，T1）。Origin 指令冗余（②③）虽单体小，但每个 followup run 必中且修复成本极低（纯模板合并，T0），止血性价比最高。

> 注：轨迹 A 里"agent source→撞墙→逐文件重读"那类**最刺眼**的浪费，经轨迹 B 对比确认是 **run-variable 单次行为**（轨迹 B 同一 prep 下 agent 克制得多），不计入此固定成本表；它的治理手段是 SKILL.md 的 **anti-re-verify 纪律**（T0），而非改注入体量。

---

## 五、不可程序化的 reasoning 侧（agent 应亲自保留什么）

用户提问的另一侧。以下不是 plumbing，**不应**被程序化掉，是 agent 在 prelude/loop 中的核心产出：

- **真实 scope-of-work 正文**——用户意图、本轮实际完成的工作、需审查文件、逐文件编辑明细、已跑验证、关键取舍与不确定点。hook 写的 `startup-scope-of-work.md` 只是最小 stub，必须由主会话以真实 reasoning 覆盖（SKILL.md 正常入口已明确）。
- **模式判定**——`full` / `review_only` / `validate_fix` / `research_checkpoint_no_handoff` / skip-review / no-handoff 的选择，依赖对用户本轮意图的理解。
- **reviewer artifact 合并取舍**——按 `references/review-merge-policy.md` 合并两路 reviewer 结果时的冲突裁决、去重、严重度归并。
- **validate/fix 分组与分配**——按根因/文件区域/测试路径/决策前提把 issue 分组、决定起几个子代理、各包边界。
- **最终中文汇总**——对 reviewers 与 validate/fixers 结果的人类可读概括。

**边界原则**（与 `workflow-plugin-design-system.md` 原则 3「Workflow State Is Not Prompt State」一致）：确定性 plumbing（路径、env、token、状态机、注入样板）应下沉到脚本/artifact/ledger；agent prose 只负责**解释与决策**。本复核的 T0–T1 止血项全部落在 plumbing 侧，**不触碰**上面这五类 reasoning 产出。

---

## 附：可复算线索（已按 main 刷新）

- 重叠 Origin 指令：`…/scripts/codex_stop_review_validate_fix.py:1055–1062` 与 `:1812–1821`（同消息双注入）+ `:3013–3017`（第三处 artifact）。
- 未闭合围栏：`…/scripts/codex_stop_review_validate_fix.py:3019`（起始）/ `:3021`（闭合被注释）。
- trust-prep 已有指令：同文件 `:3008–3011`。
- manual trigger 回退正则：`…/scripts/rvf_user_prompt_submit.py` 的 `_review_validate_fix_manually_invoked` → `detect_manual_trigger` → `RVF_MANUAL_TRIGGER_RE`。
- cross-harness 闭合证据：`docs/log/2026-05-30-cross-harness-plugin-S0-S4-implementation-log.md` §3 + 判据 `rg 'host=="codex"' core/` = 0。
