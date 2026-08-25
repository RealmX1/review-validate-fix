# Kimi Code CLI 插件注册与 hook 运行时取证（S-B0）

> ⚠️ **版本已漂移：本文正文锚在 0.29.1，本机现为 0.38.0。** 差异与新增取证见文末
> **§11「0.38.0 补充取证」**——其中一条（`listPlugins` 出站校验）是 0.38.0 才有的，
> 它让一条早已存在的写入缺陷在此时才显形。**正文与 §11 冲突时以 §11 为准。**
>
> 取证对象：`~/.kimi-code/bin/kimi`，**0.29.1**，Mach-O arm64 单体（158 MB，内嵌 JS bundle）。
> 全程只读：未写入 `~/.kimi-code` 任何文件，未起 `kimi web`，未注册任何插件。
> 取证方式：二进制内嵌源码字面量抽取 + `kanban` CLI 的只读 `--preview`。
>
> **本文是 S-B（Kimi host 适配）的实现依据。** 计划文档
> `~/.claude/plans/add-adaptation-to-kimi-detail-S-B-kimi-host.md` 的若干断言在此被
> 逐字坐实或推翻；**冲突时以本文为准**，因为本文的每条都附了可复算的取证手段。

---

## 1. 🔴 结论先行：插件注册没有任何跨进程自动化入口

主计划 §3 P-1 把「`installPlugin` 有 RPC 层 ⇒ `kimi acp` / `kimi web` / `kimi server`
**有可能**暴露它」列为「把手动升级成可自动化的最高价值线索，实施前必须先探这条路」。

**已探完，结论是否定的。** 四条候选通道逐一实测排除：

| 通道 | 取证手段 | 结果 |
|---|---|---|
| CLI 子命令 | `kimi --help` | 命令表为 `export / provider / acp / web / server / login / doctor / vis / migrate / upgrade`，**无 `plugin`** |
| `kimi web` REST | 二进制内 `/api/v1/…` 路由字面量全集 | `debug / files / fs / healthz / meta / sessions / shutdown / terminals / ws`，**无任何 plugin 路由** |
| `kimi web` WebSocket | `wsOperations = [...clientControlOperations, ...serverSystemOperations, sessionEventOperation]` 的 frame type 全集 | 17 种：`client_hello / subscribe / subscribe_v2 / unsubscribe_v2 / unsubscribe / watch_fs_add / watch_fs_remove / abort / terminal_attach / terminal_detach / terminal_input / terminal_resize / terminal_close / pong / server_hello / ping / resync_required`（+ server 侧 `error / session_event`）。**无任何 plugin 操作** |
| ACP (`kimi acp`) | `AcpServer` 类的 28 个方法 | 含 `plugin` 的方法数 = **0**。ACP 方法名全集是 `initialize / authenticate / session:{new,prompt,cancel,close,delete,fork,list,load,resume,set_config_option,set_mode,set_model,update,request_permission} / fs:{read_text_file,write_text_file} / terminal:{create,kill,output,release,wait_for_exit}` |

`installPlugin` 确实存在，但它是 **kernel 的进程内 API**：服务端 handler 与
`listWorkspaceSkills` / `listPluginCommands` / `createGoal` / `getCronTasks` 同族，
TUI 侧经 `this.rpc.installPlugin(source)` / `(await this.getRpc()).installPlugin({source})` 调用。
那个 `rpc` 是 TUI 与 kernel 的进程内桥，**没有绑定到任何对外 transport**。
`AcpServer` 虽有 `extMethod` / `extNotification`，但那是把扩展方法**转发给客户端**的反向通道，
不是暴露 kimi 自身的 plugin API。

⇒ **P-1 能力梯度的第一级（RPC/ACP 自动注册）不存在**，直接落到第二级：
**安装器写 `installed.json` + 提示 `/plugins reload`**。

**一条有利事实**：本机 `~/.kimi-code/plugins/` 目录**根本不存在**（从未装过任何 kimi 插件）
⇒ 首次注册是「创建」而非「改写」，P-1 所述的丢更新竞争窗口在首次部署时不存在。
但**第二次及以后仍然存在**（Kimi 全量重写 `installed.json.tmp`、无锁无 CAS），
所以「Kimi 未运行时写 + 写后独立复验」的要求不因此放宽。

