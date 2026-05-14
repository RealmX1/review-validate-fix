# 04 · 5 个高频反模式

每一条都伴随：触发症状 / 根因 / 修正方向。

---

## ① Shadow tree —— 把 skill/command 文件同时复制进多个 host 目录

### 症状
- 仓库里同时存在 `.claude-plugin/skills/foo.md` 与 `.codex-plugin/skills/foo.md`，内容几乎相同。
- 修了 Claude Code 侧的 skill 后，过几天发现 Codex 侧还在用旧版本。
- diff 越来越大、reviewer 看不清"哪一份才是真的"。

### 根因
误以为"每个 host 需要自己的资产目录"。实际上 host 原生 manifest 只规定**入口路径**，并不要求资产必须放在 manifest 同侧目录。

### 修正
- 把所有 skill / command / agent 文件移到仓库根的共享目录（如 `skills/`、`commands/`、`agents/`）。
- 各 host manifest 通过 `skills: "../skills"` 之类的相对路径指向同一棵树。
- 仓库根加一条 CI 检查："任何 `.<host>-plugin/skills/` 子目录视为违规"。

---

## ② Plugin-id 漂移 —— 在不同 host 上叫不同名字

> **本仓库当前正是该反模式的样本**：`plugins/review-validate-fix/.codex-plugin/plugin.json` 的 `name` 字段为 `"rvf"`，与 Claude Code 侧设计 id `review-validate-fix` 不一致。修正动作归入 [`07-implementation-slices.md`](07-implementation-slices.md) 的 **S0**，详见 [`06-rvf-application.md`](06-rvf-application.md) "当前 RVF 现状" 段。

### 症状
- Claude Code 上叫 `review-validate-fix`，Codex 上叫 `rvf`，OpenCode 上叫 `code-review-loop`。
- 用户在不同 host 上看到的命令/skill 名也对不上。
- 报告里出现"在 X host 启用 Y 插件"的指令，跨 host 时失效。

### 根因
- manifest 是分别写的，没建立"plugin id 是跨 host 不变量"的纪律。
- 偶有"在某 host 上 plugin id 必须更短/更长/不能含连字符"等技术约束，但很少真的存在；多半是开发者按各自喜好命名导致。

### 修正
- 选一个稳定 id（建议小写连字符短 slug，例：`review-validate-fix`），写进**所有** manifest 的 `name`。
- 在 [`07-implementation-slices.md`](07-implementation-slices.md) 的 Slice 0 / Slice 1 把统一 id 作为前置工作。
- 用户文档统一用同一个 id 称呼，避免"在 Claude Code 启用 review-validate-fix / 在 Codex 启用 rvf"这种行文。

---

## ③ 依赖 Codex 插件根的 hook 加载

### 症状
- 在 `.codex-plugin/hooks/stop.py` 写了 stop hook，预期 Codex 启用 plugin 时自动接上。
- 实际运行 Codex 时 stop hook 完全不触发，也没有错误日志。

### 根因
- Codex 当前**只**扫描 `~/.codex/hooks.json`（用户配置层），**不扫**已安装的 plugin 根。
- 已在上游 issue `openai/codex#16430` 提报，状态截至 2026-05-12 为 OPEN（核验链接见 [`appendix-sources.md`](appendix-sources.md)）。

### 修正
- 插件根可以放 hook 脚本（如 `adapters/codex/hooks/stop.py`），但**不要**期望 Codex runtime 自动加载。
- 通过 installer / skill 文档显式要求用户把脚本路径注册到 `~/.codex/hooks.json`：
  ```jsonc
  // ~/.codex/hooks.json
  {
    "hooks": {
      "stop": "$HOME/.codex/plugins/review-validate-fix/adapters/codex/hooks/stop.py"
    }
  }
  ```
- 或者把 hook 写成 Codex 已支持的 dispatcher 形态（如 router/dispatcher 链）。
- 在兼容性矩阵里把 Codex hook 支持标为 "Instruction-backed"（需要用户配合手动接线），而不是 "Native"。

---

## ④ Host idioms 漏到 core

### 症状
- core 的 reviewer 模块 import 了 `claude_code_sdk`，或 core 的 validate 流程里写死了 `tool_use_id` 这类 Claude 私有结构。
- 想把 core 在 Codex 上跑时，要么改 core 要么写大量 shim。

### 根因
adapter 与 core 边界没立起来，便利驱动地把 host SDK 的东西用到 core 里。

### 修正
- core 必须能在不 import 任何 host SDK 的前提下 import 通过、单元测试通过。
- 在 core 与 adapter 间定义清晰契约（见 [`05-adapter-contract.md`](05-adapter-contract.md) 的 6 维契约）。
- 加 CI / lint 规则：core 模块禁止 import `claude_code_sdk` / `codex_sdk` 等 host 私有库；只许 import 标准库与共享 utility。

---

## ⑤ Inline hook 不消费 stdin

### 症状
- Claude Code 的 stop hook 写成 `hooks: ["echo done"]` 形式 inline 命令。
- Stop event 携带的 transcript / metadata 通过 stdin 传入，但 inline `echo` 直接结束 → hook 看似"成功"但实际数据被丢弃。
- 下游 reviewer / validator 收不到任何 transcript，无声失败。

### 根因
对 host 的 hook stdin 协议理解不充分；inline 命令默认不读 stdin。

### 修正
- 所有 hook 入口写成脚本（`hooks/stop.py`、`hooks/stop.sh`）而非 inline 字符串。
- 脚本开头第一件事就是**读完 stdin**（即使只是 `sys.stdin.read()`），再决定是否使用。
- 在 adapter 层封装一个 `read_event()` utility，强制所有 hook 走它，避免重复踩坑。
- 在 [`07-implementation-slices.md`](07-implementation-slices.md) 的"验证步骤"里加入"hook 实际收到 transcript"的 e2e 断言。

---

## 反模式检查清单（可贴在 PR 模板）

- [ ] 仓库内没有 `<host>-plugin/skills|commands|agents/` 这类 shadow tree。
- [ ] 所有 manifest 的 `name` 字段是同一个 plugin id。
- [ ] 任何依赖 Codex hook 的功能都明确声明 "需要在 `~/.codex/hooks.json` 注册"，并提供注册片段。
- [ ] core 模块没有 import 任何 host 私有 SDK。
- [ ] 所有 hook 都是脚本入口，且第一件事是读 stdin。
