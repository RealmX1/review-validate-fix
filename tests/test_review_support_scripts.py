#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import importlib.util
import os
import signal
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECK_SKILL_CONTRACTS = ROOT / "scripts" / "check_skill_contracts.sh"
CHECK_PLUGIN_CONTRACTS = ROOT / "scripts" / "check_plugin_contracts.py"
SCRIPT_DIR = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
)
BUILD_PACKET = SCRIPT_DIR / "build_review_packet.py"
CHECK_REVIEW_OUTPUT = SCRIPT_DIR / "check_review_output.py"
WRITE_REVIEW_RESULT = SCRIPT_DIR / "write_review_result.py"
CHECK_REVIEW_RESULT = SCRIPT_DIR / "check_review_result.py"
COMMAND_LOCK = SCRIPT_DIR / "command_lock.py"
PREPARE_REVIEW_RUN = SCRIPT_DIR / "prepare_review_run.py"
RUN_ALTERNATIVE_REVIEWER = SCRIPT_DIR / "run_alternative_reviewer.py"
CANCEL_RVF_RUN = SCRIPT_DIR / "cancel_rvf_run.py"
CLINE_KANBAN_CLIENT = SCRIPT_DIR / "cline_kanban_client.py"
APPLY_WORKTREE_BOOTSTRAP = SCRIPT_DIR / "apply_worktree_bootstrap.py"
SESSION_MANIFEST = SCRIPT_DIR / "session_manifest.py"
DIAGNOSE_STOP_HOOK_SCOPE = SCRIPT_DIR / "diagnose_stop_hook_scope.py"
RVF_LOGGING = SCRIPT_DIR / "rvf_logging.py"
RVF_HANDOFF = SCRIPT_DIR / "rvf_handoff.py"
RVF_PREP_FILE = SCRIPT_DIR / "rvf_prep_file.py"
RVF_USER_PROMPT_SUBMIT = SCRIPT_DIR / "rvf_user_prompt_submit.py"

for _name in tuple(os.environ):
    if _name.startswith("CODEX_RVF_"):
        os.environ.pop(_name, None)


def load_alternative_reviewer_module():
    spec = importlib.util.spec_from_file_location("rvf_run_alternative_reviewer", RUN_ALTERNATIVE_REVIEWER)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load run_alternative_reviewer module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cancel_rvf_run_module():
    spec = importlib.util.spec_from_file_location("rvf_cancel_rvf_run", CANCEL_RVF_RUN)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load cancel_rvf_run module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_rvf_logging_module():
    spec = importlib.util.spec_from_file_location("rvf_logging", RVF_LOGGING)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_logging module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_rvf_prep_file_module():
    spec = importlib.util.spec_from_file_location("rvf_prep_file", RVF_PREP_FILE)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_prep_file module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_rvf_user_prompt_submit_module():
    # Ensure rvf_prep_file and other dependencies are importable from SCRIPT_DIR
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    load_rvf_prep_file_module()
    spec = importlib.util.spec_from_file_location(
        "rvf_user_prompt_submit", RVF_USER_PROMPT_SUBMIT
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load rvf_user_prompt_submit module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_check_plugin_contracts_module():
    spec = importlib.util.spec_from_file_location(
        "rvf_check_plugin_contracts",
        CHECK_PLUGIN_CONTRACTS,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load check_plugin_contracts module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cline_kanban_client_module():
    spec = importlib.util.spec_from_file_location("rvf_cline_kanban_client", CLINE_KANBAN_CLIENT)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load cline_kanban_client module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(
    cmd: list[str],
    cwd: Path | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or completed.stdout.strip() or f"{cmd[0]} failed")
    return completed


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_rvf_prep_file_round_trip_and_sweep(tmp_path: Path) -> None:
    prep = load_rvf_prep_file_module()
    root = tmp_path / "prep-root"
    now = prep.parse_timestamp("2026-05-07T00:00:00Z")

    written = prep.write_prep_file(
        {
            "origin_session_id": "session-a",
            "origin_repo": str(tmp_path / "repo"),
            "target_flow": "flow-2-branch",
            "rvf_run": {
                "run_id": "rvf-20260507T000000Z-test",
                "tracker_scope_hash": "sha256:abc",
            },
        },
        root=root,
        token="0123456789abcdef",
        now=now,
        ttl_seconds=300,
    )

    assert written.token == "0123456789abcdef"
    assert written.path == root / "0123456789abcdef.json"
    assert written.payload["schema_version"] == 1
    assert written.payload["created_at"] == "2026-05-07T00:00:00Z"
    assert written.payload["expires_at"] == "2026-05-07T00:05:00Z"
    assert written.path.stat().st_mode & 0o777 == 0o600
    assert written.path.parent.stat().st_mode & 0o777 == 0o700

    try:
        prep.write_prep_file(
            {"origin_session_id": "session-collision", "origin_repo": str(tmp_path)},
            root=root,
            token="0123456789abcdef",
            now=prep.parse_timestamp("2026-05-07T00:00:10Z"),
            ttl_seconds=300,
        )
    except prep.PrepFileError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected explicit token collision to fail")
    assert json.loads(written.path.read_text(encoding="utf-8"))["origin_session_id"] == "session-a"

    stale = prep.write_prep_file(
        {"origin_session_id": "old-session", "origin_repo": str(tmp_path)},
        root=root,
        token="bbbbbbbbbbbbbbbb",
        now=prep.parse_timestamp("2026-05-07T00:00:00Z"),
        ttl_seconds=1,
    )
    reused = prep.write_prep_file(
        {"origin_session_id": "new-session", "origin_repo": str(tmp_path)},
        root=root,
        token="bbbbbbbbbbbbbbbb",
        now=prep.parse_timestamp("2026-05-07T00:00:02Z"),
        ttl_seconds=1000,
    )
    assert reused.path == stale.path
    assert reused.payload["origin_session_id"] == "new-session"

    existing_generated = prep.write_prep_file(
        {"origin_session_id": "generated-existing", "origin_repo": str(tmp_path)},
        root=root,
        token="ffffffffffffffff",
        now=now,
        ttl_seconds=1000,
    )
    original_generate_token = prep.generate_token
    generated_tokens = iter(["ffffffffffffffff", "eeeeeeeeeeeeeeee"])
    try:
        prep.generate_token = lambda: next(generated_tokens)
        generated = prep.write_prep_file(
            {"origin_session_id": "generated-retry", "origin_repo": str(tmp_path)},
            root=root,
            now=now,
            ttl_seconds=1000,
        )
    finally:
        prep.generate_token = original_generate_token
    assert existing_generated.path.exists()
    assert generated.token == "eeeeeeeeeeeeeeee"
    assert generated.payload["origin_session_id"] == "generated-retry"

    valid = prep.read_prep_file(
        "0123456789abcdef",
        root=root,
        now=prep.parse_timestamp("2026-05-07T00:01:00Z"),
    )
    assert valid.status == "valid"
    assert valid.payload["origin_session_id"] == "session-a"

    updated = prep.update_prep_file(
        written,
        {
            "target_worktree": str(tmp_path / "task-worktree"),
            "target_kanban_task_id": "task-123",
        },
    )
    assert updated.path == written.path
    assert updated.payload["created_at"] == "2026-05-07T00:00:00Z"
    assert updated.payload["expires_at"] == "2026-05-07T00:05:00Z"
    assert updated.payload["target_worktree"] == str(tmp_path / "task-worktree")
    valid_updated = prep.read_prep_file(
        "0123456789abcdef",
        root=root,
        now=prep.parse_timestamp("2026-05-07T00:01:00Z"),
    )
    assert valid_updated.status == "valid"
    assert valid_updated.payload["target_kanban_task_id"] == "task-123"

    for protected_key in ("created_at", "expires_at", "token", "schema_version"):
        try:
            prep.update_prep_file(updated, {protected_key: "bad"})
        except prep.PrepFileError as exc:
            assert protected_key in str(exc)
        else:
            raise AssertionError(f"expected PrepFileError for protected field {protected_key}")

    expired = prep.read_prep_file(
        "0123456789abcdef",
        root=root,
        now=prep.parse_timestamp("2026-05-07T00:06:00Z"),
    )
    assert expired.status == "expired"

    removed = prep.sweep_stale(
        root=root,
        now=prep.parse_timestamp("2026-05-07T00:06:00Z"),
    )
    assert removed == [written.path]
    assert not written.path.exists()


def test_rvf_user_prompt_submit_dispatches_shared_workflow(tmp_path: Path) -> None:
    prep = load_rvf_prep_file_module()
    submit = load_rvf_user_prompt_submit_module()
    root = tmp_path / "prep-root"
    os.environ["CODEX_RVF_PREP_ROOT"] = str(root)
    try:
        now = prep.parse_timestamp("2026-05-07T00:00:00Z")
        record = prep.write_prep_file(
            {
                "origin_session_id": "session-a",
                "origin_repo": str(tmp_path),
                "origin_cwd": str(tmp_path),
                "target_flow": "flow-1-self-rising",
                "target_worktree": str(tmp_path),
                "rvf_run": {"run_id": "rvf-test", "run_dir": str(tmp_path / "run")},
            },
            root=root,
            token="aaaaaaaaaaaaaaaa",
            now=now,
            ttl_seconds=300,
        )

        prepare_calls: list[dict[str, object]] = []

        def fake_prepare(record_arg, *, timeout_seconds=60.0, user_prompt_excerpt=None, **_):
            assert record_arg.token == record.token
            prepare_calls.append(
                {
                    "token": record_arg.token,
                    "timeout_seconds": timeout_seconds,
                    "excerpt": user_prompt_excerpt,
                }
            )
            state = {
                "started_at": "2026-05-07T00:01:00Z",
                "completed_at": "2026-05-07T00:01:01Z",
                "status": "completed",
                "target_flow": record_arg.payload.get("target_flow"),
                "artifacts": {"review_env": "/tmp/review-env.sh"},
            }
            new_rvf_run = dict(record_arg.payload.get("rvf_run") or {})
            new_rvf_run["shared_workflow_state"] = state
            prep.update_prep_file(record_arg, {"rvf_run": new_rvf_run})
            return state

        # Replace the lazy-imported prepare_run_from_prep_file with a stub.
        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        import importlib

        prepare_module = importlib.import_module("prepare_review_run")
        original_prepare = prepare_module.prepare_run_from_prep_file
        prepare_module.prepare_run_from_prep_file = fake_prepare
        try:
            no_token_payload = submit.inspect_user_prompt_submit(
                {"prompt": "ordinary prompt"}, prep_root=root
            )
            assert no_token_payload["status"] == "no_token"
            assert no_token_payload["continue"] is True
            assert prepare_calls == []

            valid_payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "run RVF_DISPATCH=token=aaaaaaaaaaaaaaaa",
                    "cwd": str(tmp_path),
                    "hook_event_name": "UserPromptSubmit",
                },
                prep_root=root,
                now="2026-05-07T00:01:00Z",
            )
            assert valid_payload["status"] == "valid"
            assert valid_payload["workflow_started"] is True
            assert valid_payload["shared_workflow_state"]["status"] == "completed"
            assert valid_payload["prep_file_path"] == str(root / "aaaaaaaaaaaaaaaa.json")
            assert len(prepare_calls) == 1

            # Idempotent: state is now completed, second invocation should not re-run prepare.
            second_payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "run RVF_DISPATCH=token=aaaaaaaaaaaaaaaa",
                    "cwd": str(tmp_path),
                },
                prep_root=root,
                now="2026-05-07T00:01:30Z",
            )
            assert second_payload["status"] == "valid"
            assert second_payload["workflow_started"] is False
            assert second_payload["shared_workflow_state"]["status"] == "completed"
            assert len(prepare_calls) == 1, "second call should be cached"

            diagnostics_path = root / "diagnostics" / "aaaaaaaaaaaaaaaa.jsonl"
            diagnostics = read_jsonl(diagnostics_path)
            statuses = [event["status"] for event in diagnostics]
            assert "valid" in statuses
            assert any(
                event.get("event") == "user_prompt_submit_shared_workflow_skipped"
                for event in diagnostics
            )
        finally:
            prepare_module.prepare_run_from_prep_file = original_prepare
    finally:
        os.environ.pop("CODEX_RVF_PREP_ROOT", None)


def test_rvf_user_prompt_submit_marker_without_token(tmp_path: Path) -> None:
    submit = load_rvf_user_prompt_submit_module()
    root = tmp_path / "prep-root"
    payload = submit.inspect_user_prompt_submit(
        {
            "prompt": "Stop hook fork prompt body referencing RVF_FORKED_REVIEW_VALIDATE_FIX without token",
            "cwd": str(tmp_path),
            "hook_event_name": "UserPromptSubmit",
        },
        prep_root=root,
    )
    assert payload["status"] == "dispatch_marker_without_token"
    assert payload["origin_marker"] == "fork"
    assert payload["continue"] is True


def test_rvf_user_prompt_submit_manual_path_creates_prep_and_runs_prepare(tmp_path: Path) -> None:
    prep = load_rvf_prep_file_module()
    submit = load_rvf_user_prompt_submit_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "prep-root"
    repo = tmp_path / "repo"
    repo.mkdir()
    os.environ["CODEX_RVF_PREP_ROOT"] = str(root)
    os.environ["CODEX_RVF_LOG_ROOT"] = str(tmp_path / "rvf-state")
    try:
        captured: list[dict[str, object]] = []

        def fake_prepare(record, *, timeout_seconds=60.0, user_prompt_excerpt=None, **_):
            captured.append(
                {
                    "token": record.token,
                    "target_flow": record.payload.get("target_flow"),
                    "dispatch_origin": record.payload.get("dispatch_origin"),
                    "excerpt": user_prompt_excerpt,
                }
            )
            state = {
                "started_at": "2026-05-07T00:00:00Z",
                "completed_at": "2026-05-07T00:00:01Z",
                "status": "completed",
                "target_flow": record.payload.get("target_flow"),
                "artifacts": {},
            }
            new_rvf_run = dict(record.payload.get("rvf_run") or {})
            new_rvf_run["shared_workflow_state"] = state
            prep.update_prep_file(record, {"rvf_run": new_rvf_run})
            return state

        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        import importlib

        prepare_module = importlib.import_module("prepare_review_run")
        original_prepare = prepare_module.prepare_run_from_prep_file
        prepare_module.prepare_run_from_prep_file = fake_prepare
        try:
            payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "/review-validate-fix please review my work",
                    "cwd": str(repo),
                    "session_id": "manual-session",
                    "hook_event_name": "UserPromptSubmit",
                },
                prep_root=root,
            )
            assert payload["status"] == "manual_prep_created"
            assert payload["dispatch_origin"] == "post_user_prompt_manual"
            assert payload["workflow_started"] is True
            assert payload["shared_workflow_state"]["status"] == "completed"
            assert len(captured) == 1
            assert captured[0]["target_flow"] == "flow-manual"
            assert captured[0]["dispatch_origin"] == "post_user_prompt_manual"
            # Prep file must exist on disk under the configured root.
            prep_path = root / f"{payload['token']}.json"
            assert prep_path.is_file()
            # Manual same-session path must inject the prep file path back into
            # the agent context via hookSpecificOutput.additionalContext.
            assert "hookSpecificOutput" in payload
            hook_specific = payload["hookSpecificOutput"]
            assert hook_specific["hookEventName"] == "UserPromptSubmit"
            additional_context = hook_specific["additionalContext"]
            assert "RVF dispatch prep" in additional_context
            assert str(prep_path) in additional_context
            assert "shared_workflow_state.status: completed" in additional_context
        finally:
            prepare_module.prepare_run_from_prep_file = original_prepare
    finally:
        os.environ.pop("CODEX_RVF_PREP_ROOT", None)
        os.environ.pop("CODEX_RVF_LOG_ROOT", None)


def test_rvf_user_prompt_submit_manual_substring_does_not_falsely_trigger(tmp_path: Path) -> None:
    """Quoted/embedded references to the trigger literal must not create a manual prep."""

    submit = load_rvf_user_prompt_submit_module()
    root = tmp_path / "prep-root"

    # A normal-conversation reference to the trigger literal (preceded by a
    # word character, not whitespace/start-of-line) must not fire. Without the
    # word-boundary regex, plain substring matching would fire here.
    embedded_payloads = [
        "see RVF_DOC[/review-validate-fix] for details",
        "FOO/review-validate-fix BAR",
        "x:review-validate-fix",
        "abc$review-validate-fixyz",
    ]
    for prompt in embedded_payloads:
        payload = submit.inspect_user_prompt_submit(
            {
                "prompt": prompt,
                "cwd": str(tmp_path),
                "hook_event_name": "UserPromptSubmit",
            },
            prep_root=root,
        )
        assert payload["status"] == "no_token", (prompt, payload)
        assert "hookSpecificOutput" not in payload

    # detect_manual_trigger should also positively recognize the legitimate
    # trigger forms (line-start or whitespace-prefixed).
    assert submit.detect_manual_trigger("/review-validate-fix") is True
    assert submit.detect_manual_trigger("$review-validate-fix") is True
    assert submit.detect_manual_trigger(":review-validate-fix") is True
    assert submit.detect_manual_trigger("please run /review-validate-fix now") is True
    assert submit.detect_manual_trigger("first line\n/review-validate-fix") is True
    # And reject quoted / embedded uses.
    assert submit.detect_manual_trigger("see /review-validate-fixtool docs") is False
    assert submit.detect_manual_trigger("FOO/review-validate-fix BAR") is False
    assert submit.detect_manual_trigger("RVF_DOC[/review-validate-fix]") is False


def test_rvf_user_prompt_submit_failed_prepare_records_state_without_blocking(tmp_path: Path) -> None:
    prep = load_rvf_prep_file_module()
    submit = load_rvf_user_prompt_submit_module()
    root = tmp_path / "prep-root"
    os.environ["CODEX_RVF_PREP_ROOT"] = str(root)
    try:
        now = prep.parse_timestamp("2026-05-07T00:00:00Z")
        prep.write_prep_file(
            {
                "origin_session_id": "session-a",
                "origin_repo": str(tmp_path),
                "origin_cwd": str(tmp_path),
                "target_flow": "flow-1-self-rising",
                "target_worktree": str(tmp_path),
                "rvf_run": {"run_id": "rvf-fail", "run_dir": str(tmp_path / "run")},
            },
            root=root,
            token="cccccccccccccccc",
            now=now,
            ttl_seconds=300,
        )

        def boom(record, *, timeout_seconds=60.0, user_prompt_excerpt=None, **_):
            raise RuntimeError("prepare boom")

        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        import importlib

        prepare_module = importlib.import_module("prepare_review_run")
        original_prepare = prepare_module.prepare_run_from_prep_file
        prepare_module.prepare_run_from_prep_file = boom
        try:
            payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "go RVF_DISPATCH=token=cccccccccccccccc",
                    "cwd": str(tmp_path),
                },
                prep_root=root,
                now="2026-05-07T00:01:00Z",
            )
            assert payload["continue"] is True
            assert payload["workflow_started"] is False
            assert payload["shared_workflow_state"]["status"] == "failed"
            assert "prepare boom" in payload["shared_workflow_state"]["error"]
            # The prep file on disk must reflect the failed state.
            stored = json.loads((root / "cccccccccccccccc.json").read_text(encoding="utf-8"))
            assert stored["rvf_run"]["shared_workflow_state"]["status"] == "failed"
        finally:
            prepare_module.prepare_run_from_prep_file = original_prepare
    finally:
        os.environ.pop("CODEX_RVF_PREP_ROOT", None)


def test_claude_plugin_hooks_declare_user_prompt_submit() -> None:
    """Option C: the Claude plugin's own hooks/hooks.json must declare both
    Stop and UserPromptSubmit (no installer / settings.json mutation needed),
    and the UserPromptSubmit shim must exist and compile."""
    import py_compile

    hooks_json = ROOT / "plugins" / "review-validate-fix" / "hooks" / "hooks.json"
    data = json.loads(hooks_json.read_text(encoding="utf-8"))
    hooks = data["hooks"]
    assert "Stop" in hooks, "Stop hook regression"

    ups_groups = hooks.get("UserPromptSubmit")
    assert isinstance(ups_groups, list) and ups_groups, "UserPromptSubmit not declared"
    commands = [
        entry.get("command")
        for group in ups_groups
        for entry in group.get("hooks", [])
    ]
    assert any(
        isinstance(cmd, str)
        and "${CLAUDE_PLUGIN_ROOT}" in cmd
        and "hooks/user_prompt_submit.py" in cmd
        for cmd in commands
    ), f"UserPromptSubmit command not wired to the shim: {commands}"

    shim = ROOT / "plugins" / "review-validate-fix" / "hooks" / "user_prompt_submit.py"
    assert shim.is_file(), "hooks/user_prompt_submit.py missing"
    py_compile.compile(str(shim), doraise=True)


def test_rvf_user_prompt_submit_backfills_child_session(tmp_path: Path) -> None:
    """Cline Kanban dispatch: the task agent's UserPromptSubmit hook must
    self-backfill child_session_id / child_transcript_path into both the prep
    payload and the persistent origin.json, and skip when same-session."""
    prep = load_rvf_prep_file_module()
    submit = load_rvf_user_prompt_submit_module()
    root = tmp_path / "prep-root"
    os.environ["CODEX_RVF_PREP_ROOT"] = str(root)
    try:
        now = prep.parse_timestamp("2026-05-07T00:00:00Z")

        run_dir = tmp_path / "run"
        (run_dir / "artifacts").mkdir(parents=True)
        origin_json = run_dir / "artifacts" / "origin.json"
        origin_json.write_text(
            json.dumps(
                {"session_id": "parent-codex", "transcript_path": "/parent/codex.jsonl"}
            ),
            encoding="utf-8",
        )
        child_transcript = tmp_path / "child_claude.jsonl"
        child_transcript.write_text("{}\n", encoding="utf-8")

        prep.write_prep_file(
            {
                "origin_session_id": "parent-codex",
                "origin_repo": str(tmp_path),
                "origin_cwd": str(tmp_path),
                "origin_metadata_path": str(origin_json),
                "target_flow": "flow-2-branch",
                "target_worktree": str(tmp_path),
                "rvf_run": {"run_id": "rvf-kanban", "run_dir": str(run_dir)},
            },
            root=root,
            token="dddddddddddddddd",
            now=now,
            ttl_seconds=300,
        )

        def fake_prepare(record_arg, *, timeout_seconds=60.0, user_prompt_excerpt=None, **_):
            state = {
                "started_at": "2026-05-07T00:01:00Z",
                "completed_at": "2026-05-07T00:01:01Z",
                "status": "completed",
                "artifacts": {"review_env": "/tmp/review-env.sh"},
            }
            new_rvf_run = dict(record_arg.payload.get("rvf_run") or {})
            new_rvf_run["shared_workflow_state"] = state
            prep.update_prep_file(record_arg, {"rvf_run": new_rvf_run})
            return state

        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        import importlib

        prepare_module = importlib.import_module("prepare_review_run")
        original_prepare = prepare_module.prepare_run_from_prep_file
        prepare_module.prepare_run_from_prep_file = fake_prepare
        try:
            payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "task: RVF_DISPATCH=token=dddddddddddddddd",
                    "cwd": str(tmp_path),
                    "session_id": "child-claude",
                    "transcript_path": str(child_transcript),
                },
                prep_root=root,
                now="2026-05-07T00:01:00Z",
            )
            assert payload["status"] == "valid"
            assert payload["child_session_id"] == "child-claude"
            assert payload["child_transcript_path"] == str(child_transcript.resolve())

            # Persistent channel: origin.json merged child fields, parent intact.
            merged_origin = json.loads(origin_json.read_text(encoding="utf-8"))
            assert merged_origin["session_id"] == "parent-codex"
            assert merged_origin["transcript_path"] == "/parent/codex.jsonl"
            assert merged_origin["child_session_id"] == "child-claude"
            assert merged_origin["child_transcript_path"] == str(child_transcript.resolve())

            # Prep payload also records the child fields.
            stored = json.loads((root / "dddddddddddddddd.json").read_text(encoding="utf-8"))
            assert stored["child_session_id"] == "child-claude"
            assert stored["child_transcript_path"] == str(child_transcript.resolve())

            diagnostics = read_jsonl(root / "diagnostics" / "dddddddddddddddd.jsonl")
            assert any(
                event.get("event") == "user_prompt_submit_child_session_backfill"
                and event.get("status") == "ok"
                for event in diagnostics
            )

            # Same-session guard: child == origin → no backfill.
            same_origin = run_dir / "artifacts" / "origin-same.json"
            same_origin.write_text(
                json.dumps({"session_id": "same-sess"}), encoding="utf-8"
            )
            prep.write_prep_file(
                {
                    "origin_session_id": "same-sess",
                    "origin_repo": str(tmp_path),
                    "origin_cwd": str(tmp_path),
                    "origin_metadata_path": str(same_origin),
                    "target_flow": "flow-manual",
                    "target_worktree": str(tmp_path),
                    "rvf_run": {"run_id": "rvf-same", "run_dir": str(run_dir)},
                },
                root=root,
                token="eeeeeeeeeeeeeeee",
                now=now,
                ttl_seconds=300,
            )
            same_payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "again RVF_DISPATCH=token=eeeeeeeeeeeeeeee",
                    "cwd": str(tmp_path),
                    "session_id": "same-sess",
                    "transcript_path": str(child_transcript),
                },
                prep_root=root,
                now="2026-05-07T00:01:00Z",
            )
            assert same_payload["status"] == "valid"
            assert "child_session_id" not in same_payload
            same_origin_after = json.loads(same_origin.read_text(encoding="utf-8"))
            assert "child_session_id" not in same_origin_after

            # Fallback: no origin_metadata_path → derive origin.json from
            # rvf_run.run_dir. Also: missing child transcript → null path.
            run_dir2 = tmp_path / "run2"
            (run_dir2 / "artifacts").mkdir(parents=True)
            (run_dir2 / "artifacts" / "origin.json").write_text(
                json.dumps({"session_id": "parent2"}), encoding="utf-8"
            )
            prep.write_prep_file(
                {
                    "origin_session_id": "parent2",
                    "origin_repo": str(tmp_path),
                    "origin_cwd": str(tmp_path),
                    "target_flow": "flow-2-inplace",
                    "target_worktree": str(tmp_path),
                    "rvf_run": {"run_id": "rvf-fb", "run_dir": str(run_dir2)},
                },
                root=root,
                token="ffffffffffffffff",
                now=now,
                ttl_seconds=300,
            )
            fb_payload = submit.inspect_user_prompt_submit(
                {
                    "prompt": "fb RVF_DISPATCH=token=ffffffffffffffff",
                    "cwd": str(tmp_path),
                    "session_id": "child2",
                    "transcript_path": str(tmp_path / "does_not_exist.jsonl"),
                },
                prep_root=root,
                now="2026-05-07T00:01:00Z",
            )
            assert fb_payload["status"] == "valid"
            assert fb_payload["child_session_id"] == "child2"
            assert fb_payload["child_transcript_path"] is None
            fb_origin = json.loads(
                (run_dir2 / "artifacts" / "origin.json").read_text(encoding="utf-8")
            )
            assert fb_origin["session_id"] == "parent2"
            assert fb_origin["child_session_id"] == "child2"
            assert fb_origin["child_transcript_path"] is None
            fb_stored = json.loads((root / "ffffffffffffffff.json").read_text(encoding="utf-8"))
            assert fb_stored["child_session_id"] == "child2"
            assert fb_stored["child_transcript_path"] is None
        finally:
            prepare_module.prepare_run_from_prep_file = original_prepare
    finally:
        os.environ.pop("CODEX_RVF_PREP_ROOT", None)


