# RVF 开审前独立消息与 checkpoint 契约

本契约把自主发起资格与可回退消息分界分开。Agent 可以自己判断条件满足并请求下一次征询；这不等于用户刚刚明确命令开审。任何新一轮正式 RVF 都必须先验证独立消息、会话、当前候选和不可变 Git 快照。当前 RVF 实现已提供消费门禁；cline-kanban 的完整 receipt 生产接口尚未完成，因此缺少证据时返回 `review_start_boundary_pending`。未验证其它宿主的原生 rewind 能力，不从 Codex 样本外推。

## 事故与偏差

研究基线和本任务 bootstrap 基线为本地 `main` 的 `0ac2e595521673cc2f316cfbd44f13f45e2d835c`；实施期间又单向吸收 `851a00d10b62bbdeac859f8eea07dad5004edbf9` 的无看板 Codex GUI 关闭策略，保留本任务全部 WIP，未反向更新 main。事故现场只读，未重跑或修改 Trace-of-Thought 的 `1d2b0`。

| UTC 时间（2026-10-18） | 事件 | 能证明什么 |
| --- | --- | --- |
| 20:21:27.155 | Stop 创建 `513533daf14bfed7`，transport 为 cline-kanban task chat | 存在征询意图，不证明已消费 |
| 20:21:29.632 | Codex 收到原生独立消息 | 第一轮确有消息分界 |
| 20:21:42.237 | Agent 选择 `deferred_until_condition` | 等待 b0669 修复及验收 |
| 20:30:30.921 | 用户交接运行时成果 | 前置条件的新证据，原文没有明确重新开审命令 |
| 20:44:32.025 | Agent 调用已删除的旧重开入口并自填 reason | 执行偏差：把自己的判断当成用户口头请求 |
| 20:44:32.625 | `f5ea163d178641e0` 创建并接受；transport/status 为空，explicit 为 true | 设计缺口：CLI 接受自述来源，mint 后直接 acknowledge，无新消息 gate |
| 20:44:39—52 | 已分配 lease、准备 run | 正式副作用已发生，不能等到 dispatched 才定义开审 |
| 20:46:15.408 | run `rvf-20261018T204434Z-agentic-initiation-start-c34d2857` 派发 | 第二轮没有新的独立消息分界 |

两枚 token 的候选指纹不同，第一轮分界不能覆盖后续实现。原 token 的终端投递 ledger 曾显示 `delivered_and_submit_confirmed`，而 token 字段仍 pending，说明投递状态本身也需对账。相关 Kanban ledger 的 turn 6 checkpoint 创建于 20:21:20，早于 token；它是复制的 latest checkpoint，不能证明与本次消息绑定。

证据入口是创建需求中的两枚 token、来源更正文档、run handoff 及原始 rollout。只摘录用户消息与工具调用的事件事实，不导出内部推理或整段会话。

## 状态链与正式开始

```mermaid
flowchart TD
  stop[Stop: 计算候选并 mint] --> delivery[持久化投递意图 / 发送独立边界标记]
  delivery --> native[宿主提交原生 UserMessage / 独立 user turn]
  native --> snapshot[宿主绑定原生回退消息和不可变 Git 内容快照]
  snapshot --> gate[统一分界 gate: 身份 / token / 候选 / base / 活动会话分支]
  gate --> start[acknowledge → prepare / lease / 在途凭证]
  start --> dispatch[再次 gate → dispatch / detached → reviewer]
  native --> defer[Agent: deferred_until_condition]
  defer --> condition[Agent 判断前置条件已满足]
  condition --> request[request-review-start-boundary: 新 token / 新消息请求]
  request --> delivery
  user[真实明确用户命令] --> native
  gate -->|缺证据| pending[等待分界 / 不进入正式审查]
  gate -->|候选或分支失效| invalid[作废当前分界 / 重新请求]
```

