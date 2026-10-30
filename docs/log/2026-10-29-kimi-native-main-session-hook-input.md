# Kimi 主会话 hook 输入修复与原生验证

本次修复的是 Kimi 原生 `UserPromptSubmit.prompt` 内容块数组未被解析的问题：显式 RVF 入口和真实用户回合补证都被字符串检查提前跳过。修复前后已用真实 Kimi 2.3.1 主会话对照验证。原生 Stop 的 `permissionDecision: deny` 在修复前即可到达同一主会话，未修改该输出协议。

任务为 `review-validate-fix/4beef`，起始提交为 `b567dc6cbbad6b114a8d9c0edb7647de93206315`。初次实现验证阶段按用户要求在测试后停止，当时未运行 RVF。后续用户显式调用 `review-validate-fix:review-validate-fix` 并要求继续，评审与本地提交按该新授权执行。本报告中的原生 hook 验证没有派发 reviewers；未部署或改写全局 Kimi 配置，未修改 external reviewer/fixer 派发链。

## 根因与证据边界

原生主会话捕获的输入形状为：

```json
{
  "hook_event_name": "UserPromptSubmit",
  "session_id": "session_baff6ddb-292d-4d70-895f-72aaebadf62b",
  "prompt": [{"type": "text", "text": ":review-validate-fix 审查当前任务"}]
}
```

`handle_hook` 原来要求 `isinstance(prompt, str)`，因此既不执行真实用户回合的起点补证，也不返回显式入口上下文。即使只放宽入口检查，补证器的 `_real_prompt_identity` 仍只接受字符串，所以必须把提取后的文本同时传给已有补证器。

修复前原生对照中，显式入口 stdout 为空；同一缺证状态下第二条真实用户消息没有增加补证次数。修复后显式入口产生顶层 `message`，该正文出现在同一主会话的实际模型请求中；新的真实用户消息使 unknown 记录进行一次有界重验，重复 Stop 不增加尝试。证据恢复后，已提交候选的 Stop 决策正常送达。

未提交候选的修复前原生对照已经能发出 Stop deny，后续模型请求包含完整决策正文。故这次证据不能支持“当前版本的 Stop 从不调用”或“deny 字段错误”。全局和 Kanban managed home 的静态 activation check 均返回 0，但既有主会话未保留与本次观察绑定的加载、调用和接受证据，用户过去未见触发的全部历史原因仍为未知。

standalone 会话缺少 Kanban task 绑定时继续不自动发起；缺证、无候选、已提示和子角色抑制继续静默。缺证恢复仍需要真实证据，文本适配不会把 unknown 改写为成功。

## 最小改动

- `HostAdapter.normalized_user_prompt_text` 只为 Kimi 内容块数组提取 `type: text` 且正文为字符串的块；现有宿主的字符串输入保持原语义。
- `handle_hook` 的显式入口和补证共用提取文本，补证事件保留原有身份字段；不修改传入事件。
- 增加入口、非文本块和原生 prompt 补证回归。未增加依赖、配置开关、迁移或旧版本兼容旁路。

## 验证结果

先新增回归，修复前为 2 failed、1 passed；修复后三个相关测试文件为 **50 passed**。

```sh
python3 -m pytest -q tests/test_task_snapshot_hook_output.py tests/test_kanban_committed_task_review_decision.py tests/test_kanban_starting_point_validation_fixer_regressions.py
```

原生验证通过 `kimi web --host 127.0.0.1 --no-open` 启动独立进程，经原生 REST API 创建 `main` agent 并提交用户消息。使用隔离的 HOME、CODEX_HOME、CODEX_SQLITE_HOME、KIMI_CODE_HOME、RVF 状态、Git main/task worktree 与 Kanban 卡片/来源证据。本地确定性模型服务只返回确认文字，不输出工具调用；它用于记录宿主实际送入模型的上下文，不证明外部模型会选择发起 RVF。

观测包装器原样转发原发布入口的 stdout、stderr 与退出码，记录 native session、payload cwd、插件进程 cwd 和任务环境。正向场景同时核对原生 wire 中 `system_trigger / stop_hook` 的插入以及下一次模型请求中的完整正文，没有用 Python helper 直调或 `kimi -p` 代替主会话证据。

| 修复后场景 | 原生 Stop 次数 | Stop 决策数 | 模型请求数 | 结果 |
| --- | ---: | ---: | ---: | --- |
| 显式入口内容块 | 1 | 0 | 1 | UserPromptSubmit 上下文随当前回合送达 |
| 未提交候选，同会话再 Stop，再换主会话 | 3 | 1 | 4 | 同任务同候选只提示一次 |
| 干净且无候选 | 1 | 0 | 1 | 静默 |
| 干净已提交的任务候选 | 1 | 1 | 2 | 同一主会话收到 Stop 决策 |
| RVF 子角色 suppress | 1 | 0 | 1 | 静默 |
| 无 Kanban task 绑定 | 1 | 0 | 1 | 静默 |
| 活动 run 注册记录损坏 | 1 | 0 | 1 | 诊断仅在 stderr，不续跑 |
| unknown 来源、真实 prompt 重验、证据恢复 | 3 | 1 | 4 | 有界补证，恢复后送达一次 |

