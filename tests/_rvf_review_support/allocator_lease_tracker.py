#!/usr/bin/env python3
"""Slice-3 allocator / lease / diff-tracker tests.

Bounded extraction from tests/test_review_support_scripts.py. The 12
cross-cutting deps (run/init_repo/_slice_2b_*/path constants/...) live
in the aggregator and are pushed in via inject() before the registry
runs, avoiding a circular import with the __main__ script.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# Injected by the aggregator (tests/test_review_support_scripts.py).
__all__ = ['test_allocate_review_scope_busy_timeout_degrades', 'test_allocate_review_scope_concurrent_writers_serialize', 'test_allocate_review_scope_disable_env_short_circuits', 'test_allocate_review_scope_dry_run_does_not_create_lease', 'test_allocate_review_scope_emits_valid_tracker_scope_json', 'test_allocate_review_scope_empty_returns_no_unassigned_review_scope', 'test_allocate_review_scope_excludes_active_leased_units', 'test_allocate_review_scope_inserts_lease_and_marks_units_assigned', 'test_allocate_review_scope_output_consumed_by_prepare_run', 'test_allocate_review_scope_preserves_untracked_file_under_new_directory', 'test_allocate_review_scope_prunes_stale_leases_first', 'test_allocate_review_scope_writes_paths_and_hunks', 'test_allocator_event_appended_to_events_jsonl', 'test_complete_review_scope_does_not_complete_failed_released_lease', 'test_complete_review_scope_keeps_different_scope_active_lease', 'test_complete_review_scope_keeps_partial_edit_claim_pending', 'test_complete_review_scope_supersedes_overlapping_active_lease', 'test_complete_review_scope_unions_contract_and_lease_units', 'test_fork_first_stop_takeover_transfers_unleased_units', 'test_fork_takeover_skips_actively_leased_units', 'test_heartbeat_refreshes_tracker_lease_and_records_backend', 'test_heartbeat_treats_same_second_expiry_as_expired', 'test_invalidate_reviewed_units_for_run_reopens_and_is_idempotent', 'test_invalidate_reviewed_units_for_run_is_run_scoped', 'test_invalidate_reviewed_units_for_run_excludes_tombstoned', 'test_latest_reviewed_run_for_worktree_resolution', 'test_lease_acquire_concurrent_writers_serialize', 'test_lease_acquire_creates_lease_and_assigns_units', 'test_lease_acquire_prunes_stale_leases_first', 'test_lease_acquire_rejects_tombstoned_unit', 'test_lease_acquire_rejects_when_any_unit_already_leased', 'test_lease_participants_finish_does_not_release_shared_lease', 'test_lease_refresh_extends_expires_at', 'test_lease_refresh_returns_expired_when_past_ttl', 'test_lease_release_completed_marks_units_reviewed', 'test_lease_release_idempotent', 'test_manual_rvf_run_ensures_table_for_existing_v2_db', 'test_manual_rvf_run_find_respects_ttl', 'test_manual_rvf_run_find_returns_latest_completed_at', 'test_manual_rvf_run_inserts_row_and_emits_event', 'test_manual_rvf_run_upserts_on_pk_conflict', 'test_manual_takeover_cli_records_takeover', 'test_manual_takeover_rejects_missing_parent_session', 'test_manual_takeover_skips_actively_leased_units', 'test_manual_takeover_transfers_unleased_units', 'test_record_manual_run_cli_writes_row', 'test_run_alternative_reviewer_releases_lease_on_codex_backend_challenge', 'test_run_alternative_reviewer_releases_lease_on_normal_exit', 'test_run_alternative_reviewer_releases_lease_on_timeout', 'test_run_alternative_reviewer_shared_lease_does_not_release_on_exit', 'test_run_alternative_reviewer_sigterm_kills_child_before_release', 'test_scope_hash_is_sha256_of_sorted_unit_ids', 'test_stale_prune_does_not_release_unit_reacquired_by_fresh_lease', 'test_sweep_stale_no_op_when_all_active_leases_fresh', 'test_sweep_stale_releases_expired_active_leases', 'test_sweep_stale_releases_same_second_expired_lease', 'test_tracker_schema_v2_migrates_lease_participants_table', 'test_tracker_schema_v4_rebuilds_legacy_tombstoned_review_state', 'test_declare_units_review_not_required_survives_tracker_refresh', 'test_declare_units_review_not_required_is_not_inherited_by_changed_hunk', 'test_allocate_review_scope_unions_prior_scope_after_lease_expiry', 'test_invalidate_reviewed_units_for_run_excludes_review_not_required_declarations', 'test_allocate_review_scope_union_list_excludes_units_held_by_active_lease', 'test_kanban_followup_trigger_transcript_scan_reads_only_bounded_tail', 'test_kanban_followup_trigger_presence_on_codex_rollout_without_uuid_chain_is_unknown_not_absent']


def inject(**deps: object) -> None:
    """Bind shared helpers/constants from the aggregator into this
    module's globals so the moved tests resolve them at call time."""
    globals().update(deps)


def _alloc_invoke(
    *,
    repo: Path,
    log_root: Path,
    session_id: str,
    run_id: str,
    reviewer_id: str | None = "reviewer-a",
    output_scope: Path | None = None,
    parent_session_id: str | None = None,
    holder_kind: str = "reviewer",
    lease_ttl_seconds: int | None = None,
    dry_run: bool = False,
    extra_env: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> dict[str, object]:
    cmd = [
        sys.executable,
        str(DIFF_TRACKER),
        "allocate-review-scope",
        "--repo",
        str(repo),
        "--session-id",
        session_id,
        "--run-id",
        run_id,
        "--log-root",
        str(log_root),
        "--print-result",
    ]
    if reviewer_id is not None:
        cmd.extend(["--reviewer-id", reviewer_id])
    if output_scope is not None:
        cmd.extend(["--output-scope", str(output_scope)])
    if parent_session_id is not None:
        cmd.extend(["--parent-session-id", parent_session_id])
    if holder_kind != "reviewer":
        cmd.extend(["--holder-kind", holder_kind])
    if lease_ttl_seconds is not None:
        cmd.extend(["--lease-ttl-seconds", str(lease_ttl_seconds)])
    if dry_run:
        cmd.append("--dry-run")
    env = {**os.environ}
    if extra_env:
        env.update(extra_env)
    completed = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False, timeout=timeout)
    if completed.returncode != 0:
        raise AssertionError(
            f"reviewable_unit_diff_tracker.py allocate-review-scope failed (exit {completed.returncode}):\n"
            f"stdout=\n{completed.stdout}\nstderr=\n{completed.stderr}"
        )
    last_line = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "{}"
    return json.loads(last_line)


def _alloc_db_path(log_root: Path, repo_key: str) -> Path:
    return log_root / "diff-tracker" / "repos" / repo_key / "tracker.sqlite3"


def _alloc_events_path(log_root: Path, repo_key: str) -> Path:
    return log_root / "diff-tracker" / "repos" / repo_key / "events.jsonl"


def _alloc_open_db(log_root: Path, repo_key: str):
    import sqlite3 as _sqlite

    return _sqlite.connect(str(_alloc_db_path(log_root, repo_key)))