- mint、候选计算、投递意图和 receipt/run 绑定意图是轻量协调记录，不运行 reviewer、不分配审查 lease。
- `acknowledge` 是最早正式副作用；消费 reopen 意图、分配 lease、创建正式 run/prepare 工件、arm 在途凭证也算正式开始。诊断用 Stop ledger 不等于正式 review run。
- `start` 在 acknowledge 前验证；默认 prepare 流水线在创建 run 前再验证；直接 `prepare_run`、prep 文件重放以及 reviewer 的直接、CLI、detached 执行均检查同一证据。旧 Stop 路由在 precheck/reopen/allocate 前检查，fork、prep 写入与冻结入口也验证同一条件。off/report-only 和 dry-run 诊断不创建正式 prep 或接受决策，诊断的计划工件不能直接派发。
- `accepted`、`submitted`、`delivered_and_submit_confirmed`、模型声称消费，以及 `initiated_by_explicit_user_request` 都不是 gate 的授权证明。
- Git checkpoint 与会话回退是两个能力。隔离 Codex CLI 0.163.0 实测：Stop 的 `<hook_prompt>` 虽在 rollout 中是 role=user，却不是原生独立回退 prompt；独立用户消息可以选中回退，界面说明只回退会话、文件不变。Git 快照负责保存候选内容，不能声称 Codex 原生 rewind 会恢复文件。

## RVF 已实现的门禁

实现入口：`core/review_start_boundary/review_start_boundary_verification.py`、Codex 原生证据 reader、`scripts/review_start_boundary_gate.py`。

当前只放行已做原生实验的 Codex CLI 0.163.0 TUI（originator=codex-tui，已观察 source=cli/vscode）；Codex GUI、exec 与其它宿主形态不借用这份原生回退证明。原生 session_meta 的版本不在已验证集合时保持 pending，升级后须先重跑原生实验。本次直接启动 TUI 的隔离 app-server 实验原生记录 source=vscode，当前 Kanban TUI 记录 source=cli；source 字段单独不能证明 VS Code 扩展或 GUI 的回退能力。每次首次开审或重入，先验证静态 receipt、原始 JSONL 和 Git 候选，再在状态迁移前重新查询宿主实时活动分支；首次没有静态 receipt 时另先向宿主取一次，最后的实时查询仍不可省略。JSONL 要求 `session_meta.id` 相同、`task_started.turn_attribution.turn_trigger=user`、对应 turn 的 `item_completed.item.type=UserMessage` 和原生 message ID 相符。只存在 `response_item.role=user` 不算证据；后续出现另一用户发起的工作 turn、消息被截断移除或发生已持久化 rollback 都拒绝旧分界。真实 PTY 实验进一步确认：TUI 已显示回退后，原始 rollout 仍保留旧 UserMessage，静态 reader 仍判存在；所以它只证明消息曾存在，活动分支资格必须由本次 nonce 查询的宿主快照证明。重启后也不复用旧分支观测。

自动正文必须精确包含本 token 的独立单行边界标记，不能混入其它输入。自动来源的 token 必须为 explicit=false；手动来源必须对应原生明确 `/review-validate-fix`、`$review-validate-fix` 或 `:review-validate-fix` 主 skill 调用，也接受合法的前置文字及命名空间形态，例如 `now $review-validate-fix:review-validate-fix your own work.` 和 `$rvf:review-validate-fix`。UPS 与 verifier 共用纯文本识别与抑制规则；`$rvf:review-validate-fix` 等其它命名空间在 UPS 仍须由 Codex `text_elements` 确认，verifier 先核实 manual UPS provenance，再识别该原文，不扩大 UPS 的文本 fallback。兄弟 `rvf-*` skill、交接正文和被引号包裹的普通提及不能代替主 skill 调用。原文仍须是通过宿主证明的独立 user turn，并同时满足 `initiated_by_explicit_user_request=true`、`developer_manual_trigger_via_user_prompt_submit` provenance、原始消息 SHA256、完整 checkpoint 绑定及实时活动分支证明；合法命令形态本身不构成开审授权。Agent 自填 reason 不参与来源裁决。

