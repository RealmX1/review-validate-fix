#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
)


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _user_event(text: str, ts: str = "2026-05-04T00:00:01Z") -> dict:
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text},
    }


def _agent_message(text: str, ts: str = "2026-05-04T00:00:02Z") -> dict:
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": text},
    }


def test_find_rvf_start_with_marker(tmp_path: Path) -> None:
    capture = _load("trajectory_capture")
    rollout = tmp_path / "rollout.jsonl"
    _write_jsonl(
        rollout,
        [
            {"timestamp": "t0", "type": "session_meta", "payload": {"id": "s1"}},
            _user_event("plain question, no marker"),
            _agent_message("answer 1"),
            _user_event(
                f"please run {capture.RVF_FORK_MARKER} now",
                ts="2026-05-04T00:00:10Z",
            ),
            _agent_message("acknowledged"),
        ],
    )
    cut = capture.find_rvf_start_in_jsonl(rollout)
    assert cut is not None
    assert cut.marker_matched == capture.RVF_FORK_MARKER
    assert cut.line_index == 3
    # pre + post bytes round-trip the original
    pre = rollout.read_bytes()[: cut.byte_offset]
    post = rollout.read_bytes()[cut.byte_offset :]
    assert pre + post == rollout.read_bytes()
    # pre slice must end with newline
    assert pre.endswith(b"\n")


def test_find_rvf_start_kanban_marker(tmp_path: Path) -> None:
    capture = _load("trajectory_capture")
    rollout = tmp_path / "rollout.jsonl"
    _write_jsonl(
        rollout,
        [
            {"timestamp": "t0", "type": "session_meta", "payload": {"id": "s2"}},
            _user_event(f"trigger via {capture.KANBAN_FOLLOWUP_MARKER}"),
        ],
    )
    cut = capture.find_rvf_start_in_jsonl(rollout)
    assert cut is not None
    assert cut.marker_matched == capture.KANBAN_FOLLOWUP_MARKER
    assert cut.line_index == 1


def test_find_rvf_start_returns_none_when_no_marker(tmp_path: Path) -> None:
    capture = _load("trajectory_capture")
    rollout = tmp_path / "rollout.jsonl"
    _write_jsonl(
        rollout,
        [
            _user_event("hi"),
            _agent_message("hello"),
        ],
    )
    assert capture.find_rvf_start_in_jsonl(rollout) is None


def test_capture_run_same_session_slice(tmp_path: Path) -> None:
    capture = _load("trajectory_capture")
    transcript = tmp_path / "rollout.jsonl"
    _write_jsonl(
        transcript,
        [
            {"timestamp": "t0", "type": "session_meta", "payload": {"id": "session-A"}},
            _user_event("background work"),
            _agent_message("ok"),
            _user_event(f"start {capture.RVF_FORK_MARKER}"),
            _agent_message("running RVF..."),
        ],
    )
    run_dir = tmp_path / "rvf-run"
    (run_dir / "artifacts").mkdir(parents=True)
    summary = capture.capture_run(
        run_dir=run_dir,
        event={"transcript_path": str(transcript), "session_id": "session-A"},
    )
    assert summary["pre_rvf_source_kind"] == "same-session-slice"
    assert summary["post_rvf_source_kind"] == "same-session-slice"
    pre = run_dir / "artifacts" / "trajectory" / "pre-rvf" / "rollout.codex.jsonl"
    post = run_dir / "artifacts" / "trajectory" / "rvf" / "rollout.codex.jsonl"
    assert pre.exists() and post.exists()
    # 字节级互补
    assert pre.read_bytes() + post.read_bytes() == transcript.read_bytes()
    # pre 不含 marker
    assert capture.RVF_FORK_MARKER.encode("utf-8") not in pre.read_bytes()
    # post 必含 marker
    assert capture.RVF_FORK_MARKER.encode("utf-8") in post.read_bytes()
    # 蒸馏文件存在
    distilled = run_dir / "artifacts" / "trajectory" / "rvf" / "trajectory.jsonl"
    assert distilled.exists()


def test_capture_run_forked_session_full_copies(tmp_path: Path) -> None:
    capture = _load("trajectory_capture")
    parent_transcript = tmp_path / "parent.jsonl"
    child_transcript = tmp_path / "child.jsonl"
    _write_jsonl(parent_transcript, [_user_event("pre work in parent")])
    _write_jsonl(
        child_transcript,
        [
            {"timestamp": "t0", "type": "session_meta", "payload": {"id": "child-session"}},
            _user_event(f"forked: {capture.RVF_FORK_MARKER}"),
            _agent_message("running"),
        ],
    )
    run_dir = tmp_path / "rvf-run"
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "artifacts" / "origin.json").write_text(
        json.dumps(
            {
                "session_id": "parent-session",
                "transcript_path": str(parent_transcript),
            }
        ),
        encoding="utf-8",
    )
    summary = capture.capture_run(
        run_dir=run_dir,
        event={"transcript_path": str(child_transcript), "session_id": "child-session"},
    )
    assert summary["pre_rvf_source_kind"] == "forked-source-full"
    assert summary["post_rvf_source_kind"] == "forked-target-full"
    pre = run_dir / "artifacts" / "trajectory" / "pre-rvf" / "rollout.codex.jsonl"
    post = run_dir / "artifacts" / "trajectory" / "rvf" / "rollout.codex.jsonl"
    assert pre.read_bytes() == parent_transcript.read_bytes()
    assert post.read_bytes() == child_transcript.read_bytes()


