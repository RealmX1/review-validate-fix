# Stop hook 出口受众契约（as-built · 维护参考）

本文是维护 RVF hook 出口时的**强制阅读**参考。改动任何会写 hook stdout 的代码前先看一遍。

真相源：`core/stop_hook_output_audience/stop_hook_output_audience_contract.py`（翻译层）+
`tests/test_stop_hook_output_audience_contract.py`（结构性 + 行为 gate）。

---

## 0. 一句话铁律

> **失败、降级、skip、诊断——一切「错误」一律 `developer_only`，永远不驱动 agent 再跑一回合。
> 只有两类消息够格驱动 agent：(a) 是否发起 RVF 的决策点；(b) RVF 未完成时的续跑提示。**

历史依据：改造前统计，Claude 终端里 92 条 `Stop hook error:` **全部**是 RVF 刻意的、可执行的
agent 指令，**一条真失败也没有**。一个 100% 假阳性的告警通道会训练人忽略它，真故障随之被
一并忽略。腾出 `decision:block` 专供真失败，这个信号才重新有意义。

### 0.1 一个**不是**错误、却也走 `developer_only` 的出口（读到它别以为是违规）

决策点横幅在 cline-kanban task 内改走**带外投递**（经终端投递成为一条真实 user message）之后，
那次 Stop 的 stdout 走的是 `developer_only` + `systemMessage`——尽管它既不是失败也不是降级。

这不是给铁律开的口子，而是铁律的正确应用：**这一刻 stdout 上确实没有任何要驱动 agent 的东西**。
正文根本不在 stdout 里：几秒后一行**无指令的边界标记**作为 user message 到达并自己开一个新回合，
正文由目标会话的 UserPromptSubmit hook 在那一刻以 `additionalContext` 注入（仅在 token 首次
`minted → injected` 时注入一次；重复到达、正文缺失、推进失败、token 已终态一律 `developer_only`）。

为什么正文不随标记一起粘贴：cline-kanban 用 bracketed paste 写 TUI，Claude Code（≥ 2.1.278）
把它包成 `<pasted_content>`，系统提示规定其中的指令「只有在用户亲口要求时才执行」——粘贴块里的
「由你决定」无法替自己授权，会话内没有用户侧授权锚点时 agent 会反过来问开发者。hook 注入的
来源标注如实，不受这条规则约束；标记 user message 保留了 Claude Code 原生 rewind 边界，而 hook
注入是它的子节点，rewind 到标记之前会连同正文一起离开活动链（2026-09-29 真实 TUI 实测）。若给它安一个
「携带 agent_directive 但不渲染进 stdout」的第三受众，契约就会声称「本次出口驱动了 agent」，
而实际可能根本没送达——那是把契约变成谎言。

带外路径上契约不是权威。权威是三样东西：投递意图 marker（`kanban_followup_lock` 的第三族）、
decision token 状态、以及目标会话 UserPromptSubmit 观测到 `RVF_DECISION_TOKEN=<token>` 的那一刻。

---

## 1. Claude Code 的 Stop hook 有**三**条出口

| 出口 | transcript 形态 | 驱动 agent 再跑一回合 | 开发者呈现 |
| --- | --- | --- | --- |
| `{"continue": true, "systemMessage": …}` | `hook_system_message` | ❌ | `Stop says:` |
| `{"decision": "block", "reason": …}` | `hook_blocking_error` **+ 一条 `type:"user"` / `isMeta:true` 的 `Stop hook feedback:` 消息** | ✅ | `Stop hook error:`；正文进 `hookErrors[]` |
| `{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": …}}` | `hook_additional_context` | ✅ | 无错误框架 |

第三条的权威描述来自 Claude Code 2.1.233 二进制内嵌 schema：

> Hook-specific output for the Stop event. `additionalContext` is non-error feedback delivered
> to the model; the conversation continues so the model can act on it.

**关键推论：**

1. `hook_blocking_error` 是 harness 给 `decision:block` 起的**名字**，不代表失败。
2. **通道决定标签，内容不决定，且无法 opt out。** 你没法在 `decision:block` 里写一句
   「这不是错误」就让它不被渲染成 `Stop hook error:`。
3. `decision:block` 把注入变成一条**看起来像用户说的话**的消息（`type:"user"`），在记录里与
   开发者自己敲的字不可分辨；`additionalContext` 落成工具侧 attachment，不冒充用户。
