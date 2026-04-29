# Cancel RVF Run

当用户要求停止、取消或终止一个由 Vibe-Kanban 管理的 headless RVF run 时，使用本流程。不要把用户主动停止造成的 `SIGTERM` / negative return code 记为 failed。

## 入口

- 如果用户提供 Codex Stop hook 的 systemMessage，优先使用其中的 `summary=<path>`：
  - `python3 scripts/cancel_rvf_run.py --summary <summary.json>`
- 如果用户只提供 run id：
  - `python3 scripts/cancel_rvf_run.py --run-id <rvf-run-id>`
- 如果用户提供 run dir：
  - `python3 scripts/cancel_rvf_run.py --run-dir <run_dir>`

## 行为契约

- 取消态必须写为 `cancelled`，RunLedger summary status 必须是 `vibe-kanban-rvf-cancelled`。
- 取消原因使用 `user_cancelled`；runner 自己观察到 `codex exec` 被信号终止时使用 `codex_exec_cancelled`。
- Vibe-Kanban local workspace 应更新为 `RVF cancelled: ...`，remote issue 路径也应更新为 `Cancelled`。
- 只终止当前 run 相关进程：summary 中的 `runner_pid`，以及命令行包含同一 `run_id` 的 `run_vibe_kanban_rvf.py` / `codex exec` 进程。
- 不要终止 Vibe-Kanban app/backend、tmux `rvf-vibe-kanban` session、其他 RVF run，或不含当前 `run_id` 的进程。
- 不要删除 run artifacts、workspace、events.jsonl 或 summary.json；取消本身是审计事件。

## 排查

- 可先运行 `--dry-run` 查看候选 PID，不发送信号、不改 summary。
- 取消后检查：
  - `summary.json` 的 `status` 为 `vibe-kanban-rvf-cancelled`
  - `events.jsonl` 包含 `run_cancel_requested` 和 `run_cancelled`
  - Vibe-Kanban workspace 标题显示 `RVF cancelled`
