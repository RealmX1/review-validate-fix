# adapters/kimi/

Kimi Code CLI 作为 RVF 第三个 host 的 adapter bundle。

取证依据：`docs/architecture/kimi-code-cli-plugin-and-hook-runtime-forensics.md`
（kimi **0.29.1**，二进制内嵌源码 + 本机 42 份 `wire.jsonl` / 23 个 session 目录）。

## 当前状态（S-B0…S-B4 已落地）

| 文件 | 状态 |
|---|---|
| `session_layout.py` | ✅ 会话磁盘布局解析。`KIMI_CODE_HOME` 生产 override、`session_` 前缀归一、`session_index.jsonl` 快路径 + 扫桶回落 |
| `transcript.py` | ✅ 首条用户输入抽取 + **完整蒸馏 `distill_kimi_wire_jsonl`**（与另两家 distiller 同签名同返回类型） |
| `kimi_code_cli_host_adapter.py` | ✅ 6 个 `HostAdapter` Protocol 方法全实装 |
| `subagent.py` | ✅ 观测侧 `resolve_subagents`（读 `state.json` 名册）+ `read_kimi_originator` |

**整条识别→装配→出口链路已接通**：

```
wire.jsonl 首行 metadata
  → detect_transcript_format() → HOST_KIMI_CODE
  → resolve_current_session_host_adapter() （schema 判据排在 source.provider stamp 之前）
  → resolve_host_adapter() → KimiCodeCliHostAdapter
  → stop_hook_agent_directive_channel() → hook_specific_output_permission_decision_deny
  → render_stop_hook_payload() → {"hookSpecificOutput": {"permissionDecision": "deny", …}}
```

⚠️ **判据顺序里那句「schema 排在 stamp 之前」不是风格偏好。** 2026-08-23 实测：不加那条分支
时，一个带 `claude-code` stamp 的 Kimi 会话会被判成 Claude ⇒ 拿到 `additionalContext` ⇒
Kimi 静默 allow ⇒ RVF 完全失声、零报错。回归保护见
`tests/test_current_session_host_adapter_resolution.py` 的 HOST-5 / HOST-6。

## 这个 host 与另外两个最不一样的地方

**失败是静默的。** Codex 收到不认识的键会让整条 hook 输出 Failed（响亮）；Kimi 的 hook 输出
schema 是 `looseObject({message, hookSpecificOutput})`，且 block 判据逐字是
`hookSpecificOutput?.permissionDecision !== "deny"` ⇒ 发 `decision:block` 或 `additionalContext`
都会被**静默吞掉并 allow**，零报错。

三条直接后果，写代码时必须一直记着：

1. **「host 未知 → 兜底 Codex」这条既有纪律对 Kimi 不提供任何保护**，Kimi 必须被**正向识别**。
2. **shim 退出码必须恒 0**。Kimi 的 `resultFromExitCode` 只认 `0`（解析 stdout）与 `2`（block，
   reason 取 stderr）；**其他任意退出码都会让 stdout 被完全丢弃并 allow**。崩溃、超时、非法
   JSON、spawn 失败——一律得 exit 0。
3. **`permissionDecision` 必须是字面量 `"deny"`**（JS 全等比较），拼错即静默降级。

## 蒸馏的三个「静默出错」点（都已由 gate 钉住）

1. **`message.origin.kind` 区分真实输入与系统注入。** `context.append_message` 的 `role`
   实测**全是** `user`，但 `origin.kind` 分 `user`（真实）与 `injection`（harness 注入的
   `permission_mode` / `todo_list_reminder` 等系统提醒，实测比例 1:1~1:3）。混为一谈会把系统
   提醒当成用户发言，而 RVF 的 scope 归属正建立在「用户说了什么」上。injection 映射成
   `phase_marker`（`marker="injection:<variant>"`），不污染 message 语义也不丢信息。
2. **`think` 的正文在 `part.think`，不在 `part.text`**（实测 think 占 `content.part` 的 67%）。
3. **写型工具的参数名是 `path`，不是 Claude 的 `file_path`**——照抄会让 `artifact_refs` 恒为空，
   而且**不报错**（缺键只是取不到值）。

`turn.prompt` 与 `origin.kind=="user"` 的 `append_message` 是同一条输入的两次记录，只认后者。