4. **Stop 时刻不存在「agent 可见但不额外开回合」的通道**——第三条也开回合。要 model-visible
   又不开回合，只能等下一条用户 prompt 走 UserPromptSubmit。

### ⚠️ 同名不同义（最容易踩的坑）

`additionalContext` 这个字段名在两个事件上的**投递时机相反**：

| 事件 | 语义 |
| --- | --- |
| **UserPromptSubmit** | 搭用户**这一条** prompt 的车，**不额外开回合** |
| **Stop** | **立刻续跑**，不等任何用户输入 |

所以「把提示挪到 additionalContext 就不会打断 agent 了」在 Stop 上是**错的**。

---

## 2. 跨宿主：线材必须按 host 分流

| 能力 | Claude | Codex | Cursor | Kimi | Oh My Pi | DeepSeek |
| --- | --- | --- | --- | --- | --- | --- |
| `decision:block` | ✅ | ✅ | ✅ 仅交互 | ❌ **见下方纠错** | ✅ | ✅ |
| **Stop 上 `additionalContext`** | ✅ | ❌ `additionalProperties:false`（发过去整条 hook 输出 Failed） | ❌ | ❌ | ✅ | ❌ |
| **Stop 上 `hookSpecificOutput.permissionDecision:"deny"`** | ❌ | ❌ | ❌ | ✅ **唯一可用通道** | ❌ | ❌ |
| `systemMessage` | ✅ | ⚠️ schema 有字段但 `codex exec` 实测不落地 | ❌ | ❌ 二进制 0 次命中；Stop 上静默丢弃（无害），**UPS 上会被回落成正文注入模型**（见下方纠错 2） | ✅ | ❌ |
| **UPS 上 `additionalContext`** | ✅ | ✅ | ✅ | ❌ 二进制 0 次命中；只认顶层 `message`（见下方纠错 2） | ✅ | ✅ |

**跨宿主适配负担主要在 Stop**，但 **UPS 侧 Kimi 同样是例外**（见下方纠错 2）：
Claude / Codex / Cursor / Oh My Pi / DeepSeek 五家吃 `additionalContext`，Kimi 只认顶层 `message`。

**Codex 的 `additionalProperties:false` 是硬约束**：给 Codex 发 `hookSpecificOutput` 会让整条
hook 输出被判 Failed = RVF 在 Codex 上静默停摆。所以 host 未知时**必须**兜底到最保守的
`decision:block`，绝不赌未知 host 认识 `hookSpecificOutput`。

### ⛔ 纠错（2026-08-23 取证）：Kimi **不**认 `decision:block`

本表此前把 Kimi 的 `decision:block` 记为 ✅。**那是错的**，且这条错误会诱导出一个危险的设计
判断——「Kimi 被误判成 Codex 只是降级，仍能工作」。实际是**完全失声**。

kimi 0.29.1 二进制内嵌源码逐字：

```js
HookJsonOutputSchema = looseObject({
    message: OptionalStringSchema,
    hookSpecificOutput: HookSpecificOutputSchema,   // { message, permissionDecision, permissionDecisionReason }
});
// …
const { message, hookSpecificOutput } = output.data;
if (hookSpecificOutput?.permissionDecision !== "deny") return result;   // ← 唯一的 block 判据
```

schema 只**命名** `message` 与 `hookSpecificOutput` 两个键。`looseObject` 会让顶层
`decision:"block"` 通过校验（不报错），但解构处**从不读它** ⇒ 结果是 `allow`。

⇒ 三条后果，实现时都要按最坏情况处理：

1. **Kimi 上发错线材 = 静默 allow + 零报错。** 与 Codex 的「整条 Failed」相反，这里没有任何
   可观测的失败信号，只有「RVF 好像没反应」。
2. `permissionDecision` 的 zod 类型是 `unknown().optional()`，判据是 JS 全等 `!== "deny"`
   ⇒ 必须是**字面量** `"deny"`，大小写错、拼接得到的同值字符串以外的任何偏差都静默降级。
   本仓把它单列为常量 `STOP_HOOK_PERMISSION_DECISION_DENY` 并由 gate AUD-5 钉住。
