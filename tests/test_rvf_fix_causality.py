#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
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
MERGE_DISPOSITIONS_SCRIPT = SCRIPT_DIR / "rvf_merge_dispositions.py"


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


def _register_issue_in_merge_dispositions(run_dir: Path, issue_file: Path) -> None:
    """upsert 的强制点要求 issue_id 被合并处置覆盖：两腿 no_issues，把它登记为主 agent 自审的非 reviewer 来源。

    reviewer plan / 结果缺失时补一份最小的两腿 no_issues；已登记的 issue 保留（处置工件整份重写）。"""
    reviewers = run_dir / "artifacts" / "reviewers"
    if not (reviewers / "reviewer-plan.json").is_file():
        for reviewer_id in ("reviewer-a", "reviewer-b"):
            (reviewers / reviewer_id).mkdir(parents=True, exist_ok=True)
            (reviewers / reviewer_id / "review-result.json").write_text(
                json.dumps(
                    {"schema_version": 1, "kind": "no_issues", "issues": [], "requests": [], "audit_summary": "clean"}
                )
                + "\n",
                encoding="utf-8",
            )
        (reviewers / "reviewer-plan.json").write_text(
            json.dumps(
                {
                    "status": "completed",
                    "reviewers": [{"reviewer_id": "reviewer-a", "returncode": 0}, {"reviewer_id": "reviewer-b", "returncode": 0}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
    issue_id = json.loads(issue_file.read_text(encoding="utf-8"))["issue_id"]
    dispositions_path = run_dir / "artifacts" / "merge" / "merge_dispositions.json"
    sources = (
        json.loads(dispositions_path.read_text(encoding="utf-8"))["non_reviewer_fix_issue_sources"]
        if dispositions_path.is_file()
        else []
    )
    if all(source["issue_id"] != issue_id for source in sources):
        sources.append({"issue_id": issue_id, "origin": "main_agent_self_review"})
    completed = subprocess.run(
        [sys.executable, str(MERGE_DISPOSITIONS_SCRIPT), "write", "--run-dir", str(run_dir)],
        input=json.dumps({"non_reviewer_fix_issue_sources": sources}),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)


def _upsert_issue(repo: Path, run_dir: Path, issue_file: Path, log_root: Path) -> None:
    _register_issue_in_merge_dispositions(run_dir, issue_file)
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
    diff_tracker = _load("reviewable_unit_diff_tracker")
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


def _prepare_attempt(repo: Path, run_dir: Path, log_root: Path, issue_id: str = "RVF-G1") -> dict[str, Any]:
    return _json_output(
        _run(
            [
                sys.executable, str(ATTEMPT_SCRIPT), "prepare", "--repo", str(repo), "--run-dir", str(run_dir),
                "--issue-id", issue_id, "--log-root", str(log_root),
            ]
        )
    )


def _stop_attempt(run_dir: Path, log_root: Path, attempt_id: str, *extra: str, check: bool = False):
    return _run(
        [
            sys.executable, str(ATTEMPT_SCRIPT), "stop", "--attempt-id", attempt_id, "--run-dir", str(run_dir),
            "--log-root", str(log_root), *extra,
        ],
        check=check,
    )


def _apply_attempt(repo: Path, run_dir: Path, log_root: Path, attempt_id: str, *extra: str):
    return _run(
        [
            sys.executable, str(ATTEMPT_SCRIPT), "apply", "--attempt-id", attempt_id, "--target-repo", str(repo),
            "--run-dir", str(run_dir), "--log-root", str(log_root), *extra,
        ],
        check=False,
    )


def test_attempt_protected_path_modification_requires_fixer_acknowledgement_then_main_agent_confirmation(
    tmp_path: Path,
) -> None:
    """护栏而非闸门：改 protected / background 路径不再硬拒成 elevate，而是 fixer 显式确认 + 主会话确认后放行。"""
    repo = _init_repo(tmp_path / "repo")
    (repo / "outside.txt").write_text("outside base\n", encoding="utf-8")
    _git(repo, "add", "outside.txt")
    _git(repo, "commit", "-q", "-m", "add outside")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"], protected_files=["outside.txt"])
    log_root = tmp_path / "state"
    issue = _issue_file(tmp_path)
    (repo / "README.md").write_text("hello user\n", encoding="utf-8")
    _upsert_issue(repo, run_dir, issue, log_root)
    prepared = _prepare_attempt(repo, run_dir, log_root)
    worktree = Path(prepared["worktree_path"])
    (worktree / "README.md").write_text("hello fixed\n", encoding="utf-8")
    (worktree / "outside.txt").write_text("outside changed\n", encoding="utf-8")

    # ① 只当普通外扩声明 ⇒ 拒绝，并给出可照抄的确认参数。
    unacknowledged = _stop_attempt(
        run_dir, log_root, prepared["attempt_id"], "--status", "fixed",
        "--scope-expansion-path", "outside.txt", "--scope-expansion-reason", "README fix must update its fixture",
    )
    assert unacknowledged.returncode == 2
    assert "--acknowledged-protected-or-background-path-modification outside.txt" in unacknowledged.stderr

    # ② fixer 显式确认 ⇒ stop 接受，标记 apply 前需主会话确认。
    acknowledged = _stop_attempt(
        run_dir, log_root, prepared["attempt_id"], "--status", "fixed",
        "--acknowledged-protected-or-background-path-modification", "outside.txt",
        "--scope-expansion-reason", "README fix must update its fixture",
    )
    assert acknowledged.returncode == 0, acknowledged.stderr
    result = _json_output(acknowledged)
    assert result["requires_main_agent_confirmation_before_apply"] is True
    assert result["scope_expansion"]["acknowledged_protected_or_background_paths"] == ["outside.txt"]

    # ③ 主会话未确认 ⇒ 停在待确认，主工作区一字不动。
    pending = _apply_attempt(repo, run_dir, log_root, prepared["attempt_id"])
    assert pending.returncode == 5
    payload = _json_output(pending)
    assert payload["status"] == "requires_main_agent_confirmation_for_acknowledged_protected_or_background_path_modification"
    assert payload["confirmation_checks"][0]["path"] == "outside.txt"
    assert "background / protected" in payload["confirmation_checks"][0]["ownership"]
    assert (repo / "outside.txt").read_text(encoding="utf-8") == "outside base\n"

    # ④ 主会话确认 ⇒ 合回。
    confirmed = _apply_attempt(
        repo, run_dir, log_root, prepared["attempt_id"],
        "--confirm-acknowledged-protected-or-background-path-modification",
    )
    assert confirmed.returncode == 0, confirmed.stderr
    assert (repo / "outside.txt").read_text(encoding="utf-8") == "outside changed\n"
    assert (repo / "README.md").read_text(encoding="utf-8") == "hello fixed\n"


def test_attempt_issue_path_on_background_file_also_requires_acknowledgement(tmp_path: Path) -> None:
    """issue 路径自动进 boundary，但它恰好落在别的会话 WIP 上时同样要显式确认（历史上完全不查）。"""
    repo = _init_repo(tmp_path / "repo")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=[], background_files=["README.md"])
    log_root = tmp_path / "state"
    _upsert_issue(repo, run_dir, _issue_file(tmp_path), log_root)
    prepared = _prepare_attempt(repo, run_dir, log_root)
    (Path(prepared["worktree_path"]) / "README.md").write_text("hello fixed\n", encoding="utf-8")
    stopped = _stop_attempt(run_dir, log_root, prepared["attempt_id"], "--status", "fixed")
    assert stopped.returncode == 2
    assert "--acknowledged-protected-or-background-path-modification README.md" in stopped.stderr


def test_attempt_renaming_background_file_to_declared_expansion_path_still_requires_acknowledgement(
    tmp_path: Path,
) -> None:
    """历史缺陷：``--name-status`` 的 rename 记录只取新路径，把 background 文件 ``git mv`` 到声明过的普通
    外扩路径后旧路径完全隐身，删除它绕过 fixer 确认与主会话确认。旧路径必须按删除计入并按档位分类。"""
    repo = _init_repo(tmp_path / "repo")
    background_text = "other session owns this file\n" * 8
    (repo / "background.txt").write_text(background_text, encoding="utf-8")
    _git(repo, "add", "background.txt")
    _git(repo, "commit", "-q", "-m", "add background")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"], background_files=["background.txt"])
    log_root = tmp_path / "state"
    _upsert_issue(repo, run_dir, _issue_file(tmp_path), log_root)
    prepared = _prepare_attempt(repo, run_dir, log_root)
    worktree = Path(prepared["worktree_path"])
    (worktree / "README.md").write_text("hello fixed\n", encoding="utf-8")
    _git(worktree, "mv", "background.txt", "moved.txt")

    # ① 只声明新路径为普通外扩 ⇒ 旧路径（background）的删除必须被看见并要求确认。
    unacknowledged = _stop_attempt(
        run_dir, log_root, prepared["attempt_id"], "--status", "fixed",
        "--scope-expansion-path", "moved.txt", "--scope-expansion-reason", "rename is part of the fix",
    )
    assert unacknowledged.returncode == 2, unacknowledged.stdout
    assert "--acknowledged-protected-or-background-path-modification background.txt" in unacknowledged.stderr

    # ② 显式确认旧路径 ⇒ stop 接受；rename 两端都进 patch 路径与改动记录。
    acknowledged = _stop_attempt(
        run_dir, log_root, prepared["attempt_id"], "--status", "fixed",
        "--scope-expansion-path", "moved.txt",
        "--acknowledged-protected-or-background-path-modification", "background.txt",
        "--scope-expansion-reason", "rename is part of the fix",
    )
    assert acknowledged.returncode == 0, acknowledged.stderr
    result = _json_output(acknowledged)
    assert result["requires_main_agent_confirmation_before_apply"] is True
    assert result["scope_expansion"]["acknowledged_protected_or_background_paths"] == ["background.txt"]
    assert result["scope_expansion"]["expanded_paths"] == ["moved.txt"]
    assert {"path": "background.txt", "op": "deleted"} in result["all_changed_paths"]
    assert {"path": "moved.txt", "op": "added"} in result["all_changed_paths"]

    # ③ 主会话未确认 ⇒ 停在待确认，主工作区一字不动。
    pending = _apply_attempt(repo, run_dir, log_root, prepared["attempt_id"])
    assert pending.returncode == 5, pending.stderr
    assert [check["path"] for check in _json_output(pending)["confirmation_checks"]] == ["background.txt"]
    assert (repo / "background.txt").read_text(encoding="utf-8") == background_text
    assert not (repo / "moved.txt").exists()

    # ④ 主会话确认 ⇒ rename 完整合回（旧路径删除、新路径出现）。
    confirmed = _apply_attempt(
        repo, run_dir, log_root, prepared["attempt_id"],
        "--confirm-acknowledged-protected-or-background-path-modification",
    )
    assert confirmed.returncode == 0, confirmed.stderr
    assert not (repo / "background.txt").exists()
    assert (repo / "moved.txt").read_text(encoding="utf-8") == background_text
    assert (repo / "README.md").read_text(encoding="utf-8") == "hello fixed\n"


def test_legacy_dirty_path_drift_check_reports_both_rename_ends_unmangled(tmp_path: Path) -> None:
    """``status --porcelain -z`` 的 rename 旧路径是独立的 NUL 字段；历史实现把它当成新条目切掉前 3 个字符。"""
    attempt_module = _load("rvf_fix_attempt")
    repo = _init_repo(tmp_path / "repo")
    (repo / "old_name.txt").write_text("rename me\n" * 8, encoding="utf-8")
    _git(repo, "add", "old_name.txt")
    _git(repo, "commit", "-q", "-m", "add file")
    _git(repo, "mv", "old_name.txt", "new_name.txt")
    assert sorted(attempt_module._dirty_paths(repo, [])) == ["new_name.txt", "old_name.txt"]


def test_attempt_modifying_developer_tool_managed_paths_stays_hard_rejected(tmp_path: Path) -> None:
    """内置工具托管前缀（.serena/）：即使显式确认也拒绝——编辑工具生成状态从来不是有效修复。"""
    repo = _init_repo(tmp_path / "repo")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"], excluded_path_prefixes=[".serena/"])
    log_root = tmp_path / "state"
    _upsert_issue(repo, run_dir, _issue_file(tmp_path), log_root)
    prepared = _prepare_attempt(repo, run_dir, log_root)
    worktree = Path(prepared["worktree_path"])
    (worktree / "README.md").write_text("hello fixed\n", encoding="utf-8")
    (worktree / ".serena").mkdir()
    (worktree / ".serena" / "cache.json").write_text("{}\n", encoding="utf-8")
    stopped = _stop_attempt(
        run_dir, log_root, prepared["attempt_id"], "--status", "fixed",
        "--acknowledged-protected-or-background-path-modification", ".serena/cache.json",
        "--scope-expansion-reason", "why not",
    )
    assert stopped.returncode == 2
    assert "developer-tool-managed" in stopped.stderr


def test_attempt_auto_status_never_silently_swallows_changes_outside_the_boundary(tmp_path: Path) -> None:
    """历史缺陷：改动全在 boundary 外且未声明 ⇒ patch 为空 ⇒ 被静默判成 false_positive、校验整体跳过。"""
    repo = _init_repo(tmp_path / "repo")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"])
    log_root = tmp_path / "state"
    _upsert_issue(repo, run_dir, _issue_file(tmp_path), log_root)
    prepared = _prepare_attempt(repo, run_dir, log_root)
    (Path(prepared["worktree_path"]) / "elsewhere.txt").write_text("sneaky\n", encoding="utf-8")
    stopped = _stop_attempt(run_dir, log_root, prepared["attempt_id"])
    assert stopped.returncode == 2
    assert "outside the attempt boundary" in stopped.stderr


def test_apply_by_attempt_id_refuses_attempts_that_did_not_stop_as_fixed(tmp_path: Path) -> None:
    """历史缺陷：``--attempt-id`` 点名时不查状态，误报 / 升级 attempt 留下的改动也能合回。"""
    repo = _init_repo(tmp_path / "repo")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"])
    log_root = tmp_path / "state"
    _upsert_issue(repo, run_dir, _issue_file(tmp_path), log_root)
    prepared = _prepare_attempt(repo, run_dir, log_root)
    (Path(prepared["worktree_path"]) / "README.md").write_text("hello wrongly edited\n", encoding="utf-8")
    stopped = _stop_attempt(run_dir, log_root, prepared["attempt_id"], "--status", "false_positive")
    assert stopped.returncode == 0, stopped.stderr
    assert any("not applied" in warning for warning in _json_output(stopped)["warnings"])
    applied = _apply_attempt(repo, run_dir, log_root, prepared["attempt_id"])
    assert applied.returncode == 2
    assert "only fixed attempts are applied" in applied.stderr
    assert (repo / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_attempt_worktree_materializes_the_full_working_tree_including_renames(tmp_path: Path) -> None:
    """观察面 = 真实工作区：boundary 外的 dirty / untracked 文件都在，``git mv`` 两端都正确（旧路径不在）。"""
    repo = _init_repo(tmp_path / "repo")
    (repo / "old_name.py").write_text("print('old')\n", encoding="utf-8")
    (repo / "unrelated.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "files")
    _git(repo, "mv", "old_name.py", "new_name.py")
    (repo / "unrelated.txt").write_text("someone else's dirty work\n", encoding="utf-8")
    (repo / "helpers").mkdir()
    (repo / "helpers" / "generated_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"])
    log_root = tmp_path / "state"
    _upsert_issue(repo, run_dir, _issue_file(tmp_path), log_root)
    prepared = _prepare_attempt(repo, run_dir, log_root)
    worktree = Path(prepared["worktree_path"])
    assert (worktree / "new_name.py").read_text(encoding="utf-8") == "print('old')\n"
    assert not (worktree / "old_name.py").exists()
    assert (worktree / "unrelated.txt").read_text(encoding="utf-8") == "someone else's dirty work\n"
    assert (worktree / "helpers" / "generated_module.py").is_file()
    # baseline 已含全部 WIP：fixer 没动任何东西时 diff 为空。
    assert _git(worktree, "status", "--porcelain").strip() == ""
    assert prepared["fix_issue_handling_mode"] == "validate_then_fix"


def test_attempt_worktree_materialization_resolves_directory_file_layout_swaps(tmp_path: Path) -> None:
    """被跟踪目录 -> 同路径未跟踪文件、被跟踪文件 -> 同路径目录，attempt 布局都必须与主工作区一致。"""
    repo = _init_repo(tmp_path / "repo")
    (repo / "dir_becomes_file").mkdir()
    (repo / "dir_becomes_file" / "inner.txt").write_text("tracked\n", encoding="utf-8")
    (repo / "file_becomes_dir").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "layout base")
    shutil.rmtree(repo / "dir_becomes_file")
    (repo / "dir_becomes_file").write_text("now a file\n", encoding="utf-8")
    (repo / "file_becomes_dir").unlink()
    (repo / "file_becomes_dir").mkdir()
    (repo / "file_becomes_dir" / "nested.txt").write_text("now nested\n", encoding="utf-8")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"])
    log_root = tmp_path / "state"
    _upsert_issue(repo, run_dir, _issue_file(tmp_path), log_root)
    prepared = _prepare_attempt(repo, run_dir, log_root)
    worktree = Path(prepared["worktree_path"])
    assert (worktree / "dir_becomes_file").is_file()
    assert (worktree / "dir_becomes_file").read_text(encoding="utf-8") == "now a file\n"
    assert (worktree / "file_becomes_dir").is_dir()
    assert (worktree / "file_becomes_dir" / "nested.txt").read_text(encoding="utf-8") == "now nested\n"
    assert not (worktree / "file_becomes_dir" / "file_becomes_dir").exists()
    assert _git(worktree, "status", "--porcelain").strip() == ""


def test_dirty_expansion_path_unchanged_since_prepare_applies_and_drift_still_conflicts(tmp_path: Path) -> None:
    """全量物化后，prepare 前就脏、之后没再变的外扩路径可以合回；之后又被改过的仍判冲突。"""
    repo = _init_repo(tmp_path / "repo")
    (repo / "outside.txt").write_text("outside base\n", encoding="utf-8")
    _git(repo, "add", "outside.txt")
    _git(repo, "commit", "-q", "-m", "add outside")
    (repo / "outside.txt").write_text("outside base\nlocal wip\n", encoding="utf-8")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"])
    log_root = tmp_path / "state"
    _upsert_issue(repo, run_dir, _issue_file(tmp_path), log_root)
    prepared = _prepare_attempt(repo, run_dir, log_root)
    worktree = Path(prepared["worktree_path"])
    assert (worktree / "outside.txt").read_text(encoding="utf-8") == "outside base\nlocal wip\n"
    (worktree / "outside.txt").write_text("outside base\nlocal wip\nfix\n", encoding="utf-8")
    _stop_attempt(
        run_dir, log_root, prepared["attempt_id"], "--status", "fixed",
        "--scope-expansion-path", "outside.txt", "--scope-expansion-reason", "linked fixture", check=True,
    )
    applied = _apply_attempt(repo, run_dir, log_root, prepared["attempt_id"])
    assert applied.returncode == 0, applied.stderr
    assert (repo / "outside.txt").read_text(encoding="utf-8") == "outside base\nlocal wip\nfix\n"


def _write_fake_kanban_task_command(tmp_path: Path) -> tuple[str, Path]:
    calls = tmp_path / "fake-kanban-calls.jsonl"
    fake = tmp_path / "fake_kanban.py"
    fake.write_text(
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'ok': True, 'task': {'id': 'fakeb'}}))\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {fake}", calls


def test_validate_only_attempt_must_not_modify_files_and_submits_validated_finding_to_kanban_backlog(
    tmp_path: Path,
) -> None:
    """范围外问题：只验证、不修（硬性）；属实就由 validator 在 stop 内提交到 Kanban backlog。"""
    repo = _init_repo(tmp_path / "repo")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"])
    log_root = tmp_path / "state"
    issue = _issue_file(tmp_path)
    payload = json.loads(issue.read_text(encoding="utf-8"))
    payload["fix_issue_handling_mode"] = "validate_only_then_submit_to_kanban_backlog"
    issue.write_text(json.dumps(payload), encoding="utf-8")
    _upsert_issue(repo, run_dir, issue, log_root)
    prepared = _prepare_attempt(repo, run_dir, log_root)
    assert prepared["fix_issue_handling_mode"] == "validate_only_then_submit_to_kanban_backlog"
    worktree = Path(prepared["worktree_path"])

    # ① 想修 ⇒ 拒绝（fixed 不是合法收尾；有改动也拒绝）。
    (worktree / "README.md").write_text("hello fixed\n", encoding="utf-8")
    assert _stop_attempt(run_dir, log_root, prepared["attempt_id"], "--status", "fixed").returncode == 2
    modified = _stop_attempt(run_dir, log_root, prepared["attempt_id"], "--status", "false_positive")
    assert modified.returncode == 2 and "must not modify files" in modified.stderr
    (worktree / "README.md").write_text("hello\n", encoding="utf-8")

    # ② 属实却没给 finding 文件 ⇒ 拒绝。
    missing = _stop_attempt(
        run_dir, log_root, prepared["attempt_id"], "--status", "validated_and_submitted_to_kanban_backlog"
    )
    assert missing.returncode == 2 and "--kanban-backlog-finding-file" in missing.stderr

    # ③ 给出 finding ⇒ 建卡（fake kanban），issue 终态 submitted_to_kanban_backlog。
    finding = tmp_path / "finding.json"
    finding.write_text(json.dumps({"findings": [{
        "title": "README 问候语不完整", "path": "README.md", "line": 1, "severity": "low",
        "problem_statement": "问候语缺少收件人，与文档约定不符。", "evidence": "cat README.md",
    }]}), encoding="utf-8")
    task_cmd, calls = _write_fake_kanban_task_command(tmp_path)
    env = {**__import__("os").environ, "CODEX_RVF_CLINE_KANBAN_TASK_CMD": task_cmd,
           "RVF_HOME": str(tmp_path / "rvf-home"), "CODEX_RVF_CLINE_KANBAN_BASE_REF": "main"}
    submitted = subprocess.run(
        [sys.executable, str(ATTEMPT_SCRIPT), "stop", "--attempt-id", prepared["attempt_id"], "--run-dir", str(run_dir),
         "--log-root", str(log_root), "--status", "validated_and_submitted_to_kanban_backlog",
         "--kanban-backlog-finding-file", str(finding)],
        capture_output=True, text=True, env=env, check=False,
    )
    assert submitted.returncode == 0, submitted.stderr
    result = json.loads(submitted.stdout)
    group = result["kanban_backlog_submission"]["groups"][0]
    assert group["outcome"] == "created_kanban_backlog_task" and group["kanban_task_id"] == "fakeb"
    argv = json.loads(calls.read_text(encoding="utf-8").splitlines()[0])
    assert argv[:2] == ["task", "create"] or argv[0] == "create"
    assert "--start-in-plan-mode" in argv and "--parent-session-id" not in argv
    prompt = argv[argv.index("--prompt") + 1]
    assert prompt.startswith("KANBAN_TASK_ORIGIN=rvf_validated_out_of_scope_finding\n")
    assert "KANBAN_SUPPORT_AGENT_RELATION_TRIAGE_STATUS=not_triaged" in prompt
    assert argv[argv.index("--title") + 1].startswith("[RVF 发现]")
    causality = _json_output(
        _run([sys.executable, str(ATTEMPT_SCRIPT), "status", "--repo", str(repo), "--run-dir", str(run_dir),
              "--log-root", str(log_root)])
    )
    states = {item.get("issue_id"): item.get("state") for item in causality.get("issues", [])}
    assert states.get("RVF-G1") == "submitted_to_kanban_backlog", causality


def _write_kanban_backlog_finding_file(tmp_path: Path) -> Path:
    finding = tmp_path / "finding.json"
    finding.write_text(json.dumps({"findings": [{
        "title": "README 问候语不完整", "path": "README.md", "line": 1, "severity": "low",
        "problem_statement": "问候语缺少收件人，与文档约定不符。",
    }]}), encoding="utf-8")
    return finding


def _kanban_backlog_submission_env(tmp_path: Path, rvf_home: Path) -> dict[str, str]:
    task_cmd, _ = _write_fake_kanban_task_command(tmp_path)
    return {**__import__("os").environ, "CODEX_RVF_CLINE_KANBAN_TASK_CMD": task_cmd,
            "RVF_HOME": str(rvf_home), "CODEX_RVF_CLINE_KANBAN_BASE_REF": "main"}


def test_validate_only_stop_with_failed_kanban_backlog_submission_stays_retryable_instead_of_terminal(
    tmp_path: Path,
) -> None:
    """历史缺陷：提交抛错被降成 submission_error，stop 照样把 attempt / issue 记成已提交 backlog 的终态（放跑）。"""
    repo = _init_repo(tmp_path / "repo")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"])
    log_root = tmp_path / "state"
    issue = _issue_file(tmp_path)
    payload = json.loads(issue.read_text(encoding="utf-8"))
    payload["fix_issue_handling_mode"] = "validate_only_then_submit_to_kanban_backlog"
    issue.write_text(json.dumps(payload), encoding="utf-8")
    _upsert_issue(repo, run_dir, issue, log_root)
    prepared = _prepare_attempt(repo, run_dir, log_root)
    attempt_directory = run_dir / "artifacts" / "fix-attempts" / prepared["attempt_id"]
    finding = _write_kanban_backlog_finding_file(tmp_path)
    # RVF_HOME 是个普通文件 ⇒ ledger / pending 都落不了盘：卡片与 pending 一条持久事实都留不下。
    unwritable_rvf_home = tmp_path / "rvf-home-is-a-regular-file"
    unwritable_rvf_home.write_text("not a directory\n", encoding="utf-8")
    stop_argv = [
        sys.executable, str(ATTEMPT_SCRIPT), "stop", "--attempt-id", prepared["attempt_id"], "--run-dir", str(run_dir),
        "--log-root", str(log_root), "--status", "validated_and_submitted_to_kanban_backlog",
        "--kanban-backlog-finding-file", str(finding),
    ]
    failed = subprocess.run(
        stop_argv, capture_output=True, text=True, env=_kanban_backlog_submission_env(tmp_path, unwritable_rvf_home),
        check=False,
    )
    assert failed.returncode == 3, failed.stdout + failed.stderr
    assert f"fix stop --attempt-id {prepared['attempt_id']}" in failed.stderr
    assert f"--kanban-backlog-finding-file {finding}" in failed.stderr
    # 没有半写的 result.json、attempt 状态没推进、tracker 里 issue 不是 backlog 终态。
    assert not (attempt_directory / "result.json").exists()
    assert json.loads((attempt_directory / "attempt.json").read_text(encoding="utf-8"))["status"] == "prepared"
    status_argv = [sys.executable, str(ATTEMPT_SCRIPT), "status", "--repo", str(repo), "--run-dir", str(run_dir),
                   "--log-root", str(log_root)]
    states = {item.get("issue_id"): item.get("state") for item in _json_output(_run(status_argv)).get("issues", [])}
    assert states.get("RVF-G1") != "submitted_to_kanban_backlog"

    # 排除原因后照抄重跑 ⇒ 正常收尾。
    retried = subprocess.run(
        stop_argv, capture_output=True, text=True,
        env=_kanban_backlog_submission_env(tmp_path, tmp_path / "rvf-home"), check=False,
    )
    assert retried.returncode == 0, retried.stderr
    assert json.loads(retried.stdout)["kanban_backlog_submission"]["groups"][0]["outcome"] == "created_kanban_backlog_task"
    states = {item.get("issue_id"): item.get("state") for item in _json_output(_run(status_argv)).get("issues", [])}
    assert states.get("RVF-G1") == "submitted_to_kanban_backlog"


def test_validate_then_fix_stop_with_failed_attached_kanban_backlog_submission_is_not_silent(tmp_path: Path) -> None:
    """fixer 附带提交的范围外发现同样不放跑：提交失败 ⇒ stop 非 0，attempt 不进 fixed、不写 result.json。"""
    repo = _init_repo(tmp_path / "repo")
    run_dir = _run_dir(tmp_path, repo, fix_allowlist=["README.md"])
    log_root = tmp_path / "state"
    _upsert_issue(repo, run_dir, _issue_file(tmp_path), log_root)
    prepared = _prepare_attempt(repo, run_dir, log_root)
    (Path(prepared["worktree_path"]) / "README.md").write_text("hello fixed\n", encoding="utf-8")
    unwritable_rvf_home = tmp_path / "rvf-home-is-a-regular-file"
    unwritable_rvf_home.write_text("not a directory\n", encoding="utf-8")
    failed = subprocess.run(
        [sys.executable, str(ATTEMPT_SCRIPT), "stop", "--attempt-id", prepared["attempt_id"], "--run-dir", str(run_dir),
         "--log-root", str(log_root), "--status", "fixed",
         "--kanban-backlog-finding-file", str(_write_kanban_backlog_finding_file(tmp_path))],
        capture_output=True, text=True, env=_kanban_backlog_submission_env(tmp_path, unwritable_rvf_home), check=False,
    )
    assert failed.returncode == 3, failed.stdout + failed.stderr
    assert "--status fixed" in failed.stderr
    attempt_directory = run_dir / "artifacts" / "fix-attempts" / prepared["attempt_id"]
    assert not (attempt_directory / "result.json").exists()
    assert json.loads((attempt_directory / "attempt.json").read_text(encoding="utf-8"))["status"] == "prepared"


def test_tracker_rebuild_admits_kanban_backlog_states_without_losing_causality_rows(tmp_path: Path) -> None:
    """旧库的 CHECK 约束不认 backlog 两个新状态：重建表时外键关闭，patch 事件与因果链接一行不丢。"""
    diff_tracker = _load("reviewable_unit_diff_tracker")
    db_path = tmp_path / "tracker.sqlite3"
    legacy = sqlite3.connect(db_path, isolation_level=None)
    legacy.executescript(
        diff_tracker.DDL.replace(",'submitted_to_kanban_backlog'", "").replace(
            ",'validated_and_submitted_to_kanban_backlog'", ""
        )
    )
    legacy.execute(f"PRAGMA user_version = {diff_tracker.SCHEMA_VERSION}")
    legacy.execute("PRAGMA foreign_keys = ON")
    legacy.execute("INSERT INTO rvf_issues VALUES ('k','r','run','i','{}','[]',NULL,'open','t','t')")
    legacy.execute(
        "INSERT INTO rvf_fix_attempts(attempt_id,issue_key,repo_key,run_id,issue_id,worktree_path,status,created_at,"
        "updated_at) VALUES ('a','k','r','run','i','/w','started','t','t')"
    )
    legacy.execute("INSERT INTO rvf_fix_patch_events VALUES ('p','a','k','r','run','i','x.py','modified',NULL,NULL,NULL,'t')")
    legacy.execute("INSERT INTO rvf_issue_patch_links VALUES ('k','a','p','t')")
    legacy.close()
    conn = diff_tracker._open_conn(db_path)
    try:
        assert conn.execute("SELECT count(*) FROM rvf_fix_patch_events").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM rvf_issue_patch_links").fetchone()[0] == 1
        conn.execute("UPDATE rvf_issues SET state='submitted_to_kanban_backlog'")
        conn.execute("UPDATE rvf_fix_attempts SET status='validated_and_submitted_to_kanban_backlog'")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_attempt_apply_rejects_scope_expansion_target_changed_after_prepare(tmp_path: Path) -> None:
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
    # prepare 之后有人改了这个外扩路径 ⇒ 合回会覆盖并发改动 ⇒ 冲突。
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
