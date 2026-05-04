# Phase B (deferred): workspace-diff.json ↔ tracker integration

> **Status**: deferred. Do **not** start until the
> [`global-reviewed-diff-tracker` overhaul](#background--blocking-dependency)
> Phase 2 (or later) has merged into `main` and the SQLite tracker is the
> active source of truth for review-scope ownership.
>
> **Origin**: this work is the only deferred slice of the trajectory + workspace
> diff capture plan that produced commits `07a20dc`..`28c3dd8` on
> `phase-c-rvf-analyze-and-finalize`. Everything else from that plan
> (Phase A, A.1, B.0, C) shipped in those commits. The plan itself is at
> `/Users/bominzhang/.claude/plans/develop-this-idea-with-quirky-lark.md` —
> see the `## 实施分期` section, item **Phase B**.

## What this work is

Extend `workspace-diff.json::changed_paths[]` so each entry can be cross-
referenced against the repo-wide review-scope tracker. After the overhaul,
each changed path can be classified as "freshly introduced by this RVF run"
vs "background WIP that happened to be touched". Today the diff is just a
flat byte-for-byte change list — analysers (and the future `$rvf-analyze`
LLM agent) cannot tell those apart.

### Concretely

For each item in `workspace-diff.json::changed_paths[]`, add three new
optional fields (none are required when the tracker is absent — see
"Backward behavior" below):

| Field | Type | Meaning |
|---|---|---|
| `tracker_unit_ids` | `list[str]` | Tracker unit ids that own (or co-own) this path at finalize time. Empty list means "no unit covers this path". |
| `owned_by_session` | `bool \| null` | Whether the current RVF session's lease covers any of those units. `null` when the tracker has no record for the path. |
| `review_state` | `str \| null` | Tracker's review state for the unit(s): `"reviewed"` / `"in_review"` / `"unreviewed"` / `"superseded"` / etc. Use whatever vocabulary the tracker exposes; pick the most "advanced" state if multiple units cover the path. `null` when no unit. |

Bump `SCHEMA_VERSION` from `1` → `2` in `workspace_diff.py`. Document the
new fields in the same module's docstring (the contract is currently
implicit in the `payload = { ... }` block at lines ~170-184 of
`plugins/review-validate-fix/skills/review-validate-fix/scripts/workspace_diff.py`).

## Background & blocking dependency

The repo-wide tracker overhaul — search for `feat/global-reviewed-diff-tracker-phase-1`
in the branch list and grep `git log --all --grep "global-reviewed-diff-tracker"`
for full context — moves review-scope ownership from per-session
`session_manifest` files into a SQLite database with unit / lease / takeover /
sweep semantics. As of the time this doc was written:

- **Phase 1**: merged into `main` as `feat/global-reviewed-diff-tracker-phase-1`.
- **Phase 2-6**: not yet implemented. Phase 2 specifically introduces the
  primitives (units, lease records, ownership queries) that this work depends
  on.

Until Phase 2+ ships:
- The tracker SQLite file may not exist for a given run.
- Even when it exists, ownership queries for a given path may not be exposed
  through a stable API.
- The `review_state` enum values are subject to change.

**This is the only blocker.** The Phase A trajectory + workspace diff capture
infrastructure is fully in place and unaffected — `workspace_diff.compute()`
already runs and writes `workspace-diff.json` at every finalize. We just don't
have anywhere to look up the three new fields yet.

## When to start

Trigger conditions (all must hold):

1. `git log main` shows commits implementing Phase 2 of the overhaul.
2. There is a documented Python API surface (likely a module like
   `scripts/reviewed_diff_tracker.py` or similar — find it via grep) that
   exposes:
   - "Given an absolute path in repo X at time T, list owning unit ids."
   - "Given a unit id, return its current review_state."
   - "Given a unit id and the current session id, is this session the lessee?"
3. Existing tests for the tracker (`tests/test_*tracker*` etc.) are green on
   `main`.

If any of those is missing, **stop and confirm with the user**. Do not
guess at schema or invent helpers — the overhaul author may have shipped
a different surface.

## Implementation outline

All paths below are relative to the repo root.

