# Cancel RVF Run

当用户要求停止、取消或终止一个由 Cline Kanban 管理的 RVF run 时，使用本流程。不要把用户主动停止造成的 `SIGTERM` / negative return code 记为 failed。

## 入口

- 如果用户提供 Codex Stop hook 的 systemMessage，优先使用其中的 `summary=<path>`：
  - `python3 scripts/cancel_rvf_run.py --summary <summary.json>`
- 如果用户只提供 run id：
  - `python3 scripts/cancel_rvf_run.py --run-id <rvf-run-id>`
- 如果用户提供 run dir：
  - `python3 scripts/cancel_rvf_run.py --run-dir <run_dir>`

## 行为契约

- 取消态必须写为 `cancelled`，RunLedger summary status 必须是 `cline-kanban-rvf-cancelled`。
- 取消原因使用 `user_cancelled`。
- 如果 summary 中存在 `cline_kanban_task_id`，优先调用 `kanban task trash --project-path <repo> --task-id <id>`；trash 失败也要把失败详情写入 ledger。
- 只终止当前 run 相关进程：summary 中的 `runner_pid`，以及命令行包含同一 `run_id` 的 `cline_kanban_client.py`、`apply_worktree_bootstrap.py`、`codex` 或 `review-validate-fix` 进程。
- 不要终止 Cline Kanban server、tmux `cline-kanban` / `cline-kanban-*` session、其他 RVF run，或不含当前 `run_id` 的进程。
- 不要删除 run artifacts、workspace、events.jsonl 或 summary.json；取消本身是审计事件。

## 排查

- 可先运行 `--dry-run` 查看候选 PID，不发送信号、不改 summary。
- 取消后检查：
  - `summary.json` 的 `status` 为 `cline-kanban-rvf-cancelled`
  - `events.jsonl` 包含 `run_cancel_requested` 和 `run_cancelled`
  - Cline Kanban task 已被 trash，或 ledger 中记录了 trash 失败原因