全部 8 个修复后场景通过。修复前的显式入口和补证对照也保留在证据目录。早期测试夹具的复制软链、未选择模型和 OpenAPI 信封解析问题分别留档，均不计入产品验证通过结果。

## 持久证据与复验

证据根目录：

`/Users/bominzhang/.cline/kanban/cross-task-handoff-artifacts/review-validate-fix/4beef/kimi-main-session-stop-hook/`

- `before-fix-regressions.*`：修复前失败的测试与命令。
- `targeted-regression-result.json`：50 项相关回归结果。
- `runtime-and-historical-observations.json`：原生二进制版本/digest、静态注册和历史会话观察边界。
- `native-verification-matrix.json`：每个场景的完整命令、退出码、session 与结果。
- `before-fix-native-server-attempt-003/`：修复前 Stop 正向原生调用及同会话上下文。
- `before-fix-native-manual-context-attempt-002/`、`before-fix-unknown-evidence-recovery/`：修复前输入缺陷对照。
- `after-fix-*/native-hook-invocations.jsonl`：原始 hook stdin/stdout/stderr、cwd、环境和退出码。
- `after-fix-*/native-model-requests.json`、`isolated-kimi-code-home/sessions/*/*/agents/main/wire.jsonl`：实际模型输入与原生上下文插入。
- `after-fix-*/source-payload-sha256.json`、`native-openapi.json`、`native-plugins.json`、`native-runtime-cleanup.json`：源码快照、宿主 schema、加载结果及本次进程清理回执。
- `verify_kimi_native_main_session_hook_scenarios.py`：可重放的单场景自动化脚本；`run_native_verification_matrix.py` 保存本轮场景组合。

Kimi 二进制、manifest 或 hook 实现变化时，可向一个新的持久证据目录重跑单场景，原证据目录禁止覆盖。示例将脚本路径设为上述根目录中的脚本、源码路径设为本次 checkout，再传 `--evidence-directory`、`--scenario dirty-candidate-deduplication`。修复前源代码存于 `source-before-fix/`，由起始 HEAD 的 `git archive` 生成。

协议背景参考 [Kimi 官方 Hooks 文档](https://moonshotai.github.io/kimi-code/en/customization/hooks.html) 与 [Server API 文档](https://moonshotai.github.io/kimi-code/en/reference/server-api.html)；本次判定以本机二进制提取片段、原生 OpenAPI 和实际会话记录为准。

本报告记录首次实现及原生 hook 验证，不把这些测试记作完整 RVF 完成或本机插件部署。后续显式授权的 RVF 以自己的精确 run、最终验证与提交收尾回执为准。

## RVF 补修：同文本的真实用户消息身份

三个匿名发现指向同一缺陷：Kimi 2.3.1 的实际 UserPromptSubmit 没有 `turn_id`、`prompt_id`、`event_id` 或 `transcript_path`。仅保留原字段会把同一 session 内的多条“继续”折叠为一次补证机会。原有 transcript 回归不能证明原生 Kimi 已覆盖该边界。

`HostAdapter.user_prompt_recovery_event` 现在从隔离原生会话已经持久化的 `server/events/<session_id>.jsonl` 提取当前 main agent 的运行中 `prompt.submitted.promptId`，核对 session 与提取文本后交给既有补证器。该记录在 UserPromptSubmit 之前可读，而 `wire.jsonl` 的当前 `turn.prompt` 此时尚未写入，不能拿上一条 wire 消息充当当前身份。显式消息身份及其他宿主语义保留；无有效原生身份时只检查来源变化，显式入口仍可独立使用。不增加计数器、随机身份、配置开关或兼容旁路。

新增真实 Git 回归不向 hook 人造传入消息 ID：暂移起点对象，使用原生事件形状调用 hook，恢复对象后同条 hook 仍去重；追加同文本的新原生消息后恢复为 verified/present，Stop 只产生一次决策。默认和自定义 Kimi home 均覆盖，缺失/损坏记录、其他 session/agent、通知、排队项和文本不匹配均不能制造补证机会。修复前新增两例均失败；修复后两例通过，另外 75 项既有相关回归通过（分次执行）。

本轮真实 Kimi main agent 的三次用户文本均为“继续”：起点对象仍缺失时尝试数为 1、2，恢复同一对象后为 verified/present、尝试数 3；每次重放同条发布 hook 均保持原尝试数。3 次原生 Stop 只产生 1 条 `permissionDecision:deny`，完整正文出现在同 session 的下一次模型请求和 `system_trigger/stop_hook` wire 记录中，4 次模型请求。补证 hook 的 stdout/stderr 均为空。原生服务正常退出，本地模型服务已停止；未派发 reviewer，也不证明真实外部模型自主启动 RVF。

持久脚本为初始证据根目录下的 `rvf-explicit-review/run-001/fixer/attempt-005/native-equal-prompt-smoke.py`；通过证据在同目录的 `native-equal-prompt-attempt-002/`，包含 hook 执行前的原生提交记录、原始 stdin/stdout/stderr、重复 hook 补证记录、模型请求、wire 与清理回执。`attempt-001` 保留了首次验证脚本把成功后持久化起点引起的观察摘要变化误判为失败的记录；删除该不正确断言后另建目录重验，未覆盖历史。冻结的完整 pytest 与八场景最终验证由执行器在应用补丁后的完整候选树统一执行，本节不预先宣称其通过。