### Files to touch

| File | Change |
|---|---|
| `plugins/review-validate-fix/skills/review-validate-fix/scripts/workspace_diff.py` | Bump `SCHEMA_VERSION` to `2`; add tracker lookup helper; populate the three new fields in the `changed_paths.append({...})` block (~line 145). |
| `plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_run_finalize.py` | If the tracker module needs explicit teardown (e.g. closing a SQLite handle), wire it into the finalize path. May not be needed depending on tracker API. |
| `tests/test_workspace_diff.py` | Add cases for: (a) tracker present, path covered by session-owned unit; (b) tracker present, path covered by foreign unit; (c) tracker present, path uncovered; (d) tracker absent (regression — payload must still be valid with `null` / empty list values). |
| `plugins/review-validate-fix/skills/rvf-analyze/references/rvf-analyze.md` | Brief note that `causality.json` consumers can now distinguish session-owned vs background paths; the analysis agent should call this out when it shows up in `## 工作区改动`. |

### Backward behavior (mandatory)

A run **without** a tracker present (legacy or non-tracked repos) must still
produce a valid `workspace-diff.json`. In that case:
- `tracker_unit_ids` → `[]`
- `owned_by_session` → `null`
- `review_state` → `null`

`SCHEMA_VERSION = 2` consumers must accept `null` / `[]` for all three fields.

The `--decline-finalize` and `half_broken` paths in `rvf_analyze.py`
already produce reduced-quality artifacts; they should not be additionally
gated on tracker presence. If the tracker query raises, log a diagnostic
into `workspace-diff.json::diagnostics[]` (already an existing list at
~line 118) and emit `null` / `[]` values.

## Cross-checks before merging

1. Lazy-import the tracker module inside `workspace_diff.compute()` so RVF
   runs in repos that don't ship the tracker module never pay an import cost
   or fail to load.
2. Run the full RVF unit test suite plus
   `python3 tests/test_review_support_scripts.py` (it's a standalone runner,
   not a pytest module — see test file's `main()`).
3. Manually run `$rvf-analyze` against one finalized run and confirm
   `causality.json` and `summary.md` still scaffold correctly.
4. If the tracker introduced new optional metadata at finalize time
   (e.g. session id), make sure `summary.json::finalize` records it so
   later runs of `$rvf-analyze` can re-derive ownership without re-querying
   the tracker.

## Out of scope for this work

- Changing the tracker itself or its API.
- Filtering `changed_paths[]` based on ownership (analysers want to **see**
  background WIP, just labelled).
- Anything affecting Phase A / A.1 / B.0 / C semantics — those are stable.
- Backfilling tracker fields for historic `workspace-diff.json` files.

## Why this was deferred (decision record)

When the original plan was drafted (April-May 2026), the overhaul Phase 1
had merged but Phase 2+ was speculative. The plan author decided that
shipping Phase A + A.1 + B.0 + C without these tracker fields was strictly
better than blocking on the overhaul:

- Phase A produces a self-contained, useful `workspace-diff.json` already.
- The three new fields are **augmentations**, not preconditions.
- Forcing an early tracker integration would create a hard dependency on a
  module that hadn't been designed yet, risking churn or worse — encouraging
  a parallel mini-tracker just for RVF.

Quoting the original plan's "## 实施分期" section, item Phase B:

> **Phase B（仅在 overhaul Phase 2+ 合 main 后追加，不阻塞 Phase A / A.1）**：
>   1. 给 `workspace-diff.json::changed_paths[]` 增补可选字段
>      `tracker_unit_ids` / `owned_by_session` / `review_state`，让分析者
>      一眼看清"这次 diff 里哪些是 RVF 新引入、哪些是背景 WIP"。
>   2. 若 Slice 2-A 重组了 `session_manifest.parse_apply_patch` 的位置，
>      更新 `trajectory_distill` 的 import 路径（一行改动）。

Item 2 (a possible one-line `trajectory_distill` import path adjustment) is
**not** part of this Phase B work as scoped here — it's an unrelated
maintenance touch that happens if and when the overhaul reorganises
`session_manifest`. Handle it separately when (if) it actually breaks.