**全量真实数据对账（42 份 wire.jsonl，0 失败）**：未抽取的 903 条 `context.append_loop_event`
= `step.begin`(404) + `step.end`(398) + 正文为空的 `content.part`(101)，逐项闭合；
`tool_call` 与 `tool_result` 603:603 完美配对。

## 子代理的发现路径与另两家完全不同

Codex 从主 rollout 扫 spawn 事件、Claude 从 `<uuid>/subagents/` glob 文件；**Kimi 把整份
agent 名册写在 `state.json` 里**：`{"agents": {"main": {...}, "agent-N": {"type":"sub",
"parentAgentId":"main", "homedir":"…", "swarmItem":"<派生 prompt>"}}}`。

实测（本机唯一一个多 agent 会话，20 个 agent）：`homedir` **恒等于**
`<session_dir>/agents/<agent_id>`，20/20 都有 `wire.jsonl`；发现出 19 个子代理，
19/19 有 transcript，16/19 有 `swarmItem`（另 3 个是普通 `Agent` 派生的，本就不带）。

**三个字段刻意留 `None` 而不硬凑**：`call_id`（名册不记「哪次工具调用派生了它」）、
`role`（`type` 只有 main/sub，是**层级**不是**语义角色**）、`ts`（名册无 per-agent 时间戳）。
消费者无法分辨「真的没有」与「编出来的」。

**跳过主 agent 用双判据**（`type=="main"` 或 `parentAgentId` 为空）：实测两者一致，但任一
字段缺失时另一个仍挡得住。把主 agent 当成自己的子代理会造成蒸馏自嵌套。

## hook 入口：shim 比 Claude 的厚，因为事件太稀疏

Kimi 传给 hook 的 Stop 事件**只有四个字段**（实测自二进制：`triggerInner` 构造
`{hookEventName, sessionId, cwd, ...args.inputData}`，经 `toHookInputData` 的 `camelToSnake`
转成 snake_case；Stop 的 `args.inputData` 只有 `stopHookActive`，而其触发分支的前置条件是
`if (!stopHookContinuationUsed)` ⇒ 该值**恒 false**）：

```json
{"hook_event_name":"Stop","session_id":"session_<uuid>","cwd":"…","stop_hook_active":false}
```

`_kimi_hook_entry.py` 做三件归一，每件缺了都有独立后果：

| 归一 | 来源 | 缺了会怎样 |
|---|---|---|
| `transcript_path` | 会话名册定位 `agents/main/wire.jsonl` | **没有这个字段**，而 `detect_transcript_format` 靠它判 host ⇒ 回落 Codex ⇒ 发 `decision:block` ⇒ 静默失声 |
| `cwd` | `state.json.workDir` | Kimi 的 `cwd` 可能是空串（`this.options.cwd ?? ""`）⇒ 认不出目标仓库 |
| `last_user_message` | `state.json.lastPrompt` | 触发字面量检测拿不到用户这一轮说了什么 |

**归属用正向 `owns_invocation`，不用排除式 foreign 守卫**：那道守卫存在是因为 Codex 会双加载
plugin-packaged `hooks.json`；Kimi 走自己的 manifest，不存在同类双加载，给不存在的故障加守卫
只会增加误判面。

**退出码恒 0** 是硬要求：Kimi 的 `resultFromExitCode` 只认 `0`（解析 stdout）与 `2`（block，
reason 取 stderr），**其他任意码都让 stdout 被完全丢弃并 allow 且无报错**。

⚠️ manifest 有**两个**合法位置，`kimi.plugin.json`（plugin root 下）**优先于**
`.kimi-plugin/plugin.json`——前者若出现会静默遮蔽后者。
⚠️ `HookDefSchema` 是 `.strict()`，字段恰为 `{event, matcher?, command, timeout?}`
（timeout ∈ [1,600]）；**多写一个字段 ⇒ 该 hook 被静默丢弃而 `state` 仍报 `ok`**。
默认超时只有 30 秒，所以 manifest 显式声明了 115 / 85。

⚠️ **shim 内的布局解析是 `session_layout.py` 的副本**（hook 不能 import `adapters`），
`tests/test_kimi_hook_entry_normalization.py` 的 ENTRY-6 断言两处等价——改一处必须改另一处。

## 已知留白（**不留占位代码**，按切片补）