def test_allocate_review_scope_emits_valid_tracker_scope_json(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    output_scope = tmp / "tracker-scope.json"
    result = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="sess-T1",
        run_id="run-T1",
        output_scope=output_scope,
    )
    assert result["status"] == "allocated"
    assert result["acquired"] is True
    assert result["reason"] == "unassigned_review_scope_available"
    assert result["reason_legacy_alias"] == "session_owned_dirty"
    assert output_scope.exists()
    payload = json.loads(output_scope.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("rvf_prepare_review_run", PREPARE_REVIEW_RUN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    loaded = module.load_tracker_scope(output_scope)
    assert loaded["unit_ids"] == payload["unit_ids"]
    for unit_id in payload["unit_ids"]:
        assert isinstance(unit_id, str)
        assert len(unit_id) == 64
        int(unit_id, 16)  # raises ValueError if not hex
    assert payload["lease_id"].startswith("lse-")
    assert payload["scope_hash"].startswith("sha256:")
    assert len(payload["scope_hash"].split(":", 1)[1]) == 64


def test_allocate_review_scope_empty_returns_no_unassigned_review_scope(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    # Wipe the dirty state from init_repo so the worktree is clean.
    run(["git", "checkout", "--", "tracked.txt"], cwd=repo)
    (repo / "new.txt").unlink()
    log_root = tmp / "logs"
    output_scope = tmp / "tracker-scope.json"
    result = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="sess-T2",
        run_id="run-T2",
        output_scope=output_scope,
    )
    assert result["status"] == "empty"
    assert result["acquired"] is False
    assert result["reason"] == "no_unassigned_review_scope"
    assert result["reason_legacy_alias"] == "no_session_owned_dirty"
    assert not output_scope.exists()


def test_allocate_review_scope_preserves_untracked_file_under_new_directory(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    run(["git", "checkout", "--", "tracked.txt"], cwd=repo)
    (repo / "new.txt").unlink()
    nested = repo / "newdir" / "file.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("# New file\n\nowned by apply_patch\n", encoding="utf-8")
    log_root = tmp / "logs"

    registered = module.register_claims(
        repo=repo,
        session_id="sess-untracked-dir",
        run_id="run-register",
        worktree=repo,
        branch=None,
        owned_paths=["newdir/file.md"],
        apply_patch_paths={"newdir/file.md"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert registered.status == "ok"

    allocated = module.allocate_review_scope(
        repo=repo,
        session_id="sess-untracked-dir",
        run_id="run-allocate",
        reviewer_id="reviewer-untracked-dir",
        log_root_override=log_root,
    )

    assert allocated["status"] == "allocated"
    assert allocated["scope"]["paths"] == ["newdir/file.md"]
    repo_key = allocated["repo_key"]
    conn = _alloc_open_db(log_root, repo_key)
    try:
        rows = conn.execute(
            """
            SELECT path, kind, observed_state, review_state
              FROM units
             WHERE path IN ('newdir/file.md', 'newdir/')
             ORDER BY path, kind
            """
        ).fetchall()
    finally:
        conn.close()
    assert ("newdir/file.md", "untracked_file", "dirty", "assigned") in rows
    assert all(row[0] != "newdir/" for row in rows)


def test_allocate_review_scope_excludes_active_leased_units(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    first = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="sess-T3a",
        run_id="run-T3a",
        output_scope=tmp / "first.json",
    )
    assert first["status"] == "allocated"
    repo_key = first["repo_key"]
    # Manually flip the lease's units back to 'available' while leaving the
    # active lease in place. This forces step 4 to include them as candidates
    # so step 5's anti-join is exercised — exactly the race the leased
    # exclusion is supposed to absorb.
    leased_unit_ids = first["scope"]["unit_ids"]
    conn = _alloc_open_db(log_root, repo_key)
    try:
        placeholders = ",".join("?" * len(leased_unit_ids))
        conn.execute(
            f"UPDATE units SET review_state='available' WHERE unit_id IN ({placeholders})",
            tuple(leased_unit_ids),
        )
        conn.commit()
    finally:
        conn.close()
    # Same session re-allocates: every candidate is now an actively-leased
    # unit so the result is empty AND leased_excluded_count covers them all.
    second = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="sess-T3a",
        run_id="run-T3b",
    )
    assert second["status"] == "empty"
    assert second["leased_excluded_count"] >= 1


def test_allocate_review_scope_inserts_lease_and_marks_units_assigned(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    result = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="sess-T4",
        run_id="run-T4",
        output_scope=tmp / "scope.json",
    )
    assert result["status"] == "allocated"
    repo_key = result["repo_key"]
    conn = _alloc_open_db(log_root, repo_key)
    try:
        leases = list(conn.execute("SELECT lease_id, state FROM leases"))
        assert leases, "lease row should exist"
        assert all(state == "active" for _, state in leases)
        lease_units = list(conn.execute("SELECT unit_id FROM lease_units"))
        assert {row[0] for row in lease_units} == set(result["scope"]["unit_ids"])
        unit_states = list(
            conn.execute(
                f"SELECT review_state FROM units WHERE unit_id IN ({','.join('?' * len(lease_units))})",
                tuple(row[0] for row in lease_units),
            )
        )
        assert all(state == "assigned" for (state,) in unit_states)
    finally:
        conn.close()


def test_allocate_review_scope_prunes_stale_leases_first(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    # First allocator run lays down a real lease.
    first = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="sess-T5",
        run_id="run-T5",
        output_scope=tmp / "first.json",
    )
    repo_key = first["repo_key"]
    # Manually expire the lease in the DB so the next allocator run treats it
    # as stale and frees its units.
    conn = _alloc_open_db(log_root, repo_key)
    try:
        conn.execute("UPDATE leases SET expires_at='1970-01-01T00:00:00Z'")
        conn.commit()
    finally:
        conn.close()
    second = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="sess-T5b",
        run_id="run-T5b",
        output_scope=tmp / "second.json",
    )
    assert second["status"] == "allocated"
    conn = _alloc_open_db(log_root, repo_key)
    try:
        first_state = list(conn.execute("SELECT state FROM leases WHERE lease_id=?", (first["lease_id"],)))
        assert first_state and first_state[0][0] == "stale-released"
        active = list(conn.execute("SELECT lease_id FROM leases WHERE state='active'"))
        assert active and active[0][0] == second["lease_id"]
    finally:
        conn.close()


def test_allocate_review_scope_concurrent_writers_serialize(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    snippet = (
        "import os, sys, time, json\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from pathlib import Path\n"
        "os.environ.setdefault('RVF_TRACKER_BUSY_TIMEOUT_MS', '30000')\n"
        "import core.session_scope_allocation.reviewable_unit_diff_tracker as dt\n"
        f"log_root = Path({str(log_root)!r})\n"
        f"repo = Path({str(repo)!r})\n"
        "session = sys.argv[1]\n"
        "wait_until = float(os.environ['CONCURRENT_WAIT_UNTIL'])\n"
        "remaining = wait_until - time.time()\n"
        "if remaining > 0:\n"
        "    time.sleep(remaining)\n"
        "result = dt.allocate_review_scope(\n"
        "    repo=repo, session_id=session, run_id=session,\n"
        "    reviewer_id='r-' + session,\n"
        "    log_root_override=log_root,\n"
        ")\n"
        "print(json.dumps(result, default=str))\n"
    )
    wait_until = time.time() + 1.5
    env = {**os.environ, "CONCURRENT_WAIT_UNTIL": f"{wait_until:.6f}"}
    procs = []
    for session in ("conc-A", "conc-B"):
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", snippet, session],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        )
    outputs = [proc.communicate(timeout=60) for proc in procs]
    payloads = []
    for stdout, stderr in outputs:
        if stderr.strip():
            raise AssertionError(stderr.strip())
        payloads.append(json.loads(stdout.strip().splitlines()[-1]))
    statuses = [p["status"] for p in payloads]
    assert sorted(statuses) in (["allocated", "empty"], ["allocated", "allocated"])
    repo_key = next(p["repo_key"] for p in payloads if p["repo_key"])
    conn = _alloc_open_db(log_root, repo_key)
    try:
        rows = list(conn.execute("SELECT unit_id, COUNT(*) FROM lease_units GROUP BY unit_id"))
        for unit_id, count in rows:
            assert count == 1, f"unit {unit_id} held by {count} leases"
    finally:
        conn.close()


def test_fork_first_stop_takeover_transfers_unleased_units(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    parent = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="parent",
        run_id="parent-run",
        output_scope=tmp / "parent.json",
    )
    assert parent["status"] == "allocated"
    repo_key = parent["repo_key"]
    # Free the parent's lease so its units re-enter the candidate pool.
    conn = _alloc_open_db(log_root, repo_key)
    try:
        conn.execute("UPDATE leases SET state='completed' WHERE lease_id=?", (parent["lease_id"],))
        conn.execute(
            "UPDATE units SET review_state='available' WHERE unit_id IN "
            "(SELECT unit_id FROM lease_units WHERE lease_id=?)",
            (parent["lease_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    child = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="child",
        run_id="child-run",
        parent_session_id="parent",
        output_scope=tmp / "child.json",
    )
    assert child["status"] == "allocated"
    assert child["scope"]["takeover_from_session_id"] == "parent"
    conn = _alloc_open_db(log_root, repo_key)
    try:
        parent_kinds = {
            row[0] for row in conn.execute(
                "SELECT assignment_kind FROM session_units WHERE session_id='parent'"
            )
        }
        child_kinds = {
            row[0] for row in conn.execute(
                "SELECT assignment_kind FROM session_units WHERE session_id='child'"
            )
        }
    finally:
        conn.close()
    assert parent_kinds == {"transferred"} or parent_kinds == set()
    assert "takeover" in child_kinds


def test_fork_takeover_skips_actively_leased_units(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    parent = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="parent2",
        run_id="parent2-run",
        output_scope=tmp / "parent2.json",
    )
    assert parent["status"] == "allocated"
    repo_key = parent["repo_key"]
    parent_unit_ids = parent["scope"]["unit_ids"]
    assert len(parent_unit_ids) >= 2
    # Keep parent's lease active over only one unit by deleting the other
    # lease_units row. The dropped unit goes back to 'available'.
    pinned_unit, freed_unit = parent_unit_ids[0], parent_unit_ids[1]
    conn = _alloc_open_db(log_root, repo_key)
    try:
        conn.execute("DELETE FROM lease_units WHERE lease_id=? AND unit_id=?", (parent["lease_id"], freed_unit))
        conn.execute("UPDATE units SET review_state='available' WHERE unit_id=?", (freed_unit,))
        conn.commit()
    finally:
        conn.close()
    child = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="child2",
        run_id="child2-run",
        parent_session_id="parent2",
        output_scope=tmp / "child2.json",
    )
    assert child["status"] == "allocated"
    transferred_unit_ids = set(child["scope"]["unit_ids"])
    assert pinned_unit not in transferred_unit_ids
    assert freed_unit in transferred_unit_ids


def test_scope_hash_is_sha256_of_sorted_unit_ids(tmp: Path) -> None:
    import hashlib as _hash

    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    first = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="sh-A",
        run_id="run-A",
        output_scope=tmp / "first.json",
    )
    assert first["status"] == "allocated"
    expected = "sha256:" + _hash.sha256(
        "\n".join(sorted(first["scope"]["unit_ids"])).encode("utf-8")
    ).hexdigest()
    assert first["scope_hash"] == expected
    # Second invocation over the same dirty paths but from a fresh log_root
    # must produce the same scope_hash because the unit_ids are
    # canonical-patch-hash derived.
    log_root_b = tmp / "logs-b"
    second = _alloc_invoke(
        repo=repo,
        log_root=log_root_b,
        session_id="sh-B",
        run_id="run-B",
        output_scope=tmp / "second.json",
    )
    assert second["status"] == "allocated"
    assert second["scope_hash"] == first["scope_hash"]


def test_allocator_event_appended_to_events_jsonl(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    result = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="sess-event",
        run_id="run-event",
        output_scope=tmp / "scope.json",
    )
    assert result["status"] == "allocated"
    events_path = _alloc_events_path(log_root, result["repo_key"])
    records = read_jsonl(events_path)
    matching = [r for r in records if r.get("event") == "allocate_review_scope"]
    assert matching, f"no allocate_review_scope event in {records!r}"
    record = matching[-1]
    assert record["rvf_state_phase"] == "review"
    assert record["lease_id"] == result["lease_id"]
    assert record["scope_hash"] == result["scope_hash"]
    assert record["unit_count"] == len(result["scope"]["unit_ids"])
    assert record["paths"] == result["scope"]["paths"]
    assert record["reason_code"] == "unassigned_review_scope_available"
    assert record["reason_code_legacy_alias"] == "session_owned_dirty"


def test_manual_rvf_run_inserts_row_and_emits_event(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    result = module.record_manual_rvf_run(
        repo=repo,
        session_id="manual-session",
        run_id="manual-run",
        scope_hash="sha256:manual-a",
        completed_at="2026-05-05T00:00:00Z",
        log_root_override=log_root,
    )
    assert result["status"] == "recorded"
    conn = _alloc_open_db(log_root, result["repo_key"])
    try:
        rows = list(conn.execute("SELECT session_id, run_id, scope_hash, completed_at FROM manual_rvf_runs"))
    finally:
        conn.close()
    assert rows == [("manual-session", "manual-run", "sha256:manual-a", "2026-05-05T00:00:00Z")]
    events = read_jsonl(_alloc_events_path(log_root, result["repo_key"]))
    assert any(event.get("event") == "manual_rvf_run_recorded" for event in events)


def test_manual_rvf_run_upserts_on_pk_conflict(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    first = module.record_manual_rvf_run(
        repo=repo,
        session_id="manual-session",
        run_id="manual-run",
        scope_hash="sha256:old",
        completed_at="2026-05-05T00:00:00Z",
        log_root_override=log_root,
    )
    module.record_manual_rvf_run(
        repo=repo,
        session_id="manual-session",
        run_id="manual-run",
        scope_hash="sha256:new",
        completed_at="2026-05-05T00:10:00Z",
        log_root_override=log_root,
    )
    conn = _alloc_open_db(log_root, first["repo_key"])
    try:
        rows = list(conn.execute("SELECT scope_hash, completed_at FROM manual_rvf_runs"))
    finally:
        conn.close()
    assert rows == [("sha256:new", "2026-05-05T00:10:00Z")]


def test_manual_rvf_run_find_returns_latest_completed_at(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    module.record_manual_rvf_run(
        repo=repo,
        session_id="manual-old",
        run_id="run-old",
        scope_hash="sha256:same",
        completed_at="2026-05-05T00:00:00Z",
        log_root_override=log_root,
    )
    module.record_manual_rvf_run(
        repo=repo,
        session_id="manual-new",
        run_id="run-new",
        scope_hash="sha256:same",
        completed_at="2026-05-05T00:10:00Z",
        log_root_override=log_root,
    )
    match = module.find_manual_rvf_run_for_scope_hash(
        repo=repo,
        scope_hash="sha256:same",
        log_root_override=log_root,
    )
    assert match == {
        "session_id": "manual-new",
        "run_id": "run-new",
        "completed_at": "2026-05-05T00:10:00Z",
    }


def test_manual_rvf_run_find_respects_ttl(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    module.record_manual_rvf_run(
        repo=repo,
        session_id="manual-session",
        run_id="manual-run",
        scope_hash="sha256:ttl",
        completed_at="2026-05-05T00:00:00Z",
        log_root_override=log_root,
    )
    assert (
        module.find_manual_rvf_run_for_scope_hash(
            repo=repo,
            scope_hash="sha256:ttl",
            ttl_seconds=30,
            now="2026-05-05T00:01:00Z",
            log_root_override=log_root,
        )
        is None
    )


def test_manual_rvf_run_ensures_table_for_existing_v2_db(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    initial = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="manual-schema-session",
        run_id="manual-schema-run",
        output_scope=tmp / "manual-schema.json",
    )
    conn = _alloc_open_db(log_root, initial["repo_key"])
    try:
        conn.execute("DROP TABLE manual_rvf_runs")
        conn.execute(f"PRAGMA user_version = {module.SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()

    module.record_manual_rvf_run(
        repo=repo,
        session_id="manual-schema-session",
        run_id="manual-schema-run",
        scope_hash="sha256:manual-schema",
        completed_at="2026-05-05T00:00:00Z",
        log_root_override=log_root,
    )

    conn = _alloc_open_db(log_root, initial["repo_key"])
    try:
        rows = list(conn.execute("SELECT session_id, run_id, scope_hash FROM manual_rvf_runs"))
    finally:
        conn.close()
    assert rows == [("manual-schema-session", "manual-schema-run", "sha256:manual-schema")]


def test_manual_takeover_transfers_unleased_units(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    parent = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="manual-parent",
        run_id="manual-parent-run",
        output_scope=tmp / "parent.json",
    )
    conn = _alloc_open_db(log_root, parent["repo_key"])
    try:
        conn.execute("UPDATE leases SET state='completed' WHERE lease_id=?", (parent["lease_id"],))
        conn.execute(
            "UPDATE units SET review_state='available' WHERE unit_id IN "
            "(SELECT unit_id FROM lease_units WHERE lease_id=?)",
            (parent["lease_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    takeover = module.manual_takeover(
        repo=repo,
        parent_session_id="manual-parent",
        current_session_id="manual-child",
        run_id="manual-child-run",
        log_root_override=log_root,
    )
    assert takeover["reason"] == "manual_takeover_completed"
    assert set(takeover["transferred_unit_ids"]) == set(parent["scope"]["unit_ids"])


def test_manual_takeover_skips_actively_leased_units(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    parent = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="manual-parent-active",
        run_id="manual-parent-active-run",
        output_scope=tmp / "parent-active.json",
    )
    unit_ids = parent["scope"]["unit_ids"]
    assert len(unit_ids) >= 2
    pinned_unit, freed_unit = unit_ids[0], unit_ids[1]
    conn = _alloc_open_db(log_root, parent["repo_key"])
    try:
        conn.execute("DELETE FROM lease_units WHERE lease_id=? AND unit_id=?", (parent["lease_id"], freed_unit))
        conn.execute("UPDATE units SET review_state='available' WHERE unit_id=?", (freed_unit,))
        conn.commit()
    finally:
        conn.close()
    takeover = module.manual_takeover(
        repo=repo,
        parent_session_id="manual-parent-active",
        current_session_id="manual-child-active",
        run_id="manual-child-active-run",
        log_root_override=log_root,
    )
    transferred = set(takeover["transferred_unit_ids"])
    assert pinned_unit not in transferred
    assert freed_unit in transferred


def test_manual_takeover_rejects_missing_parent_session(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    try:
        module.manual_takeover(
            repo=repo,
            parent_session_id="missing-parent",
            current_session_id="manual-child-missing-parent",
            run_id="manual-child-run",
            log_root_override=log_root,
        )
    except RuntimeError as exc:
        assert "manual takeover parent session not found: missing-parent" in str(exc)
    else:
        raise AssertionError("manual_takeover accepted a missing parent session")

    common_dir = module.git_common_dir(repo.resolve())
    assert common_dir is not None
    repo_key_value = module.repo_key(common_dir)
    conn = _alloc_open_db(log_root, repo_key_value)
    try:
        rows = list(conn.execute("SELECT session_id FROM sessions ORDER BY session_id"))
    finally:
        conn.close()
    assert rows == []


def test_manual_takeover_cli_records_takeover(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    parent = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="manual-cli-parent",
        run_id="manual-cli-parent-run",
        output_scope=tmp / "manual-cli-parent.json",
    )
    conn = _alloc_open_db(log_root, parent["repo_key"])
    try:
        conn.execute("UPDATE leases SET state='completed' WHERE lease_id=?", (parent["lease_id"],))
        conn.execute(
            "UPDATE units SET review_state='available' WHERE unit_id IN "
            "(SELECT unit_id FROM lease_units WHERE lease_id=?)",
            (parent["lease_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    completed = subprocess.run(
        [
            sys.executable,
            str(DIFF_TRACKER),
            "manual-takeover",
            "--repo",
            str(repo),
            "--parent-session-id",
            "manual-cli-parent",
            "--current-session-id",
            "manual-cli-child",
            "--run-id",
            "manual-cli-child-run",
            "--log-root",
            str(log_root),
            "--print-result",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["reason"] == "manual_takeover_completed"
    assert set(payload["transferred_unit_ids"]) == set(parent["scope"]["unit_ids"])


def test_record_manual_run_cli_writes_row(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    completed = subprocess.run(
        [
            sys.executable,
            str(DIFF_TRACKER),
            "record-manual-run",
            "--repo",
            str(repo),
            "--session-id",
            "manual-cli-session",
            "--run-id",
            "manual-cli-run",
            "--scope-hash",
            "sha256:manual-cli",
            "--completed-at",
            "2026-05-05T00:00:00Z",
            "--log-root",
            str(log_root),
            "--print-result",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    conn = _alloc_open_db(log_root, payload["repo_key"])
    try:
        rows = list(conn.execute("SELECT session_id, run_id, scope_hash FROM manual_rvf_runs"))
    finally:
        conn.close()
    assert rows == [("manual-cli-session", "manual-cli-run", "sha256:manual-cli")]


def test_allocate_review_scope_disable_env_short_circuits(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    result = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="sess-disable",
        run_id="run-disable",
        output_scope=tmp / "scope.json",
        extra_env={"RVF_TRACKER_DISABLE": "1"},
    )
    assert result["status"] == "disabled"
    assert not (tmp / "scope.json").exists()
    # No SQLite file should have been created.
    assert not _alloc_db_path(log_root, "anything").parent.parent.exists()


def test_allocate_review_scope_busy_timeout_degrades(tmp: Path) -> None:
    import threading
    import sqlite3 as _sqlite

    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    # Seed the SQLite file by running the allocator once normally.
    seeded = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="sess-seed",
        run_id="run-seed",
        output_scope=tmp / "seed.json",
    )
    assert seeded["status"] == "allocated"
    db_path = _alloc_db_path(log_root, seeded["repo_key"])
    blocker = _sqlite.connect(str(db_path), isolation_level=None, timeout=30.0)
    release = threading.Event()
    try:
        blocker.execute("BEGIN IMMEDIATE")
        result = _alloc_invoke(
            repo=repo,
            log_root=log_root,
            session_id="sess-busy",
            run_id="run-busy",
            output_scope=tmp / "busy.json",
            extra_env={"RVF_TRACKER_BUSY_TIMEOUT_MS": "300"},
            timeout=30.0,
        )
    finally:
        try:
            blocker.execute("ROLLBACK")
        except _sqlite.Error:
            pass
        blocker.close()
        release.set()
    assert result["status"] == "lock_timeout"


def test_allocate_review_scope_writes_paths_and_hunks(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    result = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="sess-T13",
        run_id="run-T13",
        output_scope=tmp / "scope.json",
    )
    assert result["status"] == "allocated"
    scope = result["scope"]
    assert scope["paths"] == sorted(set(scope["paths"]))
    for hunk in scope["hunks"]:
        assert "unit_id" in hunk
        assert "path" in hunk
        assert "hunk_header" in hunk


def test_allocate_review_scope_dry_run_does_not_create_lease(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    output_scope = tmp / "should-not-exist.json"
    result = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        session_id="sess-dry",
        run_id="run-dry",
        reviewer_id=None,
        output_scope=output_scope,
        dry_run=True,
    )
    assert result["status"] == "dry_run"
    assert result["would_acquire"] is True
    assert result["candidate_unit_count"] > 0
    assert not output_scope.exists()
    repo_key = result["repo_key"]
    conn = _alloc_open_db(log_root, repo_key)
    try:
        rows = list(conn.execute("SELECT COUNT(*) FROM leases"))
    finally:
        conn.close()
    assert rows[0][0] == 0


def test_allocate_review_scope_output_consumed_by_prepare_run(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    output_scope = tmp / "tracker-scope.json"
    allocator = _alloc_invoke(
        repo=repo,
        log_root=log_root,
        # 分配与开审使用同一合成会话，否则冻结候选会正确排除他会话的已认领路径。
        session_id="synthetic-native-session",
        run_id="run-T15",
        output_scope=output_scope,
    )
    assert allocator["status"] == "allocated"
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=output_scope, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    contract = json.loads((artifacts_dir / "inputs" / "scope.contract.json").read_text(encoding="utf-8"))
    assert contract["version"] == 3
    assert contract["primary_units"] == sorted(allocator["scope"]["unit_ids"])
    assert contract["tracker_lease_id"] == allocator["lease_id"]
    assert contract["tracker_scope_hash"] == allocator["scope_hash"]


def _lease_seed(tmp: Path) -> tuple[object, Path, Path, list[str], str]:
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    seeded = module.allocate_review_scope(
        repo=repo,
        session_id="lease-seed",
        run_id="lease-seed-run",
        reviewer_id=None,
        dry_run=True,
        log_root_override=log_root,
    )
    assert seeded["status"] == "dry_run"
    conn = _alloc_open_db(log_root, seeded["repo_key"])
    try:
        unit_ids = [
            row[0]
            for row in conn.execute(
                "SELECT unit_id FROM units WHERE review_state='available' ORDER BY path, unit_id"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert unit_ids
    return module, repo, log_root, unit_ids, seeded["repo_key"]


def _lease_contract(
    path: Path,
    *,
    repo: Path,
    unit_ids: list[str],
    tracker_lease_id: str | None = None,
) -> Path:
    payload = {
        "version": 2,
        "run_id": "lease-reviewer-run",
        "repo": str(repo),
        "primary_units": unit_ids,
        "tracker_lease_id": tracker_lease_id,
        "tracker_scope_hash": "sha256:" + "a" * 64,
        "session_manifest_path": None,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _lease_unit_states(log_root: Path, repo_key: str, unit_ids: list[str]) -> dict[str, str]:
    conn = _alloc_open_db(log_root, repo_key)
    try:
        placeholders = ",".join("?" for _ in unit_ids)
        rows = conn.execute(
            f"SELECT unit_id, review_state FROM units WHERE unit_id IN ({placeholders})",
            tuple(unit_ids),
        ).fetchall()
        return {unit_id: state for unit_id, state in rows}
    finally:
        conn.close()


def _lease_rows(log_root: Path, repo_key: str) -> list[tuple[str, str]]:
    conn = _alloc_open_db(log_root, repo_key)
    try:
        return list(conn.execute("SELECT lease_id, state FROM leases ORDER BY created_at, lease_id"))
    finally:
        conn.close()


def _lease_unit_count(log_root: Path, repo_key: str, lease_id: str) -> int:
    conn = _alloc_open_db(log_root, repo_key)
    try:
        row = conn.execute("SELECT COUNT(*) FROM lease_units WHERE lease_id=?", (lease_id,)).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _lease_participant_states(log_root: Path, repo_key: str, lease_id: str) -> dict[str, str]:
    conn = _alloc_open_db(log_root, repo_key)
    try:
        rows = conn.execute(
            """
            SELECT reviewer_id, state
              FROM lease_participants
             WHERE lease_id=?
             ORDER BY reviewer_id
            """,
            (lease_id,),
        ).fetchall()
        return {reviewer_id: state for reviewer_id, state in rows}
    finally:
        conn.close()


def test_tracker_schema_v2_migrates_lease_participants_table(tmp_path: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp_path)
    db_path = _alloc_db_path(log_root, repo_key)
    conn = _alloc_open_db(log_root, repo_key)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_lease_participants_state")
        conn.execute("DROP TABLE IF EXISTS lease_participants")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()

    acquired = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-migrate",
        run_id="lease-run-migrate",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:1],
        log_root_override=log_root,
    )

    assert acquired["acquired"] is True
    conn = _alloc_open_db(log_root, repo_key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == module.SCHEMA_VERSION
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lease_participants'"
        ).fetchone()
        rvf_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='rvf_fix_attempts'"
        ).fetchone()
        index = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_lease_participants_state'"
        ).fetchone()
    finally:
        conn.close()
    assert db_path.exists()
    assert table is not None
    assert rvf_table is not None
    assert index is not None


def test_tracker_schema_v4_rebuilds_legacy_tombstoned_review_state(tmp: Path) -> None:
    import sqlite3 as _sqlite

    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    legacy_unit = unit_ids[0]
    db_path = _alloc_db_path(log_root, repo_key)
    conn = _alloc_open_db(log_root, repo_key)
    try:
        original_rows = conn.execute(
            """
            SELECT unit_id, branch_key, worktree_key, path, old_path, kind, change_type,
                   preimage_blob, postimage_hash, hunk_header, canonical_patch_hash,
                   first_observed_at, last_observed_at, observed_state
              FROM units
            """
        ).fetchall()
        assert original_rows
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            """
            DROP TABLE units;
            CREATE TABLE units (
              unit_id              TEXT PRIMARY KEY,
              branch_key           TEXT,
              worktree_key         TEXT NOT NULL,
              path                 TEXT NOT NULL,
              old_path             TEXT,
              kind                 TEXT NOT NULL CHECK (kind IN
                                     ('tracked_hunk','untracked_file','deleted_file','binary_file','path_only')),
              change_type          TEXT NOT NULL CHECK (change_type IN ('add','modify','delete')),
              preimage_blob        TEXT,
              postimage_hash       TEXT,
              hunk_header          TEXT,
              canonical_patch_hash TEXT NOT NULL,
              first_observed_at    TEXT NOT NULL,
              last_observed_at     TEXT NOT NULL,
              observed_state       TEXT NOT NULL CHECK (observed_state IN ('dirty','committed','superseded')),
              review_state         TEXT NOT NULL CHECK (review_state IN ('available','assigned','reviewed','tombstoned')),
              FOREIGN KEY (branch_key)   REFERENCES branches(branch_key)   ON DELETE SET NULL,
              FOREIGN KEY (worktree_key) REFERENCES worktrees(worktree_key) ON DELETE CASCADE
            );
            CREATE INDEX idx_units_branch       ON units(branch_key, observed_state);
            CREATE INDEX idx_units_worktree     ON units(worktree_key, observed_state);
            CREATE INDEX idx_units_path         ON units(path);
            CREATE INDEX idx_units_review_state ON units(review_state);
            CREATE INDEX idx_units_canonical    ON units(canonical_patch_hash);
            PRAGMA user_version = 4;
            """
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO units(
                unit_id, branch_key, worktree_key, path, old_path, kind, change_type,
                preimage_blob, postimage_hash, hunk_header, canonical_patch_hash,
                first_observed_at, last_observed_at, observed_state, review_state
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available')
            """,
            original_rows,
        )
        conn.execute("UPDATE units SET review_state='tombstoned' WHERE unit_id=?", (legacy_unit,))
        conn.commit()
    finally:
        conn.close()

    result = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-v4-migrate",
        run_id="lease-run-v4-migrate",
        reviewer_id="reviewer-a",
        unit_ids=[legacy_unit],
        log_root_override=log_root,
    )

    assert result["acquired"] is False
    assert result["reason"] == "lease_unit_tombstoned"
    conn = _sqlite.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == module.SCHEMA_VERSION
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='units'"
        ).fetchone()[0]
        row = conn.execute(
            "SELECT review_state, is_tombstoned, tombstone_reason FROM units WHERE unit_id=?",
            (legacy_unit,),
        ).fetchone()
        try:
            conn.execute("UPDATE units SET review_state='tombstoned' WHERE unit_id=?", (legacy_unit,))
        except _sqlite.IntegrityError:
            rejected_review_tombstone = True
        else:
            rejected_review_tombstone = False
    finally:
        conn.close()

    assert "'tombstoned'" not in table_sql
    assert row == ("reviewed", 1, "legacy_review_state_tombstoned")
    assert rejected_review_tombstone is True


def test_lease_acquire_creates_lease_and_assigns_units(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    result = module.lease_acquire(
        repo=repo,
        session_id="lease-sess",
        run_id="lease-run",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:1],
        log_root_override=log_root,
    )
    assert result["acquired"] is True
    assert result["reason"] == "lease_acquired"
    assert _lease_unit_states(log_root, repo_key, unit_ids[:1]) == {unit_ids[0]: "assigned"}
    events = read_jsonl(_alloc_events_path(log_root, repo_key))
    assert any(event.get("event") == "lease_acquired" for event in events)


def test_lease_acquire_rejects_when_any_unit_already_leased(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    first_unit, second_unit = unit_ids[:2]
    first = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-a",
        run_id="lease-run-a",
        reviewer_id="reviewer-a",
        unit_ids=[first_unit],
        log_root_override=log_root,
    )
    assert first["acquired"] is True
    second = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-b",
        run_id="lease-run-b",
        reviewer_id="reviewer-b",
        unit_ids=[first_unit, second_unit],
        log_root_override=log_root,
    )
    assert second["acquired"] is False
    assert second["reason"] == "lease_unit_already_assigned"
    assert _lease_unit_states(log_root, repo_key, [second_unit])[second_unit] == "available"


def test_lease_acquire_rejects_tombstoned_unit(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    tombstoned_unit = unit_ids[0]
    conn = _alloc_open_db(log_root, repo_key)
    try:
        conn.execute(
            """
            UPDATE units
               SET is_tombstoned=1,
                   tombstoned_at='2026-05-05T00:00:00Z',
                   tombstone_reason='test_retired'
             WHERE unit_id=?
            """,
            (tombstoned_unit,),
        )
        conn.commit()
    finally:
        conn.close()

    result = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-tomb",
        run_id="lease-run-tomb",
        reviewer_id="reviewer-a",
        unit_ids=[tombstoned_unit],
        log_root_override=log_root,
    )

    assert result["acquired"] is False
    assert result["reason"] == "lease_unit_tombstoned"
    assert result["conflicting_unit_ids"] == [tombstoned_unit]
    assert _lease_unit_states(log_root, repo_key, [tombstoned_unit]) == {
        tombstoned_unit: "available"
    }


def test_lease_acquire_prunes_stale_leases_first(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    first = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-stale",
        run_id="lease-run-stale",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:1],
        lease_ttl_seconds=1,
        log_root_override=log_root,
        now="2026-05-05T00:00:00Z",
    )
    second = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-new",
        run_id="lease-run-new",
        reviewer_id="reviewer-b",
        unit_ids=unit_ids[:1],
        log_root_override=log_root,
        now="2026-05-05T00:00:02Z",
    )
    assert first["acquired"] is True
    assert second["acquired"] is True
    rows = dict(_lease_rows(log_root, repo_key))
    assert rows[first["lease_id"]] == "stale-released"
    assert rows[second["lease_id"]] == "active"


def test_stale_prune_does_not_release_unit_reacquired_by_fresh_lease(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    first_unit, unrelated_unit = unit_ids[:2]
    first = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-stale-old",
        run_id="lease-run-stale-old",
        reviewer_id="reviewer-a",
        unit_ids=[first_unit],
        lease_ttl_seconds=1,
        log_root_override=log_root,
        now="2026-05-05T00:00:00Z",
    )
    fresh = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-stale-fresh",
        run_id="lease-run-stale-fresh",
        reviewer_id="reviewer-b",
        unit_ids=[first_unit],
        lease_ttl_seconds=60,
        log_root_override=log_root,
        now="2026-05-05T00:00:02Z",
    )
    unrelated = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-stale-unrelated",
        run_id="lease-run-stale-unrelated",
        reviewer_id="reviewer-c",
        unit_ids=[unrelated_unit],
        lease_ttl_seconds=1,
        log_root_override=log_root,
        now="2026-05-05T00:00:02Z",
    )
    assert first["acquired"] is True
    assert fresh["acquired"] is True
    assert unrelated["acquired"] is True

    released = module.sweep_stale(
        repo=repo,
        log_root_override=log_root,
        now="2026-05-05T00:00:04Z",
    )

    assert [item["lease_id"] for item in released] == [unrelated["lease_id"]]
    assert _lease_unit_states(log_root, repo_key, [first_unit, unrelated_unit]) == {
        first_unit: "assigned",
        unrelated_unit: "available",
    }


def test_lease_refresh_extends_expires_at(tmp: Path) -> None:
    module, repo, log_root, unit_ids, _repo_key = _lease_seed(tmp)
    acquired = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-refresh",
        run_id="lease-run-refresh",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:1],
        lease_ttl_seconds=10,
        log_root_override=log_root,
        now="2026-05-05T00:00:00Z",
    )
    refreshed = module.lease_refresh(
        repo=repo,
        lease_id=acquired["lease_id"],
        ttl_seconds=20,
        log_root_override=log_root,
        now="2026-05-05T00:00:05Z",
    )
    assert refreshed["refreshed"] is True
    assert refreshed["expires_at"] == "2026-05-05T00:00:25Z"


def test_lease_refresh_returns_expired_when_past_ttl(tmp: Path) -> None:
    module, repo, log_root, unit_ids, _repo_key = _lease_seed(tmp)
    acquired = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-expired",
        run_id="lease-run-expired",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:1],
        lease_ttl_seconds=1,
        log_root_override=log_root,
        now="2026-05-05T00:00:00Z",
    )
    refreshed = module.lease_refresh(
        repo=repo,
        lease_id=acquired["lease_id"],
        log_root_override=log_root,
        now="2026-05-05T00:00:02Z",
    )
    assert refreshed["refreshed"] is False
    assert refreshed["reason"] == "lease_expired_before_refresh"


def test_heartbeat_refreshes_tracker_lease_and_records_backend(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    acquired = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-heartbeat",
        run_id="lease-run-heartbeat",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:1],
        lease_ttl_seconds=60,
        log_root_override=log_root,
    )
    heartbeat = module.heartbeat(
        repo,
        session_id="lease-sess-heartbeat",
        run_id="lease-run-heartbeat",
        lease_id=acquired["lease_id"],
        ttl_seconds=120,
        rvf_state_phase="prepare",
        rvf_backend="kanban-task",
        log_root_override=log_root,
    )
    assert heartbeat["status"] == "ok"
    assert heartbeat["lease_refreshed"] is True
    assert heartbeat["lease_refresh_reason"] == "lease_refreshed"
    events = read_jsonl(_alloc_events_path(log_root, repo_key))
    latest = [event for event in events if event.get("event") == "heartbeat"][-1]
    assert latest["rvf_state_phase"] == "prepare"
    assert latest["rvf_backend"] == "kanban-task"
    assert latest["tracker_lease_id"] == acquired["lease_id"]
    assert latest["lease_refreshed"] is True


def test_heartbeat_treats_same_second_expiry_as_expired(tmp: Path) -> None:
    module, repo, log_root, unit_ids, _repo_key = _lease_seed(tmp)
    acquired = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-heartbeat-edge",
        run_id="lease-run-heartbeat-edge",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:1],
        lease_ttl_seconds=1,
        log_root_override=log_root,
        now="2026-05-05T00:00:00Z",
    )
    heartbeat = module.heartbeat(
        repo,
        session_id="lease-sess-heartbeat-edge",
        run_id="lease-run-heartbeat-edge",
        lease_id=acquired["lease_id"],
        ttl_seconds=60,
        log_root_override=log_root,
        now="2026-05-05T00:00:01.500000Z",
    )
    assert heartbeat["status"] == "ok"
    assert heartbeat["lease_refreshed"] is False
    assert heartbeat["lease_refresh_reason"] == "lease_expired_before_refresh"


def test_lease_release_completed_marks_units_reviewed(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    acquired = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-release",
        run_id="lease-run-release",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:1],
        log_root_override=log_root,
    )
    released = module.lease_release(
        repo=repo,
        lease_id=acquired["lease_id"],
        log_root_override=log_root,
    )
    assert released["released"] is True
    assert _lease_unit_states(log_root, repo_key, unit_ids[:1]) == {unit_ids[0]: "reviewed"}


def test_lease_release_idempotent(tmp: Path) -> None:
    module, repo, log_root, unit_ids, _repo_key = _lease_seed(tmp)
    acquired = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-idem",
        run_id="lease-run-idem",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:1],
        log_root_override=log_root,
    )
    first = module.lease_release(repo=repo, lease_id=acquired["lease_id"], log_root_override=log_root)
    second = module.lease_release(repo=repo, lease_id=acquired["lease_id"], log_root_override=log_root)
    assert first["released"] is True
    assert second["released"] is True
    assert second["reason"] == "lease_already_completed"


def test_complete_review_scope_unions_contract_and_lease_units(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    assert len(unit_ids) >= 2
    acquired = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-complete-union",
        run_id="lease-run-complete-union",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:2],
        log_root_override=log_root,
    )
    completed = module.complete_review_scope(
        repo=repo,
        lease_id=acquired["lease_id"],
        unit_ids=unit_ids[:1],
        log_root_override=log_root,
    )
    assert completed["released"] is True
    assert _lease_unit_count(log_root, repo_key, acquired["lease_id"]) == 0
    assert _lease_unit_states(log_root, repo_key, unit_ids[:2]) == {
        unit_ids[0]: "reviewed",
        unit_ids[1]: "reviewed",
    }


def test_complete_review_scope_keeps_partial_edit_claim_pending(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    assert len(unit_ids) >= 2

    registered = module.register_edit_claims(
        repo=repo,
        session_id="claim-sess-partial",
        run_id="claim-run-partial",
        edit_claims=[
            {
                "claim_id": "claim-partial-units",
                "path": "a.txt",
                "tool_name": "apply_patch",
                "mapped_unit_ids": unit_ids[:2],
            }
        ],
        log_root_override=log_root,
    )
    assert registered["status"] == "ok"

    first = module.complete_review_scope(
        repo=repo,
        lease_id="missing-partial-lease-a",
        unit_ids=unit_ids[:1],
        scope_hash="partial-scope-a",
        run_id="partial-run-a",
        log_root_override=log_root,
    )
    assert first["released"] is True
    assert first["reviewed_edit_claim_count"] == 0
    conn = _alloc_open_db(log_root, repo_key)
    try:
        status = conn.execute(
            "SELECT status FROM edit_claims WHERE claim_id='claim-partial-units'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "pending"

    second = module.complete_review_scope(
        repo=repo,
        lease_id="missing-partial-lease-b",
        unit_ids=unit_ids[1:2],
        scope_hash="partial-scope-b",
        run_id="partial-run-b",
        log_root_override=log_root,
    )
    assert second["released"] is True
    assert second["reviewed_edit_claim_count"] == 1
    conn = _alloc_open_db(log_root, repo_key)
    try:
        status = conn.execute(
            "SELECT status FROM edit_claims WHERE claim_id='claim-partial-units'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "reviewed"


def test_complete_review_scope_does_not_complete_failed_released_lease(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    acquired = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-complete-failed",
        run_id="lease-run-complete-failed",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:1],
        log_root_override=log_root,
    )
    failed = module.lease_release(
        repo=repo,
        lease_id=acquired["lease_id"],
        reason="failed",
        log_root_override=log_root,
    )
    assert failed["released"] is True
    completed = module.complete_review_scope(
        repo=repo,
        lease_id=acquired["lease_id"],
        unit_ids=unit_ids[:1],
        log_root_override=log_root,
    )
    assert completed["released"] is False
    assert completed["reason"] == "lease_failed_released"
    assert dict(_lease_rows(log_root, repo_key))[acquired["lease_id"]] == "failed-released"
    assert _lease_unit_states(log_root, repo_key, unit_ids[:1]) == {unit_ids[0]: "available"}


def test_complete_review_scope_supersedes_overlapping_active_lease(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    stale = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-stale-complete",
        run_id="lease-run-stale-complete",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:1],
        lease_ttl_seconds=1,
        log_root_override=log_root,
        now="2026-05-05T00:00:00Z",
    )
    swept = module.sweep_stale(
        repo=repo,
        log_root_override=log_root,
        now="2026-05-05T00:00:02Z",
    )
    assert [item["lease_id"] for item in swept] == [stale["lease_id"]]
    active = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-overlap-complete",
        run_id="lease-run-overlap-complete",
        reviewer_id="reviewer-b",
        unit_ids=unit_ids[:1],
        log_root_override=log_root,
    )
    assert active["acquired"] is True
    completed = module.complete_review_scope(
        repo=repo,
        lease_id=stale["lease_id"],
        unit_ids=unit_ids[:1],
        log_root_override=log_root,
    )
    assert completed["released"] is True
    assert active["lease_id"] in completed["superseded_active_lease_ids"]
    assert _lease_unit_count(log_root, repo_key, active["lease_id"]) == 0
    assert dict(_lease_rows(log_root, repo_key))[active["lease_id"]] == "completed"
    assert _lease_unit_states(log_root, repo_key, unit_ids[:1]) == {unit_ids[0]: "reviewed"}


def test_complete_review_scope_keeps_different_scope_active_lease(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    active = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-overlap-different-scope",
        run_id="lease-run-overlap-different-scope",
        reviewer_id="reviewer-b",
        unit_ids=unit_ids[:1],
        log_root_override=log_root,
    )
    completed = module.complete_review_scope(
        repo=repo,
        lease_id="missing-old-lease",
        unit_ids=unit_ids[:1],
        scope_hash="different-old-scope",
        run_id="old-run",
        log_root_override=log_root,
    )
    assert completed["released"] is True
    assert completed["unit_ids"] == []
    assert completed["blocked_active_lease_ids"] == [active["lease_id"]]
    assert dict(_lease_rows(log_root, repo_key))[active["lease_id"]] == "active"
    assert _lease_unit_count(log_root, repo_key, active["lease_id"]) == 1
    assert _lease_unit_states(log_root, repo_key, unit_ids[:1]) == {unit_ids[0]: "assigned"}


def test_lease_participants_finish_does_not_release_shared_lease(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    acquired = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-shared",
        run_id="lease-run-shared",
        reviewer_id="allocator",
        unit_ids=unit_ids[:1],
        log_root_override=log_root,
    )
    assert acquired["acquired"] is True
    for reviewer_id, owns_lease in (("reviewer-a", True), ("reviewer-b", False)):
        joined = module.lease_participant_join(
            repo=repo,
            lease_id=acquired["lease_id"],
            reviewer_id=reviewer_id,
            run_id="lease-run-shared",
            owns_lease=owns_lease,
            log_root_override=log_root,
        )
        assert joined["joined"] is True

    first_finish = module.lease_participant_finish(
        repo=repo,
        lease_id=acquired["lease_id"],
        reviewer_id="reviewer-a",
        run_id="lease-run-shared",
        reason="completed",
        log_root_override=log_root,
    )

    assert first_finish["finished"] is True
    assert dict(_lease_rows(log_root, repo_key))[acquired["lease_id"]] == "active"
    assert _lease_unit_count(log_root, repo_key, acquired["lease_id"]) == 1
    assert _lease_unit_states(log_root, repo_key, unit_ids[:1]) == {unit_ids[0]: "assigned"}

    second_finish = module.lease_participant_finish(
        repo=repo,
        lease_id=acquired["lease_id"],
        reviewer_id="reviewer-b",
        run_id="lease-run-shared",
        reason="completed",
        log_root_override=log_root,
    )

    assert second_finish["finished"] is True
    assert second_finish["active_participant_count"] == 0
    assert dict(_lease_rows(log_root, repo_key))[acquired["lease_id"]] == "active"
    assert _lease_unit_count(log_root, repo_key, acquired["lease_id"]) == 1
    assert _lease_unit_states(log_root, repo_key, unit_ids[:1]) == {unit_ids[0]: "assigned"}

    released = module.lease_release(
        repo=repo,
        lease_id=acquired["lease_id"],
        log_root_override=log_root,
    )
    assert released["released"] is True
    assert _lease_unit_count(log_root, repo_key, acquired["lease_id"]) == 0
    assert _lease_unit_states(log_root, repo_key, unit_ids[:1]) == {unit_ids[0]: "reviewed"}


def test_sweep_stale_releases_expired_active_leases(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    acquired = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-sweep",
        run_id="lease-run-sweep",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:1],
        lease_ttl_seconds=1,
        log_root_override=log_root,
        now="2026-05-05T00:00:00Z",
    )
    joined = module.lease_participant_join(
        repo=repo,
        lease_id=acquired["lease_id"],
        reviewer_id="reviewer-a",
        run_id="lease-run-sweep",
        owns_lease=True,
        log_root_override=log_root,
        now="2026-05-05T00:00:00Z",
    )
    assert joined["joined"] is True
    released = module.sweep_stale(
        repo=repo,
        log_root_override=log_root,
        now="2026-05-05T00:00:02Z",
    )
    assert [item["lease_id"] for item in released] == [acquired["lease_id"]]
    assert dict(_lease_rows(log_root, repo_key))[acquired["lease_id"]] == "stale-released"
    assert _lease_participant_states(log_root, repo_key, acquired["lease_id"]) == {"reviewer-a": "failed"}
    assert _lease_unit_states(log_root, repo_key, unit_ids[:1]) == {unit_ids[0]: "available"}


def test_sweep_stale_releases_same_second_expired_lease(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    acquired = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-sweep-edge",
        run_id="lease-run-sweep-edge",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:1],
        lease_ttl_seconds=1,
        log_root_override=log_root,
        now="2026-05-05T00:00:00Z",
    )
    released = module.sweep_stale(
        repo=repo,
        log_root_override=log_root,
        now="2026-05-05T00:00:01.500000Z",
    )
    assert [item["lease_id"] for item in released] == [acquired["lease_id"]]
    assert dict(_lease_rows(log_root, repo_key))[acquired["lease_id"]] == "stale-released"


def test_sweep_stale_no_op_when_all_active_leases_fresh(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    acquired = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-fresh",
        run_id="lease-run-fresh",
        reviewer_id="reviewer-a",
        unit_ids=unit_ids[:1],
        lease_ttl_seconds=60,
        log_root_override=log_root,
        now="2026-05-05T00:00:00Z",
    )
    assert module.sweep_stale(repo=repo, log_root_override=log_root, now="2026-05-05T00:00:02Z") == []
    assert dict(_lease_rows(log_root, repo_key))[acquired["lease_id"]] == "active"


def _run_reviewer_with_lease(
    *,
    tmp: Path,
    repo: Path,
    log_root: Path,
    unit_ids: list[str],
    reviewer_code: str,
    output_format: str = "text",
    max_runtime_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nlease test\n", encoding="utf-8")
    contract = _lease_contract(tmp / "scope.contract.json", repo=repo, unit_ids=unit_ids)
    config = write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse(
        tmp / "alternative-reviewer.json",
        [sys.executable, "-c", reviewer_code],
        activity_check_interval_seconds=0.05,
        max_runtime_seconds=max_runtime_seconds,
        output_format=output_format,
    )
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root), "RVF_LEASE_HEARTBEAT_SECONDS": "0.05"}
    return subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--scope-contract",
            str(contract),
            "--rvf-run-id",
            "lease-reviewer-run",
            "--rvf-run-dir",
            str(tmp / "run"),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_run_alternative_reviewer_releases_lease_on_normal_exit(tmp: Path) -> None:
    _module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    completed = _run_reviewer_with_lease(
        tmp=tmp,
        repo=repo,
        log_root=log_root,
        unit_ids=unit_ids[:1],
        reviewer_code=clean_review_result_python(),
    )
    assert completed.returncode == 0, completed.stderr
    assert _lease_unit_states(log_root, repo_key, unit_ids[:1]) == {unit_ids[0]: "reviewed"}
    assert _lease_rows(log_root, repo_key)[-1][1] == "completed"


def test_run_alternative_reviewer_shared_lease_does_not_release_on_exit(tmp: Path) -> None:
    module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    acquired = module.lease_acquire(
        repo=repo,
        session_id="lease-sess-shared-runner",
        run_id="lease-reviewer-run",
        reviewer_id="allocator",
        unit_ids=unit_ids[:1],
        log_root_override=log_root,
    )
    assert acquired["acquired"] is True
    owner = module.lease_participant_join(
        repo=repo,
        lease_id=acquired["lease_id"],
        reviewer_id="owner-reviewer",
        run_id="lease-reviewer-run",
        owns_lease=True,
        log_root_override=log_root,
    )
    assert owner["joined"] is True
    finished_owner = module.lease_participant_finish(
        repo=repo,
        lease_id=acquired["lease_id"],
        reviewer_id="owner-reviewer",
        run_id="lease-reviewer-run",
        reason="completed",
        log_root_override=log_root,
    )
    assert finished_owner["finished"] is True

    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nshared lease test\n", encoding="utf-8")
    contract = _lease_contract(
        tmp / "scope.contract.json",
        repo=repo,
        unit_ids=unit_ids[:1],
        tracker_lease_id=acquired["lease_id"],
    )
    config = write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse(
        tmp / "alternative-reviewer.json",
        [sys.executable, "-c", clean_review_result_python()],
        activity_check_interval_seconds=0.05,
    )
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root), "RVF_LEASE_HEARTBEAT_SECONDS": "0.05"}
    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--scope-contract",
            str(contract),
            "--rvf-run-id",
            "lease-reviewer-run",
            "--rvf-run-dir",
            str(tmp / "run"),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert dict(_lease_rows(log_root, repo_key))[acquired["lease_id"]] == "active"
    assert _lease_unit_count(log_root, repo_key, acquired["lease_id"]) == 1
    assert _lease_unit_states(log_root, repo_key, unit_ids[:1]) == {unit_ids[0]: "assigned"}
    assert _lease_participant_states(log_root, repo_key, acquired["lease_id"]) == {
        "owner-reviewer": "completed",
        "test": "completed",
    }


def test_run_alternative_reviewer_releases_lease_on_codex_backend_challenge(tmp: Path) -> None:
    _module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    html = "<html><title>Just a moment</title><body>Cloudflare challenge-platform</body></html>"
    completed = _run_reviewer_with_lease(
        tmp=tmp,
        repo=repo,
        log_root=log_root,
        unit_ids=unit_ids[:1],
        reviewer_code=f"import sys; sys.stdin.read(); print({html!r})",
        output_format="codex_json",
    )
    assert completed.returncode != 0
    assert "RVF_CODEX_BACKEND_CHALLENGE" in completed.stderr
    assert _lease_unit_states(log_root, repo_key, unit_ids[:1]) == {unit_ids[0]: "available"}


def test_run_alternative_reviewer_releases_lease_on_timeout(tmp: Path) -> None:
    _module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    completed = _run_reviewer_with_lease(
        tmp=tmp,
        repo=repo,
        log_root=log_root,
        unit_ids=unit_ids[:1],
        reviewer_code="import sys, time; sys.stdin.read(); time.sleep(5)",
        max_runtime_seconds=0.2,
    )
    assert completed.returncode == 124
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" in completed.stdout
    assert _lease_unit_states(log_root, repo_key, unit_ids[:1]) == {unit_ids[0]: "available"}


def test_run_alternative_reviewer_sigterm_kills_child_before_release(tmp: Path) -> None:
    _module, repo, log_root, unit_ids, repo_key = _lease_seed(tmp)
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nlease signal test\n", encoding="utf-8")
    contract = _lease_contract(tmp / "scope.contract.json", repo=repo, unit_ids=unit_ids[:1])
    pid_file = tmp / "reviewer.pid"
    reviewer_code = (
        "import os, pathlib, sys, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "sys.stdin.read()\n"
        "time.sleep(30)\n"
    )
    config = write_alternative_reviewer_config_with_idle_timeout_as_hang_prevention_fuse(
        tmp / "alternative-reviewer.json",
        [sys.executable, "-c", reviewer_code],
        activity_check_interval_seconds=0.05,
        max_runtime_seconds=60,
    )
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root), "RVF_LEASE_HEARTBEAT_SECONDS": "0.05"}
    proc = subprocess.Popen(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
            "--scope-contract",
            str(contract),
            "--rvf-run-id",
            "lease-reviewer-signal-run",
            "--rvf-run-dir",
            str(tmp / "run"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    child_pid: int | None = None
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not pid_file.exists() and proc.poll() is None:
            time.sleep(0.02)
        if not pid_file.exists():
            stdout, stderr = proc.communicate(timeout=5)
            raise AssertionError(f"reviewer child did not start; rc={proc.returncode}; stdout={stdout}; stderr={stderr}")
        child_pid = int(pid_file.read_text(encoding="utf-8"))

        os.kill(proc.pid, signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=10)

        assert proc.returncode == 143, f"stdout={stdout}; stderr={stderr}"
        deadline = time.time() + 5
        while time.time() < deadline and _process_is_running(child_pid):
            time.sleep(0.05)
        assert not _process_is_running(child_pid)
        assert _lease_unit_states(log_root, repo_key, unit_ids[:1]) == {unit_ids[0]: "available"}
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        if child_pid is not None and _process_is_running(child_pid):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_lease_acquire_concurrent_writers_serialize(tmp: Path) -> None:
    _module, repo, log_root, unit_ids, _repo_key = _lease_seed(tmp)
    snippet = (
        "import json, os, sys, time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from pathlib import Path\n"
        "os.environ.setdefault('RVF_TRACKER_BUSY_TIMEOUT_MS', '30000')\n"
        "import core.session_scope_allocation.reviewable_unit_diff_tracker as dt\n"
        f"repo = Path({str(repo)!r})\n"
        f"log_root = Path({str(log_root)!r})\n"
        f"unit_id = {unit_ids[0]!r}\n"
        "wait_until = float(os.environ['CONCURRENT_WAIT_UNTIL'])\n"
        "remaining = wait_until - time.time()\n"
        "if remaining > 0:\n"
        "    time.sleep(remaining)\n"
        "result = dt.lease_acquire(\n"
        "    repo=repo, session_id=sys.argv[1], run_id=sys.argv[1],\n"
        "    reviewer_id='r-' + sys.argv[1], unit_ids=[unit_id],\n"
        "    log_root_override=log_root,\n"
        ")\n"
        "print(json.dumps(result))\n"
    )
    wait_until = time.time() + 1.5
    env = {**os.environ, "CONCURRENT_WAIT_UNTIL": f"{wait_until:.6f}"}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", snippet, session],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for session in ("lease-conc-A", "lease-conc-B")
    ]
    payloads = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=60)
        if stderr.strip():
            raise AssertionError(stderr)
        payloads.append(json.loads(stdout.strip().splitlines()[-1]))
    assert sum(1 for payload in payloads if payload["acquired"]) == 1
    assert sum(1 for payload in payloads if not payload["acquired"]) == 1


# --- failed-impl-reentry: run-scoped reviewed→available reopen (Plan phase A) ---


def _reopen_seed(tmp: Path, *, run_id: str = "R1") -> tuple[object, Path, Path, str, list[str]]:
    """Build a 2-dirty-file repo, then allocate (non-dry) + complete so both
    units end up ``reviewed`` under ``run_id`` with ``session_units.run_id`` set.
    Returns (module, repo, log_root, repo_key, unit_ids)."""
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    allocated = module.allocate_review_scope(
        repo=repo,
        session_id="reopen-impl",
        run_id=run_id,
        reviewer_id="reviewer-a",
        dry_run=False,
        log_root_override=log_root,
    )
    assert allocated["status"] == "allocated", allocated
    repo_key = allocated["repo_key"]
    completed = module.complete_review_scope(
        repo=repo,
        lease_id=allocated["lease_id"],
        log_root_override=log_root,
    )
    assert completed["released"] is True, completed
    unit_ids = sorted(completed["unit_ids"])
    assert len(unit_ids) >= 2, unit_ids
    states = _lease_unit_states(log_root, repo_key, unit_ids)
    assert set(states.values()) == {"reviewed"}, states
    return module, repo, log_root, repo_key, unit_ids


def test_invalidate_reviewed_units_for_run_reopens_and_is_idempotent(tmp: Path) -> None:
    module, repo, log_root, repo_key, unit_ids = _reopen_seed(tmp, run_id="R1")
    result = module.invalidate_reviewed_units_for_run(
        repo=repo, run_id="R1", log_root_override=log_root
    )
    assert result["status"] == "reopened", result
    assert sorted(result["reopened_unit_ids"]) == unit_ids
    assert result["reopened_unit_count"] == len(unit_ids)
    states = _lease_unit_states(log_root, repo_key, unit_ids)
    assert set(states.values()) == {"available"}, states

    records = read_jsonl(_alloc_events_path(log_root, repo_key))
    reopened = [r for r in records if r.get("event") == "review_scope_reopened_for_run"]
    assert reopened, records
    assert reopened[-1]["run_id"] == "R1"
    assert reopened[-1]["reopened_unit_count"] == len(unit_ids)
    assert reopened[-1]["reason_code"] == "failed_impl_reentry"
    assert reopened[-1]["rvf_state_phase"] == "review"

    second = module.invalidate_reviewed_units_for_run(
        repo=repo, run_id="R1", log_root_override=log_root
    )
    assert second["status"] == "noop", second
    assert second["reopened_unit_count"] == 0
    assert _lease_unit_states(log_root, repo_key, unit_ids) == states


def test_invalidate_reviewed_units_for_run_is_run_scoped(tmp: Path) -> None:
    module, repo, log_root, repo_key, unit_ids = _reopen_seed(tmp, run_id="R1")
    other_unit = unit_ids[-1]
    r1_units = unit_ids[:-1]
    # Simulate `other_unit` having been reviewed by a *different* run; the
    # reopen must never broadcast beyond the target run.
    conn = _alloc_open_db(log_root, repo_key)
    try:
        conn.execute(
            "UPDATE session_units SET run_id='R-other' WHERE unit_id=?", (other_unit,)
        )
        conn.commit()
    finally:
        conn.close()

    result = module.invalidate_reviewed_units_for_run(
        repo=repo, run_id="R1", log_root_override=log_root
    )
    assert sorted(result["reopened_unit_ids"]) == sorted(r1_units), result
    states = _lease_unit_states(log_root, repo_key, unit_ids)
    assert states[other_unit] == "reviewed", states
    for uid in r1_units:
        assert states[uid] == "available", states


def test_invalidate_reviewed_units_for_run_excludes_tombstoned(tmp: Path) -> None:
    module, repo, log_root, repo_key, unit_ids = _reopen_seed(tmp, run_id="R1")
    gone = unit_ids[-1]
    live = unit_ids[:-1]
    conn = _alloc_open_db(log_root, repo_key)
    try:
        conn.execute("UPDATE units SET is_tombstoned=1 WHERE unit_id=?", (gone,))
        conn.commit()
    finally:
        conn.close()

    result = module.invalidate_reviewed_units_for_run(
        repo=repo, run_id="R1", log_root_override=log_root
    )
    assert result["candidate_unit_count"] == len(live), result
    assert sorted(result["reopened_unit_ids"]) == sorted(live), result
    states = _lease_unit_states(log_root, repo_key, unit_ids)
    assert states[gone] == "reviewed", states
    for uid in live:
        assert states[uid] == "available", states


def test_latest_reviewed_run_for_worktree_resolution(tmp: Path) -> None:
    module, repo, log_root, repo_key, unit_ids = _reopen_seed(tmp, run_id="R1")
    found = module.latest_reviewed_run_for_worktree(repo=repo, log_root_override=log_root)
    assert found["status"] == "found", found
    assert found["run_id"] == "R1"
    assert found["reviewed_unit_count"] == len(unit_ids)

    # A later run (greater assigned_at) reviewing one unit wins the ordering.
    later_unit = unit_ids[-1]
    conn = _alloc_open_db(log_root, repo_key)
    try:
        conn.execute(
            "UPDATE session_units SET run_id='R-late', assigned_at='2099-01-01T00:00:00Z' "
            "WHERE unit_id=?",
            (later_unit,),
        )
        conn.commit()
    finally:
        conn.close()
    picked = module.latest_reviewed_run_for_worktree(repo=repo, log_root_override=log_root)
    assert picked["status"] == "found", picked
    assert picked["run_id"] == "R-late", picked

    # After both runs are reopened, no reviewed run remains in this worktree.
    module.invalidate_reviewed_units_for_run(repo=repo, run_id="R1", log_root_override=log_root)
    module.invalidate_reviewed_units_for_run(repo=repo, run_id="R-late", log_root_override=log_root)
    empty = module.latest_reviewed_run_for_worktree(repo=repo, log_root_override=log_root)
    assert empty["status"] == "not_found", empty
    assert empty["run_id"] is None


# ---------------------------------------------------------------------------
# 「声明不需审核」豁免（diff/hunk 级）+ lease 过期后的并集全审
# ---------------------------------------------------------------------------

def _unit_row_by_path(log_root: Path, repo_key: str, path: str):
    conn = _alloc_open_db(log_root, repo_key)
    try:
        rows = list(
            conn.execute(
                "SELECT unit_id, review_state, review_not_required_declared_at, "
                "review_not_required_declared_reason, review_not_required_declared_by_run_id "
                "FROM units WHERE path=? AND is_tombstoned=0",
                (path,),
            )
        )
    finally:
        conn.close()
    return rows


def test_declare_units_review_not_required_survives_tracker_refresh(tmp: Path) -> None:
    """豁免必须跨 tracker refresh 存活——这条直接钉死 ``_upsert_unit`` 的 ON CONFLICT 陷阱。

    隔壁的 tombstone 三列在 ON CONFLICT 里被**无条件重置**，而每次 Stop 都会跑一次 refresh；
    豁免若挂在 tombstone 一侧会被静默抹掉。故豁免挂在 ``review_state`` 一侧 + 三列审计侧车，
    三者都不在那份 SET 列表里。
    """
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"

    first = module.allocate_review_scope(
        repo=repo, session_id="sess-waiver", run_id="run-waiver-1",
        reviewer_id=None, dry_run=True, log_root_override=log_root,
    )
    assert first["status"] == "dry_run", first
    repo_key = first["repo_key"]
    rows = _unit_row_by_path(log_root, repo_key, "tracked2.txt")
    assert rows, "fixture 应该有 tracked2.txt 的 dirty unit"
    waived_unit_id = rows[0][0]
    assert waived_unit_id in first["unit_ids"]

    declared = module.declare_units_review_not_required(
        repo=repo, reason="agent_declined", unit_ids=[waived_unit_id],
        declared_by_run_id="run-waiver-1", log_root_override=log_root,
    )
    assert declared["status"] == "declared"
    assert declared["declared_unit_ids"] == [waived_unit_id]

    # 再跑一次 allocate = 一次完整的 refresh（重新观察 + upsert 同一批 unit）。
    second = module.allocate_review_scope(
        repo=repo, session_id="sess-waiver", run_id="run-waiver-2",
        reviewer_id=None, dry_run=True, log_root_override=log_root,
    )
    assert waived_unit_id not in second["unit_ids"], second  # 已被排除出审核范围
    row = _unit_row_by_path(log_root, repo_key, "tracked2.txt")[0]
    assert row[1] == "reviewed"                      # 豁免仍在（未被 ON CONFLICT 抹掉）
    assert row[2] and row[2].endswith("Z")           # declared_at
    assert row[3] == "agent_declined"                # declared_reason
    assert row[4] == "run-waiver-1"                  # declared_by_run_id
    # 事件落盘可审计。
    events = [
        json.loads(line)
        for line in _alloc_events_path(log_root, repo_key).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(e.get("event") == "units_declared_review_not_required" for e in events), events


def test_declare_units_review_not_required_is_not_inherited_by_changed_hunk(tmp: Path) -> None:
    """粒度是 diff 级而非 file 级：被豁免的 hunk 内容一改，就是新 unit、不继承豁免。"""
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"

    first = module.allocate_review_scope(
        repo=repo, session_id="sess-waiver-diff", run_id="run-wd-1",
        reviewer_id=None, dry_run=True, log_root_override=log_root,
    )
    repo_key = first["repo_key"]
    waived_unit_id = _unit_row_by_path(log_root, repo_key, "tracked2.txt")[0][0]
    module.declare_units_review_not_required(
        repo=repo, reason="agent_declined", unit_ids=[waived_unit_id],
        declared_by_run_id="run-wd-1", log_root_override=log_root,
    )

    # 改动同一个文件的同一段内容 → 内容派生的 unit_id 变了。
    # 用新 session 观察：老 session 已有 session_units，allocator 的 auto-claim 只在「该 session
    # 尚无任何归属」时兜底认领（正常路径的归属来自 manifest/register_claims），沿用老 session 会
    # 让新 unit 因无归属而落不进候选集——那是归属机制，不是本测试要验的豁免继承性。
    (repo / "tracked2.txt").write_text("base2\nchange2-edited-after-waiver\n", encoding="utf-8")
    second = module.allocate_review_scope(
        repo=repo, session_id="sess-waiver-diff-after-edit", run_id="run-wd-2",
        reviewer_id=None, dry_run=True, log_root_override=log_root,
    )
    assert waived_unit_id not in second["unit_ids"]           # 老 unit 仍被豁免
    assert "tracked2.txt" in second["paths"], second          # 但新内容重新进入审核范围


def test_allocate_review_scope_unions_prior_scope_after_lease_expiry(tmp: Path) -> None:
    """lease 过期后重新分配：不带并集会把原本圈定的 committed round 整个换掉；带上就恢复。

    复现 run rvf-20260808T090747Z-stop-hook-ea36796d 的第四条结论——scope 的**口径**（而不只是
    大小）在中断 + rewind 后被从 committed-round 悄悄换成了纯 dirty。
    """
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    baseline = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    (repo / "committed_in_round.txt").write_text("committed round content\n", encoding="utf-8")
    run(["git", "add", "committed_in_round.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "committed round"], cwd=repo)

    # 归属登记：committed-round 的 unit 不走 allocator 的 dirty auto-claim 兜底，正常路径由
    # manifest → register_claims 归属（Stop hook 的 refresh 就是这么做的）。
    registered = module.register_claims(
        repo=repo, session_id="sess-union-1", run_id="run-union-1",
        worktree=repo, branch=None,
        owned_paths=["committed_in_round.txt"],
        apply_patch_paths={"committed_in_round.txt"},
        exec_only_paths=set(),
        committed_paths={"committed_in_round.txt"},
        committed_baseline=baseline,
        log_root_override=log_root,
    )
    assert registered.status == "ok", registered

    first = module.allocate_review_scope(
        repo=repo, session_id="sess-union-1", run_id="run-union-1",
        reviewer_id="reviewer-union", committed_baseline=baseline,
        log_root_override=log_root,
    )
    assert first["status"] == "allocated", first
    assert "committed_in_round.txt" in first["scope"]["paths"], first["scope"]["paths"]
    # 口径写进了 scope artifact，供下一次重新分配继承。
    assert first["scope"]["committed_baseline"] == baseline
    repo_key = first["repo_key"]
    prior_unit_ids = list(first["scope"]["unit_ids"])
    committed_unit_id = _unit_row_by_path(log_root, repo_key, "committed_in_round.txt")[0][0]
    assert committed_unit_id in prior_unit_ids

    # lease 过期（agent 被中断 / 会话走远后回来）。
    conn = _alloc_open_db(log_root, repo_key)
    try:
        conn.execute("UPDATE leases SET expires_at='1970-01-01T00:00:00Z'")
        conn.commit()
    finally:
        conn.close()

    # ① 不带并集、换了 session：committed round 被整个漏掉（这正是事故行为）。
    without_union = module.allocate_review_scope(
        repo=repo, session_id="sess-union-2", run_id="run-union-2",
        reviewer_id=None, dry_run=True, log_root_override=log_root,
    )
    assert committed_unit_id not in without_union["unit_ids"], without_union

    # ② 带并集：原 scope 里仍可审的 unit 全部并回。
    # 并集必须**连口径一起**继承：只并 unit_id 而不带回 committed_baseline，committed 那批会在
    # 观察阶段被判为「已不在场」而 superseded，并集查询自然找不到它们。CLI 的
    # --union-with-prior-scope 会从上一轮 scope artifact 自动继承 baseline，这里显式传等价值。
    with_union = module.allocate_review_scope(
        repo=repo, session_id="sess-union-2", run_id="run-union-3",
        reviewer_id="reviewer-union-2",
        committed_baseline=first["scope"]["committed_baseline"],
        union_with_prior_scope_unit_ids=prior_unit_ids,
        log_root_override=log_root,
    )
    assert with_union["status"] == "allocated", with_union
    union_unit_ids = with_union["scope"]["unit_ids"]
    assert committed_unit_id in union_unit_ids, with_union
    assert set(prior_unit_ids) <= set(union_unit_ids)               # 新 scope ⊇ 原 scope
    assert committed_unit_id in with_union["unioned_prior_scope_unit_ids"]
    events = [
        json.loads(line)
        for line in _alloc_events_path(log_root, repo_key).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    reallocated = [e for e in events if e.get("event") == "scope_reallocated_after_lease_expiry"]
    assert len(reallocated) == 1, events
    assert reallocated[0]["unioned_prior_scope_unit_count"] >= 1


def test_invalidate_reviewed_units_for_run_excludes_review_not_required_declarations(
    tmp: Path,
) -> None:
    """失败再入的重开不得撤销「不需审核」声明：那条 reviewed 不是「审过了」而是「决定不审」。"""
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"

    allocated = module.allocate_review_scope(
        repo=repo, session_id="sess-reopen-waiver", run_id="run-reopen-waiver",
        reviewer_id="reviewer-reopen-waiver", log_root_override=log_root,
    )
    assert allocated["status"] == "allocated", allocated
    scoped = list(allocated["scope"]["unit_ids"])
    assert len(scoped) >= 2, scoped
    waived_unit_id, genuinely_reviewed_unit_id = scoped[0], scoped[1]

    # 一半走真实审完（lease 完成 → reviewed），一半走豁免声明。
    module.complete_review_scope(
        repo=repo, lease_id=allocated["lease_id"], log_root_override=log_root
    )
    module.declare_units_review_not_required(
        repo=repo, reason="agent_declined", unit_ids=[waived_unit_id],
        declared_by_run_id="run-reopen-waiver", log_root_override=log_root,
    )

    reopened = module.invalidate_reviewed_units_for_run(
        repo=repo, run_id="run-reopen-waiver", log_root_override=log_root,
    )
    assert genuinely_reviewed_unit_id in reopened["reopened_unit_ids"], reopened
    assert waived_unit_id not in reopened["reopened_unit_ids"], reopened


def test_allocate_review_scope_union_list_excludes_units_held_by_active_lease(
    tmp: Path,
) -> None:
    """「承接自上一轮」的清单必须与实际进入本轮 scope 的集合一致。

    并集（Step 4b）刻意排在 active-lease 反连接（Step 5）**之前**，好让并回来的 unit 与本轮
    新观察到的走同一道排他检查。代价是：被别的活跃租约占住的 prior unit 会在 Step 5 掉队。
    若对外报告的仍是反连接**之前**的那份清单，就会出现两个后果——packet 的「承接自上一轮」
    列出 reviewer 根本没拿到的 unit；事件里 ``newly_observed_unit_count``（本轮 scope 大小减
    并集大小）算成负数，审计读到的是假的。

    构造手法与既有的 ``test_allocate_review_scope_excludes_active_leased_units`` 相同：手工把
    被租住的 unit 翻回 ``available``、租约原样留着——那正是 Step 5 反连接存在的理由，也是并集
    清单唯一会与实际 scope 分叉的入口。
    """
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"

    # dry_run 只观察 + 登记归属，不建租约：拿到「上一轮 scope」的 unit 清单。
    seeded = module.allocate_review_scope(
        repo=repo, session_id="sess-union-lease-seed", run_id="run-union-lease-seed",
        reviewer_id=None, dry_run=True, log_root_override=log_root,
    )
    assert seeded["status"] == "dry_run", seeded
    repo_key = seeded["repo_key"]
    prior_scope_unit_ids = list(seeded["unit_ids"])
    assert len(prior_scope_unit_ids) >= 2, prior_scope_unit_ids
    unit_held_by_other_active_lease = prior_scope_unit_ids[0]
    units_still_reviewable_this_round = prior_scope_unit_ids[1:]

    held = module.lease_acquire(
        repo=repo, session_id="sess-union-lease-holder", run_id="run-union-lease-holder",
        reviewer_id="reviewer-union-lease-holder",
        unit_ids=[unit_held_by_other_active_lease],
        log_root_override=log_root,
    )
    assert held["acquired"] is True, held
    conn = _alloc_open_db(log_root, repo_key)
    try:
        conn.execute(
            "UPDATE units SET review_state='available' WHERE unit_id=?",
            (unit_held_by_other_active_lease,),
        )
        conn.commit()
    finally:
        conn.close()

    reallocated = module.allocate_review_scope(
        repo=repo, session_id="sess-union-lease-reallocated",
        run_id="run-union-lease-reallocated",
        reviewer_id="reviewer-union-lease-reallocated",
        # 新 session 没有任何归属登记；关掉「dirty 兜底认领」好让候选集**只**来自并集，
        # 于是「并集清单 vs 实际 scope」这一处差异被单独暴露出来，不被新观察的 unit 掩盖。
        auto_claim_observed=False,
        union_with_prior_scope_unit_ids=prior_scope_unit_ids,
        log_root_override=log_root,
    )
    assert reallocated["status"] == "allocated", reallocated
    assert reallocated["leased_excluded_count"] == 1, reallocated
    assert reallocated["scope"]["unit_ids"] == units_still_reviewable_this_round, reallocated["scope"]
    # 承接清单只能包含真正进了本轮 scope 的那部分：被活跃租约占住的那个必须掉出去。
    assert reallocated["unioned_prior_scope_unit_ids"] == units_still_reviewable_this_round, reallocated
    assert (
        reallocated["scope"]["unioned_prior_scope_unit_ids"]
        == units_still_reviewable_this_round
    ), reallocated["scope"]
    assert unit_held_by_other_active_lease not in reallocated["unioned_prior_scope_unit_ids"]

    events = [
        json.loads(line)
        for line in _alloc_events_path(log_root, repo_key).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    reallocation_events = [
        event for event in events if event.get("event") == "scope_reallocated_after_lease_expiry"
    ]
    assert len(reallocation_events) == 1, events
    reallocation_event = reallocation_events[0]
    assert reallocation_event["unioned_prior_scope_unit_ids"] == units_still_reviewable_this_round
    assert reallocation_event["unioned_prior_scope_unit_count"] == len(
        units_still_reviewable_this_round
    )
    # 本轮一个新 unit 都没观察到，所以是 0；用反连接**之前**的清单去减则是
    # len(scope) - len(prior) = -1，负数会直接把审计读者带偏。
    assert reallocation_event["newly_observed_unit_count"] == 0, reallocation_event


# ---------------------------------------------------------------------------
# kanban-followup trigger 在场判定：transcript 扫描的上界必须是**读取**层面的上界
#
# 该被测模块（plugins/.../scripts/kanban_followup_trigger_transcript_presence.py）同属
# review-support scripts，故其测试登记在本 aggregator（tests/test_review_support_scripts.py）。
# ---------------------------------------------------------------------------

def _load_kanban_followup_trigger_transcript_presence_module():
    """加载 transcript 在场判定模块。纯 stdlib 依赖，无需把 SCRIPT_DIR 放进 sys.path。"""
    module_path = Path(SCRIPT_DIR) / "kanban_followup_trigger_transcript_presence.py"
    cached = sys.modules.get("kanban_followup_trigger_transcript_presence_under_test")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "kanban_followup_trigger_transcript_presence_under_test", module_path
    )
    if spec is None or spec.loader is None:
        raise AssertionError(
            f"failed to load kanban_followup_trigger_transcript_presence module at {module_path}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


class _ByteCountingBinaryReadProxy:
    """包住一个二进制文件句柄，记下每次 ``read`` 实际拿到多少字节。

    刻意只实现被测代码用到的那几个方法（上下文管理 / ``seek`` / ``tell`` / ``read``）：多实现
    一个通用文件方法，就多一分「被测代码换了读法而这条测试悄悄失去意义」的风险。
    """

    def __init__(self, handle, read_byte_sizes: list) -> None:
        self._handle = handle
        self._read_byte_sizes = read_byte_sizes

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._handle.__exit__(*exc_info)

    def seek(self, *args, **kwargs):
        return self._handle.seek(*args, **kwargs)

    def tell(self):
        return self._handle.tell()

    def read(self, *args, **kwargs):
        data = self._handle.read(*args, **kwargs)
        self._read_byte_sizes.append(len(data))
        return data


class _TranscriptPathWithByteCountingOpen:
    """替身 ``Path``，只为量出被测模块**真的**从 transcript 读了多少字节。

    刻意不去 monkeypatch ``pathlib.Path.open`` 本身：那会波及测试进程里所有文件访问。这里只
    替换被测模块自己的 ``Path`` 名字，作用域收敛到那一个模块。
    """

    def __init__(self, target, read_byte_sizes: list) -> None:
        self._target = Path(target)
        self._read_byte_sizes = read_byte_sizes

    def expanduser(self):
        return self

    def open(self, *args, **kwargs):
        return _ByteCountingBinaryReadProxy(
            self._target.open(*args, **kwargs), self._read_byte_sizes
        )

    def __str__(self) -> str:
        return str(self._target)


def _write_transcript_with_uuid_chain(
    path: Path,
    *,
    entry_count: int,
    token_literal_at_entry_index: int | None,
    dispatch_token_literal: str,
    padding_width: int,
    trailing_newline: bool = True,
) -> None:
    """写一份 ``parentUuid`` 首尾相连的 transcript，``padding_width`` 用来把文件撑大。"""
    lines = []
    for index in range(entry_count):
        text = "x" * padding_width
        if token_literal_at_entry_index == index:
            text = f"{text}\n{dispatch_token_literal}"
        lines.append(
            json.dumps(
                {
                    "uuid": f"entry-{index}",
                    "parentUuid": None if index == 0 else f"entry-{index - 1}",
                    "type": "user" if index % 2 == 0 else "assistant",
                    "timestamp": "2026-08-08T00:00:00.000Z",
                    "message": {"role": "user", "content": text},
                },
                ensure_ascii=False,
            )
        )
    body = "\n".join(lines)
    if trailing_newline and lines:
        body += "\n"
    path.write_text(body, encoding="utf-8")


def test_kanban_followup_trigger_presence_on_codex_rollout_without_uuid_chain_is_unknown_not_absent(
    tmp: Path,
) -> None:
    """Codex rollout 没有 ``uuid`` / ``parentUuid``：祖先链无从回溯 ⇒ 无结论，**不得**报 ABSENT。

    f03b5 取证：Codex 的用户正文在 ``response_item.payload.content[].text``，条目也不带 uuid，
    于是检测器对 Codex 恒报 ``absent_from_transcript``（「从未落地成回合」）——对一条**确实**落地过、
    甚至已 handoff 的 trigger 下了一个错误的确定性结论。正确的结论是「这种 transcript 我们判不了」。
    Claude 形态（带 uuid 链、却找不到 token）仍是 ABSENT，语义不变。
    """
    presence = _load_kanban_followup_trigger_transcript_presence_module()
    decision_token = "bca5ac470e8d9f18"
    needle = f"RVF_DECISION_TOKEN={decision_token}"
    tmp.mkdir(parents=True, exist_ok=True)

    codex_rollout = tmp / "rollout-2026-10-04T21-03-33-01a10703-34b8-72d0-9bd8-976ad362b12a.jsonl"
    codex_records = [
        {"type": "session_meta", "payload": {"id": "01a10703-34b8-72d0-9bd8-976ad362b12a"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"RVF 发起决策点 · {needle}"}],
            },
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": f"RVF 发起决策点 · {needle}"}},
    ]
    codex_rollout.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in codex_records),
        encoding="utf-8",
    )
    classified_codex = presence.classify_dispatch_trigger_presence(
        transcript_path=codex_rollout,
        dispatch_token=decision_token,
        trigger_literal=needle,
    )
    assert classified_codex.presence == "unknown_transcript_has_no_parseable_ancestor_chain", classified_codex
    assert (
        presence.TRIGGER_PRESENCE_UNKNOWN_TRANSCRIPT_HAS_NO_PARSEABLE_ANCESTOR_CHAIN
        == "unknown_transcript_has_no_parseable_ancestor_chain"
    )
    assert classified_codex.scanned_entry_count == len(codex_records), classified_codex

    claude_transcript_without_token = tmp / "claude-session-without-token.jsonl"
    _write_transcript_with_uuid_chain(
        claude_transcript_without_token,
        entry_count=4,
        token_literal_at_entry_index=None,
        dispatch_token_literal=needle,
        padding_width=8,
    )
    classified_claude = presence.classify_dispatch_trigger_presence(
        transcript_path=claude_transcript_without_token,
        dispatch_token=decision_token,
        trigger_literal=needle,
    )
    assert classified_claude.presence == presence.TRIGGER_PRESENCE_ABSENT_FROM_TRANSCRIPT, classified_claude


def test_kanban_followup_trigger_transcript_scan_reads_only_bounded_tail(tmp: Path) -> None:
    """``DEFAULT_MAX_SCANNED_LINES`` 声明的是**读取**上界，不只是返回值上界。

    原实现是 ``readlines()`` 整份读进内存再切最后 N 行——注释写着「Stop hook 的热路径，必须
    有界」，实际每次 Stop 都按完整会话历史耗时并占内存（长会话 jsonl 几万行、几十 MB），而
    Stop 侧锁对账与 UPS 侧陈旧 prep 否决都会同步走这条路。

    这条测试同时钉住三件事：只读尾部若干块（**不是**整份文件）、尾部条目的顺序与内容不因分块
    读取而变形、以及原有的「坏行跳过 / 不可读不抛」语义没被改动带走。
    """
    presence = _load_kanban_followup_trigger_transcript_presence_module()
    dispatch_token = "0123456789abcdef"
    literal = presence.dispatch_token_literal(dispatch_token)
    tmp.mkdir(parents=True, exist_ok=True)

    # 文件要显著大于若干个读块，否则「没读整份」这个断言可能被块大小偶然满足。
    long_transcript = tmp / "long-session.jsonl"
    _write_transcript_with_uuid_chain(
        long_transcript,
        entry_count=4000,
        token_literal_at_entry_index=3,  # 远在尾部窗口之外
        dispatch_token_literal=literal,
        padding_width=400,
    )
    transcript_size_bytes = long_transcript.stat().st_size
    assert transcript_size_bytes > presence.TAIL_SCAN_CHUNK_BYTES * 3, transcript_size_bytes

    read_byte_sizes: list = []
    original_path_symbol = presence.Path
    presence.Path = lambda target: _TranscriptPathWithByteCountingOpen(target, read_byte_sizes)
    try:
        classified = presence.classify_dispatch_trigger_presence(
            transcript_path=long_transcript,
            dispatch_token=dispatch_token,
            max_scanned_lines=10,
        )
    finally:
        presence.Path = original_path_symbol

    # 只读了尾部：总读入字节数远小于整份文件，且只随块大小走、不随会话长度走。
    total_read_bytes = sum(read_byte_sizes)
    assert total_read_bytes > 0, read_byte_sizes
    assert total_read_bytes < transcript_size_bytes, (total_read_bytes, transcript_size_bytes)
    assert total_read_bytes <= presence.TAIL_SCAN_CHUNK_BYTES * 2, read_byte_sizes
    # 尾部窗口之外的 token 不算「在场」——与原实现一致（截断丢掉的是更早的历史）。
    assert classified.presence == presence.TRIGGER_PRESENCE_ABSENT_FROM_TRANSCRIPT, classified
    assert classified.truncated is True, classified
    assert classified.scanned_entry_count == 10, classified
    assert classified.leaf_entry_uuid == "entry-3999", classified

    # 尾部条目本身必须完好：token 落在最后一条时，祖先链回溯照样认得出来。
    tail_token_transcript = tmp / "tail-token-session.jsonl"
    _write_transcript_with_uuid_chain(
        tail_token_transcript,
        entry_count=4000,
        token_literal_at_entry_index=3999,
        dispatch_token_literal=literal,
        padding_width=400,
    )
    on_chain = presence.classify_dispatch_trigger_presence(
        transcript_path=tail_token_transcript,
        dispatch_token=dispatch_token,
        max_scanned_lines=10,
    )
    assert on_chain.presence == presence.TRIGGER_PRESENCE_ON_ANCESTOR_CHAIN, on_chain
    assert on_chain.trigger_entry_uuid == "entry-3999", on_chain
    assert on_chain.truncated is True, on_chain

    # 短文件（不足上限、且不以换行结尾）：整份都在，truncated 为假；坏行照旧跳过。
    short_transcript = tmp / "short-session.jsonl"
    _write_transcript_with_uuid_chain(
        short_transcript,
        entry_count=3,
        token_literal_at_entry_index=2,
        dispatch_token_literal=literal,
        padding_width=8,
        trailing_newline=False,
    )
    short_transcript.write_text(
        short_transcript.read_text(encoding="utf-8") + "\n{ not json at all\n\n",
        encoding="utf-8",
    )
    short = presence.classify_dispatch_trigger_presence(
        transcript_path=short_transcript,
        dispatch_token=dispatch_token,
        max_scanned_lines=10,
    )
    assert short.presence == presence.TRIGGER_PRESENCE_ON_ANCESTOR_CHAIN, short
    assert short.truncated is False, short
    assert short.scanned_entry_count == 3, short  # 坏行与空行被跳过，没被算成条目

    # 不可读：返回 unknown + error，绝不抛。
    unreadable = presence.classify_dispatch_trigger_presence(
        transcript_path=tmp / "does-not-exist.jsonl",
        dispatch_token=dispatch_token,
        max_scanned_lines=10,
    )
    assert unreadable.presence == presence.TRIGGER_PRESENCE_UNKNOWN_TRANSCRIPT_UNREADABLE, unreadable
    assert unreadable.error, unreadable
    # 复用同一个 _scan 的 has_explicit_human_prompt_after 也不许抛。
    found, error = presence.has_explicit_human_prompt_after(
        transcript_path=tmp / "does-not-exist.jsonl",
        after_epoch_seconds=0.0,
        max_scanned_lines=10,
    )
    assert found is False and error, (found, error)
