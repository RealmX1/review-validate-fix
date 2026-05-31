#!/usr/bin/env python3
"""失败再入「重开评审范围」一次性 marker 模块的单元测试。"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))


def _isolate_state(tmp_path: Path) -> None:
    """让本测试进程的 log_root() 落到 tmp_path 下，避免污染本机 state。"""
    os.environ["CODEX_RVF_LOG_ROOT"] = str(tmp_path / "state")
    os.environ.pop("CODEX_RVF_STATE_DIR", None)
    os.environ.pop("CODEX_RVF_INSTALLED_SKILL_DIR", None)
    os.environ.pop("CODEX_RVF_REVIEW_REOPEN_TTL_SECONDS", None)


def test_marker_paths_prefers_task_then_session(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import review_reopen_marker as rrm

    paths = rrm.marker_paths(task_id="abc", session_id="sess-x")
    assert len(paths) == 2
    assert paths[0].name.startswith("task-")
    assert paths[1].name.startswith("sess-")

    only_task = rrm.marker_paths(task_id="abc", session_id=None)
    assert len(only_task) == 1 and only_task[0].name.startswith("task-")

    only_sess = rrm.marker_paths(task_id=None, session_id="sess-x")
    assert len(only_sess) == 1 and only_sess[0].name.startswith("sess-")

    assert rrm.marker_paths(task_id=None, session_id=None) == []


def test_marker_paths_can_use_explicit_root(tmp_path: Path) -> None:
    _isolate_state(tmp_path / "default")
    import review_reopen_marker as rrm

    explicit_root = tmp_path / "explicit-state"
    paths = rrm.marker_paths(task_id="abc", session_id="sess-x", root=explicit_root)
    assert all(
        str(path).startswith(str(explicit_root / "review-reopen-pending"))
        for path in paths
    )


def test_write_then_read_returns_marker(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import review_reopen_marker as rrm

    target = rrm.write_review_reopen_marker(
        task_id="fe662",
        session_id="sess-x",
        target_run_id="rvf-20260530T144027Z-stop-hook-06127eaf",
        repo="/x/repo",
    )
    assert target is not None
    assert target.name == "task-fe662.json"

    marker = rrm.read_review_reopen_marker(task_id="fe662", session_id="sess-x")
    assert marker is not None
    assert marker["target_run_id"] == "rvf-20260530T144027Z-stop-hook-06127eaf"
    assert marker["repo"] == "/x/repo"
    assert marker["reason"] == "failed_impl_reentry"
    assert marker["source"] == "rvf_rescope"
    assert marker["state"] == "pending_reopen"
    assert marker["marker_version"] == rrm.MARKER_VERSION
    assert marker["kanban_task_id"] == "fe662"
    assert marker["parent_session_id"] == "sess-x"
    assert rrm.review_reopen_status(marker) == rrm.STATUS_ACTIVE


def test_write_returns_none_when_no_key(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import review_reopen_marker as rrm

    assert (
        rrm.write_review_reopen_marker(
            task_id=None, session_id=None, target_run_id="x", repo=None
        )
        is None
    )


def test_session_fallback_when_no_task(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import review_reopen_marker as rrm

    target = rrm.write_review_reopen_marker(
        task_id=None,
        session_id="sess-only",
        target_run_id="rvf-R2",
        repo=None,
    )
    assert target is not None
    assert target.name == "sess-sess-only.json"
    marker = rrm.read_review_reopen_marker(task_id=None, session_id="sess-only")
    assert marker is not None and marker["target_run_id"] == "rvf-R2"


def test_clear_consumes_marker(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import review_reopen_marker as rrm

    rrm.write_review_reopen_marker(
        task_id="fe662", session_id="sess-x", target_run_id="rvf-R1", repo="/x"
    )
    removed = rrm.clear_review_reopen_marker(task_id="fe662", session_id="sess-x")
    assert removed and removed[0].endswith("task-fe662.json")
    assert rrm.read_review_reopen_marker(task_id="fe662", session_id="sess-x") is None
    # idempotent: clearing again removes nothing.
    assert rrm.clear_review_reopen_marker(task_id="fe662", session_id="sess-x") == []


def test_status_active_stale_invalid(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import review_reopen_marker as rrm

    rrm.write_review_reopen_marker(
        task_id="fe662", session_id=None, target_run_id="rvf-R1", repo="/x"
    )
    active = rrm.read_review_reopen_marker(task_id="fe662", session_id=None)
    assert rrm.review_reopen_status(active) == rrm.STATUS_ACTIVE

    stale = dict(active)
    stale["expires_at"] = "2000-01-01T00:00:00Z"
    assert rrm.review_reopen_status(stale) == rrm.STATUS_STALE

    # expires_at missing → fall back to armed_at + TTL.
    fallback_stale = dict(active)
    fallback_stale.pop("expires_at", None)
    fallback_stale["armed_at"] = "2000-01-01T00:00:00Z"
    assert rrm.review_reopen_status(fallback_stale) == rrm.STATUS_STALE

    assert rrm.review_reopen_status(None) == rrm.STATUS_INVALID
    assert rrm.review_reopen_status({}) == rrm.STATUS_INVALID
    assert rrm.review_reopen_status({"armed_at": "not-a-ts"}) == rrm.STATUS_INVALID


def test_ttl_env_override(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import review_reopen_marker as rrm

    assert rrm.ttl_seconds() == float(rrm.DEFAULT_TTL_SECONDS)
    os.environ["CODEX_RVF_REVIEW_REOPEN_TTL_SECONDS"] = "10"
    try:
        assert rrm.ttl_seconds() == 10.0
    finally:
        os.environ.pop("CODEX_RVF_REVIEW_REOPEN_TTL_SECONDS", None)
    # malformed → default
    os.environ["CODEX_RVF_REVIEW_REOPEN_TTL_SECONDS"] = "not-a-number"
    try:
        assert rrm.ttl_seconds() == float(rrm.DEFAULT_TTL_SECONDS)
    finally:
        os.environ.pop("CODEX_RVF_REVIEW_REOPEN_TTL_SECONDS", None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    args = parser.parse_args()
    if args.shard_count < 1:
        raise SystemExit("--shard-count must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise SystemExit("--shard-index must be in [0, shard-count)")

    tests = [
        test_marker_paths_prefers_task_then_session,
        test_marker_paths_can_use_explicit_root,
        test_write_then_read_returns_marker,
        test_write_returns_none_when_no_key,
        test_session_fallback_when_no_task,
        test_clear_consumes_marker,
        test_status_active_stale_invalid,
        test_ttl_env_override,
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        selected = [
            test
            for index, test in enumerate(tests)
            if args.shard_count <= 1 or index % args.shard_count == args.shard_index
        ]
        for test in selected:
            test(root / test.__name__)
    suffix = (
        f" shard {args.shard_index + 1}/{args.shard_count}"
        if args.shard_count > 1
        else ""
    )
    print(f"review reopen marker tests OK{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
