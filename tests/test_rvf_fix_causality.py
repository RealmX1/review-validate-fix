#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
)
ISSUE_SCRIPT = SCRIPT_DIR / "rvf_fix_issue.py"
ATTEMPT_SCRIPT = SCRIPT_DIR / "rvf_fix_attempt.py"


from _rvf_test_support.loader import load_script_module as _load


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return completed.stdout


def _run(args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "rvf-test@example.com")
    _git(path, "config", "user.name", "RVF Tester")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _write_scope_contract(
    run_dir: Path,
    repo: Path,
    run_id: str,
    fix_allowlist: list[str],
    *,
    background_files: list[str] | None = None,
    protected_files: list[str] | None = None,
    excluded_path_prefixes: list[str] | None = None,
) -> None:
    path = run_dir / "artifacts" / "inputs" / "scope.contract.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "repo": str(repo),
                "run_id": run_id,
                "fix_allowlist": fix_allowlist,
                "background_files": background_files or [],
                "protected_files": protected_files or [],
                "excluded_path_prefixes": excluded_path_prefixes or [],
                "canonical_scope": {
                    "fix_allowlist": fix_allowlist,
                    "background_files": background_files or [],
                    "protected_files": protected_files or [],
                    "excluded_path_prefixes": excluded_path_prefixes or [],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _run_dir(
    tmp_path: Path,
    repo: Path,
    run_id: str = "rvf-test-run",
    fix_allowlist: list[str] | None = None,
    background_files: list[str] | None = None,
    protected_files: list[str] | None = None,
    excluded_path_prefixes: list[str] | None = None,
) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(
        json.dumps({"run_id": run_id, "repo": str(repo)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_scope_contract(
        run_dir,
        repo,
        run_id,
        fix_allowlist or ["README.md"],
        background_files=background_files,
        protected_files=protected_files,
        excluded_path_prefixes=excluded_path_prefixes,
    )
    return run_dir


def _issue_file(tmp_path: Path, run_id: str = "rvf-test-run", issue_id: str = "RVF-G1") -> Path:
    path = tmp_path / "issue.json"
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "issue_id": issue_id,
                "kind": "REAL",
                "severity": "medium",
                "path": "README.md",
                "line": 1,
                "summary": "README greeting is incomplete",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _json_output(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


def _upsert_issue(repo: Path, run_dir: Path, issue_file: Path, log_root: Path) -> None:
    _run(
        [
            sys.executable,
            str(ISSUE_SCRIPT),
            "upsert",
            "--repo",
            str(repo),
            "--run-dir",
            str(run_dir),
            "--issue-file",
            str(issue_file),
            "--log-root",
            str(log_root),
        ]
    )


def test_diff_tracker_creates_and_migrates_rvf_causality_tables(tmp_path: Path) -> None:
    diff_tracker = _load("diff_tracker")
    db_path = tmp_path / "tracker.sqlite3"
    conn = diff_tracker._open_conn(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == diff_tracker.SCHEMA_VERSION
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "rvf_issues" in tables
        assert "rvf_fix_attempts" in tables
        assert "rvf_fix_patch_events" in tables
        assert "rvf_issue_patch_links" in tables
    finally:
        conn.close()

    legacy_db = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(legacy_db)
    legacy.execute("PRAGMA user_version = 3")
    legacy.execute("CREATE TABLE manual_rvf_runs(session_id TEXT, run_id TEXT, scope_hash TEXT, completed_at TEXT)")
    legacy.execute("INSERT INTO manual_rvf_runs VALUES ('s', 'r', 'h', 'now')")
    legacy.commit()
    legacy.close()
    conn = diff_tracker._open_conn(legacy_db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == diff_tracker.SCHEMA_VERSION
        assert conn.execute("SELECT run_id FROM manual_rvf_runs").fetchone()[0] == "r"
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='rvf_issues'").fetchone() is not None
    finally:
        conn.close()


def test_issue_scoped_attempt_exports_incremental_patch_and_applies(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_dir = _run_dir(tmp_path, repo)
    log_root = tmp_path / "state"
    issue = _issue_file(tmp_path)
    (repo / "README.md").write_text("hello user\n", encoding="utf-8")
    _upsert_issue(repo, run_dir, issue, log_root)

    prepared = _json_output(
        _run(
            [
                sys.executable,
                str(ATTEMPT_SCRIPT),
                "prepare",
                "--repo",
                str(repo),
                "--run-dir",
                str(run_dir),
                "--issue-id",
                "RVF-G1",
                "--log-root",
                str(log_root),
            ]
        )
    )
    attempt_id = prepared["attempt_id"]
    worktree = Path(prepared["worktree_path"])
    assert (worktree / "README.md").read_text(encoding="utf-8") == "hello user\n"
    assert _git(worktree, "status", "--porcelain") == ""

    _run([sys.executable, str(ATTEMPT_SCRIPT), "start", "--attempt-id", attempt_id, "--run-dir", str(run_dir), "--log-root", str(log_root)])
    (worktree / "README.md").write_text("hello fixed\n", encoding="utf-8")
    stopped = _json_output(
        _run(
            [
                sys.executable,
                str(ATTEMPT_SCRIPT),
                "stop",
                "--attempt-id",
                attempt_id,
                "--run-dir",
                str(run_dir),
                "--status",
                "fixed",
                "--log-root",
                str(log_root),
            ]
        )
    )
    assert stopped["changed_paths"] == [{"op": "modified", "path": "README.md"}]
    assert "hello fixed" in Path(stopped["fix_patch_path"]).read_text(encoding="utf-8")

    applied = _json_output(
        _run(
            [
                sys.executable,
                str(ATTEMPT_SCRIPT),
                "apply",
                "--attempt-id",
                attempt_id,
                "--target-repo",
                str(repo),
                "--run-dir",
                str(run_dir),
                "--log-root",
                str(log_root),
            ]
        )
    )
    assert applied["status"] == "applied"
    assert (repo / "README.md").read_text(encoding="utf-8") == "hello fixed\n"

    status = _json_output(
        _run(
            [
                sys.executable,
                str(ATTEMPT_SCRIPT),
                "status",
                "--repo",
                str(repo),
                "--run-id",
                "rvf-test-run",
                "--log-root",
                str(log_root),
            ]
        )
    )
    assert status["status"] == "found"
    assert status["issues"][0]["state"] == "fixed"
    assert status["issues"][0]["candidate_patch_call_ids"] == []
    assert status["issues"][0]["fix_patch_paths"]
    assert status["patch_events"][0]["path"] == "README.md"


def test_attempt_baseline_includes_allowlisted_dirty_files(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "allowed.txt").write_text("allowed base\n", encoding="utf-8")
    _git(repo, "add", "allowed.txt")
    _git(repo, "commit", "-q", "-m", "add allowed")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md", "allowed.txt"])
    log_root = tmp_path / "state"
    issue = _issue_file(tmp_path)
    (repo / "README.md").write_text("hello user\n", encoding="utf-8")
    (repo / "allowed.txt").write_text("allowed dirty\n", encoding="utf-8")
    _upsert_issue(repo, run_dir, issue, log_root)

    prepared = _json_output(
        _run(
            [
                sys.executable,
                str(ATTEMPT_SCRIPT),
                "prepare",
                "--repo",
                str(repo),
                "--run-dir",
                str(run_dir),
                "--issue-id",
                "RVF-G1",
                "--log-root",
                str(log_root),
            ]
        )
    )
    worktree = Path(prepared["worktree_path"])

    assert prepared["boundary_paths"] == ["README.md", "allowed.txt"]
    assert (worktree / "README.md").read_text(encoding="utf-8") == "hello user\n"
    assert (worktree / "allowed.txt").read_text(encoding="utf-8") == "allowed dirty\n"
    assert _git(worktree, "status", "--porcelain") == ""


def test_attempt_stop_includes_declared_scope_expansion(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "outside.txt").write_text("outside base\n", encoding="utf-8")
    _git(repo, "add", "outside.txt")
    _git(repo, "commit", "-q", "-m", "add outside")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"])
    log_root = tmp_path / "state"
    issue = _issue_file(tmp_path)
    (repo / "README.md").write_text("hello user\n", encoding="utf-8")
    _upsert_issue(repo, run_dir, issue, log_root)

    prepared = _json_output(
        _run(
            [
                sys.executable,
                str(ATTEMPT_SCRIPT),
                "prepare",
                "--repo",
                str(repo),
                "--run-dir",
                str(run_dir),
                "--issue-id",
                "RVF-G1",
                "--log-root",
                str(log_root),
            ]
        )
    )
    attempt_id = prepared["attempt_id"]
    worktree = Path(prepared["worktree_path"])
    (worktree / "README.md").write_text("hello fixed\n", encoding="utf-8")
    (worktree / "outside.txt").write_text("outside changed\n", encoding="utf-8")

    stopped = _json_output(
        _run(
            [
                sys.executable,
                str(ATTEMPT_SCRIPT),
                "stop",
                "--attempt-id",
                attempt_id,
                "--run-dir",
                str(run_dir),
                "--status",
                "fixed",
                "--scope-expansion-path",
                "outside.txt",
                "--scope-expansion-reason",
                "README fix must update its linked fixture",
                "--log-root",
                str(log_root),
            ]
        )
    )
    patch_text = Path(stopped["fix_patch_path"]).read_text(encoding="utf-8")

    assert stopped["changed_paths"] == [
        {"op": "modified", "path": "README.md"},
        {"op": "modified", "path": "outside.txt"},
    ]
    assert stopped["scope_expansion"]["expanded_paths"] == ["outside.txt"]
    assert stopped["scope_expansion"]["reason"] == "README fix must update its linked fixture"
    assert "hello fixed" in patch_text
    assert "outside changed" in patch_text


def test_attempt_stop_rejects_undeclared_scope_expansion(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "outside.txt").write_text("outside base\n", encoding="utf-8")
    _git(repo, "add", "outside.txt")
    _git(repo, "commit", "-q", "-m", "add outside")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"])
    log_root = tmp_path / "state"
    issue = _issue_file(tmp_path)
    (repo / "README.md").write_text("hello user\n", encoding="utf-8")
    _upsert_issue(repo, run_dir, issue, log_root)

    prepared = _json_output(
        _run(
            [
                sys.executable,
                str(ATTEMPT_SCRIPT),
                "prepare",
                "--repo",
                str(repo),
                "--run-dir",
                str(run_dir),
                "--issue-id",
                "RVF-G1",
                "--log-root",
                str(log_root),
            ]
        )
    )
    worktree = Path(prepared["worktree_path"])
    (worktree / "README.md").write_text("hello fixed\n", encoding="utf-8")
    (worktree / "outside.txt").write_text("outside changed\n", encoding="utf-8")

    stopped = _run(
        [
            sys.executable,
            str(ATTEMPT_SCRIPT),
            "stop",
            "--attempt-id",
            prepared["attempt_id"],
            "--run-dir",
            str(run_dir),
            "--status",
            "fixed",
            "--log-root",
            str(log_root),
        ],
        check=False,
    )

    assert stopped.returncode == 2
    assert "undeclared allowlist-external changes" in stopped.stderr


def test_attempt_stop_rejects_scope_expansion_without_reason(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "outside.txt").write_text("outside base\n", encoding="utf-8")
    _git(repo, "add", "outside.txt")
    _git(repo, "commit", "-q", "-m", "add outside")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"])
    log_root = tmp_path / "state"
    issue = _issue_file(tmp_path)
    (repo / "README.md").write_text("hello user\n", encoding="utf-8")
    _upsert_issue(repo, run_dir, issue, log_root)

    prepared = _json_output(
        _run(
            [
                sys.executable,
                str(ATTEMPT_SCRIPT),
                "prepare",
                "--repo",
                str(repo),
                "--run-dir",
                str(run_dir),
                "--issue-id",
                "RVF-G1",
                "--log-root",
                str(log_root),
            ]
        )
    )
    worktree = Path(prepared["worktree_path"])
    (worktree / "README.md").write_text("hello fixed\n", encoding="utf-8")
    (worktree / "outside.txt").write_text("outside changed\n", encoding="utf-8")

    stopped = _run(
        [
            sys.executable,
            str(ATTEMPT_SCRIPT),
            "stop",
            "--attempt-id",
            prepared["attempt_id"],
            "--run-dir",
            str(run_dir),
            "--status",
            "fixed",
            "--scope-expansion-path",
            "outside.txt",
            "--log-root",
            str(log_root),
        ],
        check=False,
    )

    assert stopped.returncode == 2
    assert "no reason was provided" in stopped.stderr


def test_attempt_stop_rejects_protected_scope_expansion(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "outside.txt").write_text("outside base\n", encoding="utf-8")
    _git(repo, "add", "outside.txt")
    _git(repo, "commit", "-q", "-m", "add outside")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"], protected_files=["outside.txt"])
    log_root = tmp_path / "state"
    issue = _issue_file(tmp_path)
    (repo / "README.md").write_text("hello user\n", encoding="utf-8")
    _upsert_issue(repo, run_dir, issue, log_root)

    prepared = _json_output(
        _run(
            [
                sys.executable,
                str(ATTEMPT_SCRIPT),
                "prepare",
                "--repo",
                str(repo),
                "--run-dir",
                str(run_dir),
                "--issue-id",
                "RVF-G1",
                "--log-root",
                str(log_root),
            ]
        )
    )
    worktree = Path(prepared["worktree_path"])
    (worktree / "README.md").write_text("hello fixed\n", encoding="utf-8")
    (worktree / "outside.txt").write_text("outside changed\n", encoding="utf-8")

    stopped = _run(
        [
            sys.executable,
            str(ATTEMPT_SCRIPT),
            "stop",
            "--attempt-id",
            prepared["attempt_id"],
            "--run-dir",
            str(run_dir),
            "--status",
            "fixed",
            "--scope-expansion-path",
            "outside.txt",
            "--scope-expansion-reason",
            "README fix must update its linked fixture",
            "--log-root",
            str(log_root),
        ],
        check=False,
    )

    assert stopped.returncode == 2
    assert "protected/background/excluded paths" in stopped.stderr


def test_attempt_apply_rejects_dirty_scope_expansion_target(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "outside.txt").write_text("outside base\n", encoding="utf-8")
    _git(repo, "add", "outside.txt")
    _git(repo, "commit", "-q", "-m", "add outside")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"])
    log_root = tmp_path / "state"
    issue = _issue_file(tmp_path)
    (repo / "README.md").write_text("hello user\n", encoding="utf-8")
    _upsert_issue(repo, run_dir, issue, log_root)

    prepared = _json_output(
        _run(
            [
                sys.executable,
                str(ATTEMPT_SCRIPT),
                "prepare",
                "--repo",
                str(repo),
                "--run-dir",
                str(run_dir),
                "--issue-id",
                "RVF-G1",
                "--log-root",
                str(log_root),
            ]
        )
    )
    worktree = Path(prepared["worktree_path"])
    (worktree / "README.md").write_text("hello fixed\n", encoding="utf-8")
    (worktree / "outside.txt").write_text("outside changed\n", encoding="utf-8")
    _run(
        [
            sys.executable,
            str(ATTEMPT_SCRIPT),
            "stop",
            "--attempt-id",
            prepared["attempt_id"],
            "--run-dir",
            str(run_dir),
            "--status",
            "fixed",
            "--scope-expansion-path",
            "outside.txt",
            "--scope-expansion-reason",
            "README fix must update its linked fixture",
            "--log-root",
            str(log_root),
        ]
    )
    (repo / "outside.txt").write_text("outside user dirty\n", encoding="utf-8")

    applied = _run(
        [
            sys.executable,
            str(ATTEMPT_SCRIPT),
            "apply",
            "--attempt-id",
            prepared["attempt_id"],
            "--target-repo",
            str(repo),
            "--run-dir",
            str(run_dir),
            "--log-root",
            str(log_root),
        ],
        check=False,
    )

    assert applied.returncode == 4
    payload = _json_output(applied)
    assert payload["status"] == "scope_expansion_conflict"
    assert "outside.txt" in payload["stderr"]


def test_parallel_attempts_conflict_on_second_apply(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_dir = _run_dir(tmp_path, repo)
    log_root = tmp_path / "state"
    issue = _issue_file(tmp_path)
    (repo / "README.md").write_text("hello user\n", encoding="utf-8")
    _upsert_issue(repo, run_dir, issue, log_root)

    attempt_ids: list[str] = []
    for replacement in ("hello one\n", "hello two\n"):
        prepared = _json_output(
            _run(
                [
                    sys.executable,
                    str(ATTEMPT_SCRIPT),
                    "prepare",
                    "--repo",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                    "--issue-id",
                    "RVF-G1",
                    "--log-root",
                    str(log_root),
                ]
            )
        )
        attempt_id = prepared["attempt_id"]
        attempt_ids.append(attempt_id)
        worktree = Path(prepared["worktree_path"])
        (worktree / "README.md").write_text(replacement, encoding="utf-8")
        _run(
            [
                sys.executable,
                str(ATTEMPT_SCRIPT),
                "stop",
                "--attempt-id",
                attempt_id,
                "--run-dir",
                str(run_dir),
                "--status",
                "fixed",
                "--log-root",
                str(log_root),
            ]
        )

    _run([sys.executable, str(ATTEMPT_SCRIPT), "apply", "--attempt-id", attempt_ids[0], "--target-repo", str(repo), "--run-dir", str(run_dir), "--log-root", str(log_root)])
    second = _run(
        [
            sys.executable,
            str(ATTEMPT_SCRIPT),
            "apply",
            "--attempt-id",
            attempt_ids[1],
            "--target-repo",
            str(repo),
            "--run-dir",
            str(run_dir),
            "--log-root",
            str(log_root),
        ],
        check=False,
    )
    assert second.returncode == 3
    assert _json_output(second)["status"] == "merge_conflict"
