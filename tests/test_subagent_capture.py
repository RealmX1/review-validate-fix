#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
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


from _rvf_test_support.loader import load_script_module as _load


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


SAMPLE_PATCH = """*** Begin Patch
*** Update File: src/foo.py
@@ -10,3 +10,5 @@
 unchanged
-old
+new
+also new
*** End Patch
"""


def _main_rollout_with_two_spawns(rollout: Path) -> None:
    """Build a tiny main rollout containing two spawn_agent → collab_agent_spawn_end events."""
    _write_jsonl(
        rollout,
        [
            {"timestamp": "t0", "type": "session_meta", "payload": {"id": "main-sid"}},
            {
                "timestamp": "t1",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "arguments": "{\"agent_type\":\"explorer\"}",
                    "call_id": "call_spawn_a",
                },
            },
            {
                "timestamp": "t2",
                "type": "event_msg",
                "payload": {
                    "type": "collab_agent_spawn_end",
                    "call_id": "call_spawn_a",
                    "sender_thread_id": "main-sid",
                    "new_thread_id": "agent-aaa-1111",
                    "new_agent_role": "explorer",
                    "new_agent_nickname": "Faraday",
                    "prompt": "you are reviewer A",
                    "model": "gpt-5.5",
                    "status": "pending_init",
                },
            },
            {
                "timestamp": "t3",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "arguments": "{\"agent_type\":\"worker\"}",
                    "call_id": "call_spawn_b",
                },
            },
            {
                "timestamp": "t4",
                "type": "event_msg",
                "payload": {
                    "type": "collab_agent_spawn_end",
                    "call_id": "call_spawn_b",
                    "sender_thread_id": "main-sid",
                    "new_thread_id": "agent-bbb-2222",
                    "new_agent_role": "worker",
                    "new_agent_nickname": "Tesla",
                    "prompt": "you are validate-fix",
                },
            },
        ],
    )


def _make_subagent_rollout(sessions_root: Path, agent_id: str, *, with_patch: bool) -> Path:
    day_dir = sessions_root / "2026" / "05" / "04"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-2026-05-04T18-25-21-{agent_id}.jsonl"
    records: list[dict[str, Any]] = [
        {"timestamp": "s0", "type": "session_meta", "payload": {"id": agent_id}},
        {
            "timestamp": "s1",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi from " + agent_id}],
            },
        },
    ]
    if with_patch:
        records.append(
            {
                "timestamp": "s2",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "status": "completed",
                    "name": "apply_patch",
                    "input": SAMPLE_PATCH,
                    "call_id": "subagent_call_patch_1",
                },
            }
        )
        records.append(
            {
                "timestamp": "s3",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "subagent_call_patch_1",
                    "output": "Success. Updated the following files:\nM src/foo.py",
                },
            }
        )
    _write_jsonl(path, records)
    return path


def test_discover_spawned_agents_picks_up_collab_events(tmp_path: Path) -> None:
    mod = _load("subagent_capture")
    rollout = tmp_path / "main.jsonl"
    _main_rollout_with_two_spawns(rollout)

    spawns = mod.discover_spawned_agents(rollout)
    assert [s.agent_id for s in spawns] == ["agent-aaa-1111", "agent-bbb-2222"]
    assert spawns[0].role == "explorer"
    assert spawns[0].nickname == "Faraday"
    assert spawns[0].call_id == "call_spawn_a"
    assert spawns[0].ts == "t2"
    assert spawns[1].role == "worker"
    assert spawns[1].nickname == "Tesla"
    # prompt should round-trip
    assert spawns[1].prompt == "you are validate-fix"


def test_find_subagent_rollout_globs_recursively(tmp_path: Path) -> None:
    mod = _load("subagent_capture")
    sessions_root = tmp_path / "sessions"
    target = _make_subagent_rollout(sessions_root, "agent-aaa-1111", with_patch=False)
    # Drop a same-named-prefix decoy in another date dir to confirm exact-id match.
    decoy_dir = sessions_root / "2026" / "05" / "03"
    decoy_dir.mkdir(parents=True, exist_ok=True)
    (decoy_dir / "rollout-2026-05-03T01-01-01-some-other-id.jsonl").write_text("{}\n")

    found = mod.find_subagent_rollout("agent-aaa-1111", sessions_root=sessions_root)
    assert found == target

    missing = mod.find_subagent_rollout("agent-zzz-9999", sessions_root=sessions_root)
    assert missing is None