---

## 2. hook 运行时语义（逐字坐实）

### 2.1 `PluginManager.enabledHooks()` —— cwd 与 env 由 Kimi 硬性决定

```js
enabledHooks() {
    const out = [];
    for (const record of this.records.values()) {
        if (!record.enabled || record.state !== "ok" || record.manifest === void 0) continue;
        for (const hook of record.manifest.hooks ?? []) out.push({
            ...hook,
            cwd: record.root,
            env: { KIMI_CODE_HOME: this.kimiHomeDir, KIMI_PLUGIN_ROOT: record.root }
        });
    }
    return out;
}
```

三条实现约束：

1. **`cwd` 恒等于 plugin root，hook 无法自选。** manifest 里写 `cwd` 也会被这里覆盖。
2. **只注入两个变量**：`KIMI_CODE_HOME`、`KIMI_PLUGIN_ROOT`。
3. **`record.state !== "ok"` 是过滤条件**，而 `state` 是内存派生值（见 §3）——
   手写 `installed.json` 后 state 由 `materialize()` 重新派生，**写不进去也读不出来**。

### 2.2 spawn 形态：`shell: true` + env 叠加（v1 / v2 都是）

v1（`agent-core/src/session/hooks/runner.ts`）：

```js
function buildHookSpawnOptions(options) {
    return {
        shell: true,
        cwd: options.cwd,
        stdio: "pipe",
        detached: process.platform !== "win32",
        windowsHide: true,
        env: options.env ? { ...process.env, ...options.env } : void 0
    };
}
```

v2（`agent-core-v2/src/agent/externalHooks/runner.ts`）：

```js
proc = await hostProcess.spawn(command, [], { shell: true, cwd: options.cwd, env: options.env });
```

⇒ **`shell: true` 两代都有** ⇒ `command` 里的 `$KIMI_PLUGIN_ROOT` 会被 shell 展开，
相对路径命令按 §2.1 的 cwd（= plugin root）解析。

⇒ **env 是叠加不是替换**：v1 逐字 `{...process.env, ...options.env}`；v2 侧
`SessionProcessRunner._buildExecEnv` 同样是 `{...process.env, ...invocationEnv}`。
`PATH` 因此保留。

> ⚠️ **未逐字坐实的一处**：v2 hook 路径注入的 `hostProcess`（`IHostProcessService`）
> 是否就是 `SessionProcessRunner`，本次取证没有追到底。若它是另一个不做叠加的实现，
> v2 引擎下的 hook 子进程会缺 `PATH`。**S-B5 必须实跑一次 v2 引擎
> （`KIMI_CODE_EXPERIMENTAL_FLAG`）确认**，不得凭本节推断放行。

### 2.3 退出码与 block 判据（v1 / v2 逐字一致）

```js
function resultFromExitCode(exitCode, stdout, stderr) {
    if (exitCode === 2) {
        const message = stderr.trim();
        return { action: "block", message, reason: message, stdout, stderr, exitCode };
    }
    const structured = exitCode === 0 ? structuredOutput(stdout) : void 0;
    if (structured?.action === "block") return { action: "block", ... };
    return allowResult({ ... });
}
```

| exit code | 行为 |
|---|---|
| `0` | 解析 stdout 的结构化输出；`hookSpecificOutput.permissionDecision === "deny"` 才 block |
| `2` | block，**reason 取 stderr** |
| **其他任意值** | `structured` 恒为 `undefined` ⇒ **stdout 被完全丢弃、allow、无任何报错** |

⇒ 计划 §1.3「Stop hook 出口在所有 host 上恒 exit 0」与「exit-2 通道刻意不用」的两条论据成立。
⇒ 「shim 退出码恒 0（含崩溃/超时/非法 JSON/spawn 失败）」是硬要求：任何非 0 非 2 的退出码
都会让 RVF 在 Kimi 上**完全失声且零报错**。

block 判据逐字是 `if (hookSpecificOutput?.permissionDecision !== "deny") return result;`
⇒ **全等比较**，`permissionDecision` 必须是字面量 `"deny"`，拼错即静默降级 allow。