- **`build_analyze_command`（调用侧）** —— 观测侧已实装；构造 headless analyze 调用向量尚未做。
- ~~**防死循环预算**（S-B6）~~ ✅ **已做**（2026-08-24）——`core/stop_hook_reentry_budget/`，
  挂在 `emit_stop_hook_payload` 的受众翻译之前。Kimi 的 `stop_hook_active` **恒 false**、引擎的
  `stopHookContinuationUsed` 每 turn 复位 ⇒ 跨 turn 没有保险丝，所以这道闸只能由 RVF 自带。
- ~~**`parent_harness_identity` 词表**（S-B7）~~ ✅ **已做**——三个信号层收敛到 `kimi-code`。
- **分发**（S-B8）—— ✅ **已部署**（2026-08-24）。安装链路三件都已补上：
  `install_to_codex.py` 的 `configure_kimi_plugin_registration()` +
  `--configure-kimi-plugin-registration`、`rvf_local_deploy.py` 的 `installer_command`
  已带该 flag、`sync-manifest.sh` 已四路化（含「Kimi hook 事件必须同时有 Stop 与
  UserPromptSubmit」的正向断言）；激活态核对早已有
  （`scripts/kimi_plugin_manifest_activation_state_check.py`）。
  部署实据：`installed.json` 落盘、hook 到位。首次真机使用暴露了三条缺陷，已于 2026-08-26
  修复，但该批修复**尚未 land、尚未重新部署**，见下方「首次真机使用暴露的三条缺陷」。
- **注册只能半自动**（P-1 定案，2026-08-26 按 Kimi **0.38.0** 修订）：`installPlugin` 在
  CLI / WebSocket / ACP 三条跨进程通道上**仍不可达**（0.38.0 `kimi --help` 的 Commands 里
  确实没有 `plugin`）；**REST 自 0.38.0 起可达**——`packages/kap-server/src/routes/plugins.ts`
  注册了 `GET/POST /plugins`（`installPluginRequestSchema = object({source: string().min(1)})`）
  以及 enable / disable / remove action。但它要求先起 `kimi web` 服务器、再从 stdout 抓 bearer
  token，对非交互部署器比直接写 `installed.json` 更脆 ⇒ **它的价值在「终验」而不是「注册」**：
  注册仍是安装器写 `installed.json`、再由用户跑激活命令；终验才走
  `GET /api/v1/plugins`（本轮端到端终验用的正是它）。取证与实测命令见
  `docs/architecture/kimi-code-cli-plugin-and-hook-runtime-forensics.md` §11.4-bis。
  且 `installed.json` **不持久化 `state`**（`persist()` 只写 9 个字段）⇒ 部署校验必须自己复算，
  不能读该字段。
- **Stop 上的 developer-only 出口在 Kimi 上完全不可见**：Kimi 丢弃 Stop 的 allow-stdout，
  也没有 `systemMessage` 等价物 ⇒ **RVF 自己的 ledger 是 Kimi 上唯一的开发者可见面**。
  ⚠️ **这条不能推广到 UPS** —— 两个事件的可见性是不对称的，见下节。


## Stop 与 UPS 的出口不对称（2026-08-26 取证，0.38.0）

两个事件在 Kimi 上的 hook 输出消费路径**完全不同**，照抄另一个必错：

| | Stop | UserPromptSubmit |
|---|---|---|
| 消费什么 | 只读 block 判据（`triggerBlock`），**allow-stdout 整段丢弃** | 读顶层 `message` ?? `hookSpecificOutput.message`，**两者皆缺则回落整段 stdout** |
| 驱动 agent 的线材 | `hookSpecificOutput.permissionDecision:"deny"` | 同左（block）／顶层 `message`（非 block 注入） |
| 注入形态 | `appendUserMessage(kind:"system_trigger", name:"stop_hook")` | `appendUserMessage(kind:"hook_result")` |
| TUI 是否渲染 | **否**（不发 `hook.result` 事件） | **是**（`emitEvent type:"hook.result"` → `*UserPromptSubmit hook*` 块） |
| developer-only 的正确渲染 | 照发 `systemMessage`（被静默丢弃，无害） | **完全不输出**（否则会随回落逐字进模型上下文） |

