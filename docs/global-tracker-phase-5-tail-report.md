# Global Tracker Phase 5 Tail Report

## Scope

本阶段只收紧 global reviewed-diff tracker 的 lease freshness 行为，不改变 reviewer scope authority。`scope.contract.json` 仍是 reviewer / validate-fix 的最终范围合同，session manifest 仍只作为 ownership evidence。

## Before / After

| Area | Before | After |
|---|---|---|
| Stop-hook stale sweep | `allocate_review_scope()` 自己会 prune stale lease，但 manual scope suppression probe 发生在 allocation 前；过期 lease 可能让 probe 看不到应当 suppression 的 available unit。 | `allocate_auto_review_scope()` 进入 manual suppression / dry-run / allocation 前先 lazy `sweep_stale()`，dispatcher dry-run 也经同一路径获得一致语义。 |
| Manual completed-scope suppression | 如果同一 scope 的旧 active lease 已过期但还未 sweep，unit 仍是 `assigned`，manual scope-hash match 可能被跳过。 | 过期 lease 先被标为 `stale-released`，unit 回到 `available`，manual scope-hash match 能正常阻止重复 RVF。 |
| Heartbeat | `heartbeat()` 只刷新 session/unit `last_seen_at`，事件没有统一的 `rvf_state_phase` / `rvf_backend` 字段，也不能续具体 tracker lease。 | `heartbeat()` 可选接收 `lease_id`，刷新 active lease 的 `last_activity_at` / `expires_at` / `ttl_seconds`，并写入 phase/backend 与 lease refresh outcome。 |
| Cline Kanban startup prepare | `prepare_review_run.py --tracker-scope` 生成 contract，但不会续租 tracker lease。 | startup prepare 消费 `tracker-scope.json` 时刷新对应 lease，并保留 allocator 写入的 `lease_ttl_seconds`，避免把 kanban-followup 1h TTL 错刷成默认 TTL。 |
| Dispatch prep metadata | `write_dispatch_prep_file()` 只读旧的 `lease_id` / `scope_hash` key，和 allocator 存在 ledger 上的 `tracker_lease_id` / `tracker_scope_hash` key 不一致。 | prep file 的 `rvf_run.tracker_lease_id` / `tracker_scope_hash` 能正确写出，后续 task/bootstrap 可审计同一个 lease。 |

## Remaining

- Codex-native reviewer lifecycle audit：确认是否存在非 `run_alternative_reviewer.py` 的 native subagent reviewer 路径需要 parent-side refresh/release runtime。
- Optional Kanban task-status polling：startup prepare 已 refresh，一般 TTL 足够；如果真实 Kanban RVF task 会长时间运行，应再用 `cline_kanban_client.py task status` 做周期续租。
- Request state events：`RVF_*_REQUEST` 当前保持 lease `active`；若要启用 `paused`，需要明确 release/sweep/test 语义。

## Verification

- `python3 -m py_compile plugins/review-validate-fix/skills/review-validate-fix/scripts/codex_stop_review_validate_fix.py plugins/review-validate-fix/skills/review-validate-fix/scripts/diff_tracker.py plugins/review-validate-fix/skills/review-validate-fix/scripts/prepare_review_run.py`
- `python3 tests/test_codex_stop_review_validate_fix.py --shard-count 6 --shard-index 1`
- `python3 tests/test_review_support_scripts.py --shard-count 6 --shard-index 4`