> **v1/v2 的一处差异**：v2 对 reason 多一层类型守卫
> `typeof hookSpecificOutput.permissionDecisionReason === "string" ? … : void 0`，
> v1 直接取。我们总是发字符串，两代等价；但若将来改成发结构化 reason，v2 会静默丢掉它。

---

## 3. `installed.json` 的持久化字段（R-2 CONFIRMED）

```js
async persist() {
    const installed = [...this.records.values()].map((record) => ({
        id, root, source, enabled, installedAt, updatedAt, originalSource, capabilities, github
    }));
    await writeInstalled(...);
}
```

**恰好 9 个字段，不含 `state` / `manifest` / `diagnostics`。**
⇒ 部署校验**不能**读 `installed.json` 断言 `state == "ok"`——该字段在磁盘上不存在。
必须由 RVF 侧自己复算（解析 manifest + 复刻 `readHooks()` 的 schema 校验）。

`readInstalled()` 对缺失文件返回 `EMPTY`（`ENOENT` → 空 registry），对非法 JSON 抛错。

---

## 4. manifest 有**两个**合法位置，且 root 版优先

```js
KIMI_PLUGIN_ROOT_PATH = "kimi.plugin.json";
KIMI_PLUGIN_DIR_PATH  = ".kimi-plugin/plugin.json";
```

`parseManifest()` 先查 `<pluginRoot>/kimi.plugin.json`，再查 `<pluginRoot>/.kimi-plugin/plugin.json`；
**两个都在时 root 版生效，dir 版被记为 `shadowedManifestPath`**。

⇒ 计划只写了 `.kimi-plugin/plugin.json`。选它没问题，但要知道：
若将来仓库根下出现 `kimi.plugin.json`，它会**静默遮蔽**我们的 manifest。
这是一条值得进结构性护栏的形状（`forbid_repo_literal` 或 required-files 反向断言）。

`UNSUPPORTED_RUNTIME_FIELDS = ["tools", "apps", "inject", "configFile", "config_file", "bootstrap"]`。

---

## 5. Kimi 的工具集（S-B0 取证项②）

`registerTool()` 注册的工具类共 17 个：
`AgentSwarmTool / AgentTool / AskUserQuestionTool / BashTool / EditTool / EnterPlanModeTool /
ExitPlanModeTool / GlobTool / GrepTool / ReadTool / SelectToolsTool / SkillTool / TaskListTool /
TaskOutputTool / TaskStopTool / TodoListTool / WriteTool`

**写型工具的 wire 名**：`Write`、`Edit`；另有 `Bash`（间接写）。
⇒ `artifact_refs` 的写操作归因按这三个名字识别。**没有 `MultiEdit` / `Patch` / `ApplyPatch`。**

---

## 6. cline-kanban 的 agent id（R-7 CONFIRMED，但 `--help` 陈旧）

`kanban` **0.1.68** 的 `task create --help` 逐字写着
`--agent-id <id>  Agent override: cline | claude | codex | droid | gemini | opencode | default`
——**不含 `kimi`、不含 `omp`**。

但这份文案是陈旧的。只读 `--preview` 实测（它明说 "Read-only: it does not even register the project"）：

| `--agent-id` | rc | `ok` |
|---|---|---|
| `kimi` | 0 | true |
| `omp` | 0 | true |
| `claude` | 0 | true |
| `definitely-not-an-agent-xyz`（阴性对照） | **1** | **false** |

阴性对照证明 `--preview` **确实校验** agent id ⇒ `kimi` 与 `omp` 是真被接受的。

⇒ **R-7 成立**：`cline_kanban_agent_id()` 直接返 `"kimi"`，不必退化成 `"codex"`。
⇒ 顺带对 S-C 有效：`"omp"` 同样被接受。
⇒ **教训**：`kanban --help` 的 agent-id 列表**不是**真相源，`--preview` 的校验才是。

---

## 7. 与计划文档的差异汇总