3. `systemMessage` 在 Kimi 上落在 `looseObject` 的未命名区 ⇒ 被保留但从不读取。**Stop 的**翻译层
   **仍然照附**（三通道一律附），因为 `_apply_guardian_liveness_alarm` 本就会写它、
   且 ledger 的 `emitted_payload` 是跨 host 复盘的唯一真相源。**在 Stop 上**代价为零，
   去掉反而自相矛盾。
   ⚠️ **这条「代价为零」只对 Stop 成立**：UPS 上 Kimi 会把整段 stdout 回落成正文注入模型，
   附带的 `systemMessage` 会随之逐字进入模型上下文。见下方**纠错 2**。

顺带取证到的两条实现约束：hook 子进程的 `DEFAULT_TIMEOUT_SECONDS = 30`（manifest 应显式声明
timeout，别吃默认值）；`HookSpecificOutputSchema` 内层只命名三个键，故翻译层的 Kimi 分支
**不发** `hookEventName`（那是 Claude 专属的事件校验键）。

完整取证见 `kimi-code-cli-plugin-and-hook-runtime-forensics.md`。


### ⛔ 纠错 2（2026-08-26 取证，kimi **0.38.0**）：UPS 上 Kimi 只认顶层 `message`

本表此前把 Kimi 的「UPS 上 `additionalContext`」记为含糊的 `⚠️ exit-0 stdout`，并在正文里写着
「UPS 侧 additionalContext 基本全家都支持」。**那是没取证过的猜测。** 实测：

`strings` 全量抽取 0.38.0 二进制的 751,968 条字面量，`additionalContext` 与 `systemMessage`
**各 0 次命中**。Kimi 在 UPS 上读的是：

```js
// packages/agent-core/src/session/hooks/runner.ts
const { message, hookSpecificOutput } = output.data;
const result = { message: message ?? hookSpecificOutput?.message, structuredOutput: true };

// packages/agent-core/src/session/hooks/user-prompt.ts
function userPromptHookMessage(result) {
  if (result.timedOut === true || (result.exitCode !== void 0 && result.exitCode !== 0)) return;
  const message = result.message?.trim();
  if (message !== void 0 && message.length > 0) return message;   // ① 顶层 message
  const stdout = result.stdout?.trim();
  return stdout === void 0 || stdout.length === 0 ? void 0 : stdout;   // ② 回落：整段 stdout
}
function renderHookResult(event, message) {
  return `<hook_result hook_event="${event}">\n${message}\n</hook_result>`;
}
```

⇒ 发 `additionalContext` 等于什么都没发，命中回落 ②。而产物有**两个**去向
（`applyUserPromptHook`，两者取的是**同一份** `hookResult`）：

```js
this.agent.context.appendUserMessage([{type:"text", text: hookResult.text}], {kind:"hook_result", event:"UserPromptSubmit"});
this.agent.emitEvent({type:"hook.result", turnId, hookEvent, content: hookResult.message});
```

前者进模型上下文，后者经 TUI 的 `formatHookResultMarkdown` 渲染成 `*UserPromptSubmit hook*` 块。

⇒ **四条后果**：

1. **旧行为把整份 JSON（含 developer-only 的 `systemMessage`）逐字注入模型上下文。**
   不是推测——2026-08-25T21:28:57.898Z 已在真实会话里发生过一次，实据与逐字 wire 记录见
   `kimi-code-cli-plugin-and-hook-runtime-forensics.md` §12。
2. **Kimi 上不存在「开发者可见但不喂模型」的 UPS 通道**（注入与渲染同源），
   所以 developer-only 的唯一正确渲染是**静默**。
3. **UPS 的 `decision:"block"` 在 Kimi 上必定失效**——顶层 `decision` 从不被读取；
   要 block 必须发 `hookSpecificOutput.permissionDecision:"deny"`（与 Stop 同一个判据）。
4. **Stop 与 UPS 的开发者可见性是不对称的**：Stop 的 block 正文只
   `appendUserMessage(kind:"system_trigger", name:"stop_hook")`、**不发** `hook.result`
   ⇒ TUI 不渲染；UPS 发 ⇒ TUI 渲染。「Stop 的 developer-only 出口在 Kimi 上完全不可见」
   这条结论**不能推广到 UPS**。

