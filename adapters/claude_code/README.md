# adapters/claude_code/

Claude Code adapter（路径 C：trigger-only，转发到 Codex core）。

## 当前状态

- **transcript**：S1 落点。`adapters/claude_code/transcript.py`（从 `plugins/review-validate-fix/skills/review-validate-fix/scripts/trajectory_distill.py` 物理拆分）。
- **subagent**：S2 已**双 host 实装**（无 stub）。`adapters/claude_code/subagent.py`：
  - 观测侧 `resolve_subagents(...)` 发现 Claude `<parent>/<uuid>/subagents/agent-*.jsonl` 子代理 transcript（S2-observe `b083f65`）；
  - 调用侧 `build_analyze_command(*, claude_bin)` 构造 headless analyze 调用向量（S2-invoke `e2f0e9a`）。
- **hooks**：**保留在 `plugins/review-validate-fix/hooks/`**（不迁到本目录——S3 决策，理由见下）。两入口（`stop.py` + `user_prompt_submit.py`）的共享逻辑已收敛为单一契约 `hooks/_claude_hook_entry.py`（S3 / handoff G）。

## 为什么 hooks 不迁到 adapters/claude_code/hooks/（S3 决策）

- Claude Code marketplace 通过 nested plugin 的 `${CLAUDE_PLUGIN_ROOT}` 解析 hook 入口（`hooks/hooks.json` 的 command 是 `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop.py`）。把 hooks 物理迁到 `adapters/claude_code/` 需要 `hooks.json` 跨目录引用 `../../../adapters/claude_code/hooks/stop.py`，`${CLAUDE_PLUGIN_ROOT}` 跨目录解析行为**未实测**（plan Risk #5），失败模式是「整个 Claude 触发路径静默中断」——风险高、回报仅外观一致。
- 当前两入口本质上只是触发器：接 Claude Code Stop / UserPromptSubmit event，转发 stdin 给同 plugin 内的核心脚本（Stop → `skills/.../codex_stop_review_validate_fix.py`；UPS → `skills/.../rvf_user_prompt_submit.py`）。逻辑薄，物理位置与 marketplace 卷一致即可。
- transcript 不在 hook 入口处理：入口纯 trigger-only，transcript 归一在被转发的核心脚本内完成（消费 `adapters/claude_code/transcript.py`）。故无「hook 入口的 transcript 用法对齐」之活。

## handoff G（维度 1 · hook 唯一注册）现状与 deferred

- **问题**：Codex plugin loader 把 plugin-packaged `hooks/hooks.json` 也当 hooks 源加载（`~/.codex/config.toml` `[hooks.state.review-validate-fix@local-codex-plugins:hooks/hooks.json:...]`），与 `~/.codex/hooks.json` 里 installer 注册的 RVF entry 平行执行 → 同一 Codex 事件触发两次 RVF。
- **S3 已做（守卫单源化）**：`hooks/_claude_hook_entry.py` 的 `is_foreign_invocation` 是**唯一** host-ownership 守卫——本 Claude 入口只拥有 Claude 调用，正向证据判定 Codex 时静默 no-op，由 Codex 端 entry 独自处理 → 每 host 恰好处理一次。原 `stop.py` / `user_prompt_submit.py` 各持一份逐字复制的 `_is_codex_invocation` 已被消除，收敛为本契约一处（`run_claude_hook` 同样收纳共享的 stdin→normalize→subprocess→fail-open 骨架）。该模块**刻意 stdlib-only、不依赖 `core`/`adapters`**：hook 是最该 fail-open 的安全面，不给它加 vendored import 失败模式；两入口经 same-dir `sys.path` 自举 import 该 sibling。
- **deferred（非维度 1 完美形态，受 Risk #14 约束）**：彻底删运行期守卫的「config 级单次注册」——让 Codex plugin loader **不注册** bundled `hooks.json`，使守卫可整体删除、`rg is_foreign_invocation hooks/` 命中 0、core 在注册层而非脚本层结构性不重复——需先在 live `~/.codex` 实测 Codex plugin-loader 是否支持抑制 bundled 双源（plan Risk #14「先实测确认抑制、再删守卫」），且受「勿污染真实 ~/.codex」约束。故本切片只做守卫单源化，零守卫的结构性消除留作后续；双触发回归测试（`claude_plugin_{shim,stop_shim}_codex_invocation_noop`）保留作安全网。

## 6 维契约现状

| 维度 | 现状 |
|---|---|
| hook entry | `plugins/review-validate-fix/hooks/hooks.json` 注册 **Stop + UserPromptSubmit** 两入口（命令 `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/{stop,user_prompt_submit}.py`）；两入口共享单一契约 `hooks/_claude_hook_entry.py`（host-ownership 守卫 + 转发骨架） |
| subagent | **双 host 实装**：观测 `resolve_subagents`（Claude `subagents/agent-*.jsonl` 发现）+ 调用 `build_analyze_command`（headless analyze 向量）；host 中性模型在 `core/subagents/` |
| transcript | S1 落点（Claude Code conversation jsonl → `NormalizedTranscript`）；核心脚本消费，hook 入口不碰 |
| permission | 沿用 Claude Code marketplace 标准 |
| config | nested `plugins/review-validate-fix/.claude-plugin/plugin.json`；源 `.claude-plugin/marketplace.json` 列出本 plugin |
| discovery | `~/.claude/local-marketplaces/review-validate-fix/` + `~/.claude/settings.json` 的 `enabledPlugins` / `extraKnownMarketplaces` |
