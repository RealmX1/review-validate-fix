# adapters/codex/

Codex adapter。负责：

- `transcript.py`（S1）：解析 Codex session log（`session_meta` / `turn_context` / `event_msg` / `response_item` 四类 record）为 `core.transcript.NormalizedTranscript`。
- `subagent.py`（S2）：把 `codex exec` 子进程封装为 `core.decisions.SubagentResult`（含超时 / 中断 / stream-json 解析）。
- `hooks/`：Stop hook / UserPromptSubmit 脚本（当前位于 `plugins/review-validate-fix/skills/review-validate-fix/scripts/`；S1+ 将迁过来或保留 facade）。
- `install/`：把 hook 脚本路径注册到 `~/.codex/hooks.json` 的工具（参考反模式 ③ —— Codex runtime 不扫插件根）。

当前为 S0 占位骨架。实际 transcript / subagent 实装在 S1 / S2 落入。