**这条差异不由 core 处理。** UPS 核心 `rvf_user_prompt_submit.py` 保持 host-blind（五家通吃
`additionalContext`，Kimi 是唯一例外），翻译收口在 Kimi shim 的出口：
`plugins/review-validate-fix/hooks/_kimi_hook_entry.py` 的
`translate_core_payload_to_kimi_user_prompt_submit_wire()` 与 `emit_developer_only_notice()`，
回归护栏见 `tests/test_kimi_hook_entry_normalization.py` 的 **ENTRY-7**。

---

## 3. 代码怎么写

### core 只声明受众，不碰线材

```python
return StopDecision(
    ...,
    payload=None,
    audience=STOP_HOOK_OUTPUT_AUDIENCE_AGENT_MUST_ACT_NOW,
    agent_directive=AgentDirectiveStopHookOutput(
        agent_directive_body=<喂给模型的正文>,
        developer_visible_notice=<给开发者终端的旁注>,
    ),
)
```

- `audience` 由**构造决策的调用点显式声明**，不靠 `reason_code` 白名单反查。
  （`reengage_decision` 的 `reason_code` 既有默认值、也有 `f"kanban_followup_bootstrap_{…}"`
  这样**动态生成**的取值，任何静态清单都必漏。）
- `developer_visible_notice` **不是可选装饰**：历史上 90/92 条阻塞注入不带 `systemMessage`，
  开发者在终端里没法把它与「hook 真崩了」区分开。gate 会红。
- `developer_only` 的正文放 `payload`（`{"continue": True, "systemMessage": …}` 形状，或
  `None` = 静默停）。两者**不得并存**——两条 payload 合流的优先级在各 host 上均未实测。

### 线材由注入面决定

`HostAdapter.stop_hook_agent_directive_channel()`：

- `ClaudeCodeHostAdapter` → `non_error_additional_context`
- `CodexHostAdapter`（含 host 未知兜底）→ `blocking_decision`
- `KimiCodeCliHostAdapter` → `hook_specific_output_permission_decision_deny`

通道分派是**穷举 + 末尾 `raise`**，没有 `else` 兜底：新增通道却漏加渲染分支时必须当场炸，
而不是悄悄退化成某个 host 的形状。`_self_check` 另有一条完备性断言，要求每条已登记通道都
渲染出**两两可辨**的线上形状。

当前会话的 host 由 `resolve_current_session_host_adapter(event)` 判定，判据从强到弱：
`event["source"]["provider"] == "claude-code"`（Claude 入口亲手打的标）→ 会话路径落在
`/.codex/sessions/` 下 → `detect_transcript_format` 嗅 record 签名 → 兜底 Codex。

⚠️ 这是**当前会话**的 host，与 `parent_origin["host_kind"]`（父会话）不同。在 Kanban task /
fork 场景下两者会分叉，用错就会把 `hookSpecificOutput` 发给 Codex。

### 出口只有一个

`emit_stop_hook_payload(...)` = 受众翻译 → guardian 告警并入 → ledger 留痕 → stdout。
顺序是刻意的：guardian 告警在构造**之后**改写 payload，所以留痕必须排在它后面，否则 ledger
记的不是真正发出去的那份。

**为什么必须自己留痕**：宿主留痕能力差异极大——只有 Claude 能从 transcript 事后取证；Codex
rollout **完全不承载** hook 记录（实证 120 份 / 0 命中）。跨 run 复盘的真相源是 RVF 自己的
ledger，不是宿主 transcript。

---

## 4. 不受本契约管辖的（但仍登记在案）

`tests/test_stop_hook_output_audience_contract.py` 分两层扫描：

- **Stop 层（严格）**：`hooks/stop.py`、`stop_review_validate_fix.py`、`hooks/_claude_hook_entry.py`、
  翻译层本身。`decision` / `hookSpecificOutput` 只能在翻译层里被构造。

  `_claude_hook_entry.py` 归严格层是刻意的：它在核心异常 / 空输出时**自己** emit，是 core 收口
  之外的第三个出口。它刻意 stdlib-only、不 import core（fail-open 是最该稳的面，不给它加
  vendored import 失败模式），所以靠 AST gate 而非代码收口把它钉在「只准发 developer-only」。

- **非-Stop 层（登记制）**：UserPromptSubmit / PostToolUse 的出口。同名键在那儿语义不同——
  UPS 的 `decision:block` 是**挡掉这次提交**（prompt 抑制），UPS 的 `additionalContext` 是搭车
  注入。Stop 的受众分级不适用，但每处构造仍须在 `SANCTIONED_AGENT_DRIVING_KEY_CONSTRUCTORS`
  里带理由登记，防止无声漂移。

