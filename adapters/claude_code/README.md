# adapters/claude_code/

Claude Code adapter（路径 C：trigger-only，转发到 Codex core）。

## 当前状态

- **transcript**：S1 落点。`adapters/claude_code/transcript.py`（从 `plugins/review-validate-fix/skills/review-validate-fix/scripts/trajectory_distill.py` 物理拆分）。
- **subagent**：S2 落点（stub），路径 C 模型下 RVF subagent 调用仍走 Codex adapter。
- **hooks**：**保留在 `plugins/review-validate-fix/hooks/`**（不迁到本目录）。原因见下。

## 为什么 hooks 不迁到 adapters/claude_code/hooks/

- Claude Code marketplace 通过 nested plugin 的 `${CLAUDE_PLUGIN_ROOT}` 解析 hook 入口。把 hooks 物理迁到 `adapters/claude_code/` 需要 `hooks.json` 跨目录引用 `../../../adapters/claude_code/hooks/stop.py`，`${CLAUDE_PLUGIN_ROOT}` 跨目录解析行为未实测，风险高。
- 当前 `plugins/review-validate-fix/hooks/stop.py`（路径 C）本质上只是触发器：接 Claude Code Stop event，转发 stdin 给同 plugin 内的 `skills/review-validate-fix/scripts/codex_stop_review_validate_fix.py`。逻辑薄，物理位置与 marketplace 卷一致即可。
- S3 可重新评估，但默认形态是保留 nested。

## 6 维契约现状

| 维度 | 现状 |
|---|---|
| hook entry | `plugins/review-validate-fix/hooks/hooks.json` 注册 Stop hook，命令 `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop.py` |
| subagent | 未实装（stub）；路径 C 下不在 Claude Code 内调 Task tool |
| transcript | S1 落点（Claude Code conversation jsonl → `NormalizedTranscript`） |
| permission | 沿用 Claude Code marketplace 标准 |
| config | nested `plugins/review-validate-fix/.claude-plugin/plugin.json`；源 `.claude-plugin/marketplace.json` 列出本 plugin |
| discovery | `~/.claude/local-marketplaces/review-validate-fix/` + `~/.claude/settings.json` 的 `enabledPlugins` / `extraKnownMarketplaces` |
