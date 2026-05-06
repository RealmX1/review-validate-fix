# Trivial Fix Accumulation Review Lane Plan

## Capability

After this ships, Stop hook can route very small implementation fixes through a
cheap external-review lane and accumulate them for later full RVF, so that sub
50-line fixes do not each pay the cost of a full review-validate-fix cycle.

## Crude Routing Model

Classify dirty implementation scope by size and continuity:

- `trivial_fix_candidate`: implementation diff below roughly 50 changed lines,
  no risky files, no schema/migration/security/build-system changes.
- `accumulated_trivial_batch`: accumulated trivial candidates reach roughly
  500 changed lines.
- `large_following_work`: any later work after the accumulated set has reached
  roughly 2000 changed lines.

Routing intent:

| Scope | Route |
| --- | --- |
| Single trivial fix under 50 lines | Naive external reviewer, minimal tracking |
| Reviewer reports issue | Main agent validates and fixes in-session |
| Accumulated trivial fixes reach ~500 lines | Full RVF on the batch |
| Work after accumulated scope reaches ~2000 lines | Full RVF including later similar work |

## Minimal Tracking Scaffold

Do not extend the current diff tracker schema in the first pass. Start with a
small append-only artifact under run/state:

```text
state/trivial-fix-queue/
  <repo-key>.jsonl
```

Each row should be deterministic and cheap:

```json
{
  "schema_version": 1,
  "repo": "/abs/repo",
  "session_id": "session",
  "run_id": "rvf-...",
  "created_at": "2026-05-05T00:00:00Z",
  "paths": ["src/x.py"],
  "changed_lines": 24,
  "scope_hash": "sha256:...",
  "external_review_result": "no_issues|issues|unavailable",
  "main_agent_validated": true
}
```

If this proves useful, promote it later to a dedicated SQLite store. Do not put
these rows into `diff-tracker/repos/<repo-key>/tracker.sqlite3`; that database
tracks review ownership/leases, not queue policy.

## Naive External Reviewer

The first reviewer should be intentionally small:

- input: compact diff, file list, AGENTS excerpt, and exact changed-line count;
- output: `no_issues` or a short issue list with file/line;
- no workspace mutation;
- no handoff generation;
- no multi-agent merge table.

The main agent owns validation/fix for reported issues. This lane is therefore
not a full RVF replacement; it is a cheap prefilter plus accumulation policy.

## Full RVF Escalation

When thresholds are crossed, build a customized full RVF packet:

- include all queued trivial scopes since the last full RVF;
- group by path/module and scope hash;
- mark earlier naive-review outcomes as background evidence, not canonical
  reviewer findings;
- reset or tombstone queue entries only after full RVF handoff completes.

## Open Questions

- Exact line-count metric: `git diff --numstat`, hunk added+deleted, or owned
  tracker unit size.
- Risky-file denylist: migrations, auth/security, package manager files,
  CI, generated code, lockfiles.
- Whether a trivial fix that touches tests only should be queued, skipped, or
  reviewed immediately.
- How to avoid queue staleness when later commits rewrite the same paths.

## Non-Goals For Now

- No implementation in this slice.
- No new SQLite schema yet.
- No automatic full RVF launch from the queue until the escalation contract is
  specified and tested.