**为什么是 AST 而不是字面量扫描**：`"decision"` / `"block"` 在注释、docstring、常量元组
（`PLAN_DOC_REVIEW_NAME_MARKERS` 里就有一个 `"decision"`）里到处都是 → 字面量扫会**假红**；
而 `dict(decision=…)` / `payload["decision"] = …` / 常量拼接又会让它**假绿**。

---

## 5. 防死循环必须 RVF 自带

| host | 自动续跑上限 | `stop_hook_active` |
| --- | --- | --- |
| Claude | — | ✅ 真实 |
| Codex | ❌ **无** | ✅ 真实 |
| Cursor | 5（**从 Claude 导入的 hook = `null` / 无限**） | ➖ `loop_count` |
| Kimi | 每 turn 1 次 | ❌ **恒 false** |
| Oh My Pi | 8 | ✅ |
| DeepSeek | ❌ **无**（源码 TODO） | ❌ **恒 false** |

`evaluate_stop_event` 里那道 `stop_hook_active` 早退守卫（排在 handoff 收尾之后、一切发起闸之前）在 Kimi / DeepSeek 上**永不触发**。
现有 nudge 预算（`CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET`，默认 2）是**唯一保险丝**
——动它之前先想清楚这一点。

---

## 6. 宿主侧缺口：hook **根本没跑起来**时，契约在下游

本文前五节管的都是「RVF 的 core 跑起来了，往哪个出口写什么」。有一整族失败**在这之前**：
hook 命令起不来。此时 RVF 的一切自我保护——受众翻译层、`is_foreign_invocation`、
nudge 预算——**全部在失败的下游，一行都跑不到**。

### 危险条件精确到一种

| 失败形态 | Codex 0.147.0 | Claude Code 2.1.235 |
| --- | --- | --- |
| **退出码 2 + stderr 非空** | ✅ **驱动 agent**：stderr 逐字成为 `<hook_prompt>` 续跑提示 | ✅ **驱动 agent**：`Exit code 2 - show stderr to model and continue conversation` |
| 退出码 2 + stderr 为空 | ❌ 落到错误模板 | ❌ |
| 其它非零退出码（含 `python3` 缺失的 127） | ❌ | ❌ `Other exit codes - show stderr to user only` |
| 超时 / 非法 JSON / 退出码 0 带 stderr | ❌ | ❌ |

而 `python3 <不存在的脚本>` 的退出码**正好是 2**，stderr 正好是
`[Errno 2] No such file or directory` ⇒ 命中唯一那格。

`<hook_prompt>` 不是「失败通道」，它就是 **Stop 的续跑通道本身**——RVF 正常的决策点横幅
走的也是它。投递到模型侧后两者不可区分。

**没有任何上界。** 实测 286 次注入、同一个 `turn_id`、持续 2h37m，最后由人工 kill；
Codex 二进制里不存在任何失败计数器标识符，也**没有**把 hook 失败降级为 developer-only 的
配置开关（`[hooks.state]` 只有 `enabled` 与 `trusted_hash` 两个键）。

### 所以守卫只能活在命令串里

命令串是宿主在**会话启动时**快照的一部分，磁盘被掏空也照样存在。
`core/hook_command_spawn_guard_against_missing_target_script.py` 把每条 hook 命令包成
「目标脚本不在就静默 `exit 0` + 往 `~/.rvf/user-local-data/rvf-hook-target-missing.log`
留一行 developer-only 面包屑」。这不是新契约，是把 §0 的铁律贯彻到 core 够不着的那一段。

隔离 `CODEX_HOME` 真实会话对照（同一份 payload、同一条悬空路径）：

| 命令形态 | `<hook_prompt>` 次数 | 收尾 |
| --- | --- | --- |
| `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop.py"`（改造前） | **32 次 / 100 秒**，不自停 | 被硬超时 kill |
| spawn 守卫（改造后） | **0** | 14.2 秒 exit 0，面包屑 1 行 |

**改任何写 hook 命令串的代码时（`hooks/hooks.json`、`install_to_codex.py` 的烘焙点），
都必须经由该守卫**——`tests/test_hook_command_spawn_guard.py` 有结构性 gate。