⇒ Stop 侧 core 已由 `render_stop_hook_payload` 渲染好 Kimi 线材，shim 逐字透传即可；
**UPS 侧 core 是 host-blind 的**（发 Claude 的 `hookSpecificOutput.additionalContext`），
必须由 shim 在出口翻译：

| core 发的 | Kimi 线材 |
|---|---|
| `decision:"block"` + `reason` | `hookSpecificOutput.{permissionDecision:"deny", permissionDecisionReason}` |
| `hookSpecificOutput.additionalContext` | 顶层 `message` |
| 只有 `systemMessage` / `continue` | **不输出** |

收口在 `hooks/_kimi_hook_entry.py` 的 `translate_core_payload_to_kimi_user_prompt_submit_wire()`
与 `emit_developer_only_notice()`；回归护栏 `tests/test_kimi_hook_entry_normalization.py` **ENTRY-7**。

**为什么没上提成 `HostAdapter` 方法**：UPS 的 `additionalContext` 是 Claude / Codex / Cursor /
Oh My Pi / DeepSeek **五家通吃**的，Kimi 是唯一例外；为一个 host 的差异去改 host-blind 的 UPS
热路径（要在里面付 transcript 探测的开销）是过度设计。将来若出现第二个线材不同的 host，
再把这层上提为 `HostAdapter.user_prompt_submit_hook_payload_channel()`。

🔴 **旧行为已在生产里出过事**：2026-08-25T21:28:57Z 的一次真实会话里，一条 developer-only
诊断连 JSON 引号带 `\n` 转义一起被注入模型上下文。逐字 wire 记录见取证文档 §12.4。


## 身份词表：`kimi_code` 与 `kimi_code_cli` 的分工（别改错）

RVF 里与 Kimi 有关的名字有**三套**，取值刻意不同，各有各的消费者：

| 用途 | 取值 | 谁消费 |
|---|---|---|
| **host 身份** | `kimi_code`（`HOST_KIMI_CODE`） | `detect_transcript_format` → `resolve_host_adapter`；**同时被 `prepare_review_run` 当作 `main_harness` 拿去和 reviewer-registry 比对** |
| **marker token** | `kimi-code`（`PARENT_HARNESS_IDENTITY_KIMI_CODE`） | round-baseline / review-highwater marker 的文件名 bucket |
| **模块与类名** | `kimi_code_cli_host_adapter.py` / `KimiCodeCliHostAdapter` | 只是产品「Kimi Code CLI」的名字，**不参与任何词表匹配** |

**host 身份那一行必须与 `config/reviewer-registry.json` 的 harness id 逐字相同。** 它曾经是
`kimi_code_cli`，与 registry 的 `kimi_code` 对不上 ⇒ 主 harness 排不出 alternative reviewer 池
⇒ 200-seed 实测 **44%** 的 Kimi 会话被 Kimi 自己评审，**全程零报错**（双评审的价值全在异构性上）。
`codex` / `claude_code` 两家历史上恰好同名纯属巧合，所以这条缺陷一直没暴露。

现在有 `scripts/check_host_identity_and_reviewer_registry_harness_id_vocabulary_consistency.py`
钉住它，判据是遍历 `KNOWN_HOST_IDENTITIES` 的结构性断言 ⇒ 加第四个 host 时同样当场炸。

## 6 维契约现状

| 维度 | 现状 |
|---|---|
| hook entry | ✅ `.kimi-plugin/plugin.json` 注册 **Stop + UserPromptSubmit** 两入口；共享契约 `hooks/_kimi_hook_entry.py`。**绝不加进** Claude 的 `hooks/hooks.json`（Codex plugin loader 会一并加载 ⇒ 三重触发），有 `forbid_repo_literal` 护栏 |
| subagent | ✅ 观测侧已实装并接进 `subagent_capture` 三处派发；调用侧 `build_analyze_command` 仍是留白 |
| transcript | ✅ 完整蒸馏已实装（42/42 份真实 wire 验证，0 失败） |
| permission | `-p` 隐含 `forceAuto`；**不得**再加 `-y/--yolo`（与 `-p` 互斥报错） |
| config | ✅ `.kimi-plugin/plugin.json` |
| discovery | ⚠️ 半自动：`$KIMI_CODE_HOME/plugins/installed.json` + `/plugins reload` **再** `/reload`（无目录扫描、无 CLI 入口；两条命令缺一不可，见下节） |