| 计划里的说法 | 本次取证 |
|---|---|
| P-1「`installPlugin` 有 RPC 层，`acp`/`web`/`server` **有可能**暴露」 | ❌ **四条通道全部实测排除**，无跨进程入口 |
| R-2「`installed.json` 无 `state`」 | ✅ CONFIRMED（`persist()` 逐字 9 字段） |
| R-7「kanban 原生支持 `kimi`」 | ✅ CONFIRMED（但需用 `--preview` 证，不能信 `--help`） |
| 「`enabledHooks()` 注入 env 是叠加不是替换」 | ✅ CONFIRMED（v1 逐字；v2 待 S-B5 实跑复核 hostProcess） |
| 「非 0 非 2 退出码 → 丢弃 stdout + allow + 无报错」 | ✅ CONFIRMED |
| 「`permissionDecision` 全等比较 `"deny"`」 | ✅ CONFIRMED |
| 「`spawn` 用 `shell:true` ⇒ 相对路径命令可行」 | ✅ CONFIRMED（v1/v2 都是） |
| R-11「`session_index.jsonl` 本机不存在，UNVERIFIABLE」 | ⚠️ **已过期**：本机现有该文件（5,785 B）。「`session_layout` 不读它」的设计判断需要重新论证，不能再以「文件不存在」为由 |
| R-9「think part 是 `.part.think` / `agents[id]` 无 `role` / `session_id` 已带前缀」 | ✅ 三条全部 CONFIRMED（见 §7b） |
| manifest 位置只有 `.kimi-plugin/plugin.json` | ⚠️ **不完整**：`kimi.plugin.json`（plugin root 下）是**优先**位置 |
| hook 的 `cwd` 可由 manifest 指定 | ❌ `enabledHooks()` 硬设为 `record.root`，manifest 写了也被覆盖 |

## 7b. session 布局与 wire.jsonl 结构（S-B2 / S-B3 的实现依据）

取证样本：本机 42 份 `wire.jsonl`，0 条非法行。

### 目录布局

```
$KIMI_CODE_HOME/sessions/
  wd_<workdir-basename>_<hash>/          ← 按工作目录分桶
    session_<uuid>/                       ← session_id 本身已含 `session_` 前缀
      state.json
      agents/<agent_id>/wire.jsonl        ← 每个 agent 一份，主 agent 的 id 是 "main"
      logs/kimi-code.log
```

⚠️ **`session_id` 拼路径时不要再加 `session_` 前缀**（R-9 已证，此处复核为真）。

### `state.json`（shim 归一的两个关键字段都在这里）

```json
{
  "createdAt": "...", "updatedAt": "...", "title": "...", "isCustomTitle": false,
  "agents": { "main": { "homedir": "...", "type": "main", "parentAgentId": null } },
  "custom": {},
  "workDir": "/Users/bominzhang/.cline/worktrees/01200/review-validate-fix",
  "lastPrompt": "Reply with exactly: OK"
}
```

- `agents[<id>]` **只有** `homedir` / `type` / `parentAgentId`（部分含 `swarmItem`）——**没有 `role`**（R-9 CONFIRMED）。
- **`workDir` 是 shim 恢复真实 cwd 的来源**（Stop 事件本身不带 cwd）。
- **`lastPrompt` 是 shim 注入 `last_user_message` 的来源。**

### wire.jsonl 的 20 种 record type

首行恒为 `metadata`（**42/42**），形如
`{"type":"metadata","protocol_version":"1.4","created_at":<ms>}`
⇒ 首行即可定 host，不必扫满 32 行。

**关键结构事实：助手输出与工具调用都不在 `context.append_message` 里。**
`context.append_message` 实测只承载 `role:"user"` 的消息；agent 的实际活动全部落在
`context.append_loop_event.event` 下：

| `event.type` | 关键字段 | 用途 |
|---|---|---|
| `step.begin` | `uuid` `turnId` `step` | 步骤边界 |
| `content.part` | **`part.{type,think\|text}`** `turnId` `step` `stepUuid` | 助手输出。`type` 实测只有 `think`(66) 与 `text`(19)；**think 的正文字段是 `part.think` 而不是 `part.text`**（R-9 CONFIRMED） |
| `tool.call` | `name` `args` `toolCallId` `description` `display` `traceId` | 工具调用 |
| `tool.result` | `toolCallId` `parentUuid` `result.output` `traceId` | 结果，经 `toolCallId` 与 call 配对 |
| `step.end` | `finishReason` `usage.{inputOther,output,inputCacheRead,inputCacheCreation}` `messageId` 及 5 个 latency 字段 | 步骤收尾与用量 |

