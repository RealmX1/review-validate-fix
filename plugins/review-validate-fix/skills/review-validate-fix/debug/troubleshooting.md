# RVF Troubleshooting

Use this only when an RVF script fails, the user asks for runtime debugging, or a maintainer is investigating Stop hook / Kanban / GUI fallback behavior.

## First Checks

1. Open the run summary path reported by the failing script.
2. Inspect `events.jsonl` in the same run directory if the summary is insufficient.
3. Prefer deterministic diagnostic scripts over reading runtime code:
   - Stop hook scope: `scripts/diagnose_stop_hook_scope.py --summary <summary.json>`
   - Codex fork / GUI path: `scripts/diagnose_fork.py`
   - Cancel stuck run: `scripts/cancel_rvf_run.py`

## When More Context Is Needed

- Stop hook normal workflow: `internals/stop-hook-workflow.md`
- Backend/env/ledger ownership: `internals/runtime-contracts.md`
- Review result protocol: `protocols/README.md`

Do not load internals during a normal RVF run. Use them only after a concrete failure or explicit maintenance request.
