#!/usr/bin/env python3
"""diff tracker 注册与 scope 测试簇。

从 tests/test_review_support_scripts.py 有界抽出（导航用拆分，行为不变）。共享 helper/常量
（run/read_jsonl/load_*_module/路径常量等）仍归 aggregator 所有，经 inject() 在注册表运行前推入
本模块 globals，避免与 __main__ 脚本循环导入。注册表 lambda 不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import os
import sys
import json
import subprocess
import time
from pathlib import Path

# 由 aggregator（tests/test_review_support_scripts.py）在导入后 inject 注入共享依赖。
__all__ = [
    'test_diff_tracker_register_creates_sqlite_and_events',
    'test_diff_tracker_register_concurrent_writers',
    'test_diff_tracker_hunk_anchor_distinguishes_close_hunks',
    'test_diff_tracker_register_empty_owned_paths_preserves_session_claim',
    'test_diff_tracker_list_conflicts_reports_other_session_overlap',
    'test_diff_tracker_path_claim_conflicts_with_hunk_claim',
    'test_diff_tracker_disable_env_short_circuits',
    'test_diff_tracker_lock_timeout_degrades_gracefully',
    'test_diff_tracker_observes_committed_round_units',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_diff_tracker_register_creates_sqlite_and_events(tmp: Path) -> None:
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    result = module.register_claims(
        repo=repo,
        session_id="session-1",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert result.status == "ok"
    assert result.repo_key
    assert result.tracker_dir
    tracker_path = Path(result.tracker_dir)
    # Slice 2-A: tracker dir lives under diff-tracker/repos/<key>/
    assert "diff-tracker" in tracker_path.parts and "repos" in tracker_path.parts
    db_path = tracker_path / "tracker.sqlite3"
    assert db_path.is_file()
    assert (tracker_path / "events.jsonl").is_file()
    assert (tracker_path / "meta.json").is_file()
    conn = _sqlite.connect(str(db_path))
    try:
        units = conn.execute(
            "SELECT path, kind, observed_state, review_state FROM units"
        ).fetchall()
        assert len(units) == 1, units
        assert units[0][0] == "tracked.txt"
        assert units[0][1] == "tracked_hunk"
        assert units[0][2] == "dirty"
        assert units[0][3] == "available"
        sessions = conn.execute(
            "SELECT session_id FROM sessions"
        ).fetchall()
        assert {row[0] for row in sessions} == {"session-1"}
        session_units = conn.execute(
            "SELECT session_id, assignment_kind FROM session_units"
        ).fetchall()
        assert session_units == [("session-1", "owned")]
    finally:
        conn.close()
    events = read_jsonl(tracker_path / "events.jsonl")
    assert any(event.get("event") == "claim_added" for event in events)
    # claim_ids are now content-addressed sha256 unit_ids — sanity check shape.
    assert len(result.claim_ids) == 1
    assert len(result.claim_ids[0]) == 64


def test_diff_tracker_register_concurrent_writers(tmp: Path) -> None:
    load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    (repo / "second.txt").write_text("base\nedit b\n", encoding="utf-8")
    run(["git", "add", "second.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "add second"], cwd=repo)
    (repo / "second.txt").write_text("base\nedit b session-2\n", encoding="utf-8")
    log_root = tmp / "logs"

    # Both child processes block until the same absolute wall-clock timestamp
    # before calling register_claims. Without this barrier the first proc
    # routinely finishes before the second one even imports diff_tracker, so
    # the flock/contention path is never exercised — the test would only
    # confirm "two sequential writers don't drop each other's claims".
    snippet = (
        "import os, sys, time, json\n"
        f"sys.path.insert(0, {str(SCRIPT_DIR)!r})\n"
        "from pathlib import Path\n"
        # Bump busy_timeout high enough that the second writer can wait out
        # the first's lock even under load (4-shard contract checks run several
        # tests in parallel, slowing each register_claims's git calls).
        "os.environ.setdefault('CODEX_RVF_TRACKER_BUSY_TIMEOUT_MS', '30000')\n"
        "import diff_tracker as dt\n"
        f"log_root = Path({str(log_root)!r})\n"
        f"repo = Path({str(repo)!r})\n"
        "session = sys.argv[1]\n"
        "path = sys.argv[2]\n"
        "wait_until = float(os.environ['CONCURRENT_WAIT_UNTIL'])\n"
        "remaining = wait_until - time.time()\n"
        "if remaining > 0:\n"
        "    time.sleep(remaining)\n"
        "result = dt.register_claims(\n"
        "    repo=repo, session_id=session, run_id=session,\n"
        "    worktree=None, branch=None,\n"
        "    owned_paths=[path], apply_patch_paths={path}, exec_only_paths=set(),\n"
        "    log_root_override=log_root,\n"
        ")\n"
        "print(json.dumps(result.to_dict()))\n"
    )
    # Give both subprocesses ~1.5s to start and import before they unblock.
    wait_until = time.time() + 1.5
    env = {**os.environ, "CONCURRENT_WAIT_UNTIL": f"{wait_until:.6f}"}
    procs = []
    for session, path in (("session-A", "tracked.txt"), ("session-B", "second.txt")):
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", snippet, session, path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        )
    outputs = [proc.communicate() for proc in procs]
    for stdout, stderr in outputs:
        if stderr.strip():
            raise AssertionError(stderr.strip())
        payload = json.loads(stdout.strip().splitlines()[-1])
        assert payload["status"] == "ok"
    import sqlite3 as _sqlite
    repo_key = json.loads(outputs[0][0].splitlines()[-1])["repo_key"]
    db_path = log_root / "diff-tracker" / "repos" / repo_key / "tracker.sqlite3"
    conn = _sqlite.connect(str(db_path))
    try:
        sessions = {row[0] for row in conn.execute("SELECT session_id FROM sessions").fetchall()}
    finally:
        conn.close()
    assert sessions == {"session-A", "session-B"}


def test_diff_tracker_hunk_anchor_distinguishes_close_hunks(tmp: Path) -> None:
    """Two distinct edits in the same file must yield two distinct claim_ids
    on first register, and rerunning the same session must NOT drop or fold
    them together. This guards against the regression where deriving anchors
    via `git diff -U0` produced empty `context_lines`, collapsing every
    fuzzy-match decision down to "ranges within ±5 lines".
    """
    module = load_diff_tracker_module()
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    # 14-line baseline so two well-separated edits stay as two distinct hunks
    # under -U3 (gap of 8 unchanged lines between them — beyond the 6-line
    # context window where git would otherwise merge adjacent hunks).
    baseline = "".join(f"line-{i}\n" for i in range(1, 15))
    (repo / "tracked.txt").write_text(baseline, encoding="utf-8")
    run(["git", "add", "tracked.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    edited_lines = [f"line-{i}\n" for i in range(1, 15)]
    edited_lines[0] = "LINE-1\n"    # change line 1
    edited_lines[9] = "LINE-10\n"   # change line 10 → 2 hunks under -U3
    (repo / "tracked.txt").write_text("".join(edited_lines), encoding="utf-8")
    log_root = tmp / "logs"

    import sqlite3 as _sqlite

    first = module.register_claims(
        repo=repo,
        session_id="session-close-hunks",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert first.status == "ok", first.to_dict()
    # Two distinct hunks → two distinct unit_ids.
    assert len(first.claim_ids) == 2, first.claim_ids
    assert len(set(first.claim_ids)) == 2, first.claim_ids

    db_path = Path(first.tracker_dir) / "tracker.sqlite3"
    conn = _sqlite.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT unit_id, hunk_header FROM units WHERE kind='tracked_hunk' ORDER BY hunk_header"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2, rows
    headers = {row[1] for row in rows}
    assert len(headers) == 2, headers
    unit_ids = {row[0] for row in rows}
    assert len(unit_ids) == 2, unit_ids

    # Rerun must be idempotent: same unit_ids, no stale drops, units unchanged.
    second = module.register_claims(
        repo=repo,
        session_id="session-close-hunks",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert second.status == "ok"
    assert sorted(first.claim_ids) == sorted(second.claim_ids)
    assert second.dropped_stale_claim_ids == []
    conn = _sqlite.connect(str(db_path))
    try:
        rows2 = conn.execute("SELECT unit_id FROM units WHERE kind='tracked_hunk'").fetchall()
    finally:
        conn.close()
    assert len(rows2) == 2


def test_diff_tracker_register_empty_owned_paths_preserves_session_claim(tmp: Path) -> None:
    """A second register call with an empty owned_paths list must NOT drop
    the session's existing claims — that path used to fall through to the
    drop-all branch, silently moving every claim into tombstones.
    """
    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    seed = module.register_claims(
        repo=repo,
        session_id="session-empty",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    import sqlite3 as _sqlite

    assert seed.status == "ok"
    db_path = Path(seed.tracker_dir) / "tracker.sqlite3"
    conn = _sqlite.connect(str(db_path))
    try:
        before = conn.execute("SELECT unit_id FROM session_units WHERE session_id='session-empty'").fetchall()
        before_tomb = conn.execute("SELECT tombstone_id FROM tombstones").fetchall()
    finally:
        conn.close()
    assert len(before) == 1
    assert len(before_tomb) == 0

    noop = module.register_claims(
        repo=repo,
        session_id="session-empty",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=[],
        apply_patch_paths=set(),
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert noop.status == "no_paths", noop.to_dict()
    assert noop.claim_ids == []
    assert noop.dropped_stale_claim_ids == []

    conn = _sqlite.connect(str(db_path))
    try:
        after = conn.execute("SELECT unit_id FROM session_units WHERE session_id='session-empty'").fetchall()
        after_tomb = conn.execute("SELECT tombstone_id FROM tombstones").fetchall()
    finally:
        conn.close()
    assert len(after) == 1
    assert after[0][0] == seed.claim_ids[0]
    assert len(after_tomb) == 0


def test_diff_tracker_list_conflicts_reports_other_session_overlap(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    module.register_claims(
        repo=repo,
        session_id="session-A",
        run_id="run-A",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    units = [module.OwnedUnit(path="tracked.txt", unit="path", hunk_anchor=None)]
    conflicts = module.list_conflicts(
        repo,
        current_session_id="session-B",
        owned_units=units,
        log_root_override=log_root,
    )
    assert len(conflicts) == 1
    payload = conflicts[0].to_dict()
    assert payload["other_session_id"] == "session-A"
    assert payload["path"] == "tracked.txt"
    same_session = module.list_conflicts(
        repo,
        current_session_id="session-A",
        owned_units=units,
        log_root_override=log_root,
    )
    assert same_session == []


def test_diff_tracker_path_claim_conflicts_with_hunk_claim(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    # session-A claims tracked.txt with hunk evidence (apply_patch).
    module.register_claims(
        repo=repo,
        session_id="session-A",
        run_id="run-A",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    # session-B comes in with only a path-level claim — it must overlap.
    units = [module.OwnedUnit(path="tracked.txt", unit="path", hunk_anchor=None)]
    conflicts = module.list_conflicts(
        repo,
        current_session_id="session-B",
        owned_units=units,
        log_root_override=log_root,
    )
    assert len(conflicts) == 1
    assert conflicts[0].to_dict()["unit"] == "hunk"


def test_diff_tracker_disable_env_short_circuits(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    previous = os.environ.get("CODEX_RVF_TRACKER_DISABLE")

    def _run_with_disable_value(value: str | None) -> object:
        if value is None:
            os.environ.pop("CODEX_RVF_TRACKER_DISABLE", None)
        else:
            os.environ["CODEX_RVF_TRACKER_DISABLE"] = value
        return module.register_claims(
            repo=repo,
            session_id="session-1",
            run_id="run-1",
            worktree=None,
            branch=None,
            owned_paths=["tracked.txt"],
            apply_patch_paths={"tracked.txt"},
            exec_only_paths=set(),
            log_root_override=log_root,
        )

    try:
        # Truthy values disable.
        assert _run_with_disable_value("1").status == "disabled"
        assert not (log_root / "diff-tracker").exists()
        # `no` / `off` / `false` must NOT disable — they read as "do not
        # disable", matching user intuition. Previously they silently
        # disabled because the check was a blacklist.
        for falsy in ("no", "off", "false", "False", "NO"):
            res = _run_with_disable_value(falsy)
            assert res.status == "ok", f"value={falsy!r} unexpectedly disabled tracker"
    finally:
        if previous is None:
            os.environ.pop("CODEX_RVF_TRACKER_DISABLE", None)
        else:
            os.environ["CODEX_RVF_TRACKER_DISABLE"] = previous


def test_diff_tracker_lock_timeout_degrades_gracefully(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    # Pre-register so the sqlite file exists.
    seed = module.register_claims(
        repo=repo,
        session_id="seed",
        run_id="seed",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert seed.status == "ok"
    db_path = Path(seed.tracker_dir) / "tracker.sqlite3"
    # External holder takes a BEGIN IMMEDIATE write lock and sleeps so the
    # next BEGIN IMMEDIATE inside register_claims must contend for it.
    blocker_script = (
        "import sqlite3, sys, time\n"
        "conn = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=10)\n"
        "conn.execute('BEGIN IMMEDIATE')\n"
        "sys.stdout.write('LOCKED\\n'); sys.stdout.flush()\n"
        "time.sleep(float(sys.argv[2]))\n"
        "conn.execute('ROLLBACK')\n"
        "conn.close()\n"
    )
    blocker = subprocess.Popen(
        [sys.executable, "-c", blocker_script, str(db_path), "5"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = blocker.stdout.readline()
        assert line.strip() == "LOCKED", f"blocker did not acquire lock; got: {line!r}"
        # Shrink busy_timeout so the test stays fast.
        os.environ["CODEX_RVF_TRACKER_BUSY_TIMEOUT_MS"] = "300"
        try:
            result = module.register_claims(
                repo=repo,
                session_id="session-blocked",
                run_id="run-blocked",
                worktree=None,
                branch=None,
                owned_paths=["tracked.txt"],
                apply_patch_paths={"tracked.txt"},
                exec_only_paths=set(),
                log_root_override=log_root,
            )
        finally:
            os.environ.pop("CODEX_RVF_TRACKER_BUSY_TIMEOUT_MS", None)
    finally:
        blocker.terminate()
        blocker.wait(timeout=5)
    assert result.status == "lock_timeout"


def test_diff_tracker_observes_committed_round_units(tmp: Path) -> None:
    """A committed hunk yields the SAME unit_id as the equivalent dirty
    observation — the content-identity invariant that makes dedup free (§2/§4)."""
    dt, _sm, _rbm = _round_baseline_committed_modules()
    repo, baseline = _committed_round_repo(tmp)
    # Observe the change while dirty.
    (repo / "f.txt").write_text("base\nadded\n", encoding="utf-8")
    dirty_obs = dt._classify_path(repo, "f.txt")
    dirty_ids = sorted(s.unit_id for s in dt._specs_from_observation(dirty_obs, "f.txt"))
    # Commit it; worktree is now clean.
    run(["git", "add", "f.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "work"], cwd=repo)
    assert run(["git", "status", "--porcelain"], cwd=repo).stdout.strip() == ""
    committed_obs = dt._classify_committed_path(repo, "f.txt", baseline)
    assert committed_obs is not None and committed_obs.kind == "tracked_hunk"
    committed_ids = sorted(s.unit_id for s in dt._specs_from_observation(committed_obs, "f.txt"))
    assert committed_ids == dirty_ids, (committed_ids, dirty_ids)
    assert dt._list_committed_round_changed_paths(repo, baseline) == ["f.txt"]