## 首次真机使用暴露的三条缺陷（2026-08-26 修复；取证自 Kimi 0.38.0 二进制）

⚠️ **本目录早期的取证文档锚在 Kimi 0.29.1，本机已是 0.38.0。** 三条里有一条正是 0.38.0 新增的
出站校验才让它显形——所以「当时能用」不代表现在能用，版本变更后要重跑激活态核对。

### 1. `installed.json` 的 `source` 是**枚举**，不是路径

`resolveInstallSource()` 返回 `{kind: "local-path"|"zip-url"|"github", path}`，`recordFrom()` 落的是
那个 **kind**（`source: input.source ?? "local-path"`），路径归 `originalSource`。安装器原先把绝对
路径写进了 `source`，又显式写了 `"github": null`（zod 的 `.optional()` 接受**缺省**、不接受 null）。

0.38.0 起 `pluginService.listPlugins` 有**出站** schema 校验
（`source: enum([...])` / `github: <object>.optional()`）⇒ `/plugins` 面板整个报
`output validation failed` 打不开。

**这条故障是偏斜的**：hook 派发走 `enabledHooks()` → `materialize()` → `recordFrom()`，那条路径对
`source` 不做任何枚举校验（原样透传）⇒ **Stop / UserPromptSubmit 仍在正常触发，只有 UI 全挂**。
按「插件没装上」去排查会全程走错方向。

### 2. Kimi **只**从 `manifest.skills` 发现 skill，不扫 `skills/`

`countDiscoveredPluginSkills()` 逐字是 `(manifest?.skills ?? []).map(...)`，`roots.length === 0`
时直接 `return 0`。`.kimi-plugin/plugin.json` 原先只声明了 `hooks` ⇒ **6 个 RVF skill 在 Kimi 上
全部不存在**，而 plugin `state` 仍是 `ok`、hook 照常触发、面板显示 `Skills (0)`。

对照差异值得记住：**`agents` 在字段缺省时会自动收 `agents/` 目录，`skills` 没有这个待遇**；
skills 的唯一 fallback 是 plugin 根上放一份 `SKILL.md`（`rootSkillFallback`），RVF 没有。

⚠️ 声明写错比不写更危险：`resolveDirListField` 对「不以 `./` 开头」「解析到插件之外」的处置是
`severity:"error"` ⇒ 整条 record 的 `state` 翻成 `error` ⇒ **连 hook 一起停摆**。

### 3. 激活需要**两条**命令，`/plugins reload` 一条不够

两条收敛的是两层不同的缓存：

| 命令 | 收敛什么 |
|---|---|
| `/plugins reload` | 把 `installed.json` 重新 materialize 进**进程级** `PluginManager` |
| `/reload` 或 `/new` | 重建**会话**——skill 是会话级的，`this.skillsReady = this.loadSkills()` 只在 session 构造时跑一次，已有会话不会重扫 |

Kimi 自己的 TUI 常量逐字就是这个意思：
`PLUGIN_RELOAD_HINT = "Run /new or /reload to apply plugin changes."`
只提示前者，用户会看到「插件在册、Skills 却是空的」而无从下手。

> 另：`/plugins` 面板的 **Custom 页不是「已装列表」**，而是「装自定义插件」的输入框。
> 手写进 `installed.json` 的本地插件只会出现在 **Installed** 页。

### 这三条为什么能一起活下来（结构性教训）

`kimi_plugin_manifest_activation_state_check.py` 当时对这个坏掉的注册判的是
**`sole_registration` / exit 0**——一个完整的假绿。成因是它只复算了「hook 条目级」这一层，
而三条缺陷分别落在**注册记录层**（1）、**manifest 声明层**（2）、**会话生命周期层**（3）。

同样地，闭环测试 `test_installer_output_is_independently_verifiable_as_actually_active` 用的是
**手抄的 manifest fixture**，而不是仓库里真实发布的那份 ⇒ 发布物漏声明 `skills`，fixture 也漏，
测试对着「与发布物不同的东西」判了绿。现已改为直接读发布物。

三条修复对应三道新闸：核对器的 `registry_record_rejected_by_host_schema` 新态与 skills 复算、
`sync-manifest.sh` 的「三份 host manifest 都必须声明 skills 且一致」、以及 fixture 改读发布物。