### 带外决策点的正文只能经 UserPromptSubmit 送达

带外投递（cline-kanban 终端粘贴）时，Stop 只把一行**无指令**的边界标记交给粘贴通道，决策正文
写进 token payload，由 UPS 在观测到标记时以 `additionalContext` 注入（为什么不粘贴正文见
`_render_decision_point_out_of_band_boundary_marker`）。于是 **UPS 本身成了一个失败面**：UPS
没跑、超时、崩溃、token 里没有正文、或标记到达时 token 已过 TTL，agent 都只看到一行「未随附即
忽略」的标记，这次征询空转一回合。这一族在 Stop 侧都长成「标记在 transcript 祖先链上、token
仍是 minted」，与「征询确实到达过、只是记账没跟上」形状相同。

区分靠 UPS 自己留证据：首次观测到标记时，把结局（正文注入了没有、没注入的原因）写进该 token 的
投递意图 `user_prompt_submit_first_arrival_observation`。没有这份记录本身就意味着 UPS 没处理这条标记
——一个已证实的成因是 agent 回合中途以 `queued_command` 送达的消息在 Claude Code < 2.1.260 上**不触发**
UPS（本机 45 天 transcript：≤2.1.257 约 800 条仅 1 条触发，≥2.1.260 全部触发），而 transcript 扫描器
必须把 `queued_command` 附件的 `attachment.prompt` 当作一次 user prompt 落地。

但「标记在祖先链上」还要再分一刀：**标记是在 agent 空闲时开出了自己的回合，还是在回合中途并入的？**
后者说明 Stop 铸出 token 之后 agent 已经**复工**（后台任务通知唤醒、开发者发话、别的 Stop hook 让它续跑……），
这份决策点承载的候选范围（铸 token 那一刻的工作树）与时机（agent 刚停下）都已过时（task 8d3ac 的设计修正）。
判据见 `decision_point_agent_resumed_work_after_token_mint_evidence`，三个证据源任一判复工即复工，全部只看
与 token 同一会话的证据：

- **UPS 按会话的到达记录**：铸 token 之后到达过任何不是这枚 token 自身标记的 prompt（到达时已作废的迟到
  标记除外）。记录写在 UPS 的合成回合短路**之前**——任务通知正是最主要的复工信号。
- **transcript 祖先链**：标记以 `queued_command` 并入；或标记之前（标记还没落盘时看 leaf 之前）最近一条
  user / assistant 条目晚于铸出时刻。回扫时透明跳过开发者本地斜杠命令整组（caveat / `<command-name>` /
  `<local-command-stdout|stderr>`）与 compact 摘要——空闲时跑 `/compact`、`/model` 不是复工；isMeta 不整体
  透明（`Stop hook feedback:` 就是复工）。先撞到 `compact_boundary`：auto 且晚于铸出 ⇒ 复工，manual 或 auto
  不晚于铸出 ⇒ 无证据（不沿 `logicalParentUuid` 回溯）。
- **host 回合标识**（`HostAdapter.agent_turn_identifier_shared_by_hook_events_of_one_turn`：Claude Code
  `prompt_id`、Codex `turn_id`）：当前 hook 事件与铸 token 的 Stop 同属一个回合 ⇒ 那个回合被续跑了。

2026-10-02 真机取证（Claude Code 2.1.287）：排队消息的 UPS 在**并入上下文那一刻**才触发、触发时 transcript
里还没有 `queued_command`；同一回合（含 Stop 续跑与并入的排队消息）的 hook 事件共享 `prompt_id`；对排队消息
返回 `decision:block` 有效（`dropped_by_hook`）。

处置分三格：