def test_prepare_run_from_prep_file_timeout_returns_immediately(tmp_path: Path) -> None:
    """Verify TimeoutError unblocks the hook even when the worker is still running.

    Regression: previous implementation used ``with ThreadPoolExecutor(...)``,
    whose ``__exit__`` defaults to ``wait=True``, so a timeout exception still
    blocked until the worker finished. The fix manages the executor manually
    and calls ``shutdown(wait=False, cancel_futures=True)`` on timeout.
    """

    prep = load_rvf_prep_file_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "prep-root"
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    run(["git", "add", "README.md"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)

    now = prep.parse_timestamp("2026-05-07T00:00:00Z")
    record = prep.write_prep_file(
        {
            "origin_session_id": "session-a",
            "origin_repo": str(repo),
            "origin_cwd": str(repo),
            "target_worktree": str(repo),
            "target_flow": "flow-manual",
            "rvf_run": {
                "run_id": "rvf-timeout-test",
                "run_dir": str(tmp_path / "rvf-state" / "runs" / "rvf-timeout-test"),
            },
        },
        root=root,
        token="dddddddddddddddd",
        now=now,
        ttl_seconds=3600,
    )

    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import importlib

    prepare_module = importlib.import_module("prepare_review_run")
    original_prepare_run = prepare_module.prepare_run

    def slow_prepare_run(**_: object) -> dict[str, object]:
        # Sleep well past our 1-second timeout to prove that the wrapper does
        # not block on shutdown.
        time.sleep(5.0)
        return {"run_id": "should-never-finish"}

    prepare_module.prepare_run = slow_prepare_run
    try:
        start = time.monotonic()
        state = prepare_module.prepare_run_from_prep_file(
            record,
            timeout_seconds=1.0,
            base_dir=tmp_path / "base",
        )
        elapsed = time.monotonic() - start
    finally:
        prepare_module.prepare_run = original_prepare_run

    # Wall-clock must reflect the 1s timeout, not the 5s worker sleep. Allow
    # generous slack for slow CI but still well below the worker's 5s sleep.
    assert elapsed < 3.0, f"prepare_run_from_prep_file blocked for {elapsed:.2f}s"
    assert state["status"] == "timeout", state
    assert "1s timeout" in state["error"]

    # Prep file on disk must reflect the timeout state.
    stored = json.loads((root / "dddddddddddddddd.json").read_text(encoding="utf-8"))
    assert stored["rvf_run"]["shared_workflow_state"]["status"] == "timeout"


def test_rvf_user_prompt_submit_subprocess_stays_silent_in_hook_mode(tmp_path: Path) -> None:
    prep = load_rvf_prep_file_module()
    root = tmp_path / "prep-root"
    now = prep.parse_timestamp("2026-05-07T00:00:00Z")
    prep.write_prep_file(
        {"origin_session_id": "session-a", "origin_repo": str(tmp_path), "target_flow": "flow-1-self-rising"},
        root=root,
        token="aaaaaaaaaaaaaaaa",
        now=now,
        ttl_seconds=300,
    )
    actual_hook = run(
        [
            sys.executable,
            str(RVF_USER_PROMPT_SUBMIT),
            "--prep-root",
            str(root),
            "--now",
            "2026-05-07T00:01:00Z",
        ],
        input_text=json.dumps({"prompt": "ordinary prompt without trigger"}, ensure_ascii=False),
    )
    assert actual_hook.stdout == ""
    assert actual_hook.stderr == ""


def test_rvf_user_prompt_submit_diagnostic_failure_stays_silent_in_hook_mode(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    bad_root = tmp_path / "not-a-directory"
    bad_root.write_text("not a directory\n", encoding="utf-8")

    actual_hook = run(
        [
            sys.executable,
            str(RVF_USER_PROMPT_SUBMIT),
            "--prep-root",
            str(bad_root),
        ],
        input_text=json.dumps({"prompt": "RVF_DISPATCH=token=bbbbbbbbbbbbbbbb"}, ensure_ascii=False),
    )
    assert actual_hook.stdout == ""
    assert actual_hook.stderr == ""


def test_rvf_handoff_cli_opens_with_configured_editor(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")
    marker = tmp_path / "opened.txt"
    opener = tmp_path / "open_handoff.py"
    opener.write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path(os.environ['RVF_OPEN_MARKER']).write_text(sys.argv[1], encoding='utf-8')\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "CODEX_RVF_IDE_OPEN_CMD": f"{shlex.quote(sys.executable)} {shlex.quote(str(opener))}",
        "RVF_OPEN_MARKER": str(marker),
    }

    completed = run([sys.executable, str(RVF_HANDOFF), "open", str(handoff)], env=env)
    payload = json.loads(completed.stdout)

    assert payload["valid"] is True
    assert payload["opened"] is True
    assert payload["handoff_path"] == str(handoff.resolve())
    assert marker.read_text(encoding="utf-8") == str(handoff.resolve())


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=path)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=path)
    run(["git", "config", "user.name", "RVF Test"], cwd=path)
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "tracked.txt"], cwd=path)
    run(["git", "commit", "-q", "-m", "base"], cwd=path)
    (path / "tracked.txt").write_text("base\nchange\n", encoding="utf-8")
    (path / "new.txt").write_text("new\n", encoding="utf-8")
    return path


def write_alternative_reviewer_config(
    path: Path,
    command: list[str],
    *,
    idle_timeout_seconds: float,
    activity_check_interval_seconds: float,
    activity_probe_command: list[str] | None = None,
    activity_probe_timeout_seconds: float | None = None,
    activity_probe_failure_threshold: int | None = None,
    max_runtime_seconds: float | None = None,
    output_format: str | None = "text",
    health_command: list[str] | None = None,
    pre_run_health: bool | None = None,
) -> Path:
    payload = {
        "enabled": True,
        "label": "alternative-reviewer:test",
        "command": command,
        "allow_repo_cwd": True,
        "idle_timeout_seconds": idle_timeout_seconds,
        "activity_check_interval_seconds": activity_check_interval_seconds,
        "env_unset": [],
    }
    if activity_probe_command is not None:
        payload["activity_probe_command"] = activity_probe_command
    if activity_probe_timeout_seconds is not None:
        payload["activity_probe_timeout_seconds"] = activity_probe_timeout_seconds
    if activity_probe_failure_threshold is not None:
        payload["activity_probe_failure_threshold"] = activity_probe_failure_threshold
    if max_runtime_seconds is not None:
        payload["max_runtime_seconds"] = max_runtime_seconds
    if output_format is not None:
        payload["output_format"] = output_format
    if health_command is not None:
        payload["health_command"] = health_command
    if pre_run_health is not None:
        payload["pre_run_health"] = pre_run_health
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def clean_review_result_python(*, stdout: str = "artifact written") -> str:
    return (
        "import os, subprocess, sys; "
        "sys.stdin.read(); "
        "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
        "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True); "
        f"print({stdout!r})"
    )


def write_codex_transcript(path: Path, repo: Path) -> Path:
    apply_patch_input = (
        "*** Begin Patch\n"
        "*** Update File: tracked.txt\n"
        "@@\n"
        " base\n"
        "+change\n"
        "*** Add File: owned-new.txt\n"
        "+owned\n"
        "*** Delete File: removed.txt\n"
        "*** End Patch\n"
    )
    records = [
        {
            "timestamp": "2999-04-27T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "session-tracking-test", "cwd": str(repo)},
        },
        {
            "timestamp": "2999-04-27T00:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": apply_patch_input,
                "call_id": "call_patch",
            },
        },
        {
            "timestamp": "2999-04-27T00:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "printf generated > generated.txt", "workdir": str(repo)}),
                "call_id": "call_exec",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    return path


def test_check_review_output_lock_request() -> None:
    result = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="RVF_LOCK_REQUEST name=npm-test command=npm test reason=shared-cache\n",
    )
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["kind"] == "lock_request"
    assert payload["lock_request_count"] == 1

    invalid = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_LOCK_REQUEST name=n command=x reason=y\nNO_ISSUES\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0

    malformed_lock = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_LOCK_REQUEST please-lock-npm-test\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed_lock.returncode != 0

    empty_lock_field = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_LOCK_REQUEST name= command=npm test reason=shared-cache\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert empty_lock_field.returncode != 0


def test_check_review_output_protocol_extension_requests() -> None:
    standard = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text=(
            "RVF_STANDARD_REQUEST domain=security reason=auth-boundary scope=src/auth.py\n"
        ),
    )
    standard_payload = json.loads(standard.stdout)
    assert standard_payload["valid"] is True
    assert standard_payload["kind"] == "request"
    assert standard_payload["request_count"] == 1
    assert standard_payload["request_types"] == ["standard_request"]

    mixed_requests = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text=(
            "RVF_MEASUREMENT_REQUEST metric=p95 command=pytest reason=needs-baseline\n"
            "RVF_CONTEXT_REQUEST need=test-result reason=compare-existing-output\n"
            "RVF_SUBTASK_REQUEST type=security_check scope=src/auth.py reason=auth-change\n"
        ),
    )
    mixed_payload = json.loads(mixed_requests.stdout)
    assert mixed_payload["valid"] is True
    assert mixed_payload["kind"] == "request"
    assert mixed_payload["request_count"] == 3
    assert mixed_payload["request_types"] == [
        "context_request",
        "measurement_request",
        "subtask_request",
    ]

    malformed = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_MEASUREMENT_REQUEST metric=p95 reason=missing-command\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed.returncode != 0

    empty_field = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_STANDARD_REQUEST domain=security reason= scope=src/auth.py\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert empty_field.returncode != 0

    invalid_standard_domain = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_STANDARD_REQUEST domain=privacy reason=auth-boundary scope=src/auth.py\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid_standard_domain.returncode != 0

    invalid_subtask_type = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_SUBTASK_REQUEST type=delete_repo scope=src/auth.py reason=auth-change\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid_subtask_type.returncode != 0

    invalid_context_need = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="RVF_CONTEXT_REQUEST need=secret reason=compare-existing-output\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid_context_need.returncode != 0

    mixed_with_completion = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input=(
            "RVF_STANDARD_REQUEST domain=performance reason=needs-budget scope=src/app.ts\n"
            "NO_ISSUES\n"
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert mixed_with_completion.returncode != 0


def test_review_result_artifact_no_issues_and_issues(tmp_path: Path) -> None:
    clean = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")

    run(
        [sys.executable, str(WRITE_REVIEW_RESULT), "no-issues", "--out", str(clean)],
        env=env,
    )
    clean_check = run([sys.executable, str(CHECK_REVIEW_RESULT), str(clean), "--json"])
    clean_payload = json.loads(clean_check.stdout)
    assert clean_payload["valid"] is True
    assert clean_payload["kind"] == "no_issues"

    issues = tmp_path / "run" / "artifacts" / "reviewers" / "b" / "review-result.json"
    run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(issues),
            "--path",
            "src/foo.ts",
            "--line",
            "42",
            "--message",
            "空输入时会跳过必要校验。",
            "--kind",
            "REAL",
            "--severity",
            "high",
        ],
        env=env,
    )
    run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(issues),
            "--path",
            "Dockerfile",
            "--line",
            "3",
            "--message",
            "构建参数没有传入默认值。",
            "--kind",
            "REAL",
            "--severity",
            "medium",
        ],
        env=env,
    )
    issue_check = run([sys.executable, str(CHECK_REVIEW_RESULT), str(issues), "--json"])
    issue_payload = json.loads(issue_check.stdout)
    assert issue_payload["valid"] is True
    assert issue_payload["kind"] == "issues"
    assert issue_payload["issue_count"] == 2
    assert issue_payload["issues"][0]["path"] == "src/foo.ts"
    assert issue_payload["issues"][0]["kind"] == "REAL"
    assert issue_payload["issues"][0]["severity"] == "high"
    assert issue_payload["issues"][1]["kind"] == "REAL"
    assert issue_payload["issues"][1]["severity"] == "medium"


def test_review_result_artifact_requests_and_scope_exclusions(tmp_path: Path) -> None:
    result = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")
    run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "lock-request",
            "--out",
            str(result),
            "--name",
            "npm-test",
            "--command",
            "npm test",
            "--reason",
            "测试命令会争用共享缓存。",
        ],
        env=env,
    )
    payload = json.loads(run([sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"]).stdout)
    assert payload["valid"] is True
    assert payload["kind"] == "request"
    assert payload["request_types"] == ["lock_request"]

    excluded = tmp_path / "run" / "artifacts" / "reviewers" / "b" / "review-result.json"
    contract = tmp_path / "scope.contract.json"
    contract.write_text('{"canonical_scope":{"excluded_path_prefixes":["vendor"]}}\n', encoding="utf-8")
    run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(excluded),
            "--path",
            "vendor/generated.py",
            "--line",
            "1",
            "--message",
            "不应报告 excluded path。",
            "--kind",
            "REAL",
            "--severity",
            "medium",
        ],
        env=env,
    )
    invalid = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(excluded), "--scope-contract", str(contract), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0
    assert "excluded" in invalid.stdout


def test_review_result_artifact_rejects_malformed_and_mixed_state(tmp_path: Path) -> None:
    result = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")

    missing = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode != 0
    assert "missing review result artifact" in missing.stdout

    bad_path = subprocess.run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(result),
            "--path",
            "../escape.py",
            "--line",
            "1",
            "--message",
            "bad",
            "--kind",
            "REAL",
            "--severity",
            "high",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_path.returncode != 0

    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "no_issues",
                "issues": [
                    {
                        "path": "src/a.py",
                        "line": 1,
                        "message": "mixed",
                        "kind": "REAL",
                        "severity": "high",
                    }
                ],
                "requests": [],
            }
        ),
        encoding="utf-8",
    )
    mixed = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert mixed.returncode != 0
    assert "no_issues result must not include issues" in mixed.stdout

    outside = subprocess.run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "no-issues",
            "--out",
            str(tmp_path / "outside.json"),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert outside.returncode != 0
    assert "RVF_RUN_DIR" in outside.stderr


def test_issue_requires_kind(tmp_path: Path) -> None:
    out = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")
    result = subprocess.run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(out),
            "--path",
            "src/foo.py",
            "--line",
            "1",
            "--message",
            "missing kind argument",
            "--severity",
            "high",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "--kind" in result.stderr


def test_issue_requires_severity(tmp_path: Path) -> None:
    out = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(tmp_path / "run")
    result = subprocess.run(
        [
            sys.executable,
            str(WRITE_REVIEW_RESULT),
            "issue",
            "--out",
            str(out),
            "--path",
            "src/foo.py",
            "--line",
            "1",
            "--message",
            "missing severity argument",
            "--kind",
            "REAL",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "--severity" in result.stderr


def test_check_rejects_issue_without_kind(tmp_path: Path) -> None:
    result = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "issues",
                "issues": [
                    {
                        "path": "src/foo.py",
                        "line": 1,
                        "message": "missing kind",
                        "severity": "high",
                    }
                ],
                "requests": [],
            }
        ),
        encoding="utf-8",
    )
    check = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode != 0
    assert "kind" in check.stdout


def test_check_rejects_invalid_severity(tmp_path: Path) -> None:
    result = tmp_path / "run" / "artifacts" / "reviewers" / "a" / "review-result.json"
    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "issues",
                "issues": [
                    {
                        "path": "src/foo.py",
                        "line": 1,
                        "message": "bad severity value",
                        "kind": "REAL",
                        "severity": "wat",
                    }
                ],
                "requests": [],
            }
        ),
        encoding="utf-8",
    )
    check = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(result), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode != 0
    assert "severity" in check.stdout


def test_check_skill_contracts_requires_validate_fix_request_literals() -> None:
    script = CHECK_SKILL_CONTRACTS.read_text(encoding="utf-8")
    for literal in (
        "require_literal \"prompts/validate-fix.md\" 'RVF_STANDARD_REQUEST'",
        "require_literal \"prompts/validate-fix.md\" 'RVF_MEASUREMENT_REQUEST'",
        "require_literal \"prompts/validate-fix.md\" 'RVF_SUBTASK_REQUEST'",
        "require_literal \"prompts/validate-fix.md\" 'RVF_CONTEXT_REQUEST'",
    ):
        assert literal in script


def test_contract_check_entrypoints_default_quiet_with_verbose_flag() -> None:
    skill_script = CHECK_SKILL_CONTRACTS.read_text(encoding="utf-8")
    plugin_script = CHECK_PLUGIN_CONTRACTS.read_text(encoding="utf-8")
    for literal in (
        "verbose=0",
        "-v|--verbose)",
        "run_step()",
        "run_parallel_test_steps()",
        "RVF_CONTRACT_PARALLEL_TESTS",
        "RVF_CONTRACT_PARALLEL_JOBS",
        "RVF_CONTRACT_REVIEW_SUPPORT_SHARDS",
        "RVF_CONTRACT_STOP_HOOK_SHARDS",
        "RVF_CONTRACT_DISPATCHER_SHARDS",
        "command_status",
        "验证失败:",
        'return "$command_status"',
    ):
        assert literal in skill_script
    for literal in (
        'parser.add_argument("-v", "--verbose"',
        'command.append("--verbose")',
        "capture_output=True",
        "plugin 契约检查通过",
    ):
        assert literal in plugin_script

    function_start = skill_script.index("timestamp_ms() {")
    function_end = skill_script.index("\nhash_file() {")
    probe = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "verbose=0\n"
        f"{skill_script[function_start:function_end]}\n"
        f"run_step failing {shlex.quote(sys.executable)} -c "
        "'import sys; print(\"boom\"); sys.exit(7)'\n"
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        probe_path = Path(tmp_dir) / "probe.sh"
        timing_path = Path(tmp_dir) / "timing.jsonl"
        probe_path.write_text(probe, encoding="utf-8")
        env = os.environ.copy()
        env["RVF_CONTRACT_TIMING_JSONL"] = str(timing_path)
        env["RVF_CONTRACT_TIMING_SCRIPT"] = str(CHECK_SKILL_CONTRACTS)
        completed = subprocess.run(
            ["bash", str(probe_path)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert not timing_path.exists()
    assert completed.returncode == 7
    assert "验证失败: failing" in completed.stderr
    assert "boom" in completed.stderr


def test_contract_check_parallel_test_steps_record_parallel_timing() -> None:
    skill_script = CHECK_SKILL_CONTRACTS.read_text(encoding="utf-8")
    function_start = skill_script.index("timestamp_ms() {")
    function_end = skill_script.index("\nhash_file() {")
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        tests_dir = root / "tests"
        tests_dir.mkdir()
        for name in (
            "test_install_to_codex.py",
            "test_rvf_handoff_intake.py",
            "test_review_support_scripts.py",
            "test_codex_stop_hook_dispatcher.py",
            "test_codex_stop_review_validate_fix.py",
        ):
            (tests_dir / name).write_text(
                "#!/usr/bin/env python3\n"
                "import time\n"
                "time.sleep(0.05)\n"
                "print('ok')\n",
                encoding="utf-8",
            )
        timing_path = root / "timing.jsonl"
        probe = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "verbose=0\n"
            f"tests_dir={shlex.quote(str(tests_dir))}\n"
            f"export RVF_CONTRACT_TIMING_JSONL={shlex.quote(str(timing_path))}\n"
            "export RVF_CONTRACT_TIMING_SCRIPT=\"$0\"\n"
            "export RVF_CONTRACT_PARALLEL_TESTS=1\n"
            "export RVF_CONTRACT_PARALLEL_JOBS=4\n"
            "export RVF_CONTRACT_REVIEW_SUPPORT_SHARDS=4\n"
            "export RVF_CONTRACT_STOP_HOOK_SHARDS=4\n"
            "export RVF_CONTRACT_DISPATCHER_SHARDS=2\n"
            f"{skill_script[function_start:function_end]}\n"
            "run_parallel_test_steps\n"
        )
        probe_path = root / "probe.sh"
        probe_path.write_text(probe, encoding="utf-8")
        completed = subprocess.run(
            ["bash", str(probe_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        records = [json.loads(line) for line in timing_path.read_text(encoding="utf-8").splitlines()]

    assert completed.returncode == 0
    assert [record["execution_mode"] for record in records] == ["parallel"] * 12
    assert {record["label"] for record in records} == {
        "tests: install_to_codex",
        "tests: rvf_handoff_intake",
        "tests: review_support_scripts shard 1/4",
        "tests: review_support_scripts shard 2/4",
        "tests: review_support_scripts shard 3/4",
        "tests: review_support_scripts shard 4/4",
        "tests: codex_stop_hook_dispatcher shard 1/2",
        "tests: codex_stop_hook_dispatcher shard 2/2",
        "tests: codex_stop_review_validate_fix shard 1/4",
        "tests: codex_stop_review_validate_fix shard 2/4",
        "tests: codex_stop_review_validate_fix shard 3/4",
        "tests: codex_stop_review_validate_fix shard 4/4",
    }


def test_contract_check_timing_report_accounts_internal_steps() -> None:
    module = load_check_plugin_contracts_module()

    report = module.build_timing_report(
        started_at="2026-05-01T00:00:00Z",
        ended_at="2026-05-01T00:00:01Z",
        duration_ms=1000,
        returncode=0,
        command=["bash", "scripts/check_skill_contracts.sh"],
        top_level_steps=[
            {
                "label": "preflight",
                "source": "check_plugin_contracts.py",
                "status": "completed",
                "returncode": 0,
                "duration_ms": 50,
            },
            {
                "label": "contract shell script",
                "source": "check_plugin_contracts.py",
                "status": "completed",
                "returncode": 0,
                "duration_ms": 900,
            },
        ],
        shell_steps=[
            {
                "label": "python compile",
                "source": "check_skill_contracts.sh",
                "status": "completed",
                "returncode": 0,
                "duration_ms": 200,
            },
            {
                "label": "tests: codex_stop_review_validate_fix",
                "source": "check_skill_contracts.sh",
                "status": "completed",
                "returncode": 0,
                "duration_ms": 600,
            },
        ],
    )

    assert report["slowest_step"]["label"] == "tests: codex_stop_review_validate_fix"
    assert report["slowest_step"]["percentage_of_total"] == 60.0
    assert report["slowest_step"]["percentage_of_wall_time"] == 60.0
    assert report["slowest_step"]["percentage_of_measured_work"] == 63.16
    assert report["measured_work_duration_ms"] == 950
    assert any(step["label"] == "shell script overhead" for step in report["steps"])
    assert report["groups"][0]["name"] == "tests"
    assert report["groups"][0]["duration_ms"] == 600


def test_run_ledger_summary_preserves_contract_timing_fields(tmp_path: Path) -> None:
    module = load_rvf_logging_module()
    ledger = module.RunLedger(
        component="dispatcher",
        run_id="rvf-contract-timing-preserve",
        run_dir=tmp_path / "run",
    )

    ledger.summary(
        status="synced",
        reason_code="synced",
        dev_sync_steps=[{"name": "contract-check"}],
        contract_check_timing_report_path="/tmp/contract-check.timing.json",
        contract_check_timing={
            "path": "/tmp/contract-check.timing.json",
            "slowest_step": {"label": "tests: codex_stop_review_validate_fix"},
        },
    )
    later = ledger.summary(
        status="session-hook-control",
        reason_code="session_hook_gate_disabled",
    )

    assert later["dev_sync_steps"] == [{"name": "contract-check"}]
    assert later["contract_check_timing_report_path"] == "/tmp/contract-check.timing.json"
    assert later["contract_check_timing"]["slowest_step"]["label"] == (
        "tests: codex_stop_review_validate_fix"
    )


def test_rvf_logging_non_canonical_skill_dirs_default_to_installed_plugin_state(
    tmp_path: Path,
) -> None:
    module = load_rvf_logging_module()
    installed_skill = tmp_path / "home" / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"
    installed_skill.mkdir(parents=True)
    (installed_skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    cline_skill = (
        tmp_path
        / "home"
        / ".cline"
        / "worktrees"
        / "9336c"
        / "review-validate-fix"
        / "plugins"
        / "review-validate-fix"
        / "skills"
        / "review-validate-fix"
    )
    dev_skill = tmp_path / "dev" / "skills" / "review-validate-fix"

    original = os.environ.get("CODEX_RVF_INSTALLED_SKILL_DIR")
    os.environ["CODEX_RVF_INSTALLED_SKILL_DIR"] = str(installed_skill)
    try:
        assert module.default_log_root_for_skill_dir(cline_skill) == installed_skill / "state"
        assert module.default_log_root_for_skill_dir(dev_skill) == installed_skill / "state"
        assert (
            module.default_log_root_for_skill_dir(installed_skill) == installed_skill / "state"
        )
    finally:
        if original is None:
            os.environ.pop("CODEX_RVF_INSTALLED_SKILL_DIR", None)
        else:
            os.environ["CODEX_RVF_INSTALLED_SKILL_DIR"] = original


def test_rvf_logging_falls_back_to_skill_dir_state_when_install_missing(
    tmp_path: Path,
) -> None:
    module = load_rvf_logging_module()
    installed_skill = tmp_path / "home" / "plugins" / "missing" / "skills" / "review-validate-fix"
    dev_skill = tmp_path / "dev" / "skills" / "review-validate-fix"

    original = os.environ.get("CODEX_RVF_INSTALLED_SKILL_DIR")
    os.environ["CODEX_RVF_INSTALLED_SKILL_DIR"] = str(installed_skill)
    try:
        assert module.default_log_root_for_skill_dir(dev_skill) == dev_skill / "state"
    finally:
        if original is None:
            os.environ.pop("CODEX_RVF_INSTALLED_SKILL_DIR", None)
        else:
            os.environ["CODEX_RVF_INSTALLED_SKILL_DIR"] = original


def test_check_review_output_accepts_wrapped_issue_continuation() -> None:
    result = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text=(
            "1. apps/theseus-mcp/src/tool_registry.ts:1306 task 级上下文先截断 reviewRuns。\n"
            "`query_checkpoint_context` 随后用截断后的 run 集合过滤 signals，可能漏掉同 task 的较早 run。\n"
        ),
    )
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["kind"] == "issues"
    assert payload["issue_count"] == 1
    assert payload["continuation_line_count"] == 1

    extensionless_numbered = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="1. Dockerfile:3 合法 issue 可以引用没有扩展名的文件。\n",
    )
    extensionless_payload = json.loads(extensionless_numbered.stdout)
    assert extensionless_payload["valid"] is True
    assert extensionless_payload["issue_count"] == 1

    invalid = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. apps/foo.ts 这条缺少行号\n续行不能补足 path:line\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0

    misplaced_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. 这里先写说明，再引用 plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert misplaced_path_line.returncode != 0

    english_misplaced_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. explanation before plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert english_misplaced_path_line.returncode != 0

    prose_see_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. See plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44 misplaced path\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert prose_see_path_line.returncode != 0

    prose_in_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. in plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44 misplaced path\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert prose_in_path_line.returncode != 0

    prose_because_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. Because a.py:1 misplaced path\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert prose_because_path_line.returncode != 0

    chinese_because_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. 因为 a.py:1 misplaced path\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert chinese_because_path_line.returncode != 0

    chinese_file_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. 文件 a.py:1 misplaced path\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert chinese_file_path_line.returncode != 0

    prose_note_colon_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. Note: a.py:1 misplaced path\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert prose_note_colon_path_line.returncode != 0

    prose_warning_path_line = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. warning a.py:1 misplaced path\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert prose_warning_path_line.returncode != 0

    invalid_extensionless = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input=(
            "1. plugins/review-validate-fix/skills/review-validate-fix/scripts/check_review_output.py:44 valid issue\n"
            "Dockerfile:2 missing numbered prefix\n"
            "Makefile:10 missing numbered prefix\n"
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid_extensionless.returncode != 0

    unnumbered_issue = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\nb.py:2 第二条问题但缺少编号\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert unnumbered_issue.returncode != 0

    unnumbered_no_extension_issue = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\nMakefile:2 第二条问题但缺少编号\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert unnumbered_no_extension_issue.returncode != 0

    malformed_numbered_issue = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\n2) b.py:2 第二条编号格式错误\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed_numbered_issue.returncode != 0

    malformed_numbered_continuation = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\n2) 第二条编号格式错误\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert malformed_numbered_continuation.returncode != 0

    spaced_path = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="1. slide-versions/claude cowork 1/deck.txt:2 含空格路径仍是合法 path:line。\n",
    )
    spaced_payload = json.loads(spaced_path.stdout)
    assert spaced_payload["valid"] is True
    assert spaced_payload["issue_count"] == 1

    spaced_root_component = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="1. my dir/file.py:2 根目录组件含空格仍是合法 path:line。\n",
    )
    spaced_root_payload = json.loads(spaced_root_component.stdout)
    assert spaced_root_payload["valid"] is True
    assert spaced_root_payload["issue_count"] == 1

    colon_path = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="1. foo:bar.py:2 路径名含冒号时应使用最后的 :line 作为行号。\n",
    )
    colon_payload = json.loads(colon_path.stdout)
    assert colon_payload["valid"] is True
    assert colon_payload["issue_count"] == 1

    unicode_root_path = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="1. 设计 文档.md:3 非 ASCII 根路径也应支持。\n",
    )
    unicode_root_payload = json.loads(unicode_root_path.stdout)
    assert unicode_root_payload["valid"] is True
    assert unicode_root_payload["issue_count"] == 1

    repeated_path_line = run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input_text="1. a.py:1 causes b.py:2 to fail when both paths are involved.\n",
    )
    repeated_payload = json.loads(repeated_path_line.stdout)
    assert repeated_payload["valid"] is True
    assert repeated_payload["issue_count"] == 1

    chinese_no_issue_continuation = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\n没有问题\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert chinese_no_issue_continuation.returncode != 0

    fix_summary_continuation = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\n修复说明：已修改文件\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert fix_summary_continuation.returncode != 0

    handoff_completion_continuation = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\nRVF_HANDOFF_FILE: /tmp/rvf-handoff.md\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert handoff_completion_continuation.returncode != 0

    handoff_reviewers_summary_continuation = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\nReviewers：NO_ISSUES\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert handoff_reviewers_summary_continuation.returncode != 0

    handoff_validate_fixers_summary_continuation = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\nValidate/fixers：REAL fixed\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert handoff_validate_fixers_summary_continuation.returncode != 0

    unnumbered_spaced_issue = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\nmy file.py:2 第二条问题但缺少编号\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert unnumbered_spaced_issue.returncode != 0

    unnumbered_spaced_dir_issue = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\nmy dir/file.py:2 第二条问题但缺少编号\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert unnumbered_spaced_dir_issue.returncode != 0

    unnumbered_colon_issue = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\nfoo:bar.py:2 第二条问题但缺少编号\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert unnumbered_colon_issue.returncode != 0

    unnumbered_unicode_issue = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_OUTPUT), "--json"],
        input="1. a.py:1 第一条问题\n设计 文档.md:3 第二条问题但缺少编号\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert unnumbered_unicode_issue.returncode != 0


