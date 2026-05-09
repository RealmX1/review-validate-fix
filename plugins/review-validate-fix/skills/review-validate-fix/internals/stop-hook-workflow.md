# Stop Hook Workflow

Audience: RVF maintainers. Normal RVF agents should not read this file.

Normative sources:

- Router: `scripts/codex_stop_hook_router.py`
- Dispatcher: `scripts/codex_stop_hook_dispatcher.py`
- Runtime: `scripts/codex_stop_review_validate_fix.py`
- Kanban client: `scripts/cline_kanban_client.py`

## Intended Shape

The Stop hook is a user-configured automation boundary, not model-implicit skill invocation. Its job is to stop the parent session and leave a new explicit review checkpoint, normally through Cline Kanban task creation or Kanban follow-up message injection.

The parent session should not continue the RVF loop as a hook continuation. Continuation prompts are disabled because they do not create a real user prompt boundary.

## Backend Ownership

Backend selection, provider health checks, env/config parsing, ledger fields, prep files, Kanban task creation, GUI fallback gating, and bridge app-server behavior belong to the scripts listed above. Agents should not reconstruct this state machine from `SKILL.md`.

If a script fails, the user-facing agent should inspect the run summary and then follow `debug/troubleshooting.md`.

## Current Policy Summary

- Default backend path is Cline Kanban task creation or Kanban follow-up when already inside a Kanban task.
- If Kanban is unavailable, scripts should report a diagnostic summary instead of silently falling back.
- GUI fork is an explicit opt-in / legacy path owned by runtime scripts.
- Runtime-generated prompts must carry the needed run artifact paths and origin metadata.

This summary is descriptive only. Update the scripts and tests first, then update this file if policy changes.