- **复工**：撤回 token（CAS：锁内仍须是 `minted`；成因 `agent_resumed_work_before_decision_point_delivery_landed`，
  **冲销**熔断额度——通道健康、只是时机不对），清投递意图、best-effort 物理取消，**不注入正文**。Stop 侧
  **不早退**，同一次 Stop 按当前工作树重新评估（撤回删了去重索引、清了意图，在途闸随即放行）；标记若在
  UPS 侧到达，走下面的作废出口，重新评估发生在这一回合结束时的 Stop。Stop 的递归短路分支（续跑）也做这一步
  （只撤回、不重新评估）：Codex 的续跑不触发 UPS，那里是唯一看得到它的地方。
  **空转上界**：本会话最近连续 `CONSECUTIVE_AGENT_RESUMED_WORK_WITHDRAWALS_BEFORE_FALLING_BACK_TO_STOP_HOOK_SELF_INJECTION`
  （`EXPERIMENT_NODE`，现取 3）份带外横幅都因复工撤回 ⇒ 下一份改走同步自注入（当场送达、没有粘贴竞态），
  自注入铸出的 token 自然打断连续计数。不用「静默期」：静默期里的最后一次 Stop 之后 agent 进入空闲，就再
  没有 Stop 来征询（漏审方向）。
- **空闲落地、正文没注入、token 仍有效**（审计 M13 的保留支，须拿到「标记开了自己的回合」的正向证据）：下一次
  Stop **立即**以同一枚 token 自注入完整正文（`agent_must_act_now`，这是决策点本身、够格驱动 agent），不等在途
  意图 TTL、不撤回、不冲销也不新增熔断额度。推进用锁内首次迁移的 CAS（UPS 可能刚把正文注入、还没写首次到达
  记录），抢不到即静默停。
- **token 已过期**（审计 M9）：意图过 TTL 后由搁浅对账撤回。只有记录里写着「正文已注入」时才用冲销
  熔断额度的成因；否则用 `out_of_band_boundary_marker_became_a_turn_but_banner_body_never_injected`
  计入熔断（UPS 持续失效时自动退避），并以 **developer_only** 点名 UPS 那一侧的原因。这条告警与 §0
  一致：它是诊断，不驱动 agent。

三格都拿不到证据（判据「无证据」）时**维持现状**：不撤回、不补发，交给在途闸与搁浅对账。理由：Codex rollout
没有 uuid 链，若「无证据 ⇒ 撤回」，UPS 失效的 Codex 会话会无限「铸 → 撤」。

**作废标记**（token 已撤回 / 已决策，或到达时判定复工）落地时，UPS 走
`_apply_void_decision_point_boundary_marker_hard_neutralization`：`decision:block`（挡掉这次提交，空闲时
不白开一个回合）+ 只说「已作废、无需处理」的 `additionalContext`（**不含决策正文**）+ `systemMessage`。
它是 prompt 抑制，不驱动 agent，在契约测试的豁免表里带理由登记。

---

## 7. 已知残留

- Codex 上 agent-directive 仍走 `decision:block`，harness 照旧把它渲染成错误。这是 Codex 侧
  `additionalProperties:false` 的硬约束，不是 RVF 的选择。
- guardian liveness 告警走 `systemMessage`，而 `systemMessage` 在 Codex `exec` 实测不落地
  ⇒ 该告警在 Codex 上目前是哑的。
- 两条通道的**服从度**只做过 n=1、极简指令的对照实验（无可观察差异）。多步指令在真实长
  上下文下是否仍等价，须上线后留指标观察。
- 带外决策点「复工撤回」不在同一次 Stop 里重新评估的两处（§6）：标记在 UPS 侧被判复工、且那个回合以
  Stop 续跑结束（续跑 Stop 走递归短路）；Stop 的递归短路分支自己撤回。两处的重新征询都要等下一次非续跑的
  Stop——若 agent 此后一直空闲，候选会一直没人问。
- 复工判据「无证据 ⇒ 维持现状」：Codex 上 UPS 事件是否带 `turn_id`、回合中途 steer 进来的消息是否沿用
  当前 `turn_id` 未取证；Kimi 没有回合标识。这两个 host 上复工撤回只靠 UPS 到达记录。
- 作废出口的 `decision:block` 只在 Claude Code 上取证过对排队消息生效；不认 block 的 host 上作废标记
  仍会开一个回合（agent 只看到「已作废、无需处理」）。
- 根治在投递侧：粘贴前确认 agent 空闲。cline-kanban 的 prompt queue（task 24508，已合入其本地 main）带
  这道门控，但缺程序化入队入口，且把「parked 等后台工作」算作空闲——需求见
  `~/.rvf/cross-repo-coordination-with-cline-kanban/`（挂 task 33612）。落地后 RVF 改走排队入口，复工撤回
  与空转上界仍保留（出队瞬间的唤醒、忙闲误判、Codex 续跑都会漏过排队）。