自动消息的原生接收时间必须在 token 创建与到期之间。手动命令允许原始消息先于 UPS 铸 token，间隔最多 120 秒；这不放宽消息、会话与快照绑定。过期、迟到、无有效在途锚点的 token 不得直接 prepare。

快照必须使用 40 位不可变 commit，tree、HEAD、当前候选指纹、文件身份/mtime/ctime 签名、候选文件内容 SHA256、比较基线及其本地 ref tip 都重新核验。commit 正文必须有且只有一行 `RVF_REVIEW_START_BOUNDARY_BINDING=<JSON>`，内容等于下述绑定字段，不能复用任意 latest commit。Git 子进程清理继承的 `GIT_*` 仓库定位变量。还核对文件类型、执行权限、仓库内符号链接及 gitlink commit；仓库外链接和带未冻结修改的 submodule 拒绝使用本快照证明。

通过验证后，先用排他锁将原生 session/message 绑定到唯一 token 和静态 receipt 摘要，再绑定唯一 `run_id/run_dir`；同一消息不能借给另一 run。相同 token、静态 receipt 摘要、真实 `run_id` 与规范化 `run_dir` 才允许重入。所有 token 的绑定写入共用排他锁，并从同一组不可变 `*.run-binding.json` 记录核对反向归属；既有 run_id 或目录均不能被另一 token 占用。bridge 复用该绑定器，不能重写事实或从目录名推断 run_id；完成通知也使用冻结的真实 run_id。prep 必须在 completed 缓存返回及 startup scope/run 工件落盘前完成验证与占用。相同 run 重入仍须通过 gate。当前实现有意保守：若已准备但未派发的 run 在后续 turn 或候选变化后失效，先终止旧准备并释放范围；不得直接把旧 run 或分界转给新候选。已开始 run 的跨 turn 冻结输入重试优化未在本次放宽。

## cline-kanban 的生产接口与责任

RVF 发送独立边界标记，使用 `--deliver-when-agent-idle`，空闲等待上限为 token 剩余 TTL 减 196.5 秒终端投递预算；先落 intent，再发送。Kanban 用 task+幂等键约束重复投递。请求过程中退出、重启或未确认时复用相同派生 token/key，不在当前工具调用的 turn 内继续 start。

cline-kanban 负责证明以下因果次序：目标会话确定 → 原生独立消息提交 → 原生消息/turn 已可选作 rewind 分界 → 针对此消息捕获准确 Git 内容快照 → 提供 receipt。不能把任务卡 latest checkpoint 无条件贴到消息回执上。应捕获准确消息/turn 的 immutable checkpoint，保全 ref 以免被 GC，并在 rewind/重建会话时按活动分支更新或撤销证明。

`kanban task message-status` 应在原有回执中增加 `independent_user_turn_boundary_receipt`。RVF 当前消费者已支持该字段；每次 gate 还会通过 `--review-start-boundary-observation-nonce <本次随机 nonce>` 请求活动分支快照。旧 CLI 不支持此参数，或没有完整观测时保持 pending。canonical receipt、prep/contract 内的 receipt 和 `RVF_REVIEW_START_BOUNDARY_RECEIPT` 都只定位并冻结静态证明，不能跳过实时查询。现有手动消息也需宿主为其生成可查询的原生消息记录；用户的明确命令本身可以充当消息分界，仍需 checkpoint 和当前分支证明。

宿主必须返回 `review_start_boundary_active_branch_observation`，含本次 `request_nonce`、查询的 task/key/session、`native_active_branch_user_message_ids` 和 `native_active_branch_user_turn_id`。边界消息必须恰好出现一次，当前 user turn 必须与 receipt 相同。nonce 必须来自本次请求；不得回显旧查询、用 accepted/submitted 代替，或仅把历史 rollout 末尾当作活动分支。宿主需通过真实原生控制面观察 UI rewind 后的当前分支；无法证明时不返回正向观测。该 producer 尚未实现，因此本任务未宣称跨仓端到端完成。

