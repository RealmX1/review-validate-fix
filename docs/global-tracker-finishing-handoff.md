# Global Tracker Finishing Handoff

## Status

This handoff tracks the remaining global reviewed-diff tracker work after Slice 6.

Already landed:

- SQLite tracker state, units, sessions, branches, worktrees, leases, lease participants, manual RVF runs, and events.
- Allocator path from Stop hook gate to `tracker-scope.json`.
- Cline Kanban startup prepare wiring: allocator output is passed into `prepare_review_run.py --tracker-scope`.
- `scope.contract.json` v2 fields: `primary_units`, `tracker_lease_id`, `tracker_scope_hash`.
- Reviewer lease acquire / heartbeat / release for external reviewer runner.
- Manual takeover and manual scope-hash suppression.
- Slice 6 prompt/reference cleanup: reviewers now treat `scope.contract.json` as the final scope contract; session manifest is only ownership evidence / tracker audit context.
- Phase 5 tail partial finish: Stop-hook allocation paths now lazy-sweep stale leases before manual scope suppression / dry-run / allocation decisions; tracker heartbeat can refresh a concrete lease while recording `rvf_state_phase` and `rvf_backend`; Cline Kanban startup prepare refreshes the tracker lease from `tracker-scope.json` without changing the original TTL.

Do not redo the Slice 6 scope-contract prompt cleanup. The phase report is `docs/rvf-scope-contract-slice-6-phase-report.md`.

## Remaining Work

The global tracker plan has a smaller unfinished Phase 5 tail around deeper activity probing and request-state semantics.

Primary target:

- Complete the activity probe / heartbeat matrix so active review work reliably keeps leases fresh, and stale work releases units without duplicate review.

Concrete slices:

1. **Stop hook lazy sweep integration** - landed
   - `allocate_auto_review_scope()` now calls `sweep_stale` before manual suppression, dry-run, and allocation.
   - `codex_stop_hook_dispatcher.py` reaches the same semantics through `allocate_auto_review_scope(..., dry_run=True)`.
   - Covered by a regression where a stale lease would hide a matching manual completed scope until lazy sweep releases it.

2. **Kanban task heartbeat** - partially landed
   - `diff_tracker.heartbeat()` can now refresh a tracker lease and records phase/backend fields.
   - `prepare_review_run.py` refreshes the allocated tracker lease when Cline Kanban startup prepare consumes `tracker-scope.json`.
   - Keep the one-hour follow-up TTL override (`CODEX_RVF_KANBAN_FOLLOWUP_LEASE_TTL_SECONDS`) intact.
   - Remaining: add optional task-status polling through `cline_kanban_client.py` if real long-running Kanban tasks need periodic refresh after startup prepare.

3. **Codex-native reviewer lease lifecycle audit**
   - External reviewer runner is covered; verify whether Codex-native reviewer subagents have a real parent-side refresh/release path.
   - If not, add a thin parent-side lease runtime around spawn / wait / timeout / close.
   - Do not rely on reviewer Stop hooks; reviewer sessions are suppressed with `CODEX_RVF_SUPPRESS_STOP_HOOK=1`.

4. **Paused/request state decision**
   - Current docs mention `RVF_*_REQUEST` can keep a lease active or paused.
   - Current code deliberately keeps request leases active; `paused` remains schema capacity, not active workflow behavior.
   - Remaining: add explicit request-state events if validate/fix request retries become common.
   - If adding `paused`, update schema checks, release/sweep behavior, and tests.

5. **Plan doc cleanup**
   - Phase 5 is still "partially landed" until Codex-native reviewer lifecycle audit and optional Kanban polling are resolved.
   - Current before/after report: `docs/global-tracker-phase-5-tail-report.md`.

## Guardrails

- Preserve `commit != clear`: committed units keep their review state; do not clear reviewed/assigned state just because the worktree is clean.
- Preserve `scope.contract.json` as the final reviewer contract. Do not re-promote session manifest or live diff to scope authority.
- Do not widen review packet scope while fixing heartbeat/stale logic.
- Keep tracker writes lazy and bounded; the plan explicitly avoids a standalone daemon for the first implementation.
- Existing flake note: `test_alternative_reviewer_activity_probe_failure_threshold_times_out` has shown a transient failure inside full plugin contract but passed on immediate shard rerun.

## Suggested Validation

- `python3 tests/test_review_support_scripts.py --shard-count 4 --shard-index 2`
- `python3 tests/test_review_support_scripts.py --shard-count 4 --shard-index 3`
- `python3 tests/test_codex_stop_review_validate_fix.py --shard-count 4 --shard-index 3`
- `python3 tests/test_review_support_scripts.py --shard-count 6 --shard-index 4`
- `python3 tests/test_codex_stop_review_validate_fix.py --shard-count 6 --shard-index 1`
- `python3 tests/test_codex_stop_hook_dispatcher.py`
- `python3 scripts/check_plugin_contracts.py`
- `git diff --check`
