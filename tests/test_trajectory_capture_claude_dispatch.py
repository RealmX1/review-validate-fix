#!/usr/bin/env python3
"""capture_run 在 Claude Code transcript 上的端到端 dispatch 测试。

覆盖：
- same-session-slice：marker 命中 → pre/post 字节互补，trajectory.jsonl 非空，
  manifest.host == claude_code。
- same-session-full：marker 未命中 → pre 为空 manifest，post 全量；
  trajectory.jsonl 仍非空（Claude distiller 跑通）。
- forked mixed-host：parent Codex + child Claude → pre.host=codex，
  post.host=claude_code，summary.host=claude_code。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


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


def _claude_user(text, ts="2026-05-09T10:00:00Z", uuid="u"):
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "sessionId": "claude-sess",
        "message": {"role": "user", "content": text},
    }


def _claude_assistant_text(text, ts="2026-05-09T10:00:01Z", uuid="a"):
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "sessionId": "claude-sess",
        "message": {
            "model": "claude-opus-4-7",
            "id": "msg",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _codex_user_event(text, ts="2026-05-04T00:00:01Z"):
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text},
    }


def test_capture_run_claude_same_session_slice(tmp_path: Path) -> None:
    capture = _load("trajectory_capture")
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "permission-mode", "permissionMode": "default", "sessionId": "S"},
            _claude_user("plain background"),
            _claude_assistant_text("ack"),
            _claude_user(
                f"start {capture.RVF_SKILL_TRIGGER}",
                ts="2026-05-09T11:00:00Z",
                uuid="trig",
            ),
            _claude_assistant_text("running", ts="2026-05-09T11:00:01Z", uuid="run"),
        ],
    )
    run_dir = tmp_path / "rvf-run"
    (run_dir / "artifacts").mkdir(parents=True)
    summary = capture.capture_run(
        run_dir=run_dir,
        event={"transcript_path": str(transcript), "session_id": "S"},
    )
    assert summary["host"] == capture.HOST_CLAUDE
    assert summary["pre_rvf_source_kind"] == "same-session-slice"
    assert summary["post_rvf_source_kind"] == "same-session-slice"
    pre = run_dir / "artifacts" / "trajectory" / "pre-rvf" / "rollout.codex.jsonl"
    post = run_dir / "artifacts" / "trajectory" / "rvf" / "rollout.codex.jsonl"
    assert pre.exists() and post.exists()
    assert pre.read_bytes() + post.read_bytes() == transcript.read_bytes()
    pre_manifest = json.loads(
        (run_dir / "artifacts" / "trajectory" / "pre-rvf" / "manifest.json").read_text()
    )
    post_manifest = json.loads(
        (
            run_dir
            / "artifacts"
            / "trajectory"
            / "rvf"
            / "rollout.codex.manifest.json"
        ).read_text()
    )
    assert pre_manifest["host"] == capture.HOST_CLAUDE
    assert post_manifest["host"] == capture.HOST_CLAUDE
    # trajectory.jsonl 非空
    distilled = run_dir / "artifacts" / "trajectory" / "rvf" / "trajectory.jsonl"
    lines = [
        line for line in distilled.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert lines, "Claude distiller produced empty trajectory"
    payloads = [json.loads(line) for line in lines]
    sources = {p.get("source") for p in payloads}
    assert sources == {"claude_code"}


def test_capture_run_claude_same_session_full_when_no_marker(tmp_path: Path) -> None:
    capture = _load("trajectory_capture")
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "permission-mode", "permissionMode": "default", "sessionId": "S"},
            _claude_user("just chatting"),
            _claude_assistant_text("ok"),
        ],
    )
    run_dir = tmp_path / "rvf-run"
    (run_dir / "artifacts").mkdir(parents=True)
    summary = capture.capture_run(
        run_dir=run_dir,
        event={"transcript_path": str(transcript), "session_id": "S"},
    )
    assert summary["host"] == capture.HOST_CLAUDE
    assert summary["pre_rvf_source_kind"] == "none"
    assert summary["post_rvf_source_kind"] == "same-session-full"
    post = run_dir / "artifacts" / "trajectory" / "rvf" / "rollout.codex.jsonl"
    assert post.read_bytes() == transcript.read_bytes()
    distilled = run_dir / "artifacts" / "trajectory" / "rvf" / "trajectory.jsonl"
    lines = [
        line for line in distilled.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert lines, "no_marker fallback should still distill all records"
    payloads = [json.loads(line) for line in lines]
    assert all(p.get("source") == "claude_code" for p in payloads)


def test_capture_run_forked_mixed_host_codex_parent_claude_child(tmp_path: Path) -> None:
    capture = _load("trajectory_capture")
    parent_transcript = tmp_path / "parent_codex.jsonl"
    child_transcript = tmp_path / "child_claude.jsonl"
    _write_jsonl(
        parent_transcript,
        [
            {
                "timestamp": "t0",
                "type": "session_meta",
                "payload": {"id": "parent-session", "originator": "Codex Desktop"},
            },
            _codex_user_event("pre work"),
        ],
    )
    _write_jsonl(
        child_transcript,
        [
            {"type": "permission-mode", "permissionMode": "default", "sessionId": "child"},
            _claude_user(f"forked: {capture.RVF_SKILL_TRIGGER}"),
            _claude_assistant_text("running"),
        ],
    )
    run_dir = tmp_path / "rvf-run"
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "artifacts" / "origin.json").write_text(
        json.dumps(
            {"session_id": "parent-session", "transcript_path": str(parent_transcript)}
        ),
        encoding="utf-8",
    )
    summary = capture.capture_run(
        run_dir=run_dir,
        event={"transcript_path": str(child_transcript), "session_id": "child-session"},
    )
    assert summary["pre_rvf_source_kind"] == "forked-source-full"
    assert summary["post_rvf_source_kind"] == "forked-target-full"
    # 顶层 host 跟随 post（child Claude）
    assert summary["host"] == capture.HOST_CLAUDE
    pre_manifest = json.loads(
        (run_dir / "artifacts" / "trajectory" / "pre-rvf" / "manifest.json").read_text()
    )
    post_manifest = json.loads(
        (
            run_dir
            / "artifacts"
            / "trajectory"
            / "rvf"
            / "rollout.codex.manifest.json"
        ).read_text()
    )
    assert pre_manifest["host"] == capture.HOST_CODEX
    assert pre_manifest["host_originator"] == "Codex Desktop"
    assert post_manifest["host"] == capture.HOST_CLAUDE
    # post host_originator 必为 None（Claude 没有该字段）
    assert post_manifest["host_originator"] is None
    # trajectory.jsonl 走 Claude distiller
    distilled = run_dir / "artifacts" / "trajectory" / "rvf" / "trajectory.jsonl"
    payloads = [json.loads(line) for line in distilled.read_text().splitlines() if line.strip()]
    assert payloads
    assert all(p.get("source") == "claude_code" for p in payloads)


def test_capture_run_kanban_dispatch_prefers_child_from_origin(tmp_path: Path) -> None:
    """Cline Kanban dispatch: the stop hook event only knows the parent Codex
    transcript, but origin.json carries the task agent's self-backfilled
    child_session_id / child_transcript_path. capture_run must prefer the child
    Claude transcript (post) over the parent Codex transcript (pre)."""
    capture = _load("trajectory_capture")
    parent_transcript = tmp_path / "parent_codex.jsonl"
    child_transcript = tmp_path / "child_claude.jsonl"
    _write_jsonl(
        parent_transcript,
        [
            {
                "timestamp": "t0",
                "type": "session_meta",
                "payload": {"id": "parent-session", "originator": "Codex Desktop"},
            },
            _codex_user_event("pre-dispatch parent work"),
        ],
    )
    _write_jsonl(
        child_transcript,
        [
            {"type": "permission-mode", "permissionMode": "default", "sessionId": "child"},
            _claude_user(f"kanban task: {capture.RVF_SKILL_TRIGGER}"),
            _claude_assistant_text("running review-validate-fix"),
        ],
    )
    run_dir = tmp_path / "rvf-run"
    (run_dir / "artifacts").mkdir(parents=True)
    # origin.json written by the parent Stop hook (parent Codex) + child fields
    # self-backfilled by the task agent's UserPromptSubmit hook.
    (run_dir / "artifacts" / "origin.json").write_text(
        json.dumps(
            {
                "session_id": "parent-session",
                "transcript_path": str(parent_transcript),
                "child_session_id": "child-session",
                "child_transcript_path": str(child_transcript),
            }
        ),
        encoding="utf-8",
    )
    # Event simulates the parent Stop hook: it only knows the parent Codex
    # transcript and the parent session id — NOT the child.
    summary = capture.capture_run(
        run_dir=run_dir,
        event={"transcript_path": str(parent_transcript), "session_id": "parent-session"},
    )
    assert summary["pre_rvf_source_kind"] == "forked-source-full"
    assert summary["post_rvf_source_kind"] == "forked-target-full"
    assert summary["host"] == capture.HOST_CLAUDE
    pre_manifest = json.loads(
        (run_dir / "artifacts" / "trajectory" / "pre-rvf" / "manifest.json").read_text()
    )
    post_manifest = json.loads(
        (
            run_dir
            / "artifacts"
            / "trajectory"
            / "rvf"
            / "rollout.codex.manifest.json"
        ).read_text()
    )
    assert pre_manifest["host"] == capture.HOST_CODEX
    assert post_manifest["host"] == capture.HOST_CLAUDE
    distilled = run_dir / "artifacts" / "trajectory" / "rvf" / "trajectory.jsonl"
    payloads = [
        json.loads(line)
        for line in distilled.read_text().splitlines()
        if line.strip()
    ]
    assert payloads
    assert all(p.get("source") == "claude_code" for p in payloads)


def test_capture_run_ignores_child_origin_when_same_session(tmp_path: Path) -> None:
    """Guard: a degenerate origin.json where child_session_id == parent must NOT
    trigger the child override (keeps same-session behavior intact)."""
    capture = _load("trajectory_capture")
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "permission-mode", "permissionMode": "default", "sessionId": "S"},
            _claude_user("just chatting"),
            _claude_assistant_text("ok"),
        ],
    )
    run_dir = tmp_path / "rvf-run"
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "artifacts" / "origin.json").write_text(
        json.dumps(
            {
                "session_id": "S",
                "child_session_id": "S",
                "child_transcript_path": str(transcript),
            }
        ),
        encoding="utf-8",
    )
    summary = capture.capture_run(
        run_dir=run_dir,
        event={"transcript_path": str(transcript), "session_id": "S"},
    )
    # child_session_id == parent → override skipped → same-session path.
    assert summary["pre_rvf_source_kind"] == "none"
    assert summary["post_rvf_source_kind"] == "same-session-full"
    assert summary["host"] == capture.HOST_CLAUDE


def test_host_meta_default_codex_when_src_missing() -> None:
    capture = _load("trajectory_capture")
    meta = capture._host_meta(None)
    assert meta == {"host": capture.HOST_CODEX, "host_originator": None}


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
