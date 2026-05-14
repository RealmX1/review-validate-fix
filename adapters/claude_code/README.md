# adapters/claude_code/

Claude Code adapter。负责：

- `hooks/stop.py`（S3）：Stop event 入口；先 `sys.stdin.read()` 再 normalize event 调 `core.handle_event(...)`。
- `transcript.py`（S1）：解析 Claude Code 的 `.jsonl` transcript 为 `core.transcript.NormalizedTranscript`。
- `subagent.py`（S2 stub / S3 实装）：通过 Task tool / Claude API / 外部 trigger 封装 `core.decisions.invoke_subagent`。

当前为 S0 占位骨架。Claude Code Stop hook 与 subagent 的具体执行模型（in-session Task tool 还是独立 API 调用）会在 S3 实施时与开发者对齐。