def test_build_packet_metadata_and_scope(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：updated tracked.txt\n",
        encoding="utf-8",
    )
    packet = tmp_path / "packet.md"
    metadata = tmp_path / "packet.json"
    run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
            "--primary-file",
            "tracked.txt",
            "--background-file",
            "new.txt",
        ]
    )
    packet_text = packet.read_text(encoding="utf-8")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert "## Review Scope" in packet_text
    assert "## Session Context" in packet_text
    assert payload["session_context_provided"] is True
    assert payload["session_context_bytes"] > 0
    assert payload["scope_of_work_file"] == str(context.resolve())
    assert payload["primary_files"] == ["tracked.txt"]
    assert payload["background_files"] == ["new.txt"]
    assert payload["packet_bytes"] == len(packet_text.encode("utf-8"))


def test_build_packet_allows_clean_repo_with_manual_scope(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run(["git", "add", "tracked.txt", "new.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "settle worktree"], cwd=repo)
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：manual scoped review\n"
        "- 本 turn 主会话实际完成的工作：仓库当前 clean；本轮审查范围来自用户显式指定\n"
        "- Scope：审查 tracked.txt 的现有实现面\n",
        encoding="utf-8",
    )
    packet = tmp_path / "packet.md"
    metadata = tmp_path / "packet.json"

    run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
            "--primary-file",
            "tracked.txt",
        ]
    )

    packet_text = packet.read_text(encoding="utf-8")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert "## Review Scope" in packet_text
    assert "Primary files for this turn:" in packet_text
    assert "tracked.txt" in packet_text
    assert "## Git Status\n\n```text\n(clean)\n```" in packet_text
    assert "## Git Diff HEAD\n\n```diff\n(no tracked diff)\n```" in packet_text
    assert payload["status_bytes"] == 0
    assert payload["diff_bytes"] == 0
    assert payload["primary_files"] == ["tracked.txt"]
    assert payload["session_context_provided"] is True


def test_session_manifest_extracts_apply_patch_and_command_candidates(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo / "owned-new.txt").write_text("owned\n", encoding="utf-8")
    (repo / "generated.txt").write_text("generated\n", encoding="utf-8")
    (repo / "background.txt").write_text("background contents\n", encoding="utf-8")
    transcript = write_codex_transcript(tmp_path / "session.jsonl", repo)
    manifest_path = tmp_path / "manifest.json"

    run(
        [
            sys.executable,
            str(SESSION_MANIFEST),
            "--repo",
            str(repo),
            "--transcript",
            str(transcript),
            "--output",
            str(manifest_path),
        ]
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["session_id"] == "session-tracking-test"
    assert manifest["confidence"] == "medium"
    assert "tracked.txt" in manifest["owned_paths"]
    assert "owned-new.txt" in manifest["owned_paths"]
    assert "removed.txt" in manifest["owned_paths"]
    assert "generated.txt" in manifest["owned_paths"]
    assert "tracked.txt" in manifest["owned_dirty_paths"]
    assert "generated.txt" in manifest["owned_dirty_paths"]
    assert "background.txt" in manifest["unattributed_dirty_paths"]
    assert "new.txt" in manifest["unattributed_dirty_paths"]
    assert manifest["apply_patch_operations"][0]["operation"] == "update"
    assert manifest["command_path_candidates"][0]["reason"] == "shell_redirect"


def test_session_manifest_does_not_claim_post_commit_same_path_background_dirty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    transcript = tmp_path / "session.jsonl"
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        "-base\n"
        "+owned\n"
        "*** End Patch\n"
    )
    records = [
        {"timestamp": "2020-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": "S"}},
        {
            "timestamp": "2020-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "patch-old"},
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text("owned\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "commit owned change"], cwd=repo)
    (repo / "a.txt").write_text("background\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--no-tracker",
            ]
        ).stdout
    )

    assert manifest["owned_paths"] == ["a.txt"]
    assert manifest["owned_dirty_paths"] == []
    assert manifest["unattributed_dirty_paths"] == ["a.txt"]
    assert manifest["ownership_baseline"]["mode"] == "head_commit_time"
    assert manifest["ownership_baseline"]["ignored_tool_record_count"] == 1


def test_session_manifest_claims_apply_patch_after_commit_cutoff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    old_patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        "-base\n"
        "+owned\n"
        "*** End Patch\n"
    )
    new_patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        " owned\n"
        "+new\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "S"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": old_patch}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "git commit -m owned", "workdir": str(repo)}),
                "call_id": "commit-call",
            },
        },
        {"type": "event_msg", "payload": {"type": "exec_command_end", "call_id": "commit-call", "exit_code": 0}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": new_patch}},
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text("owned\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "owned"], cwd=repo)
    (repo / "a.txt").write_text("owned\nnew\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--no-tracker",
            ]
        ).stdout
    )

    assert manifest["owned_paths"] == ["a.txt"]
    assert manifest["owned_dirty_paths"] == ["a.txt"]
    assert manifest["unattributed_dirty_paths"] == []
    assert manifest["ownership_baseline"]["mode"] == "line_cutoff"
    assert manifest["ownership_baseline"]["included_tool_record_count"] == 1
    assert manifest["ownership_baseline"]["ignored_tool_record_count"] == 1


def test_session_manifest_only_claims_matching_apply_patch_hunk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text(
        "top\n"
        "keep\n"
        "middle\n"
        "keep\n"
        "bottom\n",
        encoding="utf-8",
    )
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        " bottom\n"
        "+session-owned\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "S"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch}},
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text(
        "top\n"
        "background\n"
        "middle\n"
        "keep\n"
        "bottom\n"
        "session-owned\n",
        encoding="utf-8",
    )
    log_root = tmp_path / "state"
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)}
    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
            ],
            env=env,
        ).stdout
    )

    owned_units = manifest["tracker"]["owned_units"]
    assert manifest["owned_dirty_paths"] == ["a.txt"]
    assert len(owned_units) == 1
    assert owned_units[0]["unit"] == "hunk"
    assert "session-owned" in run(["git", "diff", "HEAD", "--", "a.txt"], cwd=repo).stdout


