# 05 · core ↔ adapter 的 6 维契约

> 本节给出 core 与每个 host adapter 之间应该约定的 6 个维度。每一维都包含：
> - **core 侧期望**（统一抽象）
> - **adapter 侧职责**（host-specific 接线）
> - **典型陷阱**

整套契约的设计目标：把 adapter 整个删了之后，core 在新的 host 上重新写 adapter 应能立刻接通；core 不需要任何修改。

---

## 维度 1 · Hook entry（事件入口）

### core 侧期望
core 定义"事件类型"枚举（最小集合）：
- `on_stop` —— agent 完成一轮工作。
- `on_user_prompt_submit` —— 用户提交新 prompt（可选，部分 host 没有）。
- `on_session_start` —— session 启动（可选）。

core 提供 `handle_event(event_kind: str, event: NormalizedEvent) -> Decision`，输入是 host-agnostic 的 `NormalizedEvent`，输出统一决策结构（继续 / 阻断 / 注入 follow-up prompt / no-op）。

### adapter 侧职责
- 把 host 原生 hook 入口（脚本路径、inline 字符串、注册形式）写到 host 期望的位置。
- 读 stdin / 读环境变量 / 读 hook config 把 host 事件 payload 解析成 `NormalizedEvent`。
- 调用 `core.handle_event(...)`。
- 把 core 返回的 `Decision` 翻译回 host 原生反馈（exit code、stdout JSON、API 调用等）。

### 典型陷阱
- 不读 stdin（[`04`](04-anti-patterns.md) ⑤）。
- 直接在 hook 脚本里写业务逻辑（违反 core/adapter 边界）。
- Codex 上忘记走 `~/.codex/hooks.json` 注册（[`04`](04-anti-patterns.md) ③）。

---

## 维度 2 · Sub-agent invocation（子代理调用）

### core 侧期望
core 提供一个 `invoke_subagent(role: str, prompt: str, context: dict) -> SubagentResult` 抽象。返回值是结构化的（文本 / tool calls 摘要 / 是否完成）。

### adapter 侧职责
- Claude Code adapter：用 `Task` 工具或 SDK 等价物启动 subagent。
- Codex adapter：通过 `codex exec` 子进程拉起新会话，注入 system prompt，再解析结果。
- OpenCode / Cursor adapter：按各自 API 翻译。

### 典型陷阱
- subagent 输出格式 host 间不同，core 直接消费 raw 字符串 → 解析 fragile。修正：adapter 在返回前把输出 normalize 成约定结构。
- subagent 超时 / 中断行为 host 间差异巨大。core 不要假设 "subagent 必然返回"；约定 `SubagentResult.status: ok | timeout | aborted | error`。

---

## 维度 3 · Transcript parsing（会话记录解析）

### core 侧期望
core 期望 `read_transcript(session_ref) -> NormalizedTranscript`。其中 `NormalizedTranscript` 是一个 ordered list of：
- `UserMessage(text, attachments[])`
- `AssistantMessage(text, tool_calls[])`
- `ToolResult(call_id, content, is_error)`
- `SystemNotice(text)`

### adapter 侧职责
每个 host 的 transcript 文件格式不同（Claude Code 的 `.jsonl`、Codex 的 session log、OpenCode 的 store）。adapter 负责把它们 normalize 成上述结构。

### 典型陷阱
- 字段名差异：Claude Code 用 `tool_use_id`，Codex 用 `call_id`，OpenCode 又一种。core 不要去消费这些原始字段；只消费 `NormalizedTranscript`。
- 增量更新：某些 host 的 transcript 是流式追加，adapter 要支持 `since: offset` 增量读取，否则每次 stop 都全量解析会拖慢。
- transcript 路径发现：Claude Code 通过环境变量暴露；Codex 通过 session id 推导。adapter 各自实现一个 `locate_transcript(host_ctx) -> Path`。

---

## 维度 4 · Permission / capability declaration

### core 侧期望
core 不直接声明"需要 Bash / 网络 / 写文件"。core 通过统一字段（`required_capabilities: ["bash", "edit", "web_fetch"]`）描述能力需求，由 adapter 翻译到 host 原生 permission scheme。