def test_capture_subagent_writes_rollout_manifest_and_distill(tmp_path: Path) -> None:
    mod = _load("subagent_capture")
    sessions_root = tmp_path / "sessions"
    src = _make_subagent_rollout(sessions_root, "agent-bbb-2222", with_patch=True)

    spawn = mod.SpawnRecord(
        call_id="call_spawn_b",
        agent_id="agent-bbb-2222",
        role="worker",
        nickname="Tesla",
        prompt="prompt",
        ts="t4",
        line_index=4,
    )
    dst_dir = tmp_path / "out" / "agent-bbb-2222"
    manifest = mod.capture_subagent(spawn, dst_dir=dst_dir, sessions_root=sessions_root)

    assert manifest["status"] == "ok"
    assert manifest["host"] == "codex"
    assert manifest["host_originator"] is None  # 测试 rollout 未带 originator
    assert manifest["spawn"]["agent_id"] == "agent-bbb-2222"
    assert manifest["spawn"]["role"] == "worker"
    assert manifest["distill_status"] == "ok"

    rollout_copy = dst_dir / "rollout.codex.jsonl"
    traj = dst_dir / "trajectory.jsonl"
    index = dst_dir / "trajectory.index.json"
    manifest_path = dst_dir / "manifest.json"
    for path in (rollout_copy, traj, index, manifest_path):
        assert path.is_file(), f"missing artifact: {path}"

    # Captured file is byte-identical to source.
    assert rollout_copy.read_bytes() == src.read_bytes()

    # Distilled trajectory must include the apply_patch tool_call.
    distilled = [json.loads(line) for line in traj.read_text().splitlines() if line.strip()]
    patch_calls = [
        rec for rec in distilled if rec.get("kind") == "tool_call" and rec.get("tool") == "apply_patch"
    ]
    assert len(patch_calls) == 1
    assert patch_calls[0]["call_id"] == "subagent_call_patch_1"
    refs = patch_calls[0]["artifact_refs"]
    assert refs and refs[0]["path"] == "src/foo.py"


def test_capture_subagent_missing_rollout_writes_pointer_only(tmp_path: Path) -> None:
    mod = _load("subagent_capture")
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()  # exists but empty
    spawn = mod.SpawnRecord(
        call_id=None,
        agent_id="agent-missing",
        role="explorer",
        nickname=None,
        prompt=None,
        ts="t9",
        line_index=0,
    )
    dst_dir = tmp_path / "out" / "agent-missing"
    manifest = mod.capture_subagent(spawn, dst_dir=dst_dir, sessions_root=sessions_root)
    assert manifest["status"] == "rollout_unavailable"
    assert manifest["host"] == "codex"
    assert manifest["host_originator"] is None
    assert (dst_dir / "manifest.json").is_file()
    # No rollout file written when source missing.
    assert not (dst_dir / "rollout.codex.jsonl").exists()
    assert not (dst_dir / "trajectory.jsonl").exists()


def test_capture_subagent_propagates_originator_from_rollout(tmp_path: Path) -> None:
    mod = _load("subagent_capture")
    sessions_root = tmp_path / "sessions"
    day_dir = sessions_root / "2026" / "05" / "04"
    day_dir.mkdir(parents=True)
    rollout = day_dir / "rollout-2026-05-04T18-25-21-agent-orig-1.jsonl"
    _write_jsonl(
        rollout,
        [
            {
                "timestamp": "s0",
                "type": "session_meta",
                "payload": {"id": "agent-orig-1", "originator": "codex_cli_rs"},
            }
        ],
    )
    spawn = mod.SpawnRecord(
        call_id="c",
        agent_id="agent-orig-1",
        role="worker",
        nickname=None,
        prompt=None,
        ts="t",
        line_index=0,
    )
    dst_dir = tmp_path / "out" / "agent-orig-1"
    manifest = mod.capture_subagent(spawn, dst_dir=dst_dir, sessions_root=sessions_root)
    assert manifest["status"] == "ok"
    assert manifest["host"] == "codex"
    assert manifest["host_originator"] == "codex_cli_rs"


def test_capture_all_subagents_writes_one_dir_per_spawn(tmp_path: Path) -> None:
    mod = _load("subagent_capture")
    sessions_root = tmp_path / "sessions"
    main_rollout = tmp_path / "main.jsonl"
    _main_rollout_with_two_spawns(main_rollout)
    _make_subagent_rollout(sessions_root, "agent-aaa-1111", with_patch=False)
    _make_subagent_rollout(sessions_root, "agent-bbb-2222", with_patch=True)

    out_root = tmp_path / "subagents"
    manifests = mod.capture_all_subagents(
        main_rollout_path=main_rollout,
        dst_root=out_root,
        sessions_root=sessions_root,
    )
    assert len(manifests) == 2
    # Each spawn got its own subdir named by agent_id.
    dirs = sorted(p.name for p in out_root.iterdir() if p.is_dir())
    assert dirs == ["agent-aaa-1111", "agent-bbb-2222"]
    # Worker subagent (with patch) had its trajectory distilled.
    worker_traj = out_root / "agent-bbb-2222" / "trajectory.jsonl"
    assert worker_traj.is_file()
    distilled = [
        json.loads(line) for line in worker_traj.read_text().splitlines() if line.strip()
    ]
    assert any(
        rec.get("tool") == "apply_patch" and rec.get("kind") == "tool_call"
        for rec in distilled
    )


def test_capture_all_subagents_no_spawn_returns_empty(tmp_path: Path) -> None:
    mod = _load("subagent_capture")
    rollout = tmp_path / "main.jsonl"
    _write_jsonl(
        rollout,
        [
            {"timestamp": "t0", "type": "session_meta", "payload": {"id": "main-sid"}},
            {
                "timestamp": "t1",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": "{}",
                    "call_id": "x",
                },
            },
        ],
    )
    out_root = tmp_path / "subagents"
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    manifests = mod.capture_all_subagents(
        main_rollout_path=rollout,
        dst_root=out_root,
        sessions_root=sessions_root,
    )
    assert manifests == []
    # No directory created when nothing to write.
    assert not out_root.exists()
