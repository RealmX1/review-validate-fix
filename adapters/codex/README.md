# adapters/codex/

Codex adapter（native 实装）。

## 当前状态

- **hooks chain**：仍在 `plugins/review-validate-fix/skills/review-validate-fix/scripts/`：
  - `stop_hook_router.py`（609 行）
  - `stop_hook_dispatcher.py`（1470 行）
  - `stop_review_validate_fix.py`（6594 行 main dispatch）
  - 物理迁移到 `adapters/codex/hooks/` 需要 `~/.codex/hooks.json` 路径同步；S3 之后再考虑。
- **subagent**：S2 落点。`adapters/codex/subagent.py` 包装现有 6–8 处 `subprocess.Popen("codex exec", ...)`。
- **transcript**：S1 落点。`adapters/codex/transcript.py`（从 `trajectory_distill.py` 拆 Codex 栈部分）。

## 6 维契约现状

| 维度 | 现状 |
|---|---|
| hook entry | `~/.codex/hooks.json` 注册 Stop hook（绝对路径，由 `install_to_codex.py` 写入） |
| subagent | `codex exec` Popen，6–8 处调用点；S2 经 `invoke_subagent` 抽象 |
| transcript | `RVF_USER_LOCAL_DATA_ROOT/run-logs/runs/rvf-*/artifacts/trajectory.jsonl`（Codex JSONL 格式）→ `NormalizedTranscript` |
| permission | Codex CLI 标准（cwd/env merge，sandbox 由 codex 控制） |
| config | nested `plugins/review-validate-fix/.codex-plugin/plugin.json`；生成的 `.agents/plugins/marketplace.json` 由官方 CLI 注册 selector `review-validate-fix@review-validate-fix-local-main` |
| discovery | `rvf_local_deploy.py` 从干净 local `main` 生成 commit-addressed marketplace；Codex 官方 `plugin marketplace add` / `plugin add` 管理 cache 与 installed state。RVF wrapper 仅结构化维护 `~/.codex/hooks.json`。 |
