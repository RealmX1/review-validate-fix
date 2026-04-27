# Review Validate Fix

这是 `$review-validate-fix` Codex workflow 的源仓库。仓库只维护 Codex plugin：`plugins/review-validate-fix/` 是唯一 canonical 交付形态，其中的 `skills/review-validate-fix/` 是运行期 skill 内容。

## 当前结论

Codex 可以接受 plugin。这个 workflow 现在只通过 plugin 分发；plugin 通过 `.codex-plugin/plugin.json` 声明能力，并携带 `skills/review-validate-fix/` 作为实际运行内容。安装脚本只安装 plugin，不维护 standalone skill 路径。

## 核心设计支柱：Stop 后 GUI Fork

`review-validate-fix` 的 Stop hook 自动化必须以“父会话停止，新 GUI fork 会话承载 review checkpoint”为中心设计。父会话触发 Stop hook 后应结束；hook 负责通过 Codex app-server fork 出一个新会话，并像用户手动启动新会话时输入第一个 prompt 一样，在 fork 会话中提交以 `$review-validate-fix` 开头的用户 prompt。

这个新 fork 会话必须保留父会话完整上下文，同时成为 review/validate/fix 的独立可 rewind checkpoint。默认首选路径不得打开 Terminal，不得运行 `codex fork <session-id>` TUI，也不得用当前 chat continuation 代替 fork；如果 Codex Desktop control socket 不可用，hook 只通过 `systemMessage` 报告无法创建 GUI fork，并停止自动 review。Stop continuation 不会创建真正的新用户 prompt，只会作为 hook system context 出现在当前轨迹中，因此不能作为 fallback。

## 维护模型

| 维度 | 当前策略 |
| --- | --- |
| canonical 源码 | `plugins/review-validate-fix/skills/review-validate-fix/` |
| 本机安装位置 | `~/plugins/review-validate-fix` 加 `~/.agents/plugins/marketplace.json` |
| 触发方式 | plugin 暴露 `$review-validate-fix` skill，`agents/openai.yaml` 控制隐式调用 |
| 废弃路径 | `skill/review-validate-fix/` 与 `~/.codex/skills/review-validate-fix` |

## 仓库结构

```text
plugins/review-validate-fix/               # Codex plugin 包装层
plugins/review-validate-fix/.codex-plugin/plugin.json
plugins/review-validate-fix/skills/review-validate-fix/
                                            # canonical skill 内容，人工修改这里
scripts/check_plugin_contracts.py          # 仓库级契约检查入口，委托 check_skill_contracts.sh
scripts/check_skill_contracts.sh           # 覆盖 plugin runtime、安装脚本和 tests/ 的契约检查
scripts/install_to_codex.py                # 安装 plugin 到本机 Codex plugin 空间
tests/                                     # 开发和契约测试；不随 plugin runtime 分发
plugins/review-validate-fix/skills/review-validate-fix/scripts/codex_stop_hook_dispatcher.py
                                            # Stop hook 稳定入口：必要时先检查并安装本 repo plugin
```

## 开发维护流程

日常改动只应把运行期代码放在 `plugins/review-validate-fix/skills/review-validate-fix/`，把开发测试、仓库级契约检查和安装辅助脚本放在仓库根目录的 `tests/` 与 `scripts/`。plugin runtime 不应携带 `test_*.py`、`*_test.py` 或仓库级契约脚本；`scripts/check_skill_contracts.sh` 会检查这条边界。

推荐维护顺序：

```bash
bash scripts/check_skill_contracts.sh
python3 scripts/check_plugin_contracts.py
python3 scripts/install_to_codex.py --configure-stop-hook
```

`scripts/check_skill_contracts.sh` 是最完整的本地验证入口，会执行 shell 语法检查、Python 编译检查和仓库级测试。`scripts/check_plugin_contracts.py` 保留为 plugin 契约入口，当前会委托同一套仓库级检查。安装脚本默认保留本机 `alternative-reviewer.json` 和 `state/`，因此不会覆盖机器相关 setup。

## 安装机制

日常开发只改 `plugins/review-validate-fix/skills/review-validate-fix/`。改完后运行契约检查：

```bash
python3 scripts/check_plugin_contracts.py
```

这个脚本运行 plugin skill 自带的契约检查，不复制内容。

安装到本机 Codex plugin 空间：

```bash
python3 scripts/install_to_codex.py
```

安装会把包装层复制到 `~/plugins/review-validate-fix`，在 `~/.agents/plugins/marketplace.json` 中登记本机 plugin entry。这个路径遵循 Codex plugin scaffold 的本机 marketplace 约定。

配置 Codex Stop hook：

```bash
python3 scripts/install_to_codex.py --configure-stop-hook
```

这会更新 `~/.codex/hooks.json`，让 Stop hook 用 `CODEX_RVF_MODE=fork CODEX_RVF_FORK_MODE=gui` 调用 installed plugin skill 的稳定 dispatcher，并由 dispatcher 在必要检查和安装后转交给 `scripts/codex_stop_review_validate_fix.py`。该模式不会打开 Terminal；正常情况下它通过 Codex app-server 的 `thread/fork` + `turn/start` 创建一个新的 GUI fork 会话，并在新会话中提交以 `$review-validate-fix` 开头的 prompt。这样父会话保留为可 rewind 的稳定 checkpoint。如果 Codex Desktop control socket 不可用，则默认只报告无法创建 GUI fork，不再回退到 Stop continuation。