`turn.prompt`：`{"input":[…], "origin":{"kind":<见下>}, "time":…}`
——注意 `input` 是 **part 列表**而非字符串，归一时要折叠。

### ⛔ `origin.kind` 有 **5** 种，且 `turn.prompt` 的也**不恒是** `user`

本文早先版本把 `turn.prompt` 的 origin 写成恒 `{"kind":"user"}`，**那是错的**（样本太小）。
扩到本机全部 48 份 wire 后实测：

| `origin.kind` | 计数（turn.prompt 52 条中） | 含义 |
|---|---|---|
| `user` | 33 | 真实用户输入 |
| `system_trigger` | **19** | **子代理的派发 prompt**（`Agent`/`AgentSwarm` 派生时注入） |
| `injection` | — | harness 系统提醒（`permission_mode` / `todo_list_reminder`） |
| `skill_activation` | 2 | skill 激活 |
| `background_task` | 2 | 后台任务 |

**100% 的记录都带 `origin`**，`role` 则实测恒为 `"user"` —— 所以 `role` 完全不能用来判来源。

⇒ **判据必须是正向的 `origin.kind == "user"`，不能用负向的 `!= "injection"`**：
后者会放过 `system_trigger` / `skill_activation` / `background_task` 三类 harness 正文，
让它们冒充用户发言。实测影响：48 份 wire 全量蒸馏时，负向判据把 **23 条** harness 正文
错记成 `kind="message", role="user"`；改正向后 `message` 263→240、`phase_marker` 47→70。

具体后果举例：子代理 wire 的首条记录就是 `system_trigger` 的派发 prompt，于是
`parent_transcript_quoted_session_name` 会把会话名渲染成「你是一项多智能体并行调查的
子调查员…」而不是真实用户输入。

> reviewer 报这条时给的场景是「injection 早于 turn.prompt」，本机 **0/48 复现不出来**
> （`permission_mode` 注入恒落在 turn.prompt 之后）；该场景需构造才成立。但同根的
> `system_trigger` 问题在真实数据里**大量存在**——所以缺陷是真的，只是成因比报告的更广。

### 实测到的工具 wire 名

`Bash`(21) `Edit`(21) `ReadMediaFile`(11) `Read`(7) `Grep`(6) `TodoList`(4) `Write`(3)
`Agent`(3) `TaskOutput`(2) `AgentSwarm`(1) `TaskStop`(1)

⇒ 写型工具是 **`Write` / `Edit` / `Bash`**，与 §5 的 `registerTool` 取证一致。
注意 `ReadMediaFile` 与 `AgentSwarm` 是 §5 的类名列表里看不出的 wire 名，说明
**类名与 wire 名不是一一对应**，`artifact_refs` 的归因必须按 wire 名而不是类名。

---

## 8. 仍未取证、留给后续子切片的

- **v2 引擎 hook 路径的 `hostProcess` 是否叠加 `process.env`**（§2.2 警告）→ S-B5 实跑。
  这是本文唯一一条**可能推翻现有设计**的未验项：若它不叠加，v2 引擎下的 hook 子进程会连
  `PATH` 都没有，`python3` 直接找不到。
- **手写 `installed.json` 后 Kimi 是否真的加载并触发 hook** → S-B5 端到端（隔离 `KIMI_CODE_HOME`）。
- **`/plugins reload` 是否有非 TUI 等价物** → 目前判断为无；S-B5 若发现即回填本文。
- ~~**`session_index.jsonl` 的内容契约与完整性**~~ → ✅ **已取证**（见 §6 与 §7b）：每行
  `{sessionId, sessionDir, workDir}`，本机 23 条 ↔ 23 个磁盘目录、0 缺失。据此
  `adapters/kimi/session_layout.py` 把它用作**快路径**、索引失效时回落扫桶，而不是原计划的
  「完全不读它」（那条设计的依据「已实测不完整」当时并不成立）。