def test_session_manifest_records_edit_claim_user_context(tmp_path: Path) -> None:
    module = load_diff_tracker_module()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        " base\n"
        "+claimed\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "S"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "first request"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "keep the claimed line"}},
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "patch-1"},
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text("base\nclaimed\n", encoding="utf-8")
    log_root = tmp_path / "logs"

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--tracker-run-id",
                "run-1",
            ],
            env={**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)},
        ).stdout
    )

    assert len(manifest["edit_claims"]) == 1
    claim = manifest["edit_claims"][0]
    assert claim["path"] == "a.txt"
    assert claim["call_id"] == "patch-1"
    assert claim["latest_user_message"] == "keep the claimed line"
    assert claim["latest_user_line_number"] == 3
    assert claim["mapped_unit_ids"]
    assert manifest["edit_claim_registration"]["status"] == "ok"
    assert manifest["edit_claim_registration"]["registered_count"] == 1

    tracker_dir = Path(manifest["tracker"]["tracker_dir"])
    with sqlite3.connect(tracker_dir / module.SQLITE_FILENAME) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT claim_id, session_id, call_id, path, status, latest_user_line_number, latest_user_message
              FROM edit_claims
            """
        ).fetchone()
        assert row is not None
        assert row["session_id"] == "S"
        assert row["call_id"] == "patch-1"
        assert row["path"] == "a.txt"
        assert row["status"] == "pending"
        assert row["latest_user_line_number"] == 3
        assert row["latest_user_message"] == "keep the claimed line"
        unit_count = conn.execute(
            "SELECT COUNT(*) FROM edit_claim_units WHERE claim_id=?",
            (row["claim_id"],),
        ).fetchone()[0]
        assert unit_count == len(claim["mapped_unit_ids"])
        conn.execute("DELETE FROM session_units WHERE session_id='S'")

    allocated = module.allocate_review_scope(
        repo=repo,
        session_id="S",
        run_id="review-run",
        reviewer_id="reviewer-1",
        log_root_override=log_root,
        transcript_max_line_number=4,
    )
    assert allocated["status"] == "allocated"
    completed = module.complete_review_scope(
        repo=repo,
        lease_id=allocated["lease_id"],
        unit_ids=allocated["scope"]["unit_ids"],
        scope_hash=allocated["scope_hash"],
        run_id="review-run",
        log_root_override=log_root,
    )
    assert completed["status"] == "released"
    assert completed["reviewed_edit_claim_count"] == 1
    with sqlite3.connect(tracker_dir / module.SQLITE_FILENAME) as conn:
        status = conn.execute("SELECT status FROM edit_claims").fetchone()[0]
        assert status == "reviewed"


def test_session_manifest_records_codex_message_user_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        " base\n"
        "+claimed\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "S"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "codex message user context"}],
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "patch-1"},
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text("base\nclaimed\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--no-tracker",
            ]
        ).stdout
    )

    assert manifest["apply_patch_operations"][0]["latest_user_line_number"] == 2
    assert manifest["apply_patch_operations"][0]["latest_user_message"] == "codex message user context"
    claim = manifest["edit_claims"][0]
    assert claim["latest_user_line_number"] == 2
    assert claim["latest_user_message"] == "codex message user context"


def test_complete_review_scope_waits_for_all_edit_claim_units(tmp_path: Path) -> None:
    module = load_diff_tracker_module()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    run(["git", "add", "a.txt", "b.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    (repo / "a.txt").write_text("a\nclaimed-a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\nclaimed-b\n", encoding="utf-8")
    log_root = tmp_path / "logs"
    registered = module.register_claims(
        repo=repo,
        session_id="S",
        run_id="run-1",
        worktree=repo,
        branch=None,
        owned_paths=["a.txt", "b.txt"],
        apply_patch_paths={"a.txt", "b.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert registered.status == "ok"
    unit_a = module.unit_ids_for_owned_unit(repo, module.OwnedUnit(path="a.txt", unit="path", hunk_anchor=None))[0]
    unit_b = module.unit_ids_for_owned_unit(repo, module.OwnedUnit(path="b.txt", unit="path", hunk_anchor=None))[0]
    edit_registered = module.register_edit_claims(
        repo=repo,
        session_id="S",
        run_id="run-1",
        edit_claims=[
            {
                "claim_id": "edit-two-units",
                "tool_name": "apply_patch",
                "call_id": "patch-1",
                "transcript_line_number": 10,
                "path": "a.txt",
                "hunk_index": 1,
                "operation": "update",
                "mapped_unit_ids": [unit_a, unit_b],
                "latest_user_line_number": 2,
                "latest_user_message": "change both files",
            }
        ],
        log_root_override=log_root,
    )
    assert edit_registered["status"] == "ok"

    first = module.complete_review_scope(
        repo=repo,
        lease_id="missing-lease-a",
        unit_ids=[unit_a],
        scope_hash="sha256:first",
        run_id="review-a",
        log_root_override=log_root,
    )
    assert first["status"] == "released"
    assert first["reviewed_edit_claim_count"] == 0
    tracker_dir = Path(first["tracker_dir"])
    with sqlite3.connect(tracker_dir / module.SQLITE_FILENAME) as conn:
        assert conn.execute("SELECT status FROM edit_claims WHERE claim_id='edit-two-units'").fetchone()[0] == "pending"

    second = module.complete_review_scope(
        repo=repo,
        lease_id="missing-lease-b",
        unit_ids=[unit_b],
        scope_hash="sha256:second",
        run_id="review-b",
        log_root_override=log_root,
    )
    assert second["status"] == "released"
    assert second["reviewed_edit_claim_count"] == 1
    with sqlite3.connect(tracker_dir / module.SQLITE_FILENAME) as conn:
        assert conn.execute("SELECT status FROM edit_claims WHERE claim_id='edit-two-units'").fetchone()[0] == "reviewed"


def test_session_manifest_suppresses_unresolved_without_tracker_watermark(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        "-base\n"
        "+owned\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "S"}},
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "patch-1"},
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text("base\nbackground\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--no-tracker",
            ]
        ).stdout
    )

    unresolved = manifest["patch_ownership"]["unresolved_owned_patch_hunks"]
    assert manifest["owned_dirty_paths"] == []
    assert manifest["unattributed_dirty_paths"] == ["a.txt"]
    assert unresolved == []


def test_session_manifest_reports_unresolved_apply_patch_hunk_after_tracker_watermark(tmp_path: Path) -> None:
    module = load_diff_tracker_module()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    (repo / "a.txt").write_text("base\nleased\n", encoding="utf-8")
    log_root = tmp_path / "logs"
    seeded = module.allocate_review_scope(
        repo=repo,
        session_id="S",
        run_id="run-1",
        reviewer_id="reviewer-1",
        log_root_override=log_root,
        transcript_max_line_number=1,
    )
    assert seeded["status"] == "allocated"

    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        "-base\n"
        "+owned\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "S"}},
        {"type": "event_msg", "payload": {"note": "prior turn"}},
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "patch-1"},
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text("base\nbackground\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--tracker-run-id",
                "run-2",
            ],
            env={**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)},
        ).stdout
    )

    unresolved = manifest["patch_ownership"]["unresolved_owned_patch_hunks"]
    assert len(unresolved) == 1
    assert unresolved[0]["path"] == "a.txt"
    assert unresolved[0]["call_id"] == "patch-1"
    assert unresolved[0]["reason"] == "no_current_diff_hunk_contains_patch_mutations"


def test_session_manifest_uses_tracker_transcript_watermark(tmp_path: Path) -> None:
    module = load_diff_tracker_module()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    (repo / "a.txt").write_text("base\nold\n", encoding="utf-8")
    log_root = tmp_path / "logs"
    first = module.allocate_review_scope(
        repo=repo,
        session_id="S",
        run_id="run-1",
        reviewer_id="reviewer-1",
        log_root_override=log_root,
        transcript_max_line_number=2,
    )
    assert first["status"] == "allocated"

    old_patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        " base\n"
        "+old\n"
        "*** End Patch\n"
    )
    new_patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        " base\n"
        "+new\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "S"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": old_patch}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": new_patch}},
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (repo / "a.txt").write_text("base\nnew\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--tracker-run-id",
                "run-2",
            ],
            env={**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)},
        ).stdout
    )

    assert manifest["ownership_baseline"]["tracker_transcript_max_line_number"] == 2
    assert manifest["ownership_baseline"]["included_tool_record_count"] == 1
    assert manifest["ownership_baseline"]["ignored_tool_record_count"] == 1
    assert manifest["patch_ownership"]["transcript_max_line_number"] == 3
    assert manifest["patch_ownership"]["expected_apply_patch_paths"] == ["a.txt"]


def test_session_manifest_legacy_timestampless_transcript_fallback_warns(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: tracked.txt\n"
        "@@\n"
        "-base\n"
        "+base\n"
        "+legacy\n"
        "*** End Patch\n"
    )
    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "legacy-session"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": patch}},
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--no-tracker",
            ]
        ).stdout
    )

    assert manifest["ownership_baseline"]["mode"] == "legacy_full_transcript"
    assert any("ownership_baseline_fallback" in warning for warning in manifest["warnings"])


def test_session_manifest_resolves_exec_paths_from_command_workdir(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    docs = repo / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("x\n", encoding="utf-8")
    transcript = tmp_path / "session.jsonl"
    records = [
        {
            "timestamp": "2999-04-27T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "session-subdir-test", "cwd": str(repo)},
        },
        {
            "timestamp": "2999-04-27T00:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "printf x > note.md", "workdir": str(docs)}),
                "call_id": "call_exec",
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"

    run(
        [
            sys.executable,
            str(SESSION_MANIFEST),
            "--repo",
            str(repo),
            "--transcript",
            str(transcript),
            "--output",
            str(manifest_path),
        ]
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "docs/note.md" in manifest["owned_paths"]
    assert "note.md" not in manifest["owned_paths"]
    assert "docs/note.md" in manifest["owned_dirty_paths"]
    assert "docs/note.md" not in manifest["unattributed_dirty_paths"]


def test_session_manifest_claims_claude_write_tool_paths(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("base\nclaude\n", encoding="utf-8")
    notebook = repo / "analysis.ipynb"
    notebook.write_text('{"cells":[],"metadata":{},"nbformat":4,"nbformat_minor":5}\n', encoding="utf-8")
    run(["git", "add", "analysis.ipynb"], cwd=repo)
    run(["git", "commit", "-q", "-m", "add notebook"], cwd=repo)
    notebook.write_text('{"cells":[{"cell_type":"code"}],"metadata":{},"nbformat":4,"nbformat_minor":5}\n', encoding="utf-8")
    (repo / "background.txt").write_text("background\n", encoding="utf-8")
    transcript = tmp_path / "claude-session.jsonl"
    records = [
        {"timestamp": "2999-04-27T00:00:00.000Z", "sessionId": "claude-session-1"},
        {
            "timestamp": "2999-04-27T00:00:01.000Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {
                            "file_path": str(repo / "tracked.txt"),
                            "old_string": "base\n",
                            "new_string": "base\nclaude\n",
                        },
                    }
                ]
            },
        },
        {
            "timestamp": "2999-04-27T00:00:02.000Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "NotebookEdit",
                        "input": {
                            "notebook_path": str(notebook),
                            "new_source": "print('rvf')",
                            "cell_type": "code",
                        },
                    }
                ]
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")

    manifest = json.loads(
        run(
            [
                sys.executable,
                str(SESSION_MANIFEST),
                "--repo",
                str(repo),
                "--transcript",
                str(transcript),
                "--no-tracker",
            ]
        ).stdout
    )

    assert manifest["session_id"] == "claude-session-1"
    assert manifest["confidence"] == "medium"
    assert manifest["owned_paths"] == ["analysis.ipynb", "tracked.txt"]
    assert manifest["owned_dirty_paths"] == ["analysis.ipynb", "tracked.txt"]
    assert "background.txt" in manifest["unattributed_dirty_paths"]
    assert "new.txt" in manifest["unattributed_dirty_paths"]
    assert manifest["claude_write_events"] == [
        {"line_number": 2, "name": "Edit", "path": "tracked.txt"},
        {"line_number": 3, "name": "NotebookEdit", "path": "analysis.ipynb"},
    ]
    assert manifest["ownership_baseline"]["mode"] == "head_commit_time"
    assert manifest["ownership_baseline"]["included_tool_record_count"] == 2


def test_diagnose_stop_hook_scope_reports_stale_runtime_and_claude_write_gap(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    transcript = tmp_path / "claude-session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-06T06:00:00.000Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": str(repo / "tracked.txt")},
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    run_dir = tmp_path / "run"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "stop-event.json").write_text(
        json.dumps({"cwd": str(repo), "transcript_path": str(transcript)}, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifacts / "session-manifest.json").write_text(
        json.dumps(
            {
                "owned_paths": [],
                "owned_dirty_paths": [],
                "unattributed_dirty_paths": ["tracked.txt"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    summary = run_dir / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-06T06:00:01Z",
                "status": "skipped",
                "reason_code": "no_session_owned_dirty",
                "repo": str(repo),
                "run_dir": str(run_dir),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    old_scripts = tmp_path / "old-scripts"
    old_scripts.mkdir()
    (old_scripts / "session_manifest.py").write_text(
        "def build_manifest():\n"
        "    return {'owned_dirty_paths': []}\n",
        encoding="utf-8",
    )
    (old_scripts / "codex_stop_review_validate_fix.py").write_text(
        "def legacy_session_scope_gate_payload():\n"
        "    pass\n",
        encoding="utf-8",
    )

    completed = run(
        [
            sys.executable,
            str(DIAGNOSE_STOP_HOOK_SCOPE),
            "--summary",
            str(summary),
            "--runtime-scripts-dir",
            str(old_scripts),
            "--format",
            "json",
        ]
    )
    payload = json.loads(completed.stdout)
    codes = {item["code"] for item in payload["diagnoses"]}

    assert payload["transcript_probe"]["claude_dirty_write_paths"] == ["tracked.txt"]
    assert "stop_hook_skipped_no_session_owned_dirty" in codes
    assert "run_manifest_missing_tracker_field" in codes
    assert "claude_writes_not_attributed" in codes
    assert "runtime_session_manifest_differs_from_reference" in codes
    assert "runtime_session_manifest_lacks_tracker_field" in codes


def test_build_packet_uses_session_manifest_as_scope_anchor(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：updated tracked.txt\n",
        encoding="utf-8",
    )
    (repo / "owned-new.txt").write_text("owned contents\n", encoding="utf-8")
    (repo / "background.txt").write_text("background contents\n", encoding="utf-8")
    transcript = write_codex_transcript(tmp_path / "session.jsonl", repo)
    manifest = tmp_path / "manifest.json"
    run(
        [
            sys.executable,
            str(SESSION_MANIFEST),
            "--repo",
            str(repo),
            "--transcript",
            str(transcript),
            "--output",
            str(manifest),
        ]
    )

    packet = tmp_path / "packet.md"
    metadata = tmp_path / "packet.json"
    run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--session-manifest",
            str(manifest),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
        ]
    )

    packet_text = packet.read_text(encoding="utf-8")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert "## Session Manifest" in packet_text
    assert "## Session-Owned Git Diff" in packet_text
    assert "## Full Git Diff HEAD (Evidence Only)" in packet_text
    assert "Session-owned paths:" in packet_text
    assert "- tracked.txt" in packet_text
    assert "- background.txt" in packet_text
    assert "Background untracked paths below were not attributed to this session and are not inlined" in packet_text
    assert "### owned-new.txt" in packet_text
    assert "owned contents" in packet_text
    assert "### background.txt" not in packet_text
    assert "background contents" not in packet_text
    assert payload["session_manifest_provided"] is True
    assert payload["session_owned_path_count"] >= 3
    assert payload["owned_untracked_count"] == 1
    assert payload["background_untracked_count"] >= 2


def test_build_packet_filters_session_owned_diff_to_tracker_hunk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    (repo / "a.txt").write_text(
        "top\n" + "\n".join(f"keep-{index}" for index in range(20)) + "\nbottom\n",
        encoding="utf-8",
    )
    run(["git", "add", "a.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    (repo / "a.txt").write_text(
        "top\n"
        "background\n"
        + "\n".join(f"keep-{index}" for index in range(20))
        + "\nbottom\n"
        + "session-owned\n",
        encoding="utf-8",
    )
    diff_text = run(["git", "diff", "-U3", "HEAD", "--", "a.txt"], cwd=repo).stdout
    selected_header: str | None = None
    current_header: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            current_header = line
        if "+session-owned" in line:
            selected_header = current_header
            break
    assert selected_header is not None

    context = tmp_path / "context.md"
    context.write_text("session context\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": str(repo.resolve()),
                "session_id": "S",
                "confidence": "medium",
                "owned_paths": ["a.txt"],
                "owned_dirty_paths": ["a.txt"],
                "unattributed_dirty_paths": [],
                "tracker": {
                    "status": "ok",
                    "session_id": "S",
                    "tracker_scope": {
                        "unit_ids": ["unit-session"],
                        "lease_id": "lse-test",
                        "scope_hash": "sha256:" + "a" * 64,
                        "paths": ["a.txt"],
                        "hunks": [
                            {
                                "unit_id": "unit-session",
                                "path": "a.txt",
                                "hunk_header": selected_header,
                            }
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    packet = tmp_path / "packet.md"
    metadata = tmp_path / "packet.json"

    run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--session-manifest",
            str(manifest),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
        ]
    )

    packet_text = packet.read_text(encoding="utf-8")
    session_owned_diff = packet_text.split("## Full Git Diff HEAD", 1)[0]
    assert "+session-owned" in session_owned_diff
    assert "+background" not in session_owned_diff
    assert "+background" in packet_text


def test_build_packet_rejects_session_manifest_for_different_repo(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：reject mismatched manifest\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": str(tmp_path / "other-repo"),
                "owned_paths": ["tracked.txt"],
                "owned_dirty_paths": ["tracked.txt"],
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--session-manifest",
            str(manifest),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "session manifest repo does not match current repo" in completed.stderr


def test_build_packet_rejects_empty_session_owned_scope(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：reject empty manifest scope\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": str(repo.resolve()),
                "owned_paths": [],
                "owned_dirty_paths": [],
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--session-manifest",
            str(manifest),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "session manifest has no owned paths" in completed.stderr


def test_build_packet_requires_session_context(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    completed = subprocess.run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "session context is required" in completed.stderr


def test_build_packet_honors_review_validate_fix_ignore(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：prepared ignored artifacts\n",
        encoding="utf-8",
    )
    (repo / ".review-validate-fix-ignore").write_text("slide-versions/\nsecret\n", encoding="utf-8")
    (repo / "secret.txt").write_text("committed secret contents\n", encoding="utf-8")
    run(["git", "add", "secret.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "add secret"], cwd=repo)
    ignored = repo / "slide-versions" / "claude cowork 1"
    ignored.mkdir(parents=True)
    (ignored / "deck.txt").write_text("ignored deck contents\n", encoding="utf-8")
    (repo / "secret.txt").write_text("ignored secret contents\n", encoding="utf-8")
    (repo / "secret-alpha.txt").write_text("ignored secret prefix contents\n", encoding="utf-8")
    (repo / "kept.txt").write_text("visible contents\n", encoding="utf-8")

    packet = tmp_path / "packet.md"
    metadata = tmp_path / "packet.json"
    run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
        ]
    )

    packet_text = packet.read_text(encoding="utf-8")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert payload["excluded_path_prefixes"] == ["secret", "slide-versions/"]
    assert payload["untracked_count"] == 3
    assert "## Excluded Paths" in packet_text
    assert "- secret" in packet_text
    assert "- slide-versions/" in packet_text
    assert "### .review-validate-fix-ignore" in packet_text
    assert "### kept.txt" in packet_text
    assert "### new.txt" in packet_text
    assert "slide-versions/claude cowork 1/deck.txt" not in packet_text
    assert "ignored deck contents" not in packet_text
    assert "### secret.txt" not in packet_text
    assert "secret.txt |" not in packet_text
    assert "### secret-alpha.txt" not in packet_text
    assert "committed secret contents" not in packet_text
    assert "ignored secret contents" not in packet_text
    assert "ignored secret prefix contents" not in packet_text


def test_build_packet_treats_ignore_prefixes_as_literal_pathspecs(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：prepared literal ignore paths\n",
        encoding="utf-8",
    )
    (repo / ".review-validate-fix-ignore").write_text("literal[glob]/\nsecret*.txt\n", encoding="utf-8")
    literal_dir = repo / "literal[glob]"
    wildcard_dir = repo / "literalx"
    literal_dir.mkdir()
    wildcard_dir.mkdir()
    (literal_dir / "hidden.txt").write_text("hidden literal dir\n", encoding="utf-8")
    (wildcard_dir / "visible.txt").write_text("visible wildcard-like dir\n", encoding="utf-8")
    (repo / "secret*.txt").write_text("hidden literal file\n", encoding="utf-8")
    (repo / "secret-alpha.txt").write_text("visible wildcard-like file\n", encoding="utf-8")

    packet = tmp_path / "packet.md"
    metadata = tmp_path / "packet.json"
    run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
        ]
    )

    packet_text = packet.read_text(encoding="utf-8")
    assert "literal[glob]/hidden.txt" not in packet_text
    assert "hidden literal dir" not in packet_text
    assert "### secret*.txt" not in packet_text
    assert "hidden literal file" not in packet_text
    assert "### literalx/visible.txt" in packet_text
    assert "visible wildcard-like dir" in packet_text
    assert "### secret-alpha.txt" in packet_text
    assert "visible wildcard-like file" in packet_text


def test_prepare_review_run_and_command_lock(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：prepared review run\n",
        encoding="utf-8",
    )
    (repo / "secret.txt").write_text("hidden\n", encoding="utf-8")
    result = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--base-dir",
            str(tmp_path / "runs"),
            "--primary-file",
            "tracked.txt",
            "--exclude-path-prefix",
            "secret.txt",
        ]
    )
    payload = json.loads(result.stdout)
    assert Path(payload["review_packet"]).exists()
    assert Path(payload["review_packet_metadata"]).exists()
    assert Path(payload["before_workspace_snapshot"]).exists()
    assert Path(payload["scope_of_work_file"]).exists()
    assert Path(payload["inputs_dir"]).exists()
    assert Path(payload["scope_contract"]).exists()
    assert payload["scope_contract"].endswith("artifacts/inputs/scope.contract.json")
    assert Path(payload["review_env_file"]).exists()
    assert Path(payload["review_agent_context_file"]).exists()
    assert payload["session_context"] == payload["scope_of_work_file"]
    assert payload["source_session_context"] == str(context.resolve())
    assert payload["session_context_provided"] is True
    assert payload["excluded_path_prefixes"] == ["secret.txt"]
    assert payload["review_env"]["RVF_REPO"] == str(repo.resolve())
    assert payload["review_env"]["RVF_INPUTS_DIR"] == payload["inputs_dir"]
    assert payload["review_env"]["RVF_SCOPE_CONTRACT"] == payload["scope_contract"]
    assert payload["review_env"]["RVF_SCOPE_OF_WORK"] == payload["scope_of_work_file"]
    assert payload["review_env"]["RVF_REVIEW_PACKET"] == payload["review_packet"]
    assert payload["review_env"]["RVF_WRITE_REVIEW_RESULT"].endswith("scripts/write_review_result.py")
    assert payload["review_env"]["RVF_CHECK_REVIEW_RESULT"].endswith("scripts/check_review_result.py")
    assert payload["review_env"]["RVF_REVIEW_RESULT"].endswith("artifacts/reviewers/reviewer/review-result.json")
    assert "${" not in payload["review_env"]["RVF_REVIEW_RESULT"]
    assert payload["review_env"]["CODEX_RVF_LOG_ROOT"] == str(Path(payload["run_dir"]).parents[1])
    assert payload["review_env"]["CODEX_RVF_RUN_ID"] == payload["run_id"]
    assert payload["review_env"]["CODEX_RVF_RUN_DIR"] == payload["run_dir"]
    assert payload["review_env"]["RVF_BACKEND"] == "manual"
    assert payload["rvf_backend"] == "manual"
    assert payload["rvf_state_phase"] == "prepare"
    assert payload["rvf_scope_contract_path"] == payload["scope_contract"]
    assert payload["rvf_review_packet_path"] == payload["review_packet"]
    review_env_text = Path(payload["review_env_file"]).read_text(encoding="utf-8")
    assert "export RVF_RUN_DIR=" in review_env_text
    assert "export CODEX_RVF_LOG_ROOT=" in review_env_text
    assert 'export CODEX_RVF_RUN_ID="$RVF_RUN_ID"' in review_env_text
    assert 'export CODEX_RVF_RUN_DIR="$RVF_RUN_DIR"' in review_env_text
    assert "export RVF_BACKEND=manual" in review_env_text
    assert 'export RVF_ARTIFACTS_DIR="$RVF_RUN_DIR/artifacts"' in review_env_text
    assert 'export RVF_INPUTS_DIR="$RVF_ARTIFACTS_DIR/inputs"' in review_env_text
    assert 'export RVF_SCOPE_CONTRACT="$RVF_INPUTS_DIR/scope.contract.json"' in review_env_text
    assert 'export RVF_SCOPE_OF_WORK="$RVF_ARTIFACTS_DIR/scope-of-work.md"' in review_env_text
    assert 'export RVF_REVIEW_PACKET="$RVF_ARTIFACTS_DIR/review-packet.md"' in review_env_text
    assert 'export RVF_REVIEW_RESULT="$RVF_ARTIFACTS_DIR/reviewers/${RVF_REVIEWER_ID:-reviewer}/review-result.json"' in review_env_text
    review_agent_context_text = Path(payload["review_agent_context_file"]).read_text(encoding="utf-8")
    assert payload["review_agent_context"] == review_agent_context_text
    assert "## RVF Generated Reviewer Context" in review_agent_context_text
    assert f". {payload['review_env_file']}" in review_agent_context_text
    assert "- scope contract: `$RVF_SCOPE_CONTRACT`" in review_agent_context_text
    assert "- scope-of-work: `$RVF_SCOPE_OF_WORK`" in review_agent_context_text
    assert "- review packet: `$RVF_REVIEW_PACKET`" in review_agent_context_text
    assert "- command lock wrapper: `$RVF_COMMAND_LOCK`" in review_agent_context_text
    assert "- review result writer: `$RVF_WRITE_REVIEW_RESULT`" in review_agent_context_text
    assert "- reviewer result artifact: `$RVF_REVIEW_RESULT`" in review_agent_context_text
    assert "Scope precedence: read `$RVF_SCOPE_CONTRACT` first" in review_agent_context_text
    assert "`primary_units` is non-empty" in review_agent_context_text
    assert "not the final scope contract" in review_agent_context_text
    assert payload["scope_of_work_file"] not in review_agent_context_text
    assert payload["review_packet"] not in review_agent_context_text
    metadata = json.loads(Path(payload["review_packet_metadata"]).read_text(encoding="utf-8"))
    packet_text = Path(payload["review_packet"]).read_text(encoding="utf-8")
    assert metadata["excluded_path_prefixes"] == ["secret.txt"]
    assert metadata["scope_of_work_file"] == payload["scope_of_work_file"]
    assert "## Excluded Paths" in packet_text
    assert "- secret.txt" in packet_text
    assert "### secret.txt" not in packet_text
    contract = json.loads(Path(payload["scope_contract"]).read_text(encoding="utf-8"))
    assert contract["version"] == 2
    assert contract["run_id"] == payload["run_id"]
    assert contract["scope_mode"] == "custom"
    assert contract["canonical_issues"] == []
    assert contract["primary_files"] == ["tracked.txt"]
    assert contract["fix_allowlist"] == ["tracked.txt"]
    assert contract["review_packet_path"] == payload["input_review_packet"]
    assert contract["start_snapshot_path"] == payload["input_before_workspace_snapshot"]
    assert contract["scope_hash"] == payload["scope_contract_payload"]["scope_hash"]
    assert contract["primary_units"] is None
    assert contract["tracker_lease_id"] is None
    assert contract["tracker_scope_hash"] is None

    locked = run(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "contract-test",
            "--",
            sys.executable,
            "-c",
            "print('locked')",
        ]
    )
    assert "locked" in locked.stdout


def test_alternative_reviewer_prompt_uses_session_env_refs(tmp_path: Path) -> None:
    module = load_alternative_reviewer_module()
    repo = init_repo(tmp_path / "repo")
    prompt_file = tmp_path / "review-prompt.md"
    prompt_file.write_text("# Review Prompt\n\nBody\n", encoding="utf-8")
    context = tmp_path / "very" / "long" / "artifacts" / "scope-of-work.md"
    context.parent.mkdir(parents=True)
    context.write_text("scope\n", encoding="utf-8")
    packet = tmp_path / "very" / "long" / "artifacts" / "review-packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    scope_contract = packet.parent / "inputs" / "scope.contract.json"
    scope_contract.parent.mkdir()
    scope_contract.write_text('{"scope_hash":"abc"}\n', encoding="utf-8")

    result_path = tmp_path / "run" / "artifacts" / "reviewers" / "test" / "review-result.json"
    prompt = module.build_prompt(prompt_file, context, packet, repo, scope_contract, result_path)

    assert "$RVF_SCOPE_CONTRACT" in prompt
    assert "$RVF_SCOPE_OF_WORK" in prompt
    assert "$RVF_REVIEW_PACKET" in prompt
    assert "$RVF_COMMAND_LOCK" in prompt
    assert "$RVF_WRITE_REVIEW_RESULT" in prompt
    assert "$RVF_CHECK_REVIEW_RESULT" in prompt
    assert "$RVF_REVIEW_RESULT" in prompt
    assert "$RVF_REPO" in prompt
    assert "`primary_units` takes precedence over session manifest paths" in prompt
    assert "not as the final scope contract" in prompt
    assert str(scope_contract) not in prompt
    assert str(context) not in prompt
    assert str(result_path) not in prompt
    assert str(module.COMMAND_LOCK) not in prompt


def test_alternative_reviewer_infers_scope_contract_from_inputs_layout(tmp_path: Path) -> None:
    module = load_alternative_reviewer_module()
    inputs = tmp_path / "run" / "artifacts" / "inputs"
    inputs.mkdir(parents=True)
    packet = inputs / "review-packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    scope_contract = inputs / "scope.contract.json"
    scope_contract.write_text('{"scope_hash":"abc"}\n', encoding="utf-8")

    assert module.infer_scope_contract(packet) == scope_contract.resolve()


def test_alternative_reviewer_subprocess_receives_session_context_alias_and_scope_contract(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "scope-of-work.md"
    context.write_text("scope\n", encoding="utf-8")
    packet = tmp_path / "review-packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    scope_contract = tmp_path / "scope.contract.json"
    scope_contract.write_text('{"scope_hash":"abc"}\n', encoding="utf-8")
    reviewer_code = (
        "import os, sys; "
        "sys.stdin.read(); "
        f"expected = {str(context.resolve())!r}; "
        f"expected_scope = {str(scope_contract.resolve())!r}; "
        "assert os.environ['RVF_SCOPE_OF_WORK'] == expected; "
        "assert os.environ['RVF_SESSION_CONTEXT'] == expected; "
        "assert os.environ['RVF_SCOPE_CONTRACT'] == expected_scope; "
        "assert os.environ['RVF_REVIEW_RESULT']; "
        "import subprocess; "
        "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
        "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True); "
        "print('artifact written')"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-c", reviewer_code],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
    )

    completed = run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--review-packet",
            str(packet),
            "--scope-contract",
            str(scope_contract),
        ]
    )

    assert completed.stdout.strip() == "artifact written"


def test_alternative_reviewer_pre_run_health_refreshes_before_reviewer(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "review-packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    stale_run_dir = tmp_path / "stale-run"
    stale_run_dir.mkdir()
    token = tmp_path / "health-token"
    order = tmp_path / "order.txt"
    health_code = (
        "from pathlib import Path; "
        f"Path({str(token)!r}).write_text('ready', encoding='utf-8'); "
        f"Path({str(order)!r}).write_text('health\\n', encoding='utf-8'); "
        "print('HEALTH_OK')"
    )
    reviewer_code = (
        "import os, subprocess, sys; "
        "from pathlib import Path; "
        "sys.stdin.read(); "
        f"assert Path({str(token)!r}).read_text(encoding='utf-8') == 'ready'; "
        f"Path({str(order)!r}).write_text(Path({str(order)!r}).read_text(encoding='utf-8') + 'reviewer\\n', encoding='utf-8'); "
        "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
        "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True); "
        "print('artifact written')"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-c", reviewer_code],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        health_command=[sys.executable, "-c", health_code],
        pre_run_health=True,
    )
    env = os.environ.copy()
    env["RVF_RUN_DIR"] = str(stale_run_dir)
    env["RVF_REVIEW_RESULT"] = str(stale_run_dir / "wrong" / "review-result.json")

    completed = run(
        [
            sys.executable,
            str(RUN_ALTERNATIVE_REVIEWER),
            "--config",
            str(config),
            "--repo",
            str(repo),
            "--review-packet",
            str(packet),
        ],
        env=env,
    )

    assert completed.stdout.strip() == "artifact written"
    assert order.read_text(encoding="utf-8") == "health\nreviewer\n"
    assert not (stale_run_dir / "wrong" / "review-result.json").exists()


def test_alternative_reviewer_pre_run_health_failure_skips_reviewer(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "review-packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    marker = tmp_path / "reviewer-ran"
    reviewer_code = (
        "from pathlib import Path; "
        "import sys; "
        "sys.stdin.read(); "
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-c", reviewer_code],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        health_command=[sys.executable, "-c", "import sys; print('login failed'); sys.exit(7)"],
        pre_run_health=True,
    )

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
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "login failed" in completed.stderr
    assert not marker.exists()


def test_alternative_reviewer_pre_run_health_timeout_skips_reviewer(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "review-packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    marker = tmp_path / "reviewer-ran"
    reviewer_code = (
        "from pathlib import Path; "
        "import sys; "
        "sys.stdin.read(); "
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-c", reviewer_code],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        health_command=[sys.executable, "-c", "import time; time.sleep(2)"],
        pre_run_health=True,
    )
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["health_timeout_seconds"] = 0.1
    config.write_text(json.dumps(payload), encoding="utf-8")

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
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "health command timed out after" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert not marker.exists()


def test_prepare_review_run_manual_all_uncommitted_allows_dirty_paths(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text("scope\n", encoding="utf-8")

    completed = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--base-dir",
            str(tmp_path / "runs"),
        ]
    )

    payload = json.loads(completed.stdout)
    contract = json.loads(Path(payload["scope_contract"]).read_text(encoding="utf-8"))
    assert contract["scope_mode"] == "manual-all-uncommitted"
    assert contract["primary_files"] == ["new.txt", "tracked.txt"]
    assert contract["fix_allowlist"] == ["new.txt", "tracked.txt"]


def test_command_lock_writes_lifecycle_events(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    state = tmp_path / "state"
    run_id = "test-command-lock-lifecycle"
    env = os.environ.copy()
    env["CODEX_RVF_LOG_ROOT"] = str(state)
    env["CODEX_RVF_RUN_ID"] = run_id
    env.pop("CODEX_RVF_RUN_DIR", None)

    locked = run(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "lifecycle-test",
            "--",
            sys.executable,
            "-c",
            "print('locked')",
        ],
        env=env,
    )

    assert "locked" in locked.stdout
    events = read_jsonl(state / "runs" / run_id / "events.jsonl")
    event_names = [event["event"] for event in events]
    assert event_names == ["lock_wait_started", "lock_acquired", "lock_released"]
    assert {event["component"] for event in events} == {"command-lock"}
    assert all(event["phase"] == "review" for event in events)
    assert events[1]["lock_name"] == "lifecycle-test"
    assert events[2]["returncode"] == 0

    summary = json.loads((state / "runs" / run_id / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "completed"
    assert summary["reason_code"] == "lock_released"
    assert summary["lock_name"] == "lifecycle-test"


def test_command_lock_respects_env_run_dir(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    state = tmp_path / "state"
    run_dir = tmp_path / "custom-run-dir"
    env = os.environ.copy()
    env["CODEX_RVF_LOG_ROOT"] = str(state)
    env["CODEX_RVF_RUN_ID"] = "test-command-lock-custom-dir"
    env["CODEX_RVF_RUN_DIR"] = str(run_dir)

    run(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "custom-dir-test",
            "--",
            sys.executable,
            "-c",
            "print('locked')",
        ],
        env=env,
    )

    assert (run_dir / "events.jsonl").exists()
    assert not (state / "runs" / "test-command-lock-custom-dir" / "events.jsonl").exists()
    events = read_jsonl(run_dir / "events.jsonl")
    assert [event["event"] for event in events] == ["lock_wait_started", "lock_acquired", "lock_released"]


def test_command_lock_logs_timeout_with_holder_metadata(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    state = tmp_path / "state"
    lock_dir = tmp_path / "locks"
    holder_env = os.environ.copy()
    holder_env["CODEX_RVF_LOG_ROOT"] = str(state)
    holder_env["CODEX_RVF_RUN_ID"] = "test-command-lock-holder"
    holder_env.pop("CODEX_RVF_RUN_DIR", None)
    contender_env = os.environ.copy()
    contender_env["CODEX_RVF_LOG_ROOT"] = str(state)
    contender_env["CODEX_RVF_RUN_ID"] = "test-command-lock-contender"
    contender_env.pop("CODEX_RVF_RUN_DIR", None)

    lock_path_result = run(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "contended-test",
            "--lock-dir",
            str(lock_dir),
            "--print-path",
        ],
    )
    metadata_path = Path(lock_path_result.stdout.strip()).with_suffix(".json")

    holder = subprocess.Popen(
        [
            sys.executable,
            str(COMMAND_LOCK),
            "--repo",
            str(repo),
            "--name",
            "contended-test",
            "--lock-dir",
            str(lock_dir),
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(3)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=holder_env,
    )
    try:
        deadline = time.monotonic() + 5
        while not metadata_path.exists():
            if holder.poll() is not None:
                stdout, stderr = holder.communicate()
                raise AssertionError(stderr.strip() or stdout.strip() or "holder exited before acquiring lock")
            if time.monotonic() >= deadline:
                raise AssertionError("holder did not acquire lock")
            time.sleep(0.01)

        contender = subprocess.run(
            [
                sys.executable,
                str(COMMAND_LOCK),
                "--repo",
                str(repo),
                "--name",
                "contended-test",
                "--lock-dir",
                str(lock_dir),
                "--timeout",
                "0.3",
                "--poll-interval",
                "0.05",
                "--",
                sys.executable,
                "-c",
                "print('should-not-run')",
            ],
            capture_output=True,
            text=True,
            env=contender_env,
            check=False,
        )
    finally:
        if holder.poll() is None:
            holder.terminate()
        holder.communicate(timeout=5)

    assert contender.returncode == 75
    assert "current holder metadata" in contender.stderr
    events = read_jsonl(state / "runs" / "test-command-lock-contender" / "events.jsonl")
    event_names = [event["event"] for event in events]
    assert event_names == ["lock_wait_started", "lock_timeout"]
    timeout_event = events[-1]
    assert timeout_event["reason_code"] == "lock_timeout"
    assert timeout_event["lock_name"] == "contended-test"
    assert "holder_metadata" in timeout_event
    assert "contended-test" in str(timeout_event["holder_metadata"])


def test_prepare_review_run_can_build_session_manifest_from_transcript(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：prepared transcript-scoped review run\n",
        encoding="utf-8",
    )
    (repo / "owned-new.txt").write_text("owned\n", encoding="utf-8")
    (repo / "background.txt").write_text("background contents\n", encoding="utf-8")
    transcript = write_codex_transcript(tmp_path / "session.jsonl", repo)

    result = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--transcript",
            str(transcript),
            "--base-dir",
            str(tmp_path / "runs"),
        ]
    )
    payload = json.loads(result.stdout)
    assert Path(payload["session_manifest"]).exists()
    assert payload["session_manifest_provided"] is True
    assert payload["source_session_manifest"] == f"transcript:{transcript.resolve()}"
    packet_text = Path(payload["review_packet"]).read_text(encoding="utf-8")
    assert "## Session Manifest" in packet_text
    assert "background contents" not in packet_text


def test_prepare_review_run_requires_session_context(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    completed = subprocess.run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--base-dir",
            str(tmp_path / "runs"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "session context is required" in completed.stderr


def test_alternative_reviewer_idle_timeout_flag(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-c",
            "import sys, time; sys.stdin.read(); time.sleep(1.0)",
        ],
        idle_timeout_seconds=0.2,
        activity_check_interval_seconds=0.05,
    )

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
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 124
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" in completed.stderr


def test_alternative_reviewer_activity_probe_keeps_silent_reviewer_alive(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-c",
            "import time; time.sleep(0.25); " + clean_review_result_python(stdout="NO_ISSUES"),
        ],
        idle_timeout_seconds=0.08,
        activity_check_interval_seconds=0.03,
        activity_probe_command=[
            sys.executable,
            "-c",
            "import os; print('PROBE ' + os.environ.get('RVF_REVIEWER_PID', ''))",
        ],
        activity_probe_timeout_seconds=0.5,
        activity_probe_failure_threshold=2,
    )

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
            "--rvf-run-id",
            "probe-success-test",
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES"
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["activity_probe_configured"] is True
    assert summary["activity_probe_history"]
    assert any(
        item["status"] == "completed" and item["stdout"].startswith("PROBE")
        for item in summary["activity_probe_history"]
    )
    normalized = Path(summary["paths"]["normalized"]).read_text(encoding="utf-8")
    assert normalized.strip() == "NO_ISSUES"
    assert "PROBE" not in normalized
    assert summary["review_result_valid"] is True
    result_summary = json.loads(Path(summary["paths"]["review_result_summary"]).read_text(encoding="utf-8"))
    assert result_summary["kind"] == "no_issues"


def test_alternative_reviewer_requires_review_result_artifact(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-c",
            "import sys; sys.stdin.read(); print('NO_ISSUES')",
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
    )

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
            "--rvf-run-id",
            "missing-result-test",
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "missing review result artifact" in completed.stderr
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["reason_code"] == "reviewer_result_invalid"
    assert summary["review_result_valid"] is False


def test_alternative_reviewer_records_request_as_pending_state(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    reviewer_code = (
        "import os, subprocess, sys; "
        "sys.stdin.read(); "
        "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
        "'lock-request', '--out', os.environ['RVF_REVIEW_RESULT'], "
        "'--name', 'pytest', '--command', 'python3 -m pytest', "
        "'--reason', 'needs serialized test cache'], check=True); "
        "print('request written')"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-c", reviewer_code],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
    )

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
            "--rvf-run-id",
            "request-pending-test",
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "request written"
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "pending"
    assert summary["reason_code"] == "reviewer_request_pending"
    assert summary["returncode"] == 0
    assert summary["review_result_valid"] is True
    assert summary["review_result_kind"] == "request"
    assert summary["review_result_complete"] is False
    assert summary["review_request_pending"] is True
    assert summary["review_result_summary"]["request_types"] == ["lock_request"]
    events = read_jsonl(run_dir / "events.jsonl")
    assert any(
        event["event"] == "request_pending"
        and event["reason_code"] == "reviewer_request_pending"
        and event["review_result_kind"] == "request"
        for event in events
    )


def test_alternative_reviewer_activity_probe_failure_threshold_times_out(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-c",
            "import sys, time; sys.stdin.read(); time.sleep(1.0)",
        ],
        idle_timeout_seconds=0.08,
        activity_check_interval_seconds=0.03,
        activity_probe_command=[
            sys.executable,
            "-c",
            "import sys; print('inactive'); sys.exit(2)",
        ],
        activity_probe_timeout_seconds=0.5,
        activity_probe_failure_threshold=2,
    )

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
            "--rvf-run-id",
            "probe-failure-test",
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 124
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" in completed.stderr
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["reason_code"] == "reviewer_timeout"
    assert summary["timeout_reason"] == "no_observable_activity_probe_failed"
    assert summary["pid"] is not None
    assert summary["terminated_signal"] == "SIGKILL"
    assert len(summary["activity_probe_history"]) == 2
    assert all(item["returncode"] == 2 for item in summary["activity_probe_history"])
    normalized = Path(summary["paths"]["normalized"]).read_text(encoding="utf-8")
    assert normalized.strip() == "RVF_EXTERNAL_REVIEWER_TIMEOUT"


def test_alternative_reviewer_timeout_kills_child_process_group(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    marker = tmp_path / "child-survived.txt"
    child_code = (
        "import pathlib, time; "
        "time.sleep(2.0); "
        f"pathlib.Path({str(marker)!r}).write_text('survived', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess, sys, time; "
        "sys.stdin.read(); "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(10.0)"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-c", parent_code],
        idle_timeout_seconds=0.5,
        activity_check_interval_seconds=0.05,
    )

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
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 124
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" in completed.stderr
    time.sleep(2.3)
    assert not marker.exists()


def test_alternative_reviewer_activity_refreshes_idle_timeout(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import os, subprocess, sys, time; sys.stdin.read(); "
                "[print(f'tick-{i}', flush=True) or time.sleep(0.08) for i in range(4)]; "
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True); "
                "print('NO_ISSUES', flush=True)"
            ),
        ],
        idle_timeout_seconds=0.6,
        activity_check_interval_seconds=0.05,
    )

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
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "NO_ISSUES" in completed.stdout
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" not in completed.stderr


def test_alternative_reviewer_claude_bash_tool_use_suspends_idle_timeout(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import json, os, subprocess, sys, time; sys.stdin.read(); "
                "print(json.dumps({'type':'assistant','message':{'content':["
                "{'type':'tool_use','id':'toolu_1','name':'Bash','input':{'command':'sleep 1'}}"
                "]}}), flush=True); "
                "time.sleep(1.5); "
                "print(json.dumps({'type':'user','message':{'content':["
                "{'type':'tool_result','tool_use_id':'toolu_1','content':''}"
                "]}}), flush=True); "
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True); "
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)"
            ),
        ],
        idle_timeout_seconds=1.0,
        activity_check_interval_seconds=0.03,
        output_format="claude_stream_json",
    )

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
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES"
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" not in completed.stderr


def test_alternative_reviewer_claude_split_jsonl_preserves_tool_use(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import json, os, subprocess, sys, time; sys.stdin.read(); "
                "event = json.dumps({'type':'assistant','message':{'content':["
                "{'type':'tool_use','id':'toolu_1','name':'Bash','input':{'command':'sleep 1'}}"
                "]}}); "
                "split_at = len(event) // 2; "
                "sys.stdout.write(event[:split_at]); sys.stdout.flush(); "
                "time.sleep(0.04); "
                "sys.stdout.write(event[split_at:] + '\\n'); sys.stdout.flush(); "
                "time.sleep(0.25); "
                "print(json.dumps({'type':'user','message':{'content':["
                "{'type':'tool_result','tool_use_id':'toolu_1','content':''}"
                "]}}), flush=True); "
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True); "
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)"
            ),
        ],
        idle_timeout_seconds=1.0,
        activity_check_interval_seconds=0.03,
        output_format="claude_stream_json",
    )

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
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES"
    assert "RVF_EXTERNAL_REVIEWER_TIMEOUT" not in completed.stderr


def test_alternative_reviewer_repeated_run_keeps_prior_artifacts(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-c",
            clean_review_result_python(stdout="NO_ISSUES"),
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
    )
    command = [
        sys.executable,
        str(RUN_ALTERNATIVE_REVIEWER),
        "--config",
        str(config),
        "--repo",
        str(repo),
        "--review-packet",
        str(packet),
        "--rvf-run-id",
        "repeat-artifact-test",
        "--rvf-run-dir",
        str(run_dir),
    ]

    first = run(command)
    second = run(command)

    assert first.stdout.strip() == "NO_ISSUES"
    assert second.stdout.strip() == "NO_ISSUES"
    artifacts = run_dir / "artifacts" / "reviewers" / "test"
    for name in [
        "reviewer.prompt.txt",
        "reviewer.prompt.2.txt",
        "reviewer.stdout.txt",
        "reviewer.stdout.2.txt",
        "reviewer.stderr.txt",
        "reviewer.stderr.2.txt",
        "reviewer.normalized.txt",
        "reviewer.normalized.2.txt",
        "reviewer.summary.json",
        "reviewer.summary.2.json",
    ]:
        assert (artifacts / name).exists()
    assert not (run_dir / "artifacts" / "reviewer.prompt.txt").exists()


def test_alternative_reviewer_long_command_wait_uses_check_interval() -> None:
    module = load_alternative_reviewer_module()
    assert module.next_wait_seconds(
        activity_check_interval_seconds=5.0,
        remaining_idle_seconds=2.0,
        max_runtime_remaining_seconds=None,
        waiting_on_long_command=False,
    ) == 2.0
    assert module.next_wait_seconds(
        activity_check_interval_seconds=5.0,
        remaining_idle_seconds=0.0,
        max_runtime_remaining_seconds=None,
        waiting_on_long_command=True,
    ) == 5.0
    assert module.next_wait_seconds(
        activity_check_interval_seconds=5.0,
        remaining_idle_seconds=0.0,
        max_runtime_remaining_seconds=2.0,
        waiting_on_long_command=True,
    ) == 2.0
    assert module.next_wait_seconds(
        activity_check_interval_seconds=5.0,
        remaining_idle_seconds=0.0,
        max_runtime_remaining_seconds=None,
        waiting_on_long_command=False,
    ) == 0.01


def test_alternative_reviewer_claude_stream_monitor_tracks_bash_tool_state() -> None:
    module = load_alternative_reviewer_module()
    monitor = module.ClaudeStreamActivityMonitor()
    tool_use_event = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "sleep 1"},
                    }
                ]
            },
        }
    )
    split_at = len(tool_use_event) // 2

    monitor.ingest(tool_use_event[:split_at])
    assert monitor.waiting_on_long_command is False
    monitor.ingest(tool_use_event[split_at:] + "\n")
    assert monitor.waiting_on_long_command is True
    monitor.ingest(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "",
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    assert monitor.waiting_on_long_command is False

    monitor.ingest(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "sleep 1"},
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    assert monitor.waiting_on_long_command is True
    monitor.ingest(json.dumps({"type": "result", "result": "NO_ISSUES"}) + "\n")
    assert monitor.waiting_on_long_command is False


def test_alternative_reviewer_claude_stream_json_extracts_result(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import os, subprocess, sys, time, json; sys.stdin.read(); "
                "print(json.dumps({'type':'system','subtype':'init'}), flush=True); "
                "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'working'}]}}), flush=True); "
                "time.sleep(0.08); "
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True); "
                "print(json.dumps({'type':'result','subtype':'success','result':'NO_ISSUES'}), flush=True)"
            ),
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="claude_stream_json",
    )

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
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout


def test_alternative_reviewer_codex_json_extracts_agent_message(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import json, os, subprocess, sys; sys.stdin.read(); "
                "print(json.dumps({'type':'event_msg','payload':{'type':'agent_message','message':'working'}}), flush=True); "
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True); "
                "print(json.dumps({'type':'event_msg','payload':{'type':'agent_message','message':'NO_ISSUES'}}), flush=True)"
            ),
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="codex_json",
    )

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
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout


def test_alternative_reviewer_codex_json_extracts_item_completed_agent_message(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import json, os, subprocess, sys; sys.stdin.read(); "
                "print('non-json warning line', flush=True); "
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True); "
                "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'NO_ISSUES'}}), flush=True)"
            ),
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="codex_json",
    )

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
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout


def test_alternative_reviewer_codex_json_reports_backend_challenge_html(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    html = (
        "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
        "<body><script src=\"/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1\"></script>"
        "Cloudflare challenge</body></html>"
    )
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            f"import sys; sys.stdin.read(); print({html!r})",
        ],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="codex_json",
    )

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
            "--rvf-run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "RVF_CODEX_BACKEND_CHALLENGE" in completed.stdout
    assert "RVF_CODEX_BACKEND_CHALLENGE" in completed.stderr
    reviewer_dir = run_dir / "artifacts" / "reviewers" / "test"
    normalized = next(reviewer_dir.glob("reviewer.normalized*.txt")).read_text(encoding="utf-8")
    raw_stdout = next(reviewer_dir.glob("reviewer.stdout*.txt")).read_text(encoding="utf-8")
    summary = json.loads(next(reviewer_dir.glob("reviewer.summary*.json")).read_text(encoding="utf-8"))
    assert normalized.startswith("RVF_CODEX_BACKEND_CHALLENGE")
    assert "challenge-platform" in raw_stdout
    assert summary["output_error_reason"] == "codex_backend_challenge"


def test_alternative_reviewer_codex_exec_json_command_is_patched(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    shim = tmp_path / "codex"
    sink = tmp_path / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True)",
                "print(json.dumps({'type':'event_msg','payload':{'type':'agent_message','message':'NO_ISSUES'}}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        ["codex", "exec"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="codex_json",
    )

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
            "--rvf-run-dir",
            str(run_dir),
        ],
        env={"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert argv == [
        "--disable",
        "hooks",
        "exec",
        "--json",
        "--add-dir",
        str(run_dir.resolve()),
        "-",
    ]


def test_alternative_reviewer_codex_exec_after_global_options_is_patched(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    shim = tmp_path / "codex"
    sink = tmp_path / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True)",
                "print(json.dumps({'type':'event_msg','payload':{'type':'agent_message','message':'NO_ISSUES'}}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        ["codex", "--ask-for-approval", "never", "exec", "--sandbox", "workspace-write"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="codex_json",
    )

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
            "--rvf-run-dir",
            str(run_dir),
        ],
        env={"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert argv == [
        "--ask-for-approval",
        "never",
        "--disable",
        "hooks",
        "exec",
        "--sandbox",
        "workspace-write",
        "--json",
        "--add-dir",
        str(run_dir.resolve()),
        "-",
    ]


def test_alternative_reviewer_codex_hooks_disable_is_not_duplicated() -> None:
    module = load_alternative_reviewer_module()
    command = ["codex", "--disable", "hooks", "exec", "--json", "-"]
    assert module.ensure_codex_hooks_disabled_command(command) == command
    assert module.ensure_codex_hooks_disabled_command(
        ["codex", "--disable=hooks", "exec", "--json", "-"]
    ) == ["codex", "--disable=hooks", "exec", "--json", "-"]


def test_alternative_reviewer_sets_codex_stop_hook_suppress_env(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp_path / "codex"
    sink = tmp_path / "env.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps({"
                "'suppress': os.environ.get('CODEX_RVF_SUPPRESS_STOP_HOOK'), "
                "'thread': os.environ.get('CODEX_THREAD_ID')"
                "}))" % str(sink),
                "sys.stdin.read()",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True)",
                "print(json.dumps({'type':'event_msg','payload':{'type':'agent_message','message':'NO_ISSUES'}}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        ["codex", "exec"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="codex_json",
    )

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env.get('PATH', '')}"
    env["CODEX_THREAD_ID"] = "parent-thread-id-for-regression-test"
    env.pop("CODEX_RVF_SUPPRESS_STOP_HOOK", None)
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
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    payload = json.loads(sink.read_text(encoding="utf-8"))
    assert payload == {
        "suppress": "1",
        "thread": "parent-thread-id-for-regression-test",
    }


def test_alternative_reviewer_legacy_claude_config_gets_stream_json(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp_path / "claude"
    sink = tmp_path / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True)",
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        ["claude", "-p"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format=None,
    )

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
        ],
        env={"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert "--output-format" in argv
    assert "stream-json" in argv
    assert "--include-hook-events" in argv
    assert "--include-partial-messages" in argv
    assert "--verbose" in argv
    assert "--disable-slash-commands" in argv


def test_alternative_reviewer_respects_explicit_claude_text_output(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp_path / "claude"
    sink = tmp_path / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True)",
                "print('NO_ISSUES', flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        ["claude", "-p", "--output-format", "text"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format=None,
    )

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
        ],
        env={"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert argv == ["-p", "--output-format", "text"]


def test_alternative_reviewer_respects_explicit_claude_equals_text_output(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp_path / "claude"
    sink = tmp_path / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True)",
                "print('NO_ISSUES', flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        ["claude", "-p", "--output-format=text"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format=None,
    )

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
        ],
        env={"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert argv == ["-p", "--output-format=text"]


def test_alternative_reviewer_non_claude_stream_json_command_is_not_patched(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    packet = tmp_path / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp_path / "stream_wrapper"
    sink = tmp_path / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, os, subprocess, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "subprocess.run([sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], "
                "'no-issues', '--out', os.environ['RVF_REVIEW_RESULT']], check=True)",
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp_path / "alternative-reviewer.json",
        [sys.executable, "-u", str(shim), "--native-stream"],
        idle_timeout_seconds=5.0,
        activity_check_interval_seconds=0.05,
        output_format="claude_stream_json",
    )

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
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    assert json.loads(sink.read_text(encoding="utf-8")) == ["--native-stream"]



def test_cline_kanban_client_detects_runtime_port() -> None:
    module = load_cline_kanban_client_module()
    assert module.DEFAULT_START_CMD == "kanban --no-open"
    assert module.DEFAULT_TASK_CMD == "kanban task"
    assert module.resolve_runtime_port(
        start_cmd=module.DEFAULT_START_CMD,
        task_cmd=module.DEFAULT_TASK_CMD,
        env={},
    ) == 3484
    assert module.resolve_runtime_port(
        start_cmd="kanban --port 3499 --no-open",
        task_cmd="kanban task",
        env={},
    ) == 3499
    assert module.resolve_runtime_port(
        start_cmd="kanban --port=3500 --no-open",
        task_cmd="kanban --port=3500 task",
        env={},
    ) == 3500
    assert module.resolve_runtime_port(task_cmd="env KANBAN_RUNTIME_PORT=3502 kanban task", env={}) == 3502
    assert module.resolve_runtime_port(task_cmd="kanban task", env={"KANBAN_RUNTIME_PORT": "3501"}) == 3501


def test_cline_kanban_client_rejects_ambiguous_runtime_ports() -> None:
    module = load_cline_kanban_client_module()
    for kwargs, expected in (
        (
            {
                "start_cmd": "kanban --port auto --no-open",
                "task_cmd": "kanban task",
                "env": {},
            },
            "--port auto is not supported",
        ),
        (
            {
                "start_cmd": "kanban --port 3499 --no-open",
                "task_cmd": "kanban --port 3500 task",
                "env": {},
            },
            "conflicting Cline Kanban runtime ports",
        ),
    ):
        try:
            module.resolve_runtime_port(**kwargs)
        except module.KanbanError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"expected KanbanError containing {expected!r}")


def test_cline_kanban_client_reports_missing_stable_binary() -> None:
    module = load_cline_kanban_client_module()
    try:
        module.run_command(["rvf-missing-kanban-command-for-test"], check=False)
    except module.KanbanError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected missing kanban command to raise KanbanError")
    assert "Cline Kanban command not found" in message
    assert "npm install -g kanban@0.1.67" in message
    assert "does not use npx" in message


def test_cline_kanban_client_accepts_cline_tmux_listener_from_foreign_cwd(tmp_path: Path) -> None:
    module = load_cline_kanban_client_module()
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir(parents=True)
    other.mkdir()
    fake_task = tmp_path / "fake_kanban_task.py"
    fake_task.write_text(
        "import json\n"
        "print(json.dumps({'ok': True, 'tasks': []}))\n",
        encoding="utf-8",
    )

    original_listener_pids = module.listener_pids_for_port
    original_process_cwd = module.process_cwd
    original_process_command = module.process_command
    original_tmux_sessions = module.tmux_sessions_for_pid
    try:
        module.listener_pids_for_port = lambda port: [4242]
        module.process_cwd = lambda pid: other
        module.process_command = lambda pid: "node /usr/local/bin/kanban --no-open"
        module.tmux_sessions_for_pid = lambda pid: ["cline-kanban-3484"]
        result = module.ensure_kanban(
            task_cmd=f"{sys.executable} {fake_task}",
            start_cmd="kanban --no-open",
            repo=repo,
            tmux_session="unused",
            timeout_seconds=0,
            start_if_needed=False,
        )
    finally:
        module.listener_pids_for_port = original_listener_pids
        module.process_cwd = original_process_cwd
        module.process_command = original_process_command
        module.tmux_sessions_for_pid = original_tmux_sessions

    assert result["started"] is False
    assert result["list"]["ok"] is True


def test_cline_kanban_client_accepts_cline_tmux_listener_through_parent_pane() -> None:
    module = load_cline_kanban_client_module()
    original_run_command = module.run_command
    original_process_parent_pid = module.process_parent_pid
    try:
        module.process_parent_pid = lambda pid: {4242: 1000, 1000: 1}.get(pid)

        def fake_run_command(command, **kwargs):
            if command[:3] == ["tmux", "list-panes", "-a"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="cline-kanban-3484\t1000\nrvf-other\t7777\n",
                    stderr="",
                )
            raise AssertionError(f"unexpected command: {command!r}")

        module.run_command = fake_run_command
        sessions = module.tmux_sessions_for_pid(4242)
    finally:
        module.run_command = original_run_command
        module.process_parent_pid = original_process_parent_pid

    assert sessions == ["cline-kanban-3484"]


def test_cline_kanban_client_rejects_listener_without_cline_tmux_session(tmp_path: Path) -> None:
    module = load_cline_kanban_client_module()
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir(parents=True)
    other.mkdir()
    fake_task = tmp_path / "fake_kanban_task.py"
    fake_task.write_text(
        "import json\n"
        "print(json.dumps({'ok': True, 'tasks': []}))\n",
        encoding="utf-8",
    )

    original_listener_pids = module.listener_pids_for_port
    original_process_cwd = module.process_cwd
    original_process_command = module.process_command
    original_tmux_sessions = module.tmux_sessions_for_pid
    try:
        module.listener_pids_for_port = lambda port: [4242]
        module.process_cwd = lambda pid: other
        module.process_command = lambda pid: "node /usr/local/bin/kanban --no-open"
        module.tmux_sessions_for_pid = lambda pid: ["rvf-vibe-kanban"]
        try:
            module.ensure_kanban(
                task_cmd=f"{sys.executable} {fake_task}",
                start_cmd="kanban --no-open",
                repo=repo,
                tmux_session="unused",
                timeout_seconds=0,
                start_if_needed=False,
            )
        except module.KanbanError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected non-Cline Kanban tmux listener to be rejected")
    finally:
        module.listener_pids_for_port = original_listener_pids
        module.process_cwd = original_process_cwd
        module.process_command = original_process_command
        module.tmux_sessions_for_pid = original_tmux_sessions

    assert "no listener pane belongs to tmux session `cline-kanban`" in message
    assert "rvf-vibe-kanban" in message


def test_cline_kanban_client_accepts_workspace_payload_from_cline_tmux_listener(tmp_path: Path) -> None:
    module = load_cline_kanban_client_module()
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir(parents=True)
    other.mkdir()
    fake_task = tmp_path / "fake_kanban_task.py"
    fake_task.write_text(
        "import json, sys\n"
        "project_path = sys.argv[sys.argv.index('--project-path') + 1]\n"
        "print(json.dumps({'ok': True, 'workspacePath': project_path, 'tasks': []}))\n",
        encoding="utf-8",
    )

    original_listener_pids = module.listener_pids_for_port
    original_process_cwd = module.process_cwd
    original_process_command = module.process_command
    original_tmux_sessions = module.tmux_sessions_for_pid
    try:
        module.listener_pids_for_port = lambda port: [4242]
        module.process_cwd = lambda pid: other
        module.process_command = lambda pid: "node /usr/local/bin/kanban --no-open"
        module.tmux_sessions_for_pid = lambda pid: ["cline-kanban-3484"]
        result = module.ensure_kanban(
            task_cmd=f"{sys.executable} {fake_task}",
            start_cmd="npx -y kanban@0.1.66 --no-open",
            repo=repo,
            tmux_session="unused",
            timeout_seconds=0,
            start_if_needed=False,
        )
    finally:
        module.listener_pids_for_port = original_listener_pids
        module.process_cwd = original_process_cwd
        module.process_command = original_process_command
        module.tmux_sessions_for_pid = original_tmux_sessions

    assert result["started"] is False
    assert result["list"]["workspacePath"] == str(repo)


def test_cline_kanban_client_rejects_workspace_payload_without_cline_tmux_listener(tmp_path: Path) -> None:
    module = load_cline_kanban_client_module()
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir(parents=True)
    other.mkdir()
    fake_task = tmp_path / "fake_kanban_task.py"
    fake_task.write_text(
        "import json, sys\n"
        "project_path = sys.argv[sys.argv.index('--project-path') + 1]\n"
        "print(json.dumps({'ok': True, 'workspacePath': project_path, 'tasks': []}))\n",
        encoding="utf-8",
    )

    original_listener_pids = module.listener_pids_for_port
    original_process_cwd = module.process_cwd
    original_process_command = module.process_command
    original_tmux_sessions = module.tmux_sessions_for_pid
    try:
        module.listener_pids_for_port = lambda port: [4242]
        module.process_cwd = lambda pid: other
        module.process_command = lambda pid: "node /usr/local/bin/kanban --no-open"
        module.tmux_sessions_for_pid = lambda pid: []
        try:
            module.ensure_kanban(
                task_cmd=f"{sys.executable} {fake_task}",
                start_cmd="npx -y kanban@0.1.66 --no-open",
                repo=repo,
                tmux_session="unused",
                timeout_seconds=0,
                start_if_needed=False,
            )
        except module.KanbanError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected workspace echo without Cline Kanban tmux listener to be rejected")
    finally:
        module.listener_pids_for_port = original_listener_pids
        module.process_cwd = original_process_cwd
        module.process_command = original_process_command
        module.tmux_sessions_for_pid = original_tmux_sessions

    assert "no listener pane belongs to tmux session `cline-kanban`" in message
    assert str(other) in message


def test_cline_kanban_client_does_not_start_when_listener_exists_but_list_fails(tmp_path: Path) -> None:
    module = load_cline_kanban_client_module()
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir(parents=True)
    other.mkdir()
    fake_task = tmp_path / "fake_kanban_task.py"
    fake_task.write_text(
        "import sys\n"
        "print('task list failed', file=sys.stderr)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )

    started: list[object] = []
    original_listener_pids = module.listener_pids_for_port
    original_process_cwd = module.process_cwd
    original_process_command = module.process_command
    original_tmux_sessions = module.tmux_sessions_for_pid
    original_start = module.start_kanban_server
    try:
        module.listener_pids_for_port = lambda port: [4242]
        module.process_cwd = lambda pid: other
        module.process_command = lambda pid: "node /usr/local/bin/kanban --no-open"
        module.tmux_sessions_for_pid = lambda pid: ["cline-kanban-3484"]
        module.start_kanban_server = lambda **kwargs: started.append(kwargs) or {}
        try:
            module.ensure_kanban(
                task_cmd=f"{sys.executable} {fake_task}",
                start_cmd="npx -y kanban@0.1.66 --no-open",
                repo=repo,
                tmux_session="unused",
                timeout_seconds=0,
                start_if_needed=True,
            )
        except module.KanbanError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected existing listener connection failure")
    finally:
        module.listener_pids_for_port = original_listener_pids
        module.process_cwd = original_process_cwd
        module.process_command = original_process_command
        module.tmux_sessions_for_pid = original_tmux_sessions
        module.start_kanban_server = original_start

    assert started == []
    assert "will not start another Kanban server" in message
    assert "task list failed" in message


def test_cline_kanban_client_create_and_start_task(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake_task = tmp_path / "fake_kanban_task.py"
    calls = tmp_path / "calls.jsonl"
    fake_task.write_text(
        "import json, os, sys\n"
        "with open(os.environ['KANBAN_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({\n"
        "        'argv': sys.argv[1:],\n"
        "        'port': os.environ.get('KANBAN_RUNTIME_PORT'),\n"
        "    }) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        "    print(json.dumps({'ok': True, 'tasks': []}))\n"
        "elif sys.argv[1] == 'create':\n"
        "    print(json.dumps({'task_id': 'task-1'}))\n"
        "elif sys.argv[1] == 'start':\n"
        "    print(json.dumps({'task_id': 'task-1', 'status': 'started'}))\n"
        "elif sys.argv[1] == 'message':\n"
        "    print(json.dumps({'task_id': 'task-1', 'message_id': 'msg-1', 'status': 'queued'}))\n"
        "elif sys.argv[1] == 'trash':\n"
        "    print(json.dumps({'task_id': 'task-1', 'status': 'trashed'}))\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )
    repo = init_repo(tmp_path / "repo")
    env = os.environ.copy()
    env.pop("KANBAN_RUNTIME_PORT", None)
    env["CODEX_RVF_CLINE_KANBAN_START_CMD"] = "kanban --port 45678"
    env["KANBAN_CALLS"] = str(calls)
    task_cmd = f"{sys.executable} {fake_task}"
    ensure = run(
        [
            sys.executable,
            str(CLINE_KANBAN_CLIENT),
            "ensure",
            "--repo",
            str(repo),
            "--task-cmd",
            task_cmd,
        ],
        env=env,
    )
    assert json.loads(ensure.stdout)["started"] is False
    prep_file = tmp_path / "dispatch-prep.json"
    create = run([
        sys.executable,
        str(CLINE_KANBAN_CLIENT),
        "create",
        "--repo",
        str(repo),
        "--task-cmd",
        task_cmd,
        "--base-ref",
        "HEAD",
        "--prompt",
        "hello",
        "--title",
        "RVF test",
        "--agent-id",
        "codex",
        "--parent-session-id",
        "parent-session",
        "--worktree-mode",
        "branch",
        "--prep-file-path",
        str(prep_file),
    ], env=env)
    assert json.loads(create.stdout)["task_id"] == "task-1"
    started = run([
        sys.executable,
        str(CLINE_KANBAN_CLIENT),
        "start",
        "--repo",
        str(repo),
        "--task-cmd",
        task_cmd,
        "--task-id",
        "task-1",
        "--worktree-mode",
        "inplace",
    ], env=env)
    started_payload = json.loads(started.stdout)
    assert started_payload["status"] == "started"
    assert Path(started_payload["workspace_path"]).resolve() == repo.resolve()
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("$review-validate-fix\n", encoding="utf-8")
    message = run([
        sys.executable,
        str(CLINE_KANBAN_CLIENT),
        "message",
        "--repo",
        str(repo),
        "--task-cmd",
        task_cmd,
        "--task-id",
        "task-1",
        "--prompt-file",
        str(prompt_file),
        "--source",
        "review-validate-fix",
        "--idempotency-key",
        "run-1",
    ], env=env)
    assert json.loads(message.stdout)["message_id"] == "msg-1"
    recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert [entry["argv"][0] for entry in recorded] == ["list", "create", "start", "list", "message"]
    assert [entry["port"] for entry in recorded] == ["45678", "45678", "45678", "45678", "45678"]
    create_call = recorded[1]["argv"]
    assert create_call[create_call.index("--title") + 1] == "RVF test"
    assert create_call[create_call.index("--agent-id") + 1] == "codex"
    assert create_call[create_call.index("--parent-session-id") + 1] == "parent-session"
    assert create_call[create_call.index("--worktree-mode") + 1] == "branch"
    assert create_call[create_call.index("--prep-file-path") + 1] == str(prep_file.resolve())
    start_call = recorded[2]["argv"]
    assert start_call[start_call.index("--task-id") + 1] == "task-1"
    message_call = recorded[4]["argv"]
    assert message_call[message_call.index("--task-id") + 1] == "task-1"
    assert message_call[message_call.index("--prompt-file") + 1] == str(prompt_file.resolve())
    assert message_call[message_call.index("--idempotency-key") + 1] == "run-1"


def test_cline_kanban_client_rejects_main_worktree_mode(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    completed = subprocess.run(
        [
            sys.executable,
            str(CLINE_KANBAN_CLIENT),
            "create",
            "--repo",
            str(repo),
            "--base-ref",
            "HEAD",
            "--prompt",
            "hello",
            "--worktree-mode",
            "main",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "invalid choice: 'main'" in completed.stderr


def test_cline_kanban_client_start_task_uses_session_cwd_workspace(tmp_path: Path) -> None:
    module = load_cline_kanban_client_module()
    repo = init_repo(tmp_path / "repo")
    task_repo = init_repo(tmp_path / "task-worktree" / "repo")
    fake_task = tmp_path / "fake_kanban_task.py"
    fake_task.write_text(
        "import json, sys\n"
        "project_path = sys.argv[sys.argv.index('--project-path') + 1]\n"
        "if sys.argv[1] == 'start':\n"
        "    print(json.dumps({'ok': True, 'task': {'id': 'task-1', 'workspacePath': project_path}}))\n"
        "elif sys.argv[1] == 'list':\n"
        "    print(json.dumps({'ok': True, 'workspacePath': project_path, 'tasks': [\n"
        "        {'id': 'task-1', 'workspacePath': project_path, 'session': {'pid': 4242}}\n"
        "    ]}))\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )

    original_process_cwd = module.process_cwd
    try:
        module.process_cwd = lambda pid: task_repo if pid == 4242 else None
        payload = module.start_task(
            task_cmd=f"{sys.executable} {fake_task}",
            repo=repo,
            task_id="task-1",
            worktree_mode="branch",
        )
    finally:
        module.process_cwd = original_process_cwd

    assert payload["task_id"] == "task-1"
    assert Path(payload["workspace_path"]).resolve() == task_repo.resolve()
    assert Path(payload["project_path"]).resolve() == repo.resolve()
    assert payload["workspace_path_source"] == "task_session_cwd"


def test_cline_kanban_client_branch_mode_prefers_task_workspace_over_project_path(tmp_path: Path) -> None:
    module = load_cline_kanban_client_module()
    repo = init_repo(tmp_path / "repo")
    task_repo = init_repo(tmp_path / "task-worktree" / "repo")
    fake_task = tmp_path / "fake_kanban_task.py"
    fake_task.write_text(
        "import json, sys\n"
        "project_path = sys.argv[sys.argv.index('--project-path') + 1]\n"
        f"task_path = {str(task_repo)!r}\n"
        "if sys.argv[1] == 'start':\n"
        "    print(json.dumps({'ok': True, 'projectPath': project_path, 'task': {\n"
        "        'id': 'task-1', 'workspacePath': task_path\n"
        "    }}))\n"
        "elif sys.argv[1] == 'list':\n"
        "    print(json.dumps({'ok': True, 'workspacePath': project_path, 'tasks': [\n"
        "        {'id': 'task-1', 'workspacePath': task_path, 'session': None}\n"
        "    ]}))\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )

    payload = module.start_task(
        task_cmd=f"{sys.executable} {fake_task}",
        repo=repo,
        task_id="task-1",
        worktree_mode="branch",
    )

    assert Path(payload["workspace_path"]).resolve() == task_repo.resolve()
    assert Path(payload["project_path"]).resolve() == repo.resolve()
    assert payload["workspace_path_source"] == "task_payload_workspace_path"


def test_cline_kanban_client_branch_mode_rejects_parent_project_workspace(tmp_path: Path) -> None:
    module = load_cline_kanban_client_module()
    repo = init_repo(tmp_path / "repo")
    fake_task = tmp_path / "fake_kanban_task.py"
    fake_task.write_text(
        "import json, sys\n"
        "project_path = sys.argv[sys.argv.index('--project-path') + 1]\n"
        "if sys.argv[1] == 'start':\n"
        "    print(json.dumps({'ok': True, 'task': {'id': 'task-1', 'workspacePath': project_path}}))\n"
        "elif sys.argv[1] == 'list':\n"
        "    print(json.dumps({'ok': True, 'workspacePath': project_path, 'tasks': [\n"
        "        {'id': 'task-1', 'workspacePath': project_path, 'session': None}\n"
        "    ]}))\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )

    original_timeout = os.environ.get("CODEX_RVF_CLINE_KANBAN_WORKSPACE_TIMEOUT")
    try:
        os.environ["CODEX_RVF_CLINE_KANBAN_WORKSPACE_TIMEOUT"] = "0"
        module.start_task(
            task_cmd=f"{sys.executable} {fake_task}",
            repo=repo,
            task_id="task-1",
            worktree_mode="branch",
        )
    except module.KanbanError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected branch mode parent project workspace to be rejected")
    finally:
        if original_timeout is None:
            os.environ.pop("CODEX_RVF_CLINE_KANBAN_WORKSPACE_TIMEOUT", None)
        else:
            os.environ["CODEX_RVF_CLINE_KANBAN_WORKSPACE_TIMEOUT"] = original_timeout

    assert "parent project path in branch mode" in message
    assert str(repo) in message


def test_cline_kanban_client_message_accepts_response_without_task_id(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake_task = tmp_path / "fake_kanban_task.py"
    calls = tmp_path / "calls.jsonl"
    fake_task.write_text(
        "import json, os, sys\n"
        "with open(os.environ['KANBAN_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'message':\n"
        "    print(json.dumps({'message_id': 'msg-1', 'status': 'queued'}))\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )
    repo = init_repo(tmp_path / "repo")
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("$review-validate-fix\n", encoding="utf-8")
    env = os.environ.copy()
    env["KANBAN_CALLS"] = str(calls)
    task_cmd = f"{sys.executable} {fake_task}"

    message = run([
        sys.executable,
        str(CLINE_KANBAN_CLIENT),
        "message",
        "--repo",
        str(repo),
        "--task-cmd",
        task_cmd,
        "--task-id",
        "task-1",
        "--prompt-file",
        str(prompt_file),
        "--source",
        "review-validate-fix",
        "--idempotency-key",
        "run-1",
    ], env=env)

    payload = json.loads(message.stdout)
    assert payload["task_id"] == "task-1"
    assert payload["message_id"] == "msg-1"
    recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert recorded[0][recorded[0].index("--task-id") + 1] == "task-1"


def test_prepare_review_run_writes_worktree_bootstrap(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run(["git", "checkout", "--", "tracked.txt"], cwd=repo)
    (repo / "tracked.txt").write_text("base\n\n", encoding="utf-8")
    run(["git", "add", "tracked.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "blank context"], cwd=repo)
    (repo / "tracked.txt").write_text("changed\n\n", encoding="utf-8")
    (repo / "owned.txt").write_text("owned untracked\n", encoding="utf-8")
    (repo / "background.txt").write_text("background\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "repo": str(repo),
        "owned_paths": ["tracked.txt", "owned.txt"],
        "owned_dirty_paths": ["tracked.txt", "owned.txt"],
        "unattributed_dirty_paths": ["background.txt"],
        "confidence": "high",
    }), encoding="utf-8")
    context = tmp_path / "context.md"
    context.write_text("scope\n", encoding="utf-8")
    completed = run([
        sys.executable,
        str(PREPARE_REVIEW_RUN),
        "--repo",
        str(repo),
        "--session-context",
        str(context),
        "--session-manifest",
        str(manifest),
    ])
    payload = json.loads(completed.stdout)
    bootstrap = json.loads(Path(payload["worktree_bootstrap"]).read_text(encoding="utf-8"))
    assert bootstrap["tracked_paths"] == ["tracked.txt"]
    # Full-dirty bootstrap: both session-owned and unattributed untracked files
    # are now copied; background.txt is part of the bootstrap snapshot too.
    assert sorted(item["path"] for item in bootstrap["untracked_files"]) == [
        "background.txt",
        "owned.txt",
    ]
    assert bootstrap["bootstrap_kind"] == "full-dirty"
    assert bootstrap["session_owned_dirty_paths"] == ["owned.txt", "tracked.txt"]
    assert bootstrap["unattributed_dirty_paths"] == ["background.txt"]
    assert bootstrap["unattributed_path_count"] == 1
    assert "tracked.txt" in Path(payload["worktree_bootstrap_patch"]).read_text(encoding="utf-8")
    clean = tmp_path / "clean"
    run(["git", "clone", "-q", str(repo), str(clean)], cwd=tmp_path)
    run(["git", "apply", "--check", str(payload["worktree_bootstrap_patch"])], cwd=clean)


def test_prepare_review_run_worktree_bootstrap_respects_review_validate_fix_ignore(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    run(["git", "checkout", "--", "tracked.txt"], cwd=repo)
    (repo / "tracked.txt").write_text("base\n\n", encoding="utf-8")
    run(["git", "add", "tracked.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "blank context"], cwd=repo)
    (repo / "tracked.txt").write_text("changed\n\n", encoding="utf-8")
    (repo / "dist").mkdir()
    (repo / "dist" / "build.js").write_text("compiled\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "lib.js").write_text("vendor\n", encoding="utf-8")
    (repo / "owned.txt").write_text("real owned\n", encoding="utf-8")
    (repo / ".review-validate-fix-ignore").write_text("dist/\nnode_modules/\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "repo": str(repo),
                "owned_paths": ["tracked.txt", "owned.txt"],
                "owned_dirty_paths": ["tracked.txt", "owned.txt"],
                "unattributed_dirty_paths": ["dist/build.js", "node_modules/lib.js"],
                "confidence": "high",
            }
        ),
        encoding="utf-8",
    )
    context = tmp_path / "context.md"
    context.write_text("scope\n", encoding="utf-8")
    completed = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--session-manifest",
            str(manifest),
        ]
    )
    payload = json.loads(completed.stdout)
    bootstrap = json.loads(Path(payload["worktree_bootstrap"]).read_text(encoding="utf-8"))
    paths_in_bootstrap = set(bootstrap["owned_dirty_paths"])
    assert "dist/build.js" not in paths_in_bootstrap
    assert "node_modules/lib.js" not in paths_in_bootstrap
    assert "owned.txt" in paths_in_bootstrap
    assert "tracked.txt" in paths_in_bootstrap
    assert bootstrap["bootstrap_kind"] == "session-owned-only"
    # Both ignored paths land in ignored_dirty_paths for transparency.
    ignored = set(bootstrap.get("ignored_dirty_paths") or [])
    assert "dist/build.js" in ignored
    assert "node_modules/lib.js" in ignored


def test_prepare_review_run_worktree_bootstrap_untracked_storage_names_do_not_collide(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo / "a").mkdir()
    (repo / "a" / "b.txt").write_text("slash path\n", encoding="utf-8")
    (repo / "a__b.txt").write_text("flat path\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "repo": str(repo),
                "owned_paths": ["a/b.txt", "a__b.txt"],
                "owned_dirty_paths": ["a/b.txt", "a__b.txt"],
                "unattributed_dirty_paths": [],
                "confidence": "high",
            }
        ),
        encoding="utf-8",
    )
    context = tmp_path / "context.md"
    context.write_text("scope\n", encoding="utf-8")

    completed = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--session-manifest",
            str(manifest),
        ]
    )
    payload = json.loads(completed.stdout)
    bootstrap = json.loads(Path(payload["worktree_bootstrap"]).read_text(encoding="utf-8"))
    stored_paths = [item["stored_path"] for item in bootstrap["untracked_files"]]
    assert [item["path"] for item in bootstrap["untracked_files"]] == ["a/b.txt", "a__b.txt"]
    assert len(set(stored_paths)) == 2

    clean = tmp_path / "clean"
    run(["git", "clone", "-q", str(repo), str(clean)], cwd=tmp_path)
    run(
        [
            sys.executable,
            str(APPLY_WORKTREE_BOOTSTRAP),
            "--metadata",
            str(payload["worktree_bootstrap"]),
            "--repo",
            str(clean),
        ]
    )
    assert (clean / "a" / "b.txt").read_text(encoding="utf-8") == "slash path\n"
    assert (clean / "a__b.txt").read_text(encoding="utf-8") == "flat path\n"


def test_prepare_review_run_scope_file_matches_metadata_through_symlink_state(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    context = tmp_path / "context.md"
    context.write_text("scope\n", encoding="utf-8")
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    symlink_state = tmp_path / "state-link"
    symlink_state.symlink_to(real_state, target_is_directory=True)
    env = os.environ.copy()
    env["CODEX_RVF_STATE_DIR"] = str(symlink_state)

    completed = run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
        ],
        env=env,
    )

    payload = json.loads(completed.stdout)
    metadata = json.loads(Path(payload["review_packet_metadata"]).read_text(encoding="utf-8"))
    assert metadata["scope_of_work_file"] == payload["scope_of_work_file"]
    assert str(real_state.resolve()) in payload["scope_of_work_file"]


def test_apply_worktree_bootstrap_replays_tracked_and_untracked(tmp_path: Path) -> None:
    source = init_repo(tmp_path / "source")
    clone = tmp_path / "clone"
    run(["git", "clone", "-q", str(source), str(clone)], cwd=tmp_path)
    (source / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (source / "owned.txt").write_text("owned\n", encoding="utf-8")
    base_ref = run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
    patch = tmp_path / "bootstrap.patch"
    patch.write_text(subprocess.run(["git", "diff", "--binary", "HEAD", "--", "tracked.txt"], cwd=source, check=True, capture_output=True, text=True).stdout, encoding="utf-8")
    files = tmp_path / "files"
    files.mkdir()
    stored = files / "owned.txt"
    stored.write_text("owned\n", encoding="utf-8")
    metadata = tmp_path / "bootstrap.json"
    metadata.write_text(json.dumps({"base_ref": base_ref, "patch_file": str(patch), "files_dir": str(files), "untracked_files": [{"path": "owned.txt", "stored_path": str(stored)}]}), encoding="utf-8")
    completed = run([sys.executable, str(APPLY_WORKTREE_BOOTSTRAP), "--metadata", str(metadata), "--repo", str(clone)])
    assert json.loads(completed.stdout)["ok"] is True
    assert (clone / "tracked.txt").read_text(encoding="utf-8") == "changed\n"
    assert (clone / "owned.txt").read_text(encoding="utf-8") == "owned\n"


def test_apply_worktree_bootstrap_rejects_mismatched_base_ref(tmp_path: Path) -> None:
    source = init_repo(tmp_path / "source")
    clone = tmp_path / "clone"
    run(["git", "clone", "-q", str(source), str(clone)], cwd=tmp_path)
    (source / "tracked.txt").write_text("changed\n", encoding="utf-8")
    base_ref = run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
    patch = tmp_path / "bootstrap.patch"
    patch.write_text(subprocess.run(["git", "diff", "--binary", "HEAD", "--", "tracked.txt"], cwd=source, check=True, capture_output=True, text=True).stdout, encoding="utf-8")
    metadata = tmp_path / "bootstrap.json"
    metadata.write_text(json.dumps({"base_ref": base_ref, "patch_file": str(patch), "untracked_files": []}), encoding="utf-8")

    (clone / "other.txt").write_text("other\n", encoding="utf-8")
    run(["git", "add", "other.txt"], cwd=clone)
    run(["git", "commit", "-q", "-m", "advance clone"], cwd=clone)
    completed = subprocess.run(
        [sys.executable, str(APPLY_WORKTREE_BOOTSTRAP), "--metadata", str(metadata), "--repo", str(clone)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "base_ref mismatch" in completed.stdout
    assert (clone / "tracked.txt").read_text(encoding="utf-8") == "base\n"


def test_run_ledger_summary_preserves_cline_kanban_fields(tmp_path: Path) -> None:
    module = load_rvf_logging_module()
    run_dir = tmp_path / "run"
    ledger = module.RunLedger(component="stop-hook", repo=tmp_path, cwd=tmp_path, run_id="run-1", run_dir=run_dir)
    ledger.summary(
        status="cline-kanban-started",
        reason_code="cline_kanban_task_started",
        cline_kanban_task_id="task-1",
        cline_kanban_base_ref="HEAD",
        worktree_bootstrap_path="/tmp/bootstrap.json",
    )
    later = ledger.summary(status="completed", reason_code="prepare_completed", message="later phase")
    assert later["cline_kanban_task_id"] == "task-1"
    assert later["cline_kanban_base_ref"] == "HEAD"
    assert later["worktree_bootstrap_path"] == "/tmp/bootstrap.json"


def test_run_ledger_summary_preserves_rvf_state_fields(tmp_path: Path) -> None:
    module = load_rvf_logging_module()
    run_dir = tmp_path / "run"
    ledger = module.RunLedger(component="stop-hook", repo=tmp_path, cwd=tmp_path, run_id="run-1", run_dir=run_dir)
    ledger.summary(
        status="cline-kanban-started",
        reason_code="cline_kanban_task_started",
        **module.rvf_state_fields(
            phase="prepare",
            backend="kanban-task",
            backend_raw="cline-kanban",
            scope_contract_path="/tmp/scope.contract.json",
            review_packet_path="/tmp/review-packet.md",
        ),
    )
    later = ledger.summary(status="completed", reason_code="prepare_completed", message="later phase")
    assert later["rvf_backend"] == "kanban-task"
    assert later["rvf_backend_raw"] == "cline-kanban"
    assert later["rvf_state_phase"] == "prepare"
    assert later["rvf_scope_contract_path"] == "/tmp/scope.contract.json"
    assert later["rvf_review_packet_path"] == "/tmp/review-packet.md"
    assert later["rvf_state"]["phases"] == list(module.RVF_STATE_PHASES)


def test_cancel_rvf_run_marks_cancelled_and_trashes_cline_task(tmp_path: Path) -> None:
    run_dir = tmp_path / "state" / "runs" / "rvf-user-cancel"
    run_dir.mkdir(parents=True)
    repo = init_repo(tmp_path / "repo")
    fake_task = tmp_path / "fake_task.py"
    calls = tmp_path / "trash-calls.jsonl"
    fake_task.write_text(
        "import json, os, sys\n"
        "with open(os.environ['KANBAN_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'task_id': sys.argv[-1], 'status': 'trashed'}))\n",
        encoding="utf-8",
    )
    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "run_id": "rvf-user-cancel",
                "status": "cline-kanban-started",
                "repo": str(repo),
                "cwd": str(repo),
                "run_dir": str(run_dir),
                "cline_kanban_task_id": "task-1",
                "runner_pid": 999999,
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["KANBAN_CALLS"] = str(calls)
    completed = run(
        [
            sys.executable,
            str(CANCEL_RVF_RUN),
            "--summary",
            str(summary_path),
            "--force-after",
            "0",
            "--task-cmd",
            f"{sys.executable} {fake_task}",
        ],
        env=env,
    )
    payload = json.loads(completed.stdout)
    assert payload["status"] == "cancelled"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "cline-kanban-rvf-cancelled"
    assert summary["reason_code"] == "user_cancelled"
    events = read_jsonl(run_dir / "events.jsonl")
    assert any(event["event"] == "run_cancel_requested" for event in events)
    assert any(event["event"] == "run_cancelled" for event in events)
    recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert recorded == [["trash", "--project-path", str(repo.resolve()), "--task-id", "task-1"]]


def test_cancel_rvf_run_ignores_stale_runner_pid_without_matching_command() -> None:
    module = load_cancel_rvf_run_module()
    original_ps = module.ps_processes
    try:
        module.ps_processes = lambda: [
            (4242, "/usr/bin/python unrelated_server.py"),
            (4343, "/usr/local/bin/codex exec --output-last-message /tmp/rvf-live/final.md -"),
        ]
        candidates = module.discover_run_processes(
            "rvf-live",
            {"runner_pid": 4242},
        )
    finally:
        module.ps_processes = original_ps

    assert 4242 not in candidates
    assert candidates == {4343: "/usr/local/bin/codex exec --output-last-message /tmp/rvf-live/final.md -"}


DIFF_TRACKER = SCRIPT_DIR / "diff_tracker.py"


def load_diff_tracker_module():
    if "rvf_diff_tracker" in sys.modules:
        return sys.modules["rvf_diff_tracker"]
    spec = importlib.util.spec_from_file_location("rvf_diff_tracker", DIFF_TRACKER)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load diff_tracker module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["rvf_diff_tracker"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop("rvf_diff_tracker", None)
        raise
    return module


def _write_session_transcript(path: Path, repo: Path, *, session_id: str, target_path: str, line: str) -> Path:
    apply_patch_input = (
        "*** Begin Patch\n"
        f"*** Update File: {target_path}\n"
        "@@\n"
        "-base\n"
        f"+{line}\n"
        "*** End Patch\n"
    )
    records = [
        {
            "timestamp": "2026-04-27T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": str(repo)},
        },
        {
            "timestamp": "2026-04-27T00:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": apply_patch_input,
                "call_id": "call_patch",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    return path


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


def test_canonical_patch_hash_stable_across_reruns(tmp: Path) -> None:
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    first = module.register_claims(
        repo=repo,
        session_id="session-stable",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    second = module.register_claims(
        repo=repo,
        session_id="session-stable",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert first.claim_ids == second.claim_ids
    assert second.dropped_stale_claim_ids == []
    db_path = Path(first.tracker_dir) / "tracker.sqlite3"
    conn = _sqlite.connect(str(db_path))
    try:
        units = conn.execute("SELECT unit_id FROM units").fetchall()
    finally:
        conn.close()
    assert len(units) == 1


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


def test_session_manifest_writes_tracker_claim(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    transcript = write_codex_transcript(tmp / "session.jsonl", repo)
    manifest_path = tmp / "manifest.json"
    log_root = tmp / "logs"
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)}
    run(
        [
            sys.executable,
            str(SESSION_MANIFEST),
            "--repo",
            str(repo),
            "--transcript",
            str(transcript),
            "--output",
            str(manifest_path),
            "--tracker-run-id",
            "run-tracker",
        ],
        env=env,
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    tracker = payload.get("tracker")
    assert isinstance(tracker, dict)
    assert tracker["status"] == "ok"
    assert tracker["repo_key"]
    assert tracker["claim_ids"]
    assert tracker["tracker_dir"]
    assert any(unit.get("unit") in {"hunk", "path"} for unit in tracker.get("owned_units", []))


def test_build_packet_emits_cross_session_conflict_section(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    # Pre-register a claim from a different session so the current run sees a conflict.
    module.register_claims(
        repo=repo,
        session_id="other-session",
        run_id="run-other",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    transcript = write_codex_transcript(tmp / "session.jsonl", repo)
    manifest_path = tmp / "manifest.json"
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)}
    run(
        [
            sys.executable,
            str(SESSION_MANIFEST),
            "--repo",
            str(repo),
            "--transcript",
            str(transcript),
            "--output",
            str(manifest_path),
            "--tracker-run-id",
            "run-current",
        ],
        env=env,
    )
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：cross-session conflict test\n"
        "- 本 turn 主会话实际完成的工作：updated tracked.txt\n",
        encoding="utf-8",
    )
    packet = tmp / "packet.md"
    metadata = tmp / "packet.json"
    run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--session-manifest",
            str(manifest_path),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
        ],
        env=env,
    )
    packet_text = packet.read_text(encoding="utf-8")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert "## Cross-Session Conflicts" in packet_text
    assert "other-session" in packet_text
    assert payload["cross_session_conflicts"]
    assert payload["cross_session_conflicts"][0]["other_session_id"] == "other-session"


def test_build_packet_omits_cross_session_section_when_clean(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    transcript = write_codex_transcript(tmp / "session.jsonl", repo)
    manifest_path = tmp / "manifest.json"
    log_root = tmp / "logs"
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)}
    run(
        [
            sys.executable,
            str(SESSION_MANIFEST),
            "--repo",
            str(repo),
            "--transcript",
            str(transcript),
            "--output",
            str(manifest_path),
            "--tracker-run-id",
            "run-1",
        ],
        env=env,
    )
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：no conflict path\n"
        "- 本 turn 主会话实际完成的工作：updated tracked.txt\n",
        encoding="utf-8",
    )
    packet = tmp / "packet.md"
    metadata = tmp / "packet.json"
    run(
        [
            sys.executable,
            str(BUILD_PACKET),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--session-manifest",
            str(manifest_path),
            "--output",
            str(packet),
            "--metadata-output",
            str(metadata),
        ],
        env=env,
    )
    packet_text = packet.read_text(encoding="utf-8")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert "## Cross-Session Conflicts" not in packet_text
    assert payload["cross_session_conflicts"] == []


def test_canonical_patch_hash_stable_under_line_shift(tmp: Path) -> None:
    """Inserting blank lines above an unrelated hunk shifts only `@@ -A,B +C,D @@`
    line numbers; the hunk's content payload is untouched, so its
    canonical_patch_hash (== unit_id) must stay byte-identical."""
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    baseline = "".join(f"line-{i}\n" for i in range(1, 21))
    target = repo / "shift.txt"
    target.write_text(baseline, encoding="utf-8")
    run(["git", "add", "shift.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)

    # First edit: change line-15 only — produces a single hunk near EOF.
    first_lines = baseline.splitlines(keepends=True)
    first_lines[14] = "LINE-15\n"
    target.write_text("".join(first_lines), encoding="utf-8")
    log_root = tmp / "logs"
    first = module.register_claims(
        repo=repo,
        session_id="session-shift",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["shift.txt"],
        apply_patch_paths={"shift.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert first.status == "ok", first.to_dict()
    assert len(first.claim_ids) == 1
    first_unit_id = first.claim_ids[0]
    db_path = Path(first.tracker_dir) / "tracker.sqlite3"

    # Second edit: re-write file inserting 5 blank lines at top, keeping the
    # same line-15 → LINE-15 edit (which now sits at line 20). The hunk content
    # the diff emits is identical (same context lines, same +/- pair); only
    # the @@ header numbers change.
    shifted = ["\n"] * 5 + first_lines
    target.write_text("".join(shifted), encoding="utf-8")
    # Recommit baseline so HEAD also has the prefix blanks — otherwise the diff
    # would include the blank-line insertions and the hunk content would
    # legitimately differ.
    run(["git", "add", "shift.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "shift baseline"], cwd=repo)
    # Restore the same edit on the new baseline.
    new_baseline = "".join(["\n"] * 5 + baseline.splitlines(keepends=True))
    target.write_text(new_baseline, encoding="utf-8")
    run(["git", "add", "shift.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "restore base after shift"], cwd=repo)
    edited2 = new_baseline.splitlines(keepends=True)
    edited2[19] = "LINE-15\n"  # 14 (original index) + 5 (prefix blanks) = 19
    target.write_text("".join(edited2), encoding="utf-8")

    second = module.register_claims(
        repo=repo,
        session_id="session-shift",
        run_id="run-2",
        worktree=None,
        branch=None,
        owned_paths=["shift.txt"],
        apply_patch_paths={"shift.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert second.status == "ok", second.to_dict()
    assert second.claim_ids == [first_unit_id], (first_unit_id, second.claim_ids)
    assert second.dropped_stale_claim_ids == []
    conn = _sqlite.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT unit_id, observed_state FROM units WHERE path='shift.txt'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, rows
    assert rows[0][0] == first_unit_id
    assert rows[0][1] == "dirty"


def test_canonical_patch_hash_changes_on_content_edit(tmp: Path) -> None:
    """Editing the hunk content (not just shifting line numbers) must produce
    a new unit_id and demote the old unit to observed_state='superseded'."""
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "config", "user.email", "rvf@example.test"], cwd=repo)
    run(["git", "config", "user.name", "RVF Test"], cwd=repo)
    baseline = "alpha\nbeta\ngamma\n"
    target = repo / "edit.txt"
    target.write_text(baseline, encoding="utf-8")
    run(["git", "add", "edit.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    target.write_text("alpha\nBETA-v1\ngamma\n", encoding="utf-8")
    log_root = tmp / "logs"
    first = module.register_claims(
        repo=repo,
        session_id="session-edit",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["edit.txt"],
        apply_patch_paths={"edit.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert first.status == "ok"
    assert len(first.claim_ids) == 1
    old_unit_id = first.claim_ids[0]
    db_path = Path(first.tracker_dir) / "tracker.sqlite3"

    # Now genuinely change the hunk content — different replacement line.
    target.write_text("alpha\nBETA-v2\ngamma\n", encoding="utf-8")
    second = module.register_claims(
        repo=repo,
        session_id="session-edit",
        run_id="run-2",
        worktree=None,
        branch=None,
        owned_paths=["edit.txt"],
        apply_patch_paths={"edit.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert second.status == "ok"
    assert len(second.claim_ids) == 1
    new_unit_id = second.claim_ids[0]
    assert new_unit_id != old_unit_id
    # The old session_units row must be gone (replaced by the new unit_id),
    # and dropped_stale_claim_ids surfaces the old unit_id.
    assert old_unit_id in second.dropped_stale_claim_ids
    conn = _sqlite.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT unit_id, observed_state, review_state, is_tombstoned, tombstone_reason
              FROM units
             WHERE path='edit.txt'
             ORDER BY first_observed_at
            """
        ).fetchall()
        session_units = conn.execute(
            "SELECT unit_id FROM session_units WHERE session_id='session-edit'"
        ).fetchall()
        try:
            conn.execute("UPDATE units SET review_state='tombstoned' WHERE unit_id=?", (new_unit_id,))
        except _sqlite.IntegrityError:
            rejected_review_tombstone = True
        else:
            rejected_review_tombstone = False
    finally:
        conn.close()
    by_unit = {row[0]: row[1:] for row in rows}
    assert by_unit.get(old_unit_id) == (
        "superseded",
        "available",
        1,
        "session_no_longer_owns",
    ), by_unit
    assert by_unit.get(new_unit_id) == ("dirty", "available", 0, None), by_unit
    assert rejected_review_tombstone is True
    assert {row[0] for row in session_units} == {new_unit_id}