```json
{
  "review_start_boundary_active_branch_observation": {
    "request_nonce": "<本次 RPC 随机 nonce>",
    "kanban_task_id": "<task>",
    "idempotency_key": "rvf-decision-point-<token>",
    "session_id": "<native session>",
    "native_active_branch_user_message_ids": ["<当前分支真实 UserMessage ID>"],
    "native_active_branch_user_turn_id": "<当前独立 user turn>"
  }
}
```

最小对象示例（值均为宿主实际观测，示例不能用于生产）：

```json
{
  "schema": "rvf-independent-user-turn-boundary/v1",
  "decision_token": "<token>",
  "kanban_task_id": "<task>",
  "idempotency_key": "rvf-decision-point-<token>",
  "session_id": "<native session>",
  "worktree": "<absolute worktree>",
  "transcript_path": "<native active transcript>",
  "native_user_message_id": "<selectable native message>",
  "native_turn_id": "<independent user turn>",
  "boundary_message_sha256": "<raw native message SHA256>",
  "source": "rvf_automatic_decision_message",
  "candidate_fingerprint": "<RVF candidate fingerprint>",
  "candidate_filesystem_state_sha256": "<candidate filesystem identity signature>",
  "worktree_head_commit_sha": "<HEAD at boundary>",
  "comparison_base_tip": null,
  "comparison_base_ref": "main",
  "comparison_base_ref_tip": "<local main commit>",
  "checkpoint_commit": "<immutable commit>",
  "checkpoint_tree": "<tree containing candidate contents>"
}
```

文件身份签名由 RVF 的 `candidate_filesystem_state_sha256(repo, candidates)` 计算，producer 应调用同一 Python 实现，避免跨语言整数精度与文件时间单位差异；内容改后写回原样仍需新分界。

checkpoint 的 JSON 绑定字段见代码 `CHECKPOINT_BINDING_FIELDS`，不含 commit 自身以避免循环哈希。宿主需保留 message→checkpoint 映射；仅有 Git commit 正文不能证明宿主已经提供了原生可选回退。

本任务不编辑 cline-kanban。当前源码与已安装 CLI bundle 均未发现本契约的 receipt 字段；已安装 `task message --help` 确认支持两项 idle 参数。自动投递的可用性恢复与生产端 receipt 的真实端到端验收由关联 cline-kanban 任务负责。Claude Code/Kimi 适配器明确报告能力未取证并保持 pending；不能静默沿用 Codex reader。

## 自主恢复与旧路径移除

`rvf request-review-start-boundary --deferred-decision-token <旧 token> --reason "条件满足的自主判断"` 只适用于本会话最近的延后型谢绝。结构性谢绝、旧 token 或其它会话拒绝。普通未开始 token 可用 `--decision-token` 请求新分界；旧分界被撤回，派生 token 的来源仍是自动请求。延后重问仅释放延后条件门，冷却、结构性 snooze、铸造熔断不获得用户显式请求的豁免。

删除旧 `--reopen-own-declined-decision` parser、执行函数、伪口头来源常量、技能指引及旧正向测试。不迁移或重写历史 token。旧 CLI 参数报错，旧缺证据 token 在新 gate 下等待或失效。项目未正式分发，未增加旧入口适配器或兼容框架。

推荐方案是“下一独立 turn 重投 + 统一副作用 gate”组合。只重投仍可被直接 prepare/dispatch 绕过；只加 gate 而不提供自主重投会造成无恢复路径。每轮人工确认会削弱用户要求保留的自主性，所以未采用。

