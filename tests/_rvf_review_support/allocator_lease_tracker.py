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
__all__ = ['test_allocate_review_scope_busy_timeout_degrades', 'test_allocate_review_scope_concurrent_writers_serialize', 'test_allocate_review_scope_disable_env_short_circuits', 'test_allocate_review_scope_dry_run_does_not_create_lease', 'test_allocate_review_scope_emits_valid_tracker_scope_json', 'test_allocate_review_scope_empty_returns_no_unassigned_review_scope', 'test_allocate_review_scope_excludes_active_leased_units', 'test_allocate_review_scope_inserts_lease_and_marks_units_assigned', 'test_allocate_review_scope_output_consumed_by_prepare_run', 'test_allocate_review_scope_preserves_untracked_file_under_new_directory', 'test_allocate_review_scope_prunes_stale_leases_first', 'test_allocate_review_scope_writes_paths_and_hunks', 'test_allocator_event_appended_to_events_jsonl', 'test_complete_review_scope_does_not_complete_failed_released_lease', 'test_complete_review_scope_keeps_different_scope_active_lease', 'test_complete_review_scope_keeps_partial_edit_claim_pending', 'test_complete_review_scope_supersedes_overlapping_active_lease', 'test_complete_review_scope_unions_contract_and_lease_units', 'test_fork_first_stop_takeover_transfers_unleased_units', 'test_fork_takeover_skips_actively_leased_units', 'test_heartbeat_refreshes_tracker_lease_and_records_backend', 'test_heartbeat_treats_same_second_expiry_as_expired', 'test_lease_acquire_concurrent_writers_serialize', 'test_lease_acquire_creates_lease_and_assigns_units', 'test_lease_acquire_prunes_stale_leases_first', 'test_lease_acquire_rejects_tombstoned_unit', 'test_lease_acquire_rejects_when_any_unit_already_leased', 'test_lease_participants_finish_does_not_release_shared_lease', 'test_lease_refresh_extends_expires_at', 'test_lease_refresh_returns_expired_when_past_ttl', 'test_lease_release_completed_marks_units_reviewed', 'test_lease_release_idempotent', 'test_manual_rvf_run_ensures_table_for_existing_v2_db', 'test_manual_rvf_run_find_respects_ttl', 'test_manual_rvf_run_find_returns_latest_completed_at', 'test_manual_rvf_run_inserts_row_and_emits_event', 'test_manual_rvf_run_upserts_on_pk_conflict', 'test_manual_takeover_cli_records_takeover', 'test_manual_takeover_rejects_missing_parent_session', 'test_manual_takeover_skips_actively_leased_units', 'test_manual_takeover_transfers_unleased_units', 'test_record_manual_run_cli_writes_row', 'test_run_alternative_reviewer_releases_lease_on_codex_backend_challenge', 'test_run_alternative_reviewer_releases_lease_on_normal_exit', 'test_run_alternative_reviewer_releases_lease_on_timeout', 'test_run_alternative_reviewer_shared_lease_does_not_release_on_exit', 'test_run_alternative_reviewer_sigterm_kills_child_before_release', 'test_scope_hash_is_sha256_of_sorted_unit_ids', 'test_stale_prune_does_not_release_unit_reacquired_by_fresh_lease', 'test_sweep_stale_no_op_when_all_active_leases_fresh', 'test_sweep_stale_releases_expired_active_leases', 'test_sweep_stale_releases_same_second_expired_lease', 'test_tracker_schema_v2_migrates_lease_participants_table', 'test_tracker_schema_v4_rebuilds_legacy_tombstoned_review_state']


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
            f"diff_tracker.py allocate-review-scope failed (exit {completed.returncode}):\n"
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
        f"sys.path.insert(0, {str(SCRIPT_DIR)!r})\n"
        "from pathlib import Path\n"
        "os.environ.setdefault('CODEX_RVF_TRACKER_BUSY_TIMEOUT_MS', '30000')\n"
        "import diff_tracker as dt\n"
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
        extra_env={"CODEX_RVF_TRACKER_DISABLE": "1"},
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
            extra_env={"CODEX_RVF_TRACKER_BUSY_TIMEOUT_MS": "300"},
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
        session_id="sess-T15",
        run_id="run-T15",
        output_scope=output_scope,
    )
    assert allocator["status"] == "allocated"
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=output_scope, log_root=log_root
    )
    assert completed.returncode == 0
    contract = json.loads((artifacts_dir / "inputs" / "scope.contract.json").read_text(encoding="utf-8"))
    assert contract["version"] == 2
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
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [sys.executable, "-c", reviewer_code],
        idle_timeout_seconds=0.2,
        activity_check_interval_seconds=0.05,
        max_runtime_seconds=max_runtime_seconds,
        output_format=output_format,
    )
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root), "CODEX_RVF_LEASE_HEARTBEAT_SECONDS": "0.05"}
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
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [sys.executable, "-c", clean_review_result_python()],
        idle_timeout_seconds=0.2,
        activity_check_interval_seconds=0.05,
    )
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root), "CODEX_RVF_LEASE_HEARTBEAT_SECONDS": "0.05"}
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
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [sys.executable, "-c", reviewer_code],
        idle_timeout_seconds=60,
        activity_check_interval_seconds=0.05,
        max_runtime_seconds=60,
    )
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root), "CODEX_RVF_LEASE_HEARTBEAT_SECONDS": "0.05"}
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
        f"sys.path.insert(0, {str(SCRIPT_DIR)!r})\n"
        "from pathlib import Path\n"
        "os.environ.setdefault('CODEX_RVF_TRACKER_BUSY_TIMEOUT_MS', '30000')\n"
        "import diff_tracker as dt\n"
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