def test_migration_phase1_json_to_sqlite_idempotent(tmp: Path) -> None:
    """Hand-write a Phase 1 state.json + events.jsonl + meta.json under the
    legacy `<log_root>/tracker/<key>/` path. First register_claims call must
    create sqlite, archive legacy files into `_legacy/`, and stamp meta.json
    with `migrated_from`. Second call must not re-import or re-archive."""
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    common_dir = module.git_common_dir(repo.resolve())
    assert common_dir is not None
    repo_key = module.repo_key(common_dir)
    legacy_dir = log_root / "tracker" / repo_key
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_state = {
        "schema_version": 1,
        "claims": [
            {
                "claim_id": "clm-legacy-001",
                "session_id": "session-legacy",
                "run_id": "run-legacy",
                "worktree": str(repo.resolve()),
                "branch": "main",
                "path": "tracked.txt",
                "unit": "hunk",
                "hunk_anchor": {
                    "header": "@@ -1 +1,2 @@",
                    "context_hash": "deadbeefdeadbeef",
                    "old_range": [1, 1],
                    "new_range": [1, 2],
                },
                "evidence": "apply_patch",
                "claimed_at": "2026-04-01T00:00:00Z",
                "last_seen_at": "2026-04-01T00:00:00Z",
                "lease": None,
            },
        ],
        "tombstones": [
            {
                "claim_id": "clm-tombstoned",
                "session_id": "session-legacy",
                "path": "removed.txt",
                "unit": "path",
                "dropped_at": "2026-04-01T00:00:00Z",
                "reason": "session_no_longer_owns",
            },
        ],
    }
    (legacy_dir / "state.json").write_text(
        json.dumps(legacy_state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (legacy_dir / "events.jsonl").write_text(
        json.dumps({"timestamp": "2026-04-01T00:00:00Z", "event": "claim_added"}) + "\n",
        encoding="utf-8",
    )
    (legacy_dir / "meta.json").write_text(
        json.dumps({"schema_version": 1, "repo_key": repo_key}, ensure_ascii=False),
        encoding="utf-8",
    )

    first = module.register_claims(
        repo=repo,
        session_id="session-after-migration",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert first.status == "ok", first.to_dict()
    new_dir = log_root / "diff-tracker" / "repos" / repo_key
    db_path = new_dir / "tracker.sqlite3"
    assert db_path.is_file()
    archive = legacy_dir / "_legacy"
    assert (archive / "state.json").is_file()
    assert (archive / "events.jsonl").is_file()
    assert not (legacy_dir / "state.json").exists()
    meta_payload = json.loads((new_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta_payload.get("migrated_from") == "json-v1"
    assert meta_payload.get("schema_version") == module.SCHEMA_VERSION
    archive_state = json.loads((archive / "state.json").read_text(encoding="utf-8"))
    assert archive_state["claims"][0]["claim_id"] == "clm-legacy-001"

    conn = _sqlite.connect(str(db_path))
    try:
        units_before = conn.execute("SELECT unit_id, path FROM units ORDER BY unit_id").fetchall()
        sessions_before = conn.execute("SELECT session_id FROM sessions").fetchall()
        tombstones_before = conn.execute("SELECT ref_id FROM tombstones").fetchall()
    finally:
        conn.close()
    assert {row[0] for row in sessions_before} == {"session-legacy", "session-after-migration"}
    assert any(row[0] == "clm-tombstoned" for row in tombstones_before)
    archive_state_mtime = (archive / "state.json").stat().st_mtime

    events_first = read_jsonl(new_dir / "events.jsonl")
    migration_started_first = sum(1 for e in events_first if e.get("event") == "migration_started")
    assert migration_started_first == 1, events_first

    second = module.register_claims(
        repo=repo,
        session_id="session-after-migration",
        run_id="run-2",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert second.status == "ok"
    assert (archive / "state.json").stat().st_mtime == archive_state_mtime
    conn = _sqlite.connect(str(db_path))
    try:
        units_after = conn.execute("SELECT unit_id, path FROM units ORDER BY unit_id").fetchall()
    finally:
        conn.close()
    assert units_after == units_before
    events_second = read_jsonl(new_dir / "events.jsonl")
    migration_started_second = sum(1 for e in events_second if e.get("event") == "migration_started")
    assert migration_started_second == 1, events_second


def test_migration_phase1_recovers_when_archive_predates_db_marker(tmp: Path) -> None:
    """Simulate the historical crash window: legacy state.json was already moved
    into `_legacy/` but the SQLite transaction that recorded `migrated_from`
    was rolled back (process death between archive and COMMIT). The next
    register_claims call MUST re-import claims from the archived state.json
    rather than treat the missing live state.json as "nothing to migrate"
    and silently lose every Phase-1 claim."""
    import sqlite3 as _sqlite

    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    common_dir = module.git_common_dir(repo.resolve())
    assert common_dir is not None
    repo_key = module.repo_key(common_dir)

    # Stage the post-crash on-disk shape directly: archive dir holds the
    # legacy state.json, but live legacy_dir/state.json is gone and the
    # SQLite db has no `migrated_from` row.
    legacy_dir = log_root / "tracker" / repo_key
    archive = legacy_dir / "_legacy"
    archive.mkdir(parents=True, exist_ok=True)
    legacy_state = {
        "schema_version": 1,
        "claims": [
            {
                "claim_id": "clm-recover-001",
                "session_id": "session-recover",
                "run_id": "run-recover",
                "worktree": str(repo.resolve()),
                "branch": "main",
                "path": "tracked.txt",
                "unit": "path",
                "evidence": "apply_patch",
                "claimed_at": "2026-04-02T00:00:00Z",
                "last_seen_at": "2026-04-02T00:00:00Z",
                "lease": None,
            },
        ],
        "tombstones": [],
    }
    (archive / "state.json").write_text(
        json.dumps(legacy_state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # The archived events.jsonl is the only surviving copy after the crash
    # window: a prior _post_commit moved the live copy into _legacy/ before
    # the SQLite COMMIT rolled back. Recovery must replay these into the new
    # events.jsonl, matching the normal-migration path's side effects.
    (archive / "events.jsonl").write_text(
        json.dumps(
            {"timestamp": "2026-04-02T00:00:00Z", "event": "claim_added", "claim_id": "clm-recover-001"}
        )
        + "\n",
        encoding="utf-8",
    )
    # No live state.json — represents the "archived but not committed" gap.

    result = module.register_claims(
        repo=repo,
        session_id="session-after-crash",
        run_id="run-1",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    assert result.status == "ok", result.to_dict()

    new_dir = log_root / "diff-tracker" / "repos" / repo_key
    db_path = new_dir / "tracker.sqlite3"
    assert db_path.is_file()
    conn = _sqlite.connect(str(db_path))
    try:
        sessions = {row[0] for row in conn.execute("SELECT session_id FROM sessions").fetchall()}
        migrated_from = conn.execute(
            "SELECT value FROM meta WHERE key='migrated_from'"
        ).fetchone()
    finally:
        conn.close()
    # Both the recovered legacy session AND the new one must be present.
    assert sessions == {"session-recover", "session-after-crash"}, sessions
    assert migrated_from is not None and migrated_from[0] == "json-v1"
    # Archive remains intact; live legacy state.json must NOT have been
    # re-created (would re-trigger migration on every call).
    assert (archive / "state.json").is_file()
    assert not (legacy_dir / "state.json").exists()
    # Recovery must replay the archived phase1 events into the new events.jsonl
    # so the recovery branch is observationally aligned with the normal
    # migration path's events.jsonl side effects.
    new_events = read_jsonl(new_dir / "events.jsonl")
    replayed = [e for e in new_events if e.get("imported_from") == "phase1"]
    assert any(
        e.get("event") == "claim_added" and e.get("claim_id") == "clm-recover-001"
        for e in replayed
    ), new_events


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


def _slice_2b_repo_with_two_dirty(tmp: Path) -> Path:
    """init_repo + a second tracked file so two paths are dirty."""
    repo = init_repo(tmp / "repo")
    (repo / "tracked2.txt").write_text("base2\n", encoding="utf-8")
    run(["git", "add", "tracked2.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "add tracked2"], cwd=repo)
    (repo / "tracked2.txt").write_text("base2\nchange2\n", encoding="utf-8")
    return repo


def _slice_2b_tracker_scope_payload(
    *,
    paths: list[str] | None = None,
    unit_ids: list[str] | None = None,
    lease_id: str = "lse-2b-test",
    scope_hash: str = "sha256:" + "b" * 64,
    hunks: object = "default",
    source_session_id: str | None = "sess-2b",
    takeover_from_session_id: str | None = None,
    extras: dict[str, object] | None = None,
) -> dict[str, object]:
    if unit_ids is None:
        unit_ids = ["a" * 64, "c" * 64]
    if paths is None:
        paths = ["tracked.txt"]
    if hunks == "default":
        hunks = [
            {"unit_id": unit_ids[0], "path": paths[0] if paths else "tracked.txt", "hunk_header": "@@ -1 +1,2 @@"}
        ]
    payload: dict[str, object] = {
        "unit_ids": unit_ids,
        "lease_id": lease_id,
        "scope_hash": scope_hash,
        "paths": paths,
        "hunks": hunks,
        "source_session_id": source_session_id,
        "takeover_from_session_id": takeover_from_session_id,
    }
    if extras:
        payload.update(extras)
    return payload


def _slice_2b_write_scope_file(tmp: Path, payload: dict[str, object]) -> Path:
    path = tmp / "tracker-scope.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _slice_2b_prepare(
    *,
    tmp: Path,
    repo: Path,
    tracker_scope_path: Path | None,
    log_root: Path,
    extra_args: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path | None]:
    """Run prepare_review_run.py with a transcript, optionally with --tracker-scope.

    Returns (completed_process, run_dir_or_None). When tracker_scope rejection
    happens (non-zero exit), run_dir is None.
    """
    transcript = write_codex_transcript(tmp / "session.jsonl", repo)
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n- intent: 2-B test\n- work: edit tracked.txt\n",
        encoding="utf-8",
    )
    output_json = tmp / "run.json"
    base_dir = tmp / "runs"
    base_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(PREPARE_REVIEW_RUN),
        "--repo",
        str(repo),
        "--session-context",
        str(context),
        "--transcript",
        str(transcript),
        "--output-json",
        str(output_json),
        "--base-dir",
        str(base_dir),
    ]
    if tracker_scope_path is not None:
        cmd.extend(["--tracker-scope", str(tracker_scope_path)])
    if extra_args:
        cmd.extend(extra_args)
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)}
    completed = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if completed.returncode != 0:
        return completed, None
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    return completed, Path(payload["artifacts_dir"])


def test_tracker_scope_payload_splices_into_manifest(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    artifact_manifest = json.loads((artifacts_dir / "session-manifest.json").read_text(encoding="utf-8"))
    input_manifest = json.loads((artifacts_dir / "inputs" / "session-manifest.json").read_text(encoding="utf-8"))
    for manifest_payload in (artifact_manifest, input_manifest):
        tracker_block = manifest_payload.get("tracker")
        assert isinstance(tracker_block, dict)
        scope = tracker_block.get("tracker_scope")
        assert isinstance(scope, dict)
        assert scope["unit_ids"] == payload["unit_ids"]
        assert scope["lease_id"] == payload["lease_id"]
        assert scope["scope_hash"] == payload["scope_hash"]
        assert scope["paths"] == payload["paths"]
        assert scope["hunks"] == payload["hunks"]
        assert scope["source_session_id"] == payload["source_session_id"]
        assert scope["takeover_from_session_id"] == payload["takeover_from_session_id"]
    # tracker_scope_file (artifact-root entry) must point to artifact_dir copy,
    # not the inputs/ duplicate; otherwise downstream consumers cannot retrieve
    # the artifact-root tracker-scope.json.
    run_payload = json.loads((tmp / "run.json").read_text(encoding="utf-8"))
    assert run_payload["tracker_scope_file"] is not None
    assert run_payload["input_tracker_scope_file"] is not None
    assert run_payload["tracker_scope_file"] != run_payload["input_tracker_scope_file"]
    assert run_payload["tracker_scope_file"] == str((artifacts_dir / "tracker-scope.json").resolve())
    assert run_payload["input_tracker_scope_file"] == str(
        (artifacts_dir / "inputs" / "tracker-scope.json").resolve()
    )


def test_tracker_scope_unlocks_scope_contract_v2_fields(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload(unit_ids=["e" * 64, "d" * 64])
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    contract = json.loads((artifacts_dir / "inputs" / "scope.contract.json").read_text(encoding="utf-8"))
    assert contract["version"] == 2
    assert contract["primary_units"] == sorted({"e" * 64, "d" * 64})
    assert contract["tracker_lease_id"] == payload["lease_id"]
    assert contract["tracker_scope_hash"] == payload["scope_hash"]
    canonical = contract["canonical_scope"]
    assert "primary_units" not in canonical
    assert "tracker_lease_id" not in canonical
    assert "tracker_scope_hash" not in canonical


def test_scope_contract_v2_emitted_without_tracker_scope(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=None, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    contract = json.loads((artifacts_dir / "inputs" / "scope.contract.json").read_text(encoding="utf-8"))
    assert contract["version"] == 2
    assert contract["primary_units"] is None
    assert contract["tracker_lease_id"] is None
    assert contract["tracker_scope_hash"] is None
    canonical = contract["canonical_scope"]
    assert canonical["version"] == 2
    assert set(canonical.keys()) == {
        "version",
        "repo",
        "scope_mode",
        "primary_files",
        "background_files",
        "protected_files",
        "canonical_issues",
        "fix_allowlist",
        "excluded_path_prefixes",
    }


def test_packet_emits_tracker_scope_section_when_present(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    packet_text = (artifacts_dir / "review-packet.md").read_text(encoding="utf-8")
    assert "## Tracker Scope" in packet_text
    assert "## Allocated Git Diff" in packet_text
    assert "## Full Git Diff HEAD (Evidence Only)" in packet_text
    assert "## Session-Owned Git Diff" not in packet_text
    assert payload["lease_id"] in packet_text
    assert payload["scope_hash"] in packet_text


def test_packet_omits_tracker_scope_section_when_absent(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=None, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    packet_text = (artifacts_dir / "review-packet.md").read_text(encoding="utf-8")
    assert "## Tracker Scope" not in packet_text
    assert "## Allocated Git Diff" not in packet_text
    assert "## Session-Owned Git Diff" in packet_text


def test_packet_metadata_carries_tracker_scope_keys(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    metadata = json.loads((artifacts_dir / "review-packet.metadata.json").read_text(encoding="utf-8"))
    assert metadata["tracker_scope_present"] is True
    assert metadata["tracker_scope_unit_count"] == len(payload["unit_ids"])
    assert metadata["tracker_scope_lease_id"] == payload["lease_id"]
    assert metadata["tracker_scope_hash"] == payload["scope_hash"]
    assert metadata["tracker_scope_paths"] == payload["paths"]
    assert metadata["tracker_scope_source_session_id"] == payload["source_session_id"]
    assert metadata["tracker_scope_takeover_from_session_id"] == payload["takeover_from_session_id"]

    completed_b, artifacts_b = _slice_2b_prepare(
        tmp=tmp / "no-scope",
        repo=init_repo(tmp / "no-scope" / "repo"),
        tracker_scope_path=None,
        log_root=tmp / "no-scope" / "logs",
    )
    assert completed_b.returncode == 0, completed_b.stderr
    assert artifacts_b is not None
    metadata_b = json.loads((artifacts_b / "review-packet.metadata.json").read_text(encoding="utf-8"))
    assert metadata_b["tracker_scope_present"] is False
    assert metadata_b["tracker_scope_unit_count"] == 0
    assert metadata_b["tracker_scope_lease_id"] is None
    assert metadata_b["tracker_scope_hash"] is None
    assert metadata_b["tracker_scope_paths"] == []
    assert metadata_b["tracker_scope_source_session_id"] is None
    assert metadata_b["tracker_scope_takeover_from_session_id"] is None


def test_tracker_scope_payload_rejects_invalid_payloads(tmp: Path) -> None:
    cases = [
        ("missing_unit_ids", {"unit_ids": None}, "tracker_scope.unit_ids"),
        ("empty_unit_ids", {"unit_ids": []}, "tracker_scope.unit_ids"),
        ("non_string_unit_id", {"unit_ids": [123, "a" * 64]}, "tracker_scope.unit_ids"),
        ("non_hex_unit_id", {"unit_ids": ["zzzz" * 16]}, "tracker_scope.unit_ids"),
        ("missing_lease", {"lease_id": ""}, "tracker_scope.lease_id"),
        ("missing_scope_hash", {"scope_hash": ""}, "tracker_scope.scope_hash"),
        ("malformed_scope_hash", {"scope_hash": "sha256:short"}, "tracker_scope.scope_hash"),
        ("non_list_paths", {"paths": "tracked.txt"}, "tracker_scope.paths"),
        ("non_string_path", {"paths": [123]}, "tracker_scope.paths"),
        ("non_list_hunks", {"hunks": "x"}, "tracker_scope.hunks"),
    ]
    log_root = tmp / "logs"
    for index, (label, override, expected_substr) in enumerate(cases):
        sub_tmp = tmp / f"case-{index}-{label}"
        sub_tmp.mkdir(parents=True, exist_ok=True)
        repo = init_repo(sub_tmp / "repo")
        payload = _slice_2b_tracker_scope_payload()
        for key, value in override.items():
            if value is None:
                payload.pop(key, None)
            else:
                payload[key] = value
        scope_path = _slice_2b_write_scope_file(sub_tmp, payload)
        completed, artifacts_dir = _slice_2b_prepare(
            tmp=sub_tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
        )
        assert completed.returncode != 0, f"case {label} should have failed, got: {completed.stdout}"
        assert artifacts_dir is None
        assert expected_substr in completed.stderr, (
            f"case {label}: stderr did not mention {expected_substr!r}: {completed.stderr!r}"
        )


def test_tracker_scope_requires_session_manifest_or_transcript(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    context = tmp / "context.md"
    context.write_text("## Session context\n- intent: D4 test\n", encoding="utf-8")
    base_dir = tmp / "runs"
    base_dir.mkdir(parents=True, exist_ok=True)
    log_root = tmp / "logs"
    env = {**os.environ, "CODEX_RVF_LOG_ROOT": str(log_root)}
    completed = subprocess.run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--session-context",
            str(context),
            "--tracker-scope",
            str(scope_path),
            "--base-dir",
            str(base_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode != 0
    assert "--tracker-scope requires --session-manifest or --transcript" in completed.stderr


def test_tracker_scope_tolerates_unknown_keys(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload(extras={"future_field": "preserved"})
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    manifest = json.loads((artifacts_dir / "session-manifest.json").read_text(encoding="utf-8"))
    spliced = manifest["tracker"]["tracker_scope"]
    assert spliced.get("future_field") == "preserved"


def test_review_env_exports_rvf_tracker_scope_path(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    env_text = (artifacts_dir / "review-env.sh").read_text(encoding="utf-8")
    assert "RVF_TRACKER_SCOPE" in env_text
    assert "tracker-scope.json" in env_text
    assert (artifacts_dir / "inputs" / "tracker-scope.json").exists()

    repo_b = init_repo(tmp / "no-scope" / "repo")
    log_root_b = tmp / "no-scope" / "logs"
    completed_b, artifacts_b = _slice_2b_prepare(
        tmp=tmp / "no-scope", repo=repo_b, tracker_scope_path=None, log_root=log_root_b
    )
    assert completed_b.returncode == 0, completed_b.stderr
    assert artifacts_b is not None
    env_text_b = (artifacts_b / "review-env.sh").read_text(encoding="utf-8")
    assert "RVF_TRACKER_SCOPE" not in env_text_b
    assert not (artifacts_b / "inputs" / "tracker-scope.json").exists()


def test_allocated_git_diff_uses_tracker_scope_paths(tmp: Path) -> None:
    repo = _slice_2b_repo_with_two_dirty(tmp)
    log_root = tmp / "logs"
    payload = _slice_2b_tracker_scope_payload(paths=["tracked.txt"])
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    packet_text = (artifacts_dir / "review-packet.md").read_text(encoding="utf-8")
    allocated_idx = packet_text.index("## Allocated Git Diff")
    full_idx = packet_text.index("## Full Git Diff HEAD (Evidence Only)")
    assert allocated_idx < full_idx
    allocated_block = packet_text[allocated_idx:full_idx]
    full_block = packet_text[full_idx:]
    assert "diff --git a/tracked.txt b/tracked.txt" in allocated_block
    assert "diff --git a/tracked2.txt b/tracked2.txt" not in allocated_block
    assert "diff --git a/tracked.txt b/tracked.txt" in full_block
    assert "diff --git a/tracked2.txt b/tracked2.txt" in full_block


def test_existing_cross_session_conflicts_path_unchanged_with_tracker_scope(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = init_repo(tmp / "repo")
    log_root = tmp / "logs"
    module.register_claims(
        repo=repo,
        session_id="other-session",
        run_id="run-other",
        worktree=None,
        branch=None,
        owned_paths=["tracked.txt"],
        apply_patch_paths={"tracked.txt"},
        exec_only_paths=set(),
        log_root_override=log_root,
    )
    payload = _slice_2b_tracker_scope_payload()
    scope_path = _slice_2b_write_scope_file(tmp, payload)
    completed, artifacts_dir = _slice_2b_prepare(
        tmp=tmp, repo=repo, tracker_scope_path=scope_path, log_root=log_root
    )
    assert completed.returncode == 0, completed.stderr
    assert artifacts_dir is not None
    packet_text = (artifacts_dir / "review-packet.md").read_text(encoding="utf-8")
    metadata = json.loads((artifacts_dir / "review-packet.metadata.json").read_text(encoding="utf-8"))
    assert "## Tracker Scope" in packet_text
    assert "## Cross-Session Conflicts" in packet_text
    assert "other-session" in packet_text
    assert metadata["cross_session_conflicts"]
    assert metadata["tracker_scope_present"] is True


def review_support_test_cases(root: Path) -> list[tuple[str, object]]:
    return [
        (
            "rvf_handoff_cli_opens_with_configured_editor",
            lambda: test_rvf_handoff_cli_opens_with_configured_editor(root / "handoff-open"),
        ),
        ("rvf_prep_file_round_trip_and_sweep", lambda: test_rvf_prep_file_round_trip_and_sweep(root / "prep-file")),
        (
            "rvf_user_prompt_submit_dispatches_shared_workflow",
            lambda: test_rvf_user_prompt_submit_dispatches_shared_workflow(root / "prompt-submit-token"),
        ),
        (
            "rvf_user_prompt_submit_marker_without_token",
            lambda: test_rvf_user_prompt_submit_marker_without_token(root / "prompt-submit-marker"),
        ),
        (
            "rvf_user_prompt_submit_manual_path_creates_prep_and_runs_prepare",
            lambda: test_rvf_user_prompt_submit_manual_path_creates_prep_and_runs_prepare(root / "prompt-submit-manual"),
        ),
        (
            "rvf_user_prompt_submit_manual_substring_does_not_falsely_trigger",
            lambda: test_rvf_user_prompt_submit_manual_substring_does_not_falsely_trigger(root / "prompt-submit-substring"),
        ),
        (
            "rvf_user_prompt_submit_failed_prepare_records_state_without_blocking",
            lambda: test_rvf_user_prompt_submit_failed_prepare_records_state_without_blocking(root / "prompt-submit-failed"),
        ),
        (
            "claude_plugin_hooks_declare_user_prompt_submit",
            lambda: test_claude_plugin_hooks_declare_user_prompt_submit(),
        ),
        (
            "rvf_user_prompt_submit_backfills_child_session",
            lambda: test_rvf_user_prompt_submit_backfills_child_session(root / "prompt-submit-child"),
        ),
        (
            "prepare_run_from_prep_file_timeout_returns_immediately",
            lambda: test_prepare_run_from_prep_file_timeout_returns_immediately(root / "prepare-run-timeout"),
        ),
        (
            "rvf_user_prompt_submit_subprocess_stays_silent_in_hook_mode",
            lambda: test_rvf_user_prompt_submit_subprocess_stays_silent_in_hook_mode(root / "prompt-submit-silent"),
        ),
        (
            "rvf_user_prompt_submit_diagnostic_failure_stays_silent_in_hook_mode",
            lambda: test_rvf_user_prompt_submit_diagnostic_failure_stays_silent_in_hook_mode(root / "prompt-submit-fail"),
        ),
        ("check_review_output_lock_request", lambda: test_check_review_output_lock_request()),
        (
            "check_review_output_protocol_extension_requests",
            lambda: test_check_review_output_protocol_extension_requests(),
        ),
        (
            "review_result_artifact_no_issues_and_issues",
            lambda: test_review_result_artifact_no_issues_and_issues(root / "review-result-basic"),
        ),
        (
            "review_result_artifact_requests_and_scope_exclusions",
            lambda: test_review_result_artifact_requests_and_scope_exclusions(root / "review-result-request"),
        ),
        (
            "review_result_artifact_rejects_malformed_and_mixed_state",
            lambda: test_review_result_artifact_rejects_malformed_and_mixed_state(root / "review-result-invalid"),
        ),
        (
            "issue_requires_kind",
            lambda: test_issue_requires_kind(root / "issue-requires-kind"),
        ),
        (
            "issue_requires_severity",
            lambda: test_issue_requires_severity(root / "issue-requires-severity"),
        ),
        (
            "check_rejects_issue_without_kind",
            lambda: test_check_rejects_issue_without_kind(root / "check-rejects-no-kind"),
        ),
        (
            "check_rejects_invalid_severity",
            lambda: test_check_rejects_invalid_severity(root / "check-rejects-bad-severity"),
        ),
        (
            "check_skill_contracts_requires_validate_fix_request_literals",
            lambda: test_check_skill_contracts_requires_validate_fix_request_literals(),
        ),
        (
            "contract_check_entrypoints_default_quiet_with_verbose_flag",
            lambda: test_contract_check_entrypoints_default_quiet_with_verbose_flag(),
        ),
        (
            "contract_check_parallel_test_steps_record_parallel_timing",
            lambda: test_contract_check_parallel_test_steps_record_parallel_timing(),
        ),
        (
            "contract_check_timing_report_accounts_internal_steps",
            lambda: test_contract_check_timing_report_accounts_internal_steps(),
        ),
        (
            "run_ledger_summary_preserves_contract_timing_fields",
            lambda: test_run_ledger_summary_preserves_contract_timing_fields(
                root / "summary-preserve-contract-timing"
            ),
        ),
        (
            "rvf_logging_non_canonical_skill_dirs_default_to_installed_plugin_state",
            lambda: test_rvf_logging_non_canonical_skill_dirs_default_to_installed_plugin_state(
                root / "non-canonical-log-root"
            ),
        ),
        (
            "rvf_logging_falls_back_to_skill_dir_state_when_install_missing",
            lambda: test_rvf_logging_falls_back_to_skill_dir_state_when_install_missing(
                root / "skill-dir-fallback-state"
            ),
        ),
        (
            "check_review_output_accepts_wrapped_issue_continuation",
            lambda: test_check_review_output_accepts_wrapped_issue_continuation(),
        ),
        ("build_packet_metadata_and_scope", lambda: test_build_packet_metadata_and_scope(root / "packet")),
        (
            "build_packet_allows_clean_repo_with_manual_scope",
            lambda: test_build_packet_allows_clean_repo_with_manual_scope(root / "packet-clean-manual-scope"),
        ),
        (
            "session_manifest_extracts_apply_patch_and_command_candidates",
            lambda: test_session_manifest_extracts_apply_patch_and_command_candidates(root / "session-manifest"),
        ),
        (
            "session_manifest_does_not_claim_post_commit_same_path_background_dirty",
            lambda: test_session_manifest_does_not_claim_post_commit_same_path_background_dirty(
                root / "session-manifest-post-commit"
            ),
        ),
        (
            "session_manifest_claims_apply_patch_after_commit_cutoff",
            lambda: test_session_manifest_claims_apply_patch_after_commit_cutoff(
                root / "session-manifest-commit-cutoff"
            ),
        ),
        (
            "session_manifest_only_claims_matching_apply_patch_hunk",
            lambda: test_session_manifest_only_claims_matching_apply_patch_hunk(
                root / "session-manifest-live-hunk"
            ),
        ),
        (
            "session_manifest_records_edit_claim_user_context",
            lambda: test_session_manifest_records_edit_claim_user_context(
                root / "session-manifest-edit-claim-user-context"
            ),
        ),
        (
            "session_manifest_records_codex_message_user_context",
            lambda: test_session_manifest_records_codex_message_user_context(
                root / "session-manifest-codex-message-user-context"
            ),
        ),
        (
            "complete_review_scope_waits_for_all_edit_claim_units",
            lambda: test_complete_review_scope_waits_for_all_edit_claim_units(
                root / "complete-review-scope-edit-claim-units"
            ),
        ),
        (
            "session_manifest_suppresses_unresolved_without_tracker_watermark",
            lambda: test_session_manifest_suppresses_unresolved_without_tracker_watermark(
                root / "session-manifest-unresolved-no-watermark"
            ),
        ),
        (
            "session_manifest_reports_unresolved_apply_patch_hunk_after_tracker_watermark",
            lambda: test_session_manifest_reports_unresolved_apply_patch_hunk_after_tracker_watermark(
                root / "session-manifest-unresolved-with-watermark"
            ),
        ),
        (
            "session_manifest_uses_tracker_transcript_watermark",
            lambda: test_session_manifest_uses_tracker_transcript_watermark(
                root / "session-manifest-tracker-watermark"
            ),
        ),
        (
            "session_manifest_legacy_timestampless_transcript_fallback_warns",
            lambda: test_session_manifest_legacy_timestampless_transcript_fallback_warns(
                root / "session-manifest-legacy-fallback"
            ),
        ),
        (
            "session_manifest_resolves_exec_paths_from_command_workdir",
            lambda: test_session_manifest_resolves_exec_paths_from_command_workdir(
                root / "session-manifest-workdir"
            ),
        ),
        (
            "session_manifest_claims_claude_write_tool_paths",
            lambda: test_session_manifest_claims_claude_write_tool_paths(
                root / "session-manifest-claude-write"
            ),
        ),
        (
            "diagnose_stop_hook_scope_reports_stale_runtime_and_claude_write_gap",
            lambda: test_diagnose_stop_hook_scope_reports_stale_runtime_and_claude_write_gap(
                root / "diagnose-stop-hook-scope"
            ),
        ),
        (
            "build_packet_uses_session_manifest_as_scope_anchor",
            lambda: test_build_packet_uses_session_manifest_as_scope_anchor(root / "packet-manifest"),
        ),
        (
            "build_packet_filters_session_owned_diff_to_tracker_hunk",
            lambda: test_build_packet_filters_session_owned_diff_to_tracker_hunk(
                root / "packet-tracker-hunk"
            ),
        ),
        (
            "build_packet_rejects_session_manifest_for_different_repo",
            lambda: test_build_packet_rejects_session_manifest_for_different_repo(
                root / "packet-manifest-repo"
            ),
        ),
        (
            "build_packet_rejects_empty_session_owned_scope",
            lambda: test_build_packet_rejects_empty_session_owned_scope(root / "packet-manifest-empty"),
        ),
        (
            "build_packet_requires_session_context",
            lambda: test_build_packet_requires_session_context(root / "packet-requires-context"),
        ),
        (
            "build_packet_honors_review_validate_fix_ignore",
            lambda: test_build_packet_honors_review_validate_fix_ignore(root / "packet-ignore"),
        ),
        (
            "build_packet_treats_ignore_prefixes_as_literal_pathspecs",
            lambda: test_build_packet_treats_ignore_prefixes_as_literal_pathspecs(root / "packet-literal-ignore"),
        ),
        ("prepare_review_run_and_command_lock", lambda: test_prepare_review_run_and_command_lock(root / "prepare")),
        (
            "alternative_reviewer_prompt_uses_session_env_refs",
            lambda: test_alternative_reviewer_prompt_uses_session_env_refs(root / "alternative-prompt-env"),
        ),
        (
            "alternative_reviewer_infers_scope_contract_from_inputs_layout",
            lambda: test_alternative_reviewer_infers_scope_contract_from_inputs_layout(
                root / "alternative-inputs-scope"
            ),
        ),
        (
            "alternative_reviewer_subprocess_receives_session_context_alias_and_scope_contract",
            lambda: test_alternative_reviewer_subprocess_receives_session_context_alias_and_scope_contract(
                root / "alternative-session-alias"
            ),
        ),
        (
            "alternative_reviewer_pre_run_health_refreshes_before_reviewer",
            lambda: test_alternative_reviewer_pre_run_health_refreshes_before_reviewer(
                root / "alternative-pre-run-health"
            ),
        ),
        (
            "alternative_reviewer_pre_run_health_failure_skips_reviewer",
            lambda: test_alternative_reviewer_pre_run_health_failure_skips_reviewer(
                root / "alternative-pre-run-health-failure"
            ),
        ),
        (
            "alternative_reviewer_pre_run_health_timeout_skips_reviewer",
            lambda: test_alternative_reviewer_pre_run_health_timeout_skips_reviewer(
                root / "alternative-pre-run-health-timeout"
            ),
        ),
        (
            "prepare_review_run_manual_all_uncommitted_allows_dirty_paths",
            lambda: test_prepare_review_run_manual_all_uncommitted_allows_dirty_paths(root / "prepare-manual-all"),
        ),
        (
            "command_lock_writes_lifecycle_events",
            lambda: test_command_lock_writes_lifecycle_events(root / "command-lock-lifecycle"),
        ),
        (
            "command_lock_respects_env_run_dir",
            lambda: test_command_lock_respects_env_run_dir(root / "command-lock-env-run-dir"),
        ),
        (
            "command_lock_logs_timeout_with_holder_metadata",
            lambda: test_command_lock_logs_timeout_with_holder_metadata(root / "command-lock-timeout"),
        ),
        (
            "prepare_review_run_can_build_session_manifest_from_transcript",
            lambda: test_prepare_review_run_can_build_session_manifest_from_transcript(root / "prepare-transcript"),
        ),
        (
            "prepare_review_run_requires_session_context",
            lambda: test_prepare_review_run_requires_session_context(root / "prepare-requires-context"),
        ),
        (
            "alternative_reviewer_idle_timeout_flag",
            lambda: test_alternative_reviewer_idle_timeout_flag(root / "alternative-timeout"),
        ),
        (
            "alternative_reviewer_activity_probe_keeps_silent_reviewer_alive",
            lambda: test_alternative_reviewer_activity_probe_keeps_silent_reviewer_alive(
                root / "alternative-probe-success"
            ),
        ),
        (
            "alternative_reviewer_requires_review_result_artifact",
            lambda: test_alternative_reviewer_requires_review_result_artifact(
                root / "alternative-missing-result"
            ),
        ),
        (
            "alternative_reviewer_records_request_as_pending_state",
            lambda: test_alternative_reviewer_records_request_as_pending_state(
                root / "alternative-request-pending"
            ),
        ),
        (
            "alternative_reviewer_activity_probe_failure_threshold_times_out",
            lambda: test_alternative_reviewer_activity_probe_failure_threshold_times_out(
                root / "alternative-probe-failure"
            ),
        ),
        (
            "alternative_reviewer_timeout_kills_child_process_group",
            lambda: test_alternative_reviewer_timeout_kills_child_process_group(root / "alternative-timeout-child"),
        ),
        (
            "alternative_reviewer_activity_refreshes_idle_timeout",
            lambda: test_alternative_reviewer_activity_refreshes_idle_timeout(root / "alternative-activity"),
        ),
        (
            "alternative_reviewer_claude_bash_tool_use_suspends_idle_timeout",
            lambda: test_alternative_reviewer_claude_bash_tool_use_suspends_idle_timeout(
                root / "alternative-bash-tool"
            ),
        ),
        (
            "alternative_reviewer_claude_split_jsonl_preserves_tool_use",
            lambda: test_alternative_reviewer_claude_split_jsonl_preserves_tool_use(root / "alternative-split-jsonl"),
        ),
        (
            "alternative_reviewer_repeated_run_keeps_prior_artifacts",
            lambda: test_alternative_reviewer_repeated_run_keeps_prior_artifacts(root / "alternative-repeat-artifacts"),
        ),
        (
            "alternative_reviewer_long_command_wait_uses_check_interval",
            lambda: test_alternative_reviewer_long_command_wait_uses_check_interval(),
        ),
        (
            "alternative_reviewer_claude_stream_monitor_tracks_bash_tool_state",
            lambda: test_alternative_reviewer_claude_stream_monitor_tracks_bash_tool_state(),
        ),
        (
            "alternative_reviewer_claude_stream_json_extracts_result",
            lambda: test_alternative_reviewer_claude_stream_json_extracts_result(root / "alternative-stream-json"),
        ),
        (
            "alternative_reviewer_codex_json_extracts_agent_message",
            lambda: test_alternative_reviewer_codex_json_extracts_agent_message(root / "alternative-codex-json"),
        ),
        (
            "alternative_reviewer_codex_json_extracts_item_completed_agent_message",
            lambda: test_alternative_reviewer_codex_json_extracts_item_completed_agent_message(
                root / "alternative-codex-json-item-completed"
            ),
        ),
        (
            "alternative_reviewer_codex_json_reports_backend_challenge_html",
            lambda: test_alternative_reviewer_codex_json_reports_backend_challenge_html(
                root / "alternative-codex-challenge-html"
            ),
        ),
        (
            "alternative_reviewer_codex_exec_json_command_is_patched",
            lambda: test_alternative_reviewer_codex_exec_json_command_is_patched(root / "alternative-codex-command"),
        ),
        (
            "alternative_reviewer_codex_exec_after_global_options_is_patched",
            lambda: test_alternative_reviewer_codex_exec_after_global_options_is_patched(
                root / "alternative-codex-global-options"
            ),
        ),
        (
            "alternative_reviewer_codex_hooks_disable_is_not_duplicated",
            lambda: test_alternative_reviewer_codex_hooks_disable_is_not_duplicated(),
        ),
        (
            "alternative_reviewer_sets_codex_stop_hook_suppress_env",
            lambda: test_alternative_reviewer_sets_codex_stop_hook_suppress_env(root / "alternative-codex-suppress"),
        ),
        (
            "alternative_reviewer_legacy_claude_config_gets_stream_json",
            lambda: test_alternative_reviewer_legacy_claude_config_gets_stream_json(root / "alternative-legacy-config"),
        ),
        (
            "alternative_reviewer_respects_explicit_claude_text_output",
            lambda: test_alternative_reviewer_respects_explicit_claude_text_output(root / "alternative-text-config"),
        ),
        (
            "alternative_reviewer_respects_explicit_claude_equals_text_output",
            lambda: test_alternative_reviewer_respects_explicit_claude_equals_text_output(
                root / "alternative-equals-text-config"
            ),
        ),
        (
            "alternative_reviewer_non_claude_stream_json_command_is_not_patched",
            lambda: test_alternative_reviewer_non_claude_stream_json_command_is_not_patched(
                root / "alternative-wrapper"
            ),
        ),
        ("cline_kanban_client_detects_runtime_port", lambda: test_cline_kanban_client_detects_runtime_port()),
        (
            "cline_kanban_client_rejects_ambiguous_runtime_ports",
            lambda: test_cline_kanban_client_rejects_ambiguous_runtime_ports(),
        ),
        (
            "cline_kanban_client_reports_missing_stable_binary",
            test_cline_kanban_client_reports_missing_stable_binary,
        ),
        (
            "cline_kanban_client_accepts_cline_tmux_listener_from_foreign_cwd",
            lambda: test_cline_kanban_client_accepts_cline_tmux_listener_from_foreign_cwd(
                root / "cline-kanban-cline-tmux-listener"
            ),
        ),
        (
            "cline_kanban_client_accepts_cline_tmux_listener_through_parent_pane",
            test_cline_kanban_client_accepts_cline_tmux_listener_through_parent_pane,
        ),
        (
            "cline_kanban_client_rejects_listener_without_cline_tmux_session",
            lambda: test_cline_kanban_client_rejects_listener_without_cline_tmux_session(
                root / "cline-kanban-non-cline-tmux-listener"
            ),
        ),
        (
            "cline_kanban_client_accepts_workspace_payload_from_cline_tmux_listener",
            lambda: test_cline_kanban_client_accepts_workspace_payload_from_cline_tmux_listener(
                root / "cline-kanban-workspace-payload"
            ),
        ),
        (
            "cline_kanban_client_rejects_workspace_payload_without_cline_tmux_listener",
            lambda: test_cline_kanban_client_rejects_workspace_payload_without_cline_tmux_listener(
                root / "cline-kanban-workspace-payload-no-tmux"
            ),
        ),
        (
            "cline_kanban_client_does_not_start_when_listener_exists_but_list_fails",
            lambda: test_cline_kanban_client_does_not_start_when_listener_exists_but_list_fails(
                root / "cline-kanban-existing-listener-list-fails"
            ),
        ),
        (
            "cline_kanban_client_create_and_start_task",
            lambda: test_cline_kanban_client_create_and_start_task(root / "cline-kanban-client"),
        ),
        (
            "cline_kanban_client_rejects_main_worktree_mode",
            lambda: test_cline_kanban_client_rejects_main_worktree_mode(
                root / "cline-kanban-main-worktree-mode"
            ),
        ),
        (
            "cline_kanban_client_start_task_uses_session_cwd_workspace",
            lambda: test_cline_kanban_client_start_task_uses_session_cwd_workspace(
                root / "cline-kanban-session-cwd"
            ),
        ),
        (
            "cline_kanban_client_branch_mode_prefers_task_workspace_over_project_path",
            lambda: test_cline_kanban_client_branch_mode_prefers_task_workspace_over_project_path(
                root / "cline-kanban-task-workspace"
            ),
        ),
        (
            "cline_kanban_client_branch_mode_rejects_parent_project_workspace",
            lambda: test_cline_kanban_client_branch_mode_rejects_parent_project_workspace(
                root / "cline-kanban-parent-workspace"
            ),
        ),
        (
            "cline_kanban_client_message_accepts_response_without_task_id",
            lambda: test_cline_kanban_client_message_accepts_response_without_task_id(root / "cline-kanban-message"),
        ),
        (
            "prepare_review_run_writes_worktree_bootstrap",
            lambda: test_prepare_review_run_writes_worktree_bootstrap(root / "worktree-bootstrap"),
        ),
        (
            "prepare_review_run_worktree_bootstrap_untracked_storage_names_do_not_collide",
            lambda: test_prepare_review_run_worktree_bootstrap_untracked_storage_names_do_not_collide(
                root / "worktree-bootstrap-name-collision"
            ),
        ),
        (
            "prepare_review_run_scope_file_matches_metadata_through_symlink_state",
            lambda: test_prepare_review_run_scope_file_matches_metadata_through_symlink_state(
                root / "prepare-symlink-state"
            ),
        ),
        (
            "apply_worktree_bootstrap_replays_tracked_and_untracked",
            lambda: test_apply_worktree_bootstrap_replays_tracked_and_untracked(root / "apply-bootstrap"),
        ),
        (
            "apply_worktree_bootstrap_rejects_mismatched_base_ref",
            lambda: test_apply_worktree_bootstrap_rejects_mismatched_base_ref(root / "apply-bootstrap-base-ref"),
        ),
        (
            "run_ledger_summary_preserves_cline_kanban_fields",
            lambda: test_run_ledger_summary_preserves_cline_kanban_fields(root / "summary-preserve-cline"),
        ),
        (
            "run_ledger_summary_preserves_rvf_state_fields",
            lambda: test_run_ledger_summary_preserves_rvf_state_fields(root / "summary-preserve-rvf-state"),
        ),
        (
            "cancel_rvf_run_marks_cancelled_and_trashes_cline_task",
            lambda: test_cancel_rvf_run_marks_cancelled_and_trashes_cline_task(root / "cancel-rvf-run"),
        ),
        (
            "cancel_rvf_run_ignores_stale_runner_pid_without_matching_command",
            lambda: test_cancel_rvf_run_ignores_stale_runner_pid_without_matching_command(),
        ),
        (
            "diff_tracker_register_creates_sqlite_and_events",
            lambda: test_diff_tracker_register_creates_sqlite_and_events(root / "diff-tracker-register"),
        ),
        (
            "diff_tracker_register_concurrent_writers",
            lambda: test_diff_tracker_register_concurrent_writers(root / "diff-tracker-concurrent"),
        ),
        (
            "canonical_patch_hash_stable_across_reruns",
            lambda: test_canonical_patch_hash_stable_across_reruns(root / "diff-tracker-stable"),
        ),
        (
            "canonical_patch_hash_stable_under_line_shift",
            lambda: test_canonical_patch_hash_stable_under_line_shift(root / "diff-tracker-line-shift"),
        ),
        (
            "canonical_patch_hash_changes_on_content_edit",
            lambda: test_canonical_patch_hash_changes_on_content_edit(root / "diff-tracker-content-edit"),
        ),
        (
            "migration_phase1_json_to_sqlite_idempotent",
            lambda: test_migration_phase1_json_to_sqlite_idempotent(root / "diff-tracker-migration"),
        ),
        (
            "migration_phase1_recovers_when_archive_predates_db_marker",
            lambda: test_migration_phase1_recovers_when_archive_predates_db_marker(
                root / "diff-tracker-migration-recover"
            ),
        ),
        (
            "diff_tracker_hunk_anchor_distinguishes_close_hunks",
            lambda: test_diff_tracker_hunk_anchor_distinguishes_close_hunks(
                root / "diff-tracker-close-hunks"
            ),
        ),
        (
            "diff_tracker_register_empty_owned_paths_preserves_session_claim",
            lambda: test_diff_tracker_register_empty_owned_paths_preserves_session_claim(
                root / "diff-tracker-empty-paths"
            ),
        ),
        (
            "diff_tracker_list_conflicts_reports_other_session_overlap",
            lambda: test_diff_tracker_list_conflicts_reports_other_session_overlap(root / "diff-tracker-conflicts"),
        ),
        (
            "diff_tracker_path_claim_conflicts_with_hunk_claim",
            lambda: test_diff_tracker_path_claim_conflicts_with_hunk_claim(root / "diff-tracker-path-vs-hunk"),
        ),
        (
            "session_manifest_writes_tracker_claim",
            lambda: test_session_manifest_writes_tracker_claim(root / "session-manifest-tracker"),
        ),
        (
            "build_packet_emits_cross_session_conflict_section",
            lambda: test_build_packet_emits_cross_session_conflict_section(root / "packet-cross-session"),
        ),
        (
            "build_packet_omits_cross_session_section_when_clean",
            lambda: test_build_packet_omits_cross_session_section_when_clean(root / "packet-cross-session-clean"),
        ),
        (
            "diff_tracker_disable_env_short_circuits",
            lambda: test_diff_tracker_disable_env_short_circuits(root / "diff-tracker-disabled"),
        ),
        (
            "diff_tracker_lock_timeout_degrades_gracefully",
            lambda: test_diff_tracker_lock_timeout_degrades_gracefully(root / "diff-tracker-lock-timeout"),
        ),
        (
            "tracker_scope_payload_splices_into_manifest",
            lambda: test_tracker_scope_payload_splices_into_manifest(root / "tracker-scope-splice"),
        ),
        (
            "tracker_scope_unlocks_scope_contract_v2_fields",
            lambda: test_tracker_scope_unlocks_scope_contract_v2_fields(root / "tracker-scope-contract-v2"),
        ),
        (
            "scope_contract_v2_emitted_without_tracker_scope",
            lambda: test_scope_contract_v2_emitted_without_tracker_scope(root / "tracker-scope-contract-v2-bare"),
        ),
        (
            "packet_emits_tracker_scope_section_when_present",
            lambda: test_packet_emits_tracker_scope_section_when_present(root / "tracker-scope-packet-present"),
        ),
        (
            "packet_omits_tracker_scope_section_when_absent",
            lambda: test_packet_omits_tracker_scope_section_when_absent(root / "tracker-scope-packet-absent"),
        ),
        (
            "packet_metadata_carries_tracker_scope_keys",
            lambda: test_packet_metadata_carries_tracker_scope_keys(root / "tracker-scope-metadata"),
        ),
        (
            "tracker_scope_payload_rejects_invalid_payloads",
            lambda: test_tracker_scope_payload_rejects_invalid_payloads(root / "tracker-scope-rejection"),
        ),
        (
            "tracker_scope_requires_session_manifest_or_transcript",
            lambda: test_tracker_scope_requires_session_manifest_or_transcript(root / "tracker-scope-d4"),
        ),
        (
            "tracker_scope_tolerates_unknown_keys",
            lambda: test_tracker_scope_tolerates_unknown_keys(root / "tracker-scope-unknown-keys"),
        ),
        (
            "review_env_exports_rvf_tracker_scope_path",
            lambda: test_review_env_exports_rvf_tracker_scope_path(root / "tracker-scope-env"),
        ),
        (
            "allocated_git_diff_uses_tracker_scope_paths",
            lambda: test_allocated_git_diff_uses_tracker_scope_paths(root / "tracker-scope-diff-filter"),
        ),
        (
            "existing_cross_session_conflicts_path_unchanged_with_tracker_scope",
            lambda: test_existing_cross_session_conflicts_path_unchanged_with_tracker_scope(root / "tracker-scope-cross-session"),
        ),
        # Slice 3 allocator T1-T15.
        (
            "allocate_review_scope_emits_valid_tracker_scope_json",
            lambda: test_allocate_review_scope_emits_valid_tracker_scope_json(root / "alloc-T1"),
        ),
        (
            "allocate_review_scope_empty_returns_no_unassigned_review_scope",
            lambda: test_allocate_review_scope_empty_returns_no_unassigned_review_scope(root / "alloc-T2"),
        ),
        (
            "allocate_review_scope_preserves_untracked_file_under_new_directory",
            lambda: test_allocate_review_scope_preserves_untracked_file_under_new_directory(root / "alloc-T2b"),
        ),
        (
            "allocate_review_scope_excludes_active_leased_units",
            lambda: test_allocate_review_scope_excludes_active_leased_units(root / "alloc-T3"),
        ),
        (
            "allocate_review_scope_inserts_lease_and_marks_units_assigned",
            lambda: test_allocate_review_scope_inserts_lease_and_marks_units_assigned(root / "alloc-T4"),
        ),
        (
            "allocate_review_scope_prunes_stale_leases_first",
            lambda: test_allocate_review_scope_prunes_stale_leases_first(root / "alloc-T5"),
        ),
        (
            "allocate_review_scope_concurrent_writers_serialize",
            lambda: test_allocate_review_scope_concurrent_writers_serialize(root / "alloc-T6"),
        ),
        (
            "fork_first_stop_takeover_transfers_unleased_units",
            lambda: test_fork_first_stop_takeover_transfers_unleased_units(root / "alloc-T7"),
        ),
        (
            "fork_takeover_skips_actively_leased_units",
            lambda: test_fork_takeover_skips_actively_leased_units(root / "alloc-T8"),
        ),
        (
            "scope_hash_is_sha256_of_sorted_unit_ids",
            lambda: test_scope_hash_is_sha256_of_sorted_unit_ids(root / "alloc-T9"),
        ),
        (
            "allocator_event_appended_to_events_jsonl",
            lambda: test_allocator_event_appended_to_events_jsonl(root / "alloc-T10"),
        ),
        (
            "manual_rvf_run_inserts_row_and_emits_event",
            lambda: test_manual_rvf_run_inserts_row_and_emits_event(root / "manual-run-T1"),
        ),
        (
            "manual_rvf_run_upserts_on_pk_conflict",
            lambda: test_manual_rvf_run_upserts_on_pk_conflict(root / "manual-run-T2"),
        ),
        (
            "manual_rvf_run_find_returns_none_when_empty",
            lambda: test_manual_rvf_run_find_returns_none_when_empty(root / "manual-run-T3"),
        ),
        (
            "manual_rvf_run_find_returns_latest_completed_at",
            lambda: test_manual_rvf_run_find_returns_latest_completed_at(root / "manual-run-T4"),
        ),
        (
            "manual_rvf_run_find_respects_ttl",
            lambda: test_manual_rvf_run_find_respects_ttl(root / "manual-run-T5"),
        ),
        (
            "manual_rvf_run_ensures_table_for_existing_v2_db",
            lambda: test_manual_rvf_run_ensures_table_for_existing_v2_db(root / "manual-run-T6"),
        ),
        (
            "manual_takeover_transfers_unleased_units",
            lambda: test_manual_takeover_transfers_unleased_units(root / "manual-takeover-T1"),
        ),
        (
            "manual_takeover_skips_actively_leased_units",
            lambda: test_manual_takeover_skips_actively_leased_units(root / "manual-takeover-T2"),
        ),
        (
            "manual_takeover_rejects_missing_parent_session",
            lambda: test_manual_takeover_rejects_missing_parent_session(root / "manual-takeover-T3"),
        ),
        (
            "manual_takeover_cli_records_takeover",
            lambda: test_manual_takeover_cli_records_takeover(root / "manual-takeover-cli"),
        ),
        (
            "record_manual_run_cli_writes_row",
            lambda: test_record_manual_run_cli_writes_row(root / "manual-record-cli"),
        ),
        (
            "allocate_review_scope_disable_env_short_circuits",
            lambda: test_allocate_review_scope_disable_env_short_circuits(root / "alloc-T11"),
        ),
        (
            "allocate_review_scope_busy_timeout_degrades",
            lambda: test_allocate_review_scope_busy_timeout_degrades(root / "alloc-T12"),
        ),
        (
            "allocate_review_scope_writes_paths_and_hunks",
            lambda: test_allocate_review_scope_writes_paths_and_hunks(root / "alloc-T13"),
        ),
        (
            "allocate_review_scope_dry_run_does_not_create_lease",
            lambda: test_allocate_review_scope_dry_run_does_not_create_lease(root / "alloc-T14"),
        ),
        (
            "allocate_review_scope_output_consumed_by_prepare_run",
            lambda: test_allocate_review_scope_output_consumed_by_prepare_run(root / "alloc-T15"),
        ),
        # Slice 4 lease lifecycle.
        (
            "tracker_schema_v2_migrates_lease_participants_table",
            lambda: test_tracker_schema_v2_migrates_lease_participants_table(root / "lease-T0"),
        ),
        (
            "tracker_schema_v4_rebuilds_legacy_tombstoned_review_state",
            lambda: test_tracker_schema_v4_rebuilds_legacy_tombstoned_review_state(root / "lease-T0b"),
        ),
        (
            "lease_acquire_creates_lease_and_assigns_units",
            lambda: test_lease_acquire_creates_lease_and_assigns_units(root / "lease-T1"),
        ),
        (
            "lease_acquire_rejects_when_any_unit_already_leased",
            lambda: test_lease_acquire_rejects_when_any_unit_already_leased(root / "lease-T2"),
        ),
        (
            "lease_acquire_rejects_tombstoned_unit",
            lambda: test_lease_acquire_rejects_tombstoned_unit(root / "lease-T2b"),
        ),
        (
            "lease_acquire_prunes_stale_leases_first",
            lambda: test_lease_acquire_prunes_stale_leases_first(root / "lease-T3"),
        ),
        (
            "stale_prune_does_not_release_unit_reacquired_by_fresh_lease",
            lambda: test_stale_prune_does_not_release_unit_reacquired_by_fresh_lease(root / "lease-T3b"),
        ),
        (
            "lease_refresh_extends_expires_at",
            lambda: test_lease_refresh_extends_expires_at(root / "lease-T4"),
        ),
        (
            "lease_refresh_returns_expired_when_past_ttl",
            lambda: test_lease_refresh_returns_expired_when_past_ttl(root / "lease-T5"),
        ),
        (
            "heartbeat_refreshes_tracker_lease_and_records_backend",
            lambda: test_heartbeat_refreshes_tracker_lease_and_records_backend(root / "lease-T5b"),
        ),
        (
            "heartbeat_treats_same_second_expiry_as_expired",
            lambda: test_heartbeat_treats_same_second_expiry_as_expired(root / "lease-T5c"),
        ),
        (
            "lease_release_completed_marks_units_reviewed",
            lambda: test_lease_release_completed_marks_units_reviewed(root / "lease-T6"),
        ),
        (
            "lease_release_idempotent",
            lambda: test_lease_release_idempotent(root / "lease-T7"),
        ),
        (
            "complete_review_scope_unions_contract_and_lease_units",
            lambda: test_complete_review_scope_unions_contract_and_lease_units(root / "lease-T7a"),
        ),
        (
            "complete_review_scope_keeps_partial_edit_claim_pending",
            lambda: test_complete_review_scope_keeps_partial_edit_claim_pending(root / "lease-T7a-partial-claim"),
        ),
        (
            "complete_review_scope_does_not_complete_failed_released_lease",
            lambda: test_complete_review_scope_does_not_complete_failed_released_lease(root / "lease-T7c"),
        ),
        (
            "complete_review_scope_supersedes_overlapping_active_lease",
            lambda: test_complete_review_scope_supersedes_overlapping_active_lease(root / "lease-T7d"),
        ),
        (
            "complete_review_scope_keeps_different_scope_active_lease",
            lambda: test_complete_review_scope_keeps_different_scope_active_lease(root / "lease-T7e"),
        ),
        (
            "lease_participants_finish_does_not_release_shared_lease",
            lambda: test_lease_participants_finish_does_not_release_shared_lease(root / "lease-T7b"),
        ),
        (
            "sweep_stale_releases_expired_active_leases",
            lambda: test_sweep_stale_releases_expired_active_leases(root / "lease-T8"),
        ),
        (
            "sweep_stale_releases_same_second_expired_lease",
            lambda: test_sweep_stale_releases_same_second_expired_lease(root / "lease-T8b"),
        ),
        (
            "sweep_stale_no_op_when_all_active_leases_fresh",
            lambda: test_sweep_stale_no_op_when_all_active_leases_fresh(root / "lease-T9"),
        ),
        (
            "run_alternative_reviewer_releases_lease_on_normal_exit",
            lambda: test_run_alternative_reviewer_releases_lease_on_normal_exit(root / "lease-T10"),
        ),
        (
            "run_alternative_reviewer_shared_lease_does_not_release_on_exit",
            lambda: test_run_alternative_reviewer_shared_lease_does_not_release_on_exit(root / "lease-T10b"),
        ),
        (
            "run_alternative_reviewer_releases_lease_on_codex_backend_challenge",
            lambda: test_run_alternative_reviewer_releases_lease_on_codex_backend_challenge(root / "lease-T11"),
        ),
        (
            "run_alternative_reviewer_releases_lease_on_timeout",
            lambda: test_run_alternative_reviewer_releases_lease_on_timeout(root / "lease-T12"),
        ),
        (
            "run_alternative_reviewer_sigterm_kills_child_before_release",
            lambda: test_run_alternative_reviewer_sigterm_kills_child_before_release(root / "lease-T12b"),
        ),
        (
            "lease_acquire_concurrent_writers_serialize",
            lambda: test_lease_acquire_concurrent_writers_serialize(root / "lease-T13"),
        ),
    ]


def selected_test_cases(
    cases: list[tuple[str, object]],
    *,
    shard_count: int,
    shard_index: int,
) -> list[tuple[str, object]]:
    if shard_count <= 1:
        return cases
    return [case for index, case in enumerate(cases) if index % shard_count == shard_index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    args = parser.parse_args()
    if args.shard_count < 1:
        raise SystemExit("--shard-count must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise SystemExit("--shard-index must be in [0, shard-count)")

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        cases = selected_test_cases(
            review_support_test_cases(root),
            shard_count=args.shard_count,
            shard_index=args.shard_index,
        )
        for _, test_case in cases:
            test_case()
    suffix = (
        f" shard {args.shard_index + 1}/{args.shard_count}"
        if args.shard_count > 1
        else ""
    )
    print(f"review support script tests OK{suffix}")
    return 0


# --------------------------- Slice 3 allocator tests ---------------------------

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


def test_manual_rvf_run_find_returns_none_when_empty(tmp: Path) -> None:
    module = load_diff_tracker_module()
    repo = _slice_2b_repo_with_two_dirty(tmp)
    assert (
        module.find_manual_rvf_run_for_scope_hash(
            repo=repo,
            scope_hash="sha256:missing",
            log_root_override=tmp / "logs",
        )
        is None
    )


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


if __name__ == "__main__":
    raise SystemExit(main())