- ~~**wire.jsonl 的 record 结构与蒸馏映射**~~ → ✅ **已取证并落地**（§7b）。
  `distill_kimi_wire_jsonl` 已实装，42/42 份真实 wire 验证 0 失败，未抽取记录逐项对账闭合
  （903 = `step.begin` 404 + `step.end` 398 + 空正文 `content.part` 101）。

---

## 11. 0.38.0 补充取证（2026-08-26，首次真机使用后）

> 取证对象：`~/.kimi-code/bin/kimi`，**0.38.0**，179 MB。全程只读。
> 触发原因：RVF 于 2026-08-24 部署到 Kimi 后，`/plugin` 报
> `output validation failed for pluginService.listPlugins`，且面板显示 `Skills (0)`。

### 11.1 `installed.json` 的 record 形状（推翻正文的隐含假设）

`persist()` 写的 9 个字段正文已记对，但**没记 `source` 的值域**。实测：

```js
// packages/agent-core/src/plugin/source.ts
function resolveInstallSource(source) {
  const github = parseGithubUrl(trimmed);
  if (github !== undefined) return github;                       // {kind:"github", owner, repo, ref}
  if (trimmed.startsWith("http")) return {kind:"zip-url", path:trimmed};
  if (!path.isAbsolute(trimmed)) throw …;
  return {kind:"local-path", path:trimmed};
}
// packages/agent-core/src/plugin/manager.ts
async function recordFrom(input) {
  return { …, source: input.source ?? "local-path", …, github: input.github, … };
}
```

⇒ **`source` 是安装来源的种类枚举，不是路径**；路径归 `originalSource`。
`github` 只对 `kind === "github"` 有值，本地路径安装下是 `undefined` ⇒ **键不落盘**。

### 11.2 0.38.0 新增：`listPlugins` 的出站 zod 校验

```js
// packages/kap-server/src/protocol/rest-plugin.ts
pluginSummarySchema = object({
  …,
  source: _enum(["local-path", "zip-url", "github"]),
  originalSource: string().optional(),
  github: pluginGithubMetadataSchema.optional(),
});
listPluginsResponseSchema = object({ plugins: array(pluginSummarySchema) });
```

`.optional()` 接受**缺省**，**不**接受 `null`。⇒ 把路径写进 `source`、或把 `github` 写成 `null`，
`/plugins` 面板整个打不开。

**关键的偏斜性**：hook 派发走的是 `enabledHooks()` → `materialize()` → `recordFrom()`，
那条路径对 `source` 不做枚举校验（`input.source ?? "local-path"` 原样透传）
⇒ **record 写坏时 hook 照常触发，只有 UI 挂掉**。故障表象与真实影响面不一致。

### 11.3 skill 发现只认 `manifest.skills`，且与 `agents` 不对称

```js
async function countDiscoveredPluginSkills(pluginId, manifest) {
  const roots = (manifest?.skills ?? []).map(…);
  if (roots.length === 0) return 0;                 // ← 不扫 skills/ 目录
  return (await discoverSkills({ roots })).length;
}
```

而 `parseManifest` 里两者的兜底待遇**不同**：

```js
let skills = await resolveDirListField(pluginRoot, "skills", raw["skills"], diagnostics);
let rootSkillFallback;
if (raw["skills"] === undefined) {
  if (await isFile(join(pluginRoot, "SKILL.md"))) { skills = [pluginRoot]; rootSkillFallback = true; }
}
let agents = await resolveDirListField(pluginRoot, "agents", raw["agents"], diagnostics);
if (raw["agents"] === undefined) {
  const agentsDir = join(pluginRoot, "agents");
  if (await isDir(agentsDir)) agents = [agentsDir];   // ← agents 有目录兜底，skills 没有
}
```

⇒ 漏声明 `skills` 是**零报错**状态：`state: ok`、hook 正常、所有 skill 不存在。

`resolveDirListField` 的失败等级要分清：