实际写入 `~/.codex/hooks.json` 的入口是 installed plugin skill 中的 `scripts/codex_stop_hook_dispatcher.py`，不是直接调用 `codex_stop_review_validate_fix.py`。dispatcher 会在 Stop event 来自本 RVF 源仓库、且不是 subagent 时，先顺序运行：

```bash
python3 scripts/check_plugin_contracts.py
python3 scripts/install_to_codex.py --configure-stop-hook
```

只有 contract check 和 plugin 安装成功后，dispatcher 才会把同一份 Stop event JSON 转交给 installed `codex_stop_review_validate_fix.py`。如果检查、安装或 installed hook 执行失败，dispatcher 会以非零状态退出并把失败原因写到 stderr，让 Codex 停在 hook 失败处等待用户介入；它不能把这类 breaking error 降级为 `continue: true` 的 systemMessage，也不能让主 coding agent 带着错误上下文继续工作。对其他仓库或 subagent Stop event，dispatcher 不做同步，只转交给 installed hook 正常执行。

所有 dispatcher、Stop hook、manual run、external reviewer 和 command lock 的排障日志都写入统一 run ledger。入口是 `state/latest.json` 指向的 `state/runs/<run_id>/summary.json` 和 `events.jsonl`；大文本如 Stop event、fork prompt、review packet、stdout/stderr 会作为 `artifacts/` 文件保存。command lock 会记录 `lock_wait_started`、`lock_acquired`、`lock_timeout` 与 `lock_released` 事件。hook stdout 仍只输出 Codex hook payload，用户可见 `systemMessage` 保持短格式：`review-validate-fix: <status>; reason=<reason_code>; detail=<human_readable_note>; summary=<summary_path>`，其中 `detail` 只在需要解释非错误跳过、递归保护等用户易误解状态时出现。可用 `CODEX_RVF_LOG_ROOT` 或兼容别名 `CODEX_RVF_STATE_DIR` 覆盖日志根目录，未设置时使用 plugin skill 的 `state/`。

### 日志排障入口

`state/` 是本机运行状态，已被 gitignore，不属于可分发 plugin 内容。排障时先看：

```bash
cat plugins/review-validate-fix/skills/review-validate-fix/state/latest.json
cat <summary_path>
cat <events_path>
```

`latest.json` 只是 pointer，不是完整状态源；主程序和测试都应读取 `summary.json` 或 `events.jsonl`。如果日志目录不可写，hook payload 仍应可用，并在 `systemMessage` 或 summary diagnostics 中标记 `log_unavailable`。`CODEX_RVF_LOG_MAX_INLINE_BYTES` 和 `CODEX_RVF_LOG_LEVEL` 只用于日志行为调试，不应改变 hook 协议。

hook 会优先使用 Stop event 暴露的 rollout path 进行 fork；只有没有 path 时才退回 thread/session id。这样可以避开 Desktop 环境 id 无法被外部 app-server 直接索引的问题。

如果 Codex Desktop 没有暴露 control app-server socket，hook 默认不会再启动独立 bridge app-server fork，因为该 fork 可能写入 session 文件但不被当前 GUI 立即显示。此时只报告无法创建 GUI fork，并停止自动 review；不会返回 `decision: "block"`，也不会注入 `$review-validate-fix` continuation prompt。需要保留旧 bridge 行为时，显式设置 `CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY=bridge` 或 `CODEX_RVF_ALLOW_BRIDGE_APP_SERVER=1`。

### Stop hook 工作流可视化

