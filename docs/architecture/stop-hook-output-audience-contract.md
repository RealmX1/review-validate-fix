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
正文根本不在 stdout 里，它几秒后作为一条 user message 到达并自己开一个新回合。若给它安一个
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
| `decision:block` | ✅ | ✅ | ✅ 仅交互 | ✅ | ✅ | ✅ |
| **Stop 上 `additionalContext`** | ✅ | ❌ `additionalProperties:false`（发过去整条 hook 输出 Failed） | ❌ | ❌ | ✅ | ❌ |
| `systemMessage` | ✅ | ⚠️ schema 有字段但 `codex exec` 实测不落地 | ❌ | ❌ | ✅ | ❌ |
| **UPS 上 `additionalContext`** | ✅ | ✅ | ✅ | ⚠️ exit-0 stdout | ✅ | ✅ |

**跨宿主适配负担集中在 Stop**，UPS 侧 additionalContext 基本全家都支持。

**Codex 的 `additionalProperties:false` 是硬约束**：给 Codex 发 `hookSpecificOutput` 会让整条
hook 输出被判 Failed = RVF 在 Codex 上静默停摆。所以 host 未知时**必须**兜底到最保守的
`decision:block`，绝不赌未知 host 认识 `hookSpecificOutput`。

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

`evaluate_stop_event` 开头那道 `stop_hook_active` 早退守卫在 Kimi / DeepSeek 上**永不触发**。
现有 nudge 预算（`CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_NUDGE_BUDGET`，默认 2）是**唯一保险丝**
——动它之前先想清楚这一点。

---

## 6. 已知残留

- Codex 上 agent-directive 仍走 `decision:block`，harness 照旧把它渲染成错误。这是 Codex 侧
  `additionalProperties:false` 的硬约束，不是 RVF 的选择。
- guardian liveness 告警走 `systemMessage`，而 `systemMessage` 在 Codex `exec` 实测不落地
  ⇒ 该告警在 Codex 上目前是哑的。
- 两条通道的**服从度**只做过 n=1、极简指令的对照实验（无可观察差异）。多步指令在真实长
  上下文下是否仍等价，须上线后留指标观察。