| 情形 | severity | 后果 |
|---|---|---|
| 不是 string / string[] | `error` | record `state` → `error` ⇒ **hook 一起停摆** |
| 条目不以 `./` 开头 | `error` | 同上 |
| 条目解析到 plugin root 之外 | `error` | 同上 |
| 条目不是目录 | `warn` | 只丢弃该条 |

⇒ **声明写错比不声明更严重。**

`discoverSkills` 对一个 root 的顶层行为：该目录下每个含 `SKILL.md` 的子目录算一个 skill，
目录自身的 `SKILL.md` 也算一个 ⇒ `"skills": ["./skills/"]` 能收全 `skills/<name>/SKILL.md`。

### 11.4 skill 是**会话级**的，`/plugins reload` 收敛不到

```js
this.skillsReady = this.loadSkills();      // 只在 session 构造时跑一次
async loadSkills() {
  const roots = await resolveSkillRoots({ …, pluginSkillRoots: this.options.skills?.pluginSkillRoots, … });
  await this.skills.loadRoots(roots);
}
```

`/plugins reload` 只把 `installed.json` 重新 materialize 进**进程级** `PluginManager`。
Kimi 自己的 TUI 常量把这件事说得很明白：

```js
PLUGIN_RELOAD_HINT = "Run /new or /reload to apply plugin changes.";
function pluginInlineChangeHint() { return "run /reload or /new to apply"; }
```

⇒ 激活序列是 **`/plugins reload` 然后 `/reload`（或 `/new`）**，两条缺一不可。

### 11.4-bis ⚠️ 部分推翻 §1：0.38.0 的 `kimi web` **有** plugin REST 路由

§1 断言「四条跨进程通道全部不可达」，其中「`kimi web` 的 `/api/v1/*` 无 plugin 路由」这一条
**在 0.38.0 已不成立**。二进制里有 `packages/kap-server/src/protocol/rest-plugin.ts`
（`listPluginsResponseSchema` / `installPluginRequestSchema` / `pluginIdParamSchema`）与
`packages/kap-server/src/routes/plugins.ts`（`enablePluginAction` / `disablePluginAction` 等）。

**已实测**（2026-08-26，本次修复的独立验证手段）：

```bash
kimi web --port <port> --log-level error      # 默认只绑 127.0.0.1，启动时打印 bearer token
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:<port>/api/v1/plugins
# → {"code":0,"msg":"success","data":{"plugins":[{…,"state":"ok","skillCount":6,
#     "hookCount":2,"commandCount":1,"hasErrors":false,"source":"local-path"}]}}
```

**这条路径的价值主要是「验证」而不是「注册」**：它要求先把服务器跑起来、再从 stdout 抓 token，
对一个非交互部署器来说比直接写 `installed.json` 更脆；但它是**唯一能让 Kimi 自己的代码给出结论**的
非交互手段——`skillCount` / `state` / `hasErrors` 都是它现算的，比任何复刻脚本都硬。

⇒ **修 Kimi 侧注册问题后，用这条路径做终验**，别只信复刻式核对器（复刻器与自己一致说明不了什么）。
§1 的其余三条（CLI 无 `plugin` 子命令、WebSocket 控制帧、ACP 方法表）在 0.38.0 仍成立
——`kimi --help` 的 Commands 里确实没有 `plugin`。

### 11.5 顺带确认（与正文一致，未变）

- manifest 双路径与遮蔽关系不变：`kimi.plugin.json`（root）**优先于** `.kimi-plugin/plugin.json`（dir）。
- `enabledHooks()` 过滤条件不变：`record.enabled && record.state === "ok" && record.manifest !== undefined`；
  注入 env 仍恰为 `KIMI_CODE_HOME` 与 `KIMI_PLUGIN_ROOT`。
- hook 事件枚举含 `Stop` 与 `UserPromptSubmit`，`timeout` 仍是 `int().min(1).max(600).optional()`。
- `UNSUPPORTED_RUNTIME_FIELDS = ["tools","apps","inject","configFile","config_file","bootstrap"]`
  ——命中只记 `severity:"info"`，无害。
- `commands` 与 `skills` 一样需显式声明（`./` 前缀），支持目录递归收 `.md`。
