#!/usr/bin/env python3
"""一次性 post-analyze quiet marker 模块的单元测试。"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
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


def _seed_artifacts(tmp_path: Path, *, mtime: float | None = None) -> tuple[Path, Path, Path]:
    run_dir = tmp_path / "runs" / "rvf-fake"
    analysis_dir = run_dir / "artifacts" / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    summary_md = analysis_dir / "summary.md"
    causality_json = analysis_dir / "causality.json"
    summary_md.write_text("# fake summary\n", encoding="utf-8")
    causality_json.write_text("{}\n", encoding="utf-8")
    if mtime is not None:
        os.utime(summary_md, (mtime, mtime))
        os.utime(causality_json, (mtime, mtime))
    return run_dir, summary_md, causality_json


def test_marker_paths_prefers_task_then_session(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import post_analyze_quiet as paq

    paths = paq.marker_paths(task_id="abc", session_id="sess-x")
    assert len(paths) == 2
    assert paths[0].name.startswith("task-")
    assert paths[1].name.startswith("sess-")

    only_task = paq.marker_paths(task_id="abc", session_id=None)
    assert len(only_task) == 1 and only_task[0].name.startswith("task-")

    only_sess = paq.marker_paths(task_id=None, session_id="sess-x")
    assert len(only_sess) == 1 and only_sess[0].name.startswith("sess-")

    empty = paq.marker_paths(task_id=None, session_id=None)
    assert empty == []


def test_marker_paths_can_use_explicit_root(tmp_path: Path) -> None:
    _isolate_state(tmp_path / "default")
    import post_analyze_quiet as paq

    explicit_root = tmp_path / "explicit-state"
    paths = paq.marker_paths(task_id="abc", session_id="sess-x", root=explicit_root)
    assert all(str(path).startswith(str(explicit_root / "post-analyze-quiet")) for path in paths)


def test_write_then_read_returns_marker(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import post_analyze_quiet as paq

    run_dir, summary_md, causality_json = _seed_artifacts(tmp_path)
    target = paq.write_post_analyze_quiet_marker(
        task_id="task-T1",
        session_id="sess-1",
        armed_run_id="rvf-run-A",
        armed_handoff_path=str(run_dir / "artifacts" / "handoff.md"),
        analyze_run_dir=str(run_dir),
        analyze_summary_md=str(summary_md),
        analyze_causality_json=str(causality_json),
        kanban_attempt_id="attempt-9",
    )
    assert target is not None and target.exists()
    assert target.name.startswith("task-")

    read = paq.read_post_analyze_quiet_marker(task_id="task-T1", session_id="sess-1")
    assert read is not None
    assert read["marker_version"] == paq.MARKER_VERSION
    assert read["armed_run_id"] == "rvf-run-A"
    assert read["kanban_task_id"] == "task-T1"
    assert read["kanban_attempt_id"] == "attempt-9"
    assert read["analyze_summary_md"] == str(summary_md)
    assert read["analyze_causality_json"] == str(causality_json)


def test_write_returns_none_when_no_key(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import post_analyze_quiet as paq

    target = paq.write_post_analyze_quiet_marker(
        task_id=None,
        session_id=None,
        armed_run_id="rvf-x",
        armed_handoff_path=None,
        analyze_run_dir=str(tmp_path),
        analyze_summary_md=str(tmp_path / "s.md"),
        analyze_causality_json=str(tmp_path / "c.json"),
    )
    assert target is None


def test_clear_removes_marker(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import post_analyze_quiet as paq

    run_dir, summary_md, causality_json = _seed_artifacts(tmp_path)
    paq.write_post_analyze_quiet_marker(
        task_id="task-T2",
        session_id=None,
        armed_run_id="rvf-run-B",
        armed_handoff_path=None,
        analyze_run_dir=str(run_dir),
        analyze_summary_md=str(summary_md),
        analyze_causality_json=str(causality_json),
    )
    assert paq.read_post_analyze_quiet_marker(task_id="task-T2", session_id=None) is not None

    removed = paq.clear_post_analyze_quiet_marker(task_id="task-T2", session_id=None)
    assert len(removed) == 1
    assert paq.read_post_analyze_quiet_marker(task_id="task-T2", session_id=None) is None

    # idempotent: second clear is a no-op without raising.
    removed_again = paq.clear_post_analyze_quiet_marker(task_id="task-T2", session_id=None)
    assert removed_again == []


def test_workflow_complete_true_when_artifacts_fresh(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import post_analyze_quiet as paq

    armed_ts = time.time() - 30
    _, summary_md, causality_json = _seed_artifacts(tmp_path, mtime=armed_ts + 10)
    marker = {
        "armed_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(armed_ts)
        ),
        "analyze_summary_md": str(summary_md),
        "analyze_causality_json": str(causality_json),
    }
    assert paq.post_analyze_workflow_complete(marker) is True


def test_workflow_complete_false_when_missing_artifact(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import post_analyze_quiet as paq

    armed_ts = time.time() - 30
    _, summary_md, _causality_json = _seed_artifacts(tmp_path, mtime=armed_ts + 10)
    marker = {
        "armed_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(armed_ts)
        ),
        "analyze_summary_md": str(summary_md),
        "analyze_causality_json": str(tmp_path / "missing.json"),
    }
    assert paq.post_analyze_workflow_complete(marker) is False


def test_workflow_complete_false_when_summary_still_has_todo(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import post_analyze_quiet as paq

    armed_ts = time.time() - 30
    _, summary_md, causality_json = _seed_artifacts(tmp_path, mtime=armed_ts + 10)
    summary_md.write_text("<!-- TODO(rvf-analyze): fill narrative -->\n", encoding="utf-8")
    marker = {
        "armed_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(armed_ts)
        ),
        "analyze_summary_md": str(summary_md),
        "analyze_causality_json": str(causality_json),
    }
    assert paq.post_analyze_workflow_complete(marker) is False


def test_workflow_complete_false_when_artifact_older_than_armed_at(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import post_analyze_quiet as paq

    armed_ts = time.time()
    # 文件 mtime 比 armed_at 还早 60s 模拟 "analyze 没真的再写"。
    _, summary_md, causality_json = _seed_artifacts(tmp_path, mtime=armed_ts - 60)
    marker = {
        "armed_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(armed_ts)
        ),
        "analyze_summary_md": str(summary_md),
        "analyze_causality_json": str(causality_json),
    }
    assert paq.post_analyze_workflow_complete(marker) is False


def test_workflow_complete_false_when_marker_malformed(tmp_path: Path) -> None:
    _isolate_state(tmp_path)
    import post_analyze_quiet as paq

    assert paq.post_analyze_workflow_complete(None) is False
    assert paq.post_analyze_workflow_complete({}) is False
    assert paq.post_analyze_workflow_complete(
        {"armed_at": "not-a-timestamp", "analyze_summary_md": "x", "analyze_causality_json": "y"}
    ) is False


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
        test_clear_removes_marker,
        test_workflow_complete_true_when_artifacts_fresh,
        test_workflow_complete_false_when_missing_artifact,
        test_workflow_complete_false_when_summary_still_has_todo,
        test_workflow_complete_false_when_artifact_older_than_armed_at,
        test_workflow_complete_false_when_marker_malformed,
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
    print(f"post-analyze quiet tests OK{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
