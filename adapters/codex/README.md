# adapters/codex/

Codex adapter（native 实装）。

## 当前状态

- **hooks chain**：仍在 `plugins/review-validate-fix/skills/review-validate-fix/scripts/`：
  - `codex_stop_hook_router.py`（609 行）
  - `codex_stop_hook_dispatcher.py`（1470 行）
  - `codex_stop_review_validate_fix.py`（6594 行 main dispatch）
  - 物理迁移到 `adapters/codex/hooks/` 需要 `~/.codex/hooks.json` 路径同步；S3 之后再考虑。
- **subagent**：S2 落点。`adapters/codex/subagent.py` 包装现有 6–8 处 `subprocess.Popen("codex exec", ...)`。
- **transcript**：S1 落点。`adapters/codex/transcript.py`（从 `trajectory_distill.py` 拆 Codex 栈部分）。

## 6 维契约现状

| 维度 | 现状 |
|---|---|
| hook entry | `~/.codex/hooks.json` 注册 Stop hook（绝对路径，由 `install_to_codex.py` 写入） |
| subagent | `codex exec` Popen，6–8 处调用点；S2 经 `invoke_subagent` 抽象 |
| transcript | `state/runs/rvf-*/artifacts/trajectory.jsonl`（Codex JSONL 格式）→ `NormalizedTranscript` |
| permission | Codex CLI 标准（cwd/env merge，sandbox 由 codex 控制） |
| config | nested `plugins/review-validate-fix/.codex-plugin/plugin.json`；`~/.agents/plugins/marketplace.json` 列出；`~/.codex/config.toml` 持 `[plugins."review-validate-fix@local-codex-plugins"]` slot |
| discovery | `install_to_codex.py` 执行：copy_tree 到 `~/plugins/review-validate-fix/`、marketplace cache 到 `~/.codex/plugins/cache/local-codex-plugins/review-validate-fix/0.1.0/`、写 `~/.codex/config.toml` 与 `~/.codex/hooks.json` |