```mermaid
flowchart TD
    Install["安装或更新 plugin\nscripts/install_to_codex.py --configure-stop-hook"] --> Hooks["~/.codex/hooks.json\nStop: codex_stop_hook_dispatcher.py\nCODEX_RVF_MODE=fork\nCODEX_RVF_FORK_MODE=gui"]
    Hooks --> Stop["Codex 主会话停止\nStop event JSON"]
    Stop --> Dispatcher["installed dispatcher\ncodex_stop_hook_dispatcher.py"]

    Dispatcher --> DevCheck{"Stop event 是否来自\nRVF dev repo 主会话？"}
    DevCheck -- "是" --> Sync["dev sync\ncheck_plugin_contracts.py\ninstall_to_codex.py --configure-stop-hook"]
    Sync --> SyncOk{"检查和安装成功？"}
    SyncOk -- "否" --> BlockingFail["stderr + 非零退出\n阻止使用旧 installed plugin"]
    SyncOk -- "是" --> InstalledHook["installed hook\ncodex_stop_review_validate_fix.py"]
    DevCheck -- "否：其他 repo 或 subagent" --> InstalledHook

    InstalledHook --> Recursion{"递归、fork 子会话、subagent\n或 suppress/session off？"}
    Recursion -- "是" --> Skip["continue: true\nsystemMessage 说明跳过原因"]
    Recursion -- "否" --> Gate["dirty gate\nreview_validate_fix_gate.sh cwd"]
    Gate --> GateResult{"cwd repo 状态"}
    GateResult -- "CLEAN" --> Skip
    GateResult -- "NO_GIT / NOT_FOUND / 无 cwd" --> AskRepo["continue: true\n提示主会话询问目标 repo"]
    GateResult -- "DIRTY" --> Mode{"CODEX_RVF_MODE"}

    Mode -- "off" --> Skip
    Mode -- "continuation / block" --> ReportOnly["continue: true\n报告 GUI fork 不可用\n不注入 continuation prompt"]
    Mode -- "fork" --> Fork["GUI/app-server fork\nthread/fork + turn/start\n注入 $review-validate-fix prompt"]
    Fork --> Socket{"Desktop control socket 可用？"}
    Socket -- "可用" --> Forked["新 GUI fork 会话运行\nreview -> validate/fix -> handoff"]
    Socket -- "不可用且未显式允许 bridge" --> ReportOnly
    Socket -- "显式允许 bridge" --> Bridge["bridge app-server fork\nGUI 可见性不保证"]
    Bridge --> Forked

    Forked --> ForkStop["fork 会话停止"]
    ForkStop --> Handoff{"最终回复包含\n<handoff-context>？"}
    Handoff -- "是" --> Advisory["systemMessage 提醒\n把 handoff block 粘回父会话"]
    Handoff -- "否" --> Skip
```

这张图里的关键边界是：dispatcher 只负责在本 RVF 源仓库主会话停止时先同步 installed plugin，然后把原始 Stop event 交给 installed hook；真正的 review gate 和 GUI fork 只在 `codex_stop_review_validate_fix.py` 内发生。默认成功路径会创建新的 GUI fork 用户 prompt checkpoint，失败路径只报告原因，不把 `$review-validate-fix` 作为当前 Stop continuation 注入父会话。

### 当前 session 开关

如果只想临时管理当前 chat session 的 Stop hook，而不是改全局 `~/.codex/hooks.json`，可以在用户消息中单独放一行：

```text
RVF_STOP_HOOK: off
```

这会把当前 session 标记为 disabled，后续 Stop hook 对同一 session 只跳过 RVF fork/continuation/review gate。它不会关闭 dispatcher 的 dev sync：当 Stop event 来自本 RVF 源仓库主会话时，dispatcher 仍会先检查并安装当前 plugin，然后再由 installed hook 看到该 session disabled 并跳过 RVF 流程。恢复时发送：

```text
RVF_STOP_HOOK: on
```

查看当前 session 状态：

```text
RVF_STOP_HOOK: status
```

这些状态写入 plugin skill 的 `state/session-hook/`，安装更新时会随 `state/` 一起保留，只影响当前 chat session 的 RVF 执行 gate，不修改全局 hook 配置，也不阻止本仓库开发时的 installed plugin 检查和安装。

这些 `RVF_STOP_HOOK:*` 行是 Stop hook 的会话控制元数据，不是交给主 agent 的代码任务、review issue、research 对象或 scope-of-work 内容。自动 fork prompt 会显式提醒 fork 会话忽略这类控制行，避免把临时开关误纳入 review 工作。

## Setup 相关配置

有些变化不能简单从仓库覆盖到本机，因为它们绑定机器、凭据或用户选择。当前最典型的是：

- `config/alternative-reviewer.json`
- `state/`
- `~/.codex/hooks.json` 中的 Stop hook / fork hook 绑定
- `~/.codex/hooks.json` 中 `CODEX_RVF_DEV_REPO` 指向的本机源仓库路径
- `~/.codex/app-server-control/rvf-app-server.sock` 和 `~/.codex/app-server-control/rvf-app-server.log` 这类本机 app-server bridge 文件
- 外部 reviewer 的 CLI/MCP/IDE wrapper 认证状态和环境变量

`scripts/install_to_codex.py` 默认会保留本机 plugin 中已有的 `skills/review-validate-fix/config/alternative-reviewer.json` 和 `skills/review-validate-fix/state/`，避免仓库更新覆盖掉已完成的 external reviewer setup。确实要用仓库版本覆盖 setup 配置时，显式加：

```bash
python3 scripts/install_to_codex.py --replace-setup-config
```

这条规则和当前 external reviewer config 的性质一致：workflow 本体应随仓库同步，机器相关配置应由 setup 流程或用户明确授权更新。

Stop hook 的默认首选自动路径是 GUI/app-server fork。不要把 Terminal + `codex fork <session-id>` 作为 Desktop 自动路径：Desktop thread/session id 不一定存在于 CLI 的 saved sessions 中，会出现 Terminal 打开但 fork 失败的旧问题。`CODEX_RVF_MODE=continuation` 已废弃；当 Desktop control socket 缺失且未显式允许 bridge app-server 时，fork 模式只报告无法创建 GUI fork。

## 验证

```bash
bash scripts/check_skill_contracts.sh
python3 scripts/check_plugin_contracts.py
```