def test_find_rvf_start_with_since_timestamp_skips_earlier_marker(tmp_path: Path) -> None:
    capture = _load("trajectory_capture")
    rollout = tmp_path / "rollout.jsonl"
    _write_jsonl(
        rollout,
        [
            {"timestamp": "2026-05-04T00:00:00Z", "type": "session_meta", "payload": {"id": "s"}},
            _user_event(f"first run: {capture.RVF_FORK_MARKER}", ts="2026-05-04T01:00:00Z"),
            _agent_message("first done", ts="2026-05-04T01:30:00Z"),
            _user_event("intermezzo, no marker", ts="2026-05-04T02:00:00Z"),
            _user_event(f"second run: {capture.RVF_FORK_MARKER}", ts="2026-05-04T03:00:00Z"),
            _agent_message("second running", ts="2026-05-04T03:01:00Z"),
        ],
    )
    # 不传 since_timestamp 时应命中第一段
    naive = capture.find_rvf_start_in_jsonl(rollout)
    assert naive is not None
    assert naive.timestamp == "2026-05-04T01:00:00Z"
    # 传 since_timestamp 在两段之间时应跳过第一段、命中第二段
    bounded = capture.find_rvf_start_in_jsonl(
        rollout, since_timestamp="2026-05-04T02:30:00Z"
    )
    assert bounded is not None
    assert bounded.timestamp == "2026-05-04T03:00:00Z"
    assert bounded.line_index == 4


def test_capture_run_same_session_picks_run_specific_marker(tmp_path: Path) -> None:
    capture = _load("trajectory_capture")
    transcript = tmp_path / "rollout.jsonl"
    _write_jsonl(
        transcript,
        [
            {"timestamp": "2026-05-04T00:00:00Z", "type": "session_meta", "payload": {"id": "S"}},
            _user_event(
                f"first RVF: {capture.RVF_FORK_MARKER}", ts="2026-05-04T01:00:00Z"
            ),
            _agent_message("first done", ts="2026-05-04T01:30:00Z"),
            _user_event("background chat", ts="2026-05-04T02:00:00Z"),
            _user_event(
                f"second RVF: {capture.RVF_FORK_MARKER}", ts="2026-05-04T03:00:00Z"
            ),
            _agent_message("running second", ts="2026-05-04T03:00:01Z"),
        ],
    )
    run_dir = tmp_path / "rvf-run"
    (run_dir / "artifacts").mkdir(parents=True)
    # summary.json::timestamp 模拟 prepare 时刻，介于两段 marker 之间
    (run_dir / "summary.json").write_text(
        json.dumps({"run_id": "rvf-2nd", "timestamp": "2026-05-04T02:30:00Z"}),
        encoding="utf-8",
    )
    summary = capture.capture_run(
        run_dir=run_dir,
        event={"transcript_path": str(transcript), "session_id": "S"},
    )
    assert summary["pre_rvf_source_kind"] == "same-session-slice"
    pre_manifest_path = run_dir / "artifacts" / "trajectory" / "pre-rvf" / "manifest.json"
    pre_manifest = json.loads(pre_manifest_path.read_text(encoding="utf-8"))
    assert pre_manifest["cut"]["timestamp"] == "2026-05-04T03:00:00Z"
    # post 应包含第二段 marker，且第一段 marker 行被切到 pre 里
    post_bytes = (
        run_dir / "artifacts" / "trajectory" / "rvf" / "rollout.codex.jsonl"
    ).read_bytes()
    pre_bytes = (
        run_dir / "artifacts" / "trajectory" / "pre-rvf" / "rollout.codex.jsonl"
    ).read_bytes()
    assert post_bytes.count(capture.RVF_FORK_MARKER.encode("utf-8")) == 1
    assert pre_bytes.count(capture.RVF_FORK_MARKER.encode("utf-8")) == 1
    assert pre_bytes + post_bytes == transcript.read_bytes()


def test_capture_run_no_marker_falls_back_to_full_post(tmp_path: Path) -> None:
    capture = _load("trajectory_capture")
    transcript = tmp_path / "rollout.jsonl"
    _write_jsonl(
        transcript,
        [
            {"timestamp": "t0", "type": "session_meta", "payload": {"id": "session-noop"}},
            _user_event("just chatting"),
            _agent_message("ok"),
        ],
    )
    run_dir = tmp_path / "rvf-run"
    (run_dir / "artifacts").mkdir(parents=True)
    summary = capture.capture_run(
        run_dir=run_dir,
        event={"transcript_path": str(transcript), "session_id": "session-noop"},
    )
    assert summary["pre_rvf_source_kind"] == "none"
    assert summary["post_rvf_source_kind"] == "same-session-full"
    post = run_dir / "artifacts" / "trajectory" / "rvf" / "rollout.codex.jsonl"
    assert post.read_bytes() == transcript.read_bytes()
