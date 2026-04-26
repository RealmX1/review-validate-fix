# Review Validate Fix

这是 `$review-validate-fix` Codex workflow 的源仓库。仓库以 `skill/review-validate-fix/` 作为唯一人工维护的 canonical skill，并提供一个 plugin 包装层，方便以后把同一套 workflow 挂到 Codex 的 plugin 空间里。

## 当前结论

Codex 可以接受 plugin。这个本机 Codex 环境已经加载了 `GitHub`、`Browser Use`、`Documents`、`Spreadsheets` 等 plugin；plugin 通过 `.codex-plugin/plugin.json` 声明能力，并可以携带 `skills`、MCP server、app manifest、hook、asset 等资源。对这个 workflow 来说，skill 是最小且最稳的交付形态；plugin 更适合需要统一分发、UI 展示、MCP/app/hook 组合安装，或团队 marketplace 管理的时候。

## 核心设计支柱：Stop 后 GUI Fork

`review-validate-fix` 的 Stop hook 自动化必须以“父会话停止，新 GUI fork 会话承载 review checkpoint”为中心设计。父会话触发 Stop hook 后应结束；hook 负责通过 Codex app-server fork 出一个新会话，并像用户手动启动新会话时输入第一个 prompt 一样，在 fork 会话中提交以 `$review-validate-fix` 开头的用户 prompt。

这个新 fork 会话必须保留父会话完整上下文，同时成为 review/validate/fix 的独立可 rewind checkpoint。默认路径不得打开 Terminal，不得运行 `codex fork <session-id>` TUI，也不得用当前 chat continuation 代替 fork。`CODEX_RVF_MODE=continuation` 只保留为显式 fallback，因为它没有产生独立的新会话 checkpoint。

## Skill 与 Plugin 对比

| 维度 | 作为 skill | 作为 plugin |
| --- | --- | --- |
| 安装位置 | `~/.codex/skills/review-validate-fix` | `~/plugins/review-validate-fix` 加 `~/.agents/plugins/marketplace.json` |
| 最适合 | 只需要工作流说明、脚本、references | 需要打包 skill + MCP/app/hooks/assets/UI 元数据 |
| 触发方式 | `$review-validate-fix` 直接触发，`agents/openai.yaml` 控制隐式调用 | plugin 被安装后暴露其中的 skill，同样使用 `$review-validate-fix` |
| 维护复杂度 | 低，目录就是运行时内容 | 中，需要 manifest、marketplace、可能还有版本/资源同步 |
| 本 workflow 推荐 | 默认推荐 | 作为分发/集成包装层保留 |

## 仓库结构

```text
skill/review-validate-fix/                 # canonical skill，人工修改这里
plugins/review-validate-fix/               # Codex plugin 包装层
plugins/review-validate-fix/.codex-plugin/plugin.json
plugins/review-validate-fix/skills/review-validate-fix/
scripts/sync_plugin_payload.py             # 从 canonical skill 生成 plugin 内的 skill 副本
scripts/install_to_codex.py                # 安装到本机 Codex skill/plugin 空间
```

## 同步机制

日常开发只改 `skill/review-validate-fix/`。改完后运行：

```bash
python3 scripts/sync_plugin_payload.py --check-contracts
```

这会把 canonical skill 复制到 `plugins/review-validate-fix/skills/review-validate-fix/`，并分别运行 skill 自带的契约检查。这样 plugin 包装层始终反映同一份 skill 内容，不需要手动维护两套 workflow。

安装到本机 Codex skill 空间：

```bash
python3 scripts/install_to_codex.py --as skill
```

同时安装 skill 和 plugin：

```bash
python3 scripts/install_to_codex.py --as both
```

plugin 安装会把包装层复制到 `~/plugins/review-validate-fix`，并在 `~/.agents/plugins/marketplace.json` 中登记本机 plugin entry。这个路径遵循 Codex plugin scaffold 的本机 marketplace 约定。

配置 Codex Stop hook：

```bash
python3 scripts/install_to_codex.py --as skill --configure-stop-hook
```

这会更新 `~/.codex/hooks.json`，让 Stop hook 用 `CODEX_RVF_MODE=fork CODEX_RVF_FORK_MODE=gui` 调用本 skill 的 `scripts/codex_stop_review_validate_fix.py`。该模式不会打开 Terminal，也不会在当前 chat session 里 continuation；它通过 Codex app-server 的 `thread/fork` + `turn/start` 创建一个新的 GUI fork 会话，并在新会话中提交以 `$review-validate-fix` 开头的 prompt。这样父会话保留为可 rewind 的稳定 checkpoint。

hook 会优先使用 Stop event 暴露的 rollout path 进行 fork；只有没有 path 时才退回 thread/session id。这样可以避开 Desktop 环境 id 无法被外部 app-server 直接索引的问题。

### 当前 session 开关

如果只想临时管理当前 chat session 的 Stop hook，而不是改全局 `~/.codex/hooks.json`，可以在用户消息中单独放一行：

```text
RVF_STOP_HOOK: off
```

这会把当前 session 标记为 disabled，后续 Stop hook 对同一 session 静默跳过。恢复时发送：

```text
RVF_STOP_HOOK: on
```

查看当前 session 状态：

```text
RVF_STOP_HOOK: status
```

这些状态写入 skill 的 `state/session-hook/`，安装更新时会随 `state/` 一起保留，只影响当前 chat session，不修改全局 hook 配置。

## Setup 相关配置

有些变化不能简单从仓库覆盖到本机，因为它们绑定机器、凭据或用户选择。当前最典型的是：

- `config/alternative-reviewer.json`
- `state/`
- `~/.codex/hooks.json` 中的 Stop hook / fork hook 绑定
- `~/.codex/app-server-control/rvf-app-server.sock` 和 `~/.codex/app-server-control/rvf-app-server.log` 这类本机 app-server bridge 文件
- 外部 reviewer 的 CLI/MCP/IDE wrapper 认证状态和环境变量

`scripts/install_to_codex.py` 默认会保留本机已有的 `config/alternative-reviewer.json` 和 `state/`，避免仓库更新覆盖掉已完成的 external reviewer setup。确实要用仓库版本覆盖 setup 配置时，显式加：

```bash
python3 scripts/install_to_codex.py --as skill --replace-setup-config
```

这条规则和当前 external reviewer config 的性质一致：workflow 本体应随仓库同步，机器相关配置应由 setup 流程或用户明确授权更新。

Stop hook 的默认自动路径是 GUI/app-server fork。不要把 Terminal + `codex fork <session-id>` 作为 Desktop 自动路径：Desktop thread/session id 不一定存在于 CLI 的 saved sessions 中，会出现 Terminal 打开但 fork 失败的旧问题。`CODEX_RVF_MODE=continuation` 只保留为显式 fallback，因为它不会产生独立 fork checkpoint。

## 验证

```bash
bash skill/review-validate-fix/scripts/check_contracts.sh
python3 scripts/sync_plugin_payload.py --check-contracts
```