### adapter 侧职责
- Claude Code adapter：把 `required_capabilities` 翻译到 `.claude-plugin/plugin.json` 的 `allowed-tools` / settings.json permissions。
- Codex adapter：翻译到 Codex 的 sandbox + approval 配置。
- Cursor adapter：翻译到 Cursor 接受的 permission scope（受限）。

### 典型陷阱
- 在 SKILL.md 里直接写 Claude Code 私有的 `allowed-tools` 字段 → 其它 host 不认。修正：core 的 SKILL.md 用 `agentskills.io` 兼容的 `allowed-tools` 写法（已是事实标准）；adapter 自行解析。
- 把"权限"和"能力声明"混淆：core 应只声明"我需要做什么"，adapter 决定"在这个 host 上需要请求哪些 scope"。

---

## 维度 5 · Config / settings overlay

### core 侧期望
core 配置写在 `config/` 共享目录，schema 由 core 定义（JSON Schema / pydantic / dataclass）。core 期望调用 `load_config(host_ctx)` 拿到合并后的最终配置。

### adapter 侧职责
- adapter 知道在该 host 上 config 的合并优先级（用户级 / 项目级 / 插件级 / env vars）。
- adapter 负责把 host 私有配置（如 Claude Code 的 `settings.json` 中的 `env`）合并进 core 期望的统一 config 树。
- adapter **不**改 core config 的 schema；只贡献值。

### 典型陷阱
- core 直接读 host 的 settings.json → 在其它 host 上挂掉。修正：core 只读 `config/` 下自己的 schema。
- 配置漂移：多份 manifest 各自带 default config，互相矛盾。修正：default config 集中在 `config/defaults.{json|toml}`，所有 manifest 引用同一份。

---

## 维度 6 · Discovery & install path

### core 侧期望
core 不假设安装到任何特定路径。core 通过 `plugin_root(host_ctx)` 这种 adapter 提供的函数定位自身资源（skills、scripts）。

### adapter 侧职责
- adapter 知道在该 host 上插件被安装到哪里：
  - Claude Code：marketplace 安装通常解析 `${CLAUDE_PLUGIN_ROOT}` 环境变量。
  - Codex：`~/.codex/plugins/<plugin-id>/`。
  - OpenCode / Cursor：各自约定。
- adapter 把这个解析逻辑封装为 `plugin_root(host_ctx) -> Path`，core 一律调它。

### 典型陷阱
- core 里 hardcode 路径片段（如 `Path.home() / ".claude" / "plugins"` 这种）→ 立刻和 Codex 不兼容。
- adapter 自己也 hardcode（不读环境变量） → 用户改安装路径后即坏。修正：adapter 优先读 host 提供的环境变量，再 fallback 到默认路径。

---

## 契约总览表

| 维度 | core 抽象 | adapter 接线 | 核心约束 |
|---|---|---|---|
| 1. Hook entry | `handle_event(kind, NormalizedEvent)` | 写脚本入口，读 stdin，调 core，反馈 host | 必读 stdin；不在 hook 写业务 |
| 2. Sub-agent | `invoke_subagent(role, prompt, ctx)` | 调用 host API/子进程，normalize 返回 | 约定 `SubagentResult.status` |
| 3. Transcript | `read_transcript(ref) → NormalizedTranscript` | 解析 host transcript 文件 | core 不碰 host 字段名 |
| 4. Permission | `required_capabilities: [...]` | 翻译到 host permission scheme | core 只声明能力，不声明 host scope |
| 5. Config | `load_config(host_ctx)` | 合并 host 私有 settings 进来 | default 集中在 core |
| 6. Discovery | `plugin_root(host_ctx) → Path` | 用环境变量优先 | core 不 hardcode 路径 |

---

## 如何验证契约真的生效

每个维度有一个对应的"adapter 互换"验证动作：在保持 core 不变的前提下，临时把 adapter A 替换成 adapter B（甚至 mock adapter），跑通 e2e 流程。

- 维度 1：mock adapter 喂一个 `on_stop` event → 看 core 决策是否正确。
- 维度 2：mock subagent 返回固定结构 → 看 core 是否按预期消费。
- 维度 3：用一份保存的 fixture transcript（已 normalize）→ 看 core reviewer 是否产出一致报告。
- 维度 4–6 同理。

这些 mock adapter 应该 **<200 行**；如果超过，说明 core 还在依赖 host 细节，契约未真正生效，回到 core 收紧。
