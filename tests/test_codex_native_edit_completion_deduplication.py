"""同一原生编辑的多种完成事件只登记一次，真实后续编辑保留。"""

from __future__ import annotations

import copy
import json

import pytest

from _rvf_test_support.loader import load_script_module


SYNTHETIC_CHANGES = {
    "src/synthetic-example.py": {"type": "update", "unified_diff": "@@ -1 +1 @@\n-before\n+after\n"},
}


def native_edit_completion_record(event_type, edit_id="synthetic-edit", *, changes=None,
                                  thread_id="synthetic-session"):
    changes = copy.deepcopy(SYNTHETIC_CHANGES if changes is None else changes)
    if event_type == "item_completed":
        payload = {"type": event_type, "thread_id": thread_id, "item": {
            "type": "FileChange", "id": edit_id, "status": "completed", "changes": changes,
        }}
    else:
        payload = {"type": event_type, "call_id": edit_id, "success": True, "changes": changes}
        if thread_id is not None:
            payload["thread_id"] = thread_id
    return {"type": "event_msg", "payload": payload}


def distill_synthetic_native_edits(tmp_path, records, **kwargs):
    module = load_script_module("trajectory_distill")
    rollout_path = tmp_path / "synthetic-native-edit-rollout.jsonl"
    rollout_path.write_text("".join(json.dumps(record) + "\n" for record in [
        {"type": "session_meta", "payload": {"id": "synthetic-session"}}, *records,
    ]))
    distilled, index = module.distill_codex_jsonl(
        rollout_path=rollout_path, rollout_filename=rollout_path.name, repo=None, **kwargs,
    )
    return [record for record in distilled if record.get("artifact_refs")], index


@pytest.mark.parametrize("event_order", [
    ["item_completed", "patch_apply_end"], ["patch_apply_end", "item_completed"],
    ["item_completed", "item_completed"], ["patch_apply_end", "patch_apply_end"],
    ["patch_apply_end", "item_completed", "patch_apply_end", "item_completed"],
])
def test_duplicate_native_edit_completions_emit_one_write_event(tmp_path, event_order):
    writes, index = distill_synthetic_native_edits(
        tmp_path, [native_edit_completion_record(event_type) for event_type in event_order],
    )
    assert len(writes) == 1
    assert writes[0]["call_id"] == "synthetic-edit"
    assert index["kind_counts"]["tool_result"] == 1
    assert writes[0]["raw_ref"]["line"] == 2


@pytest.mark.parametrize("same_edit_id", [False, True])
def test_real_followup_edits_on_the_same_path_are_not_swallowed(tmp_path, same_edit_id):
    later_changes = copy.deepcopy(SYNTHETIC_CHANGES)
    if same_edit_id:
        later_changes["src/synthetic-example.py"]["unified_diff"] = "@@ -1 +1 @@\n-after\n+later\n"
    records = [native_edit_completion_record("item_completed")]
    records.append(native_edit_completion_record(
        "patch_apply_end", "synthetic-edit" if same_edit_id else "later-native-edit",
        changes=later_changes,
    ))
    writes, index = distill_synthetic_native_edits(tmp_path, records)
    assert len(writes) == 2
    assert index["kind_counts"]["tool_result"] == 2


@pytest.mark.parametrize("foreign_event_type", ["item_completed", "patch_apply_end"])
def test_foreign_completion_cannot_suppress_the_current_session_edit(tmp_path, foreign_event_type):
    foreign = native_edit_completion_record(foreign_event_type, thread_id="foreign-session")
    valid_event_type = "patch_apply_end" if foreign_event_type == "item_completed" else "item_completed"
    valid = native_edit_completion_record(valid_event_type)
    writes, _ = distill_synthetic_native_edits(tmp_path, [foreign, valid])
    assert len(writes) == 1
    assert writes[0]["raw_ref"]["line"] == 3


def test_patch_completion_without_thread_field_uses_the_bound_native_session(tmp_path):
    writes, _ = distill_synthetic_native_edits(tmp_path, [
        native_edit_completion_record("item_completed"),
        native_edit_completion_record("patch_apply_end", thread_id=None),
    ])
    assert len(writes) == 1


def test_unknown_edit_identity_does_not_guess_a_duplicate_from_paths(tmp_path):
    writes, _ = distill_synthetic_native_edits(tmp_path, [
        native_edit_completion_record("item_completed", edit_id=None),
        native_edit_completion_record("patch_apply_end", edit_id=None),
    ])
    assert len(writes) == 2


def test_bound_session_mismatch_still_rejects_the_rollout(tmp_path):
    with pytest.raises(ValueError, match="会话身份不符"):
        distill_synthetic_native_edits(tmp_path, [
            native_edit_completion_record("item_completed"),
        ], native_session_id="foreign-session")
