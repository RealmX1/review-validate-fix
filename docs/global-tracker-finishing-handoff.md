# Global Tracker Finishing Handoff

## Status

This handoff is for the agent that will finish the remaining global reviewed-diff tracker work after Slice 6.

Already landed:

- SQLite tracker state, units, sessions, branches, worktrees, leases, lease participants, manual RVF runs, and events.
- Allocator path from Stop hook gate to `tracker-scope.json`.
- Cline Kanban startup prepare wiring: allocator output is passed into `prepare_review_run.py --tracker-scope`.
- `scope.contract.json` v2 fields: `primary_units`, `tracker_lease_id`, `tracker_scope_hash`.
- Reviewer lease acquire / heartbeat / release for external reviewer runner.
- Manual takeover and manual scope-hash suppression.
- Slice 6 prompt/reference cleanup: reviewers now treat `scope.contract.json` as the final scope contract; session manifest is only ownership evidence / tracker audit context.

Do not redo the Slice 6 scope-contract prompt cleanup. The phase report is `docs/rvf-scope-contract-slice-6-phase-report.md`.

## Remaining Work

The global tracker plan still has an unfinished Phase 5 tail around activity probing and stale release integration.

Primary target:

- Complete the activity probe / heartbeat matrix so active review work reliably keeps leases fresh, and stale work releases units without duplicate review.

Concrete slices:

1. **Stop hook lazy sweep integration**
   - Ensure every Stop hook gate path that touches tracker state calls `sweep_stale` before allocation / suppression decisions.
   - Confirm both `codex_stop_review_validate_fix.py` and `codex_stop_hook_dispatcher.py` use the same lazy-sweep semantics.
   - Add tests where an expired active lease blocks allocation before sweep and becomes allocatable after gate evaluation.

2. **Kanban task heartbeat**
   - Implement or wire polling through `cline_kanban_client.py` / Kanban task status so long-running `kanban-task` and `kanban-followup` RVF runs refresh their tracker lease while work is active.
   - Keep the one-hour follow-up TTL override (`CODEX_RVF_KANBAN_FOLLOWUP_LEASE_TTL_SECONDS`) intact.
   - Record heartbeat events with `rvf_state_phase` and `rvf_backend`.

3. **Codex-native reviewer lease lifecycle audit**
   - External reviewer runner is covered; verify whether Codex-native reviewer subagents have a real parent-side refresh/release path.
   - If not, add a thin parent-side lease runtime around spawn / wait / timeout / close.
   - Do not rely on reviewer Stop hooks; reviewer sessions are suppressed with `CODEX_RVF_SUPPRESS_STOP_HOOK=1`.

4. **Paused/request state decision**
   - Current docs mention `RVF_*_REQUEST` can keep a lease active or paused.
   - Confirm actual code either implements `paused` or deliberately keeps request leases active with explicit events.
   - If adding `paused`, update schema checks, release/sweep behavior, and tests.

5. **Plan doc cleanup**
   - Once the above lands, update `docs/global-reviewed-diff-tracker-overhaul-plan.md` Phase 5 from partially landed to landed.
   - Add a short before/after report if behavior changed materially.

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
- `python3 tests/test_codex_stop_hook_dispatcher.py`
- `python3 scripts/check_plugin_contracts.py`
- `git diff --check`