| 机制 | 收益 | 成本与限制 |
| --- | --- | --- |
| 仅延后恢复重新投递 | 保留自主判断，恢复时产生新消息 | 直接 start/prepare/dispatch 仍可绕过，不能独立满足不变量 |
| 仅统一来源与 checkpoint gate | 首个正式副作用前集中拒绝缺证据路径 | 缺少自主恢复入口时停在 pending；静态历史也不能识别原生 UI 已回退 |
| 重投与实时分支 gate 组合（本次采用） | 自主恢复和所有正式入口执行同一约束 | 需要宿主生产准确 receipt 与实时分支观测；接口未就绪时保持 pending |

## 验收矩阵

| 场景 | 必须观察 | 自动化证据 |
| --- | --- | --- |
| 初次自动发起 | 独立原生 UserMessage、候选快照先于 acknowledge/prepare | 专用 pytest：真实隔离 Git、合成宿主事件/实时观测接口替身，正向到真实 prepare；原生 PTY 实验单独验证可回退能力与静态历史的盲区 |
| 延后自动恢复 | 新 token、新消息请求，explicit=false，当前 turn 不开审 | 专用 pytest：重复请求、未接受状态、来源字段 |
| 用户明确手动命令 | 原始独立主 skill 命令（含前置文字与命名空间）+ manual provenance + 新绑定 commit | 专用 pytest 正向；兄弟 skill、交接正文、普通提及、hook 与伪来源反例拒绝 |
| 条件满足但无新 turn | 不 acknowledge、不 prepare、不分配/派发 | 专用 pytest 反例，不读取 accepted 作为成功 |
| 重复/失败/迟到/TTL | 相同请求 token/key；失败保持 pending；到期后消息拒绝 | 专用 pytest；Kanban 队列真实提交/消费仍须跨仓验收 |
| 候选/HEAD/base/bootstrap 改变 | 旧分界失效，不用内容指纹掩盖纯 commit/base 变化 | 专用 pytest 与既有 bootstrap 回归新断言 |
| rewind/重启 | 撤销分支上的消息不可复用；重启重新读原生事件；一条消息不能绑定另一 run | 专用 pytest；隔离原生 Codex 选择回退实验 |
| 直接 prepare/dispatch/detached | 缺证据时无正式 run/renderer/reviewer 副作用 | 专用 pytest 及已有 dispatch 子进程回归的显式合成证明 |
| 其它宿主 | 未验证的能力维持 pending | 适配器行为检查；不宣称原生 E2E 已通过 |

既有纯状态机/SOW 字段测试用可注入 gate seam 隔离其原有职责；它们不能计作真实分界完成证据。既有 prepare/dispatch 子进程正向回归改用独立测试夹具，生成实际隔离 Git commit、原生事件与 token，不设置生产 bypass。所有研究实例使用临时隔离配置，不写生产历史或事故状态。

## 关联任务与后续实施边界

- `c5db6` 处理延后后重问熔断，不因其 backlog 标题推断当前行为；本次不顺带改熔断策略。
- `9f7b5` / `8d3ac` 的投递对账、复工撤回与本地 main 上已有代码有关；它们不能证明正式开审前 gate 已存在。
- `87f2c` 研究延迟投递，cline-kanban 空闲队列相关修复已有部署；它不自动提供消息/checkpoint 的完整绑定证明。
- `cab9c` / `92ccd` / `f03b5` 聚焦完成后 rewind 的 marker/续跑；本次在开审之前设门，不覆盖其 marker 清理 WIP。
- cline-kanban `297ef` 正在实现完成后自动 rewind 与 land/base 同步，已记录其消费本任务产出的关联；它依赖准确开审分界，不等同于当前 producer 已实现。本契约可作为其对接输入，不在本任务代改该 checkout。

后续跨仓步骤：实现上述 producer → 在隔离 Kanban/Codex 实例验证真实独立投递、消费、可选 rewind 和快照绑定 → 验证自动恢复/失败/重启/rewind 矩阵 → 按独立授权执行本地安装。RVF 本任务只提交当前 worktree，不自动安装、不更新本地 main 或远端。
