#!/usr/bin/env python3
"""cline-kanban stop-hook 派发 测试簇。

从 tests/test_codex_stop_review_validate_fix.py 有界抽出（导航用拆分，行为不变）。扁平 tests=[...] 注册表
按裸名引用，故共享 helper/常量经模块级 inject()（def main() 之前）推入本模块 globals 并重绑测试名，
让注册表在 main() 运行时解析到它们。注册表与分片逻辑不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

# 由 aggregator（tests/test_codex_stop_review_validate_fix.py）在 main() 前 inject 注入共享依赖。
__all__ = [
    'test_cline_kanban_worktree_mode_rejects_main_env',
    'test_cline_kanban_mode_creates_and_starts_task_with_same_run',
    'test_cline_kanban_automatic_task_ignores_base_ref_and_worktree_env',
    'test_cline_kanban_mode_requires_workspace_path',
    'test_cline_kanban_workspace_path_reads_nested_task_workspace_path',
    'test_cline_kanban_branch_mode_rejects_parent_project_workspace',
    'test_cline_kanban_mode_without_transcript_fail_closes_before_task_start',
    'test_cline_kanban_mode_blocks_expired_codex_login_before_task_start',
    'test_cline_kanban_mode_marks_unavailable_when_task_start_fails',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


def test_cline_kanban_worktree_mode_rejects_main_env(tmp_path: Path) -> None:
    module = load_hook_module()
    original = os.environ.get("CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE")
    try:
        os.environ["CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE"] = "main"
        try:
            module.cline_kanban_worktree_mode_from_env()
        except ValueError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE=main to be rejected")
    finally:
        if original is None:
            os.environ.pop("CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE", None)
        else:
            os.environ["CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE"] = original

    assert "invalid CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE" in message
    assert "branch" in message
    assert "inplace" in message
    assert "main" not in message.split("expected one of:", 1)[-1]


def test_cline_kanban_mode_creates_and_starts_task_with_same_run(tmp_path: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    prep_root = tmp_path / "prep-root"
    prep_root.mkdir()
    stale_prep = prep_root / "cccccccccccccccc.json"
    stale_prep.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "token": "cccccccccccccccc",
                "created_at": "2026-05-07T00:00:00Z",
                "expires_at": "2026-05-07T00:00:01Z",
                "origin_session_id": "old-session",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:], 'suppress': os.environ.get('CODEX_RVF_SUPPRESS_STOP_HOOK')}) + '\\n')\n"
        "action = sys.argv[1]\n"
        "if action == 'ensure':\n"
        "    print(json.dumps({'ok': True, 'started': False}))\n"
        "elif action == 'create':\n"
        "    print(json.dumps({'task_id': 'task-123', 'workspacePath': '/tmp/task-worktree'}))\n"
        "elif action == 'start':\n"
        "    print(json.dumps({'task_id': 'task-123', 'status': 'started'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {action}')\n",
        encoding="utf-8",
    )
    original_env = {
        key: os.environ.get(key)
        for key in (
            "CODEX_RVF_STATE_DIR",
            "CODEX_RVF_FORK_MODE",
            "CODEX_RVF_CLINE_KANBAN_CLIENT",
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD",
            "CODEX_RVF_CLINE_KANBAN_AGENT_ID",
            "CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE",
            "CODEX_RVF_SUPPRESS_STOP_HOOK",
            "CODEX_RVF_PREP_ROOT",
            "FAKE_CLIENT_CALLS",
        )
    }
    original_lookup = module.parent_thread_name_from_app_server
    try:
        module.parent_thread_name_from_app_server = lambda *_: {
            "name": None,
            "thread_found": False,
            "source": "test",
            "reason": "disabled-in-test",
        }
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        os.environ["CODEX_RVF_FORK_MODE"] = "cline-kanban"
        os.environ["CODEX_RVF_CLINE_KANBAN_CLIENT"] = str(fake_client)
        os.environ["CODEX_RVF_CLINE_KANBAN_TASK_CMD"] = "fake task"
        os.environ["CODEX_RVF_CLINE_KANBAN_AGENT_ID"] = "codex"
        os.environ["CODEX_RVF_SUPPRESS_STOP_HOOK"] = "1"
        os.environ["CODEX_RVF_PREP_ROOT"] = str(prep_root)
        os.environ["FAKE_CLIENT_CALLS"] = str(client_calls)
        transcript = write_apply_patch_transcript(tmp_path / "session.jsonl", repo)
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(repo),
            prompt="$review-validate-fix",
            log_prefix="review-validate-fix-fork",
            model="gpt-test",
            reasoning_effort="high",
            parent_thread_path=transcript,
        )
    finally:
        module.parent_thread_name_from_app_server = original_lookup
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert "reason=cline_kanban_task_started" in payload["systemMessage"]
    assert "pause_origin_edits=true" in payload["systemMessage"]
    assert "workspace=/tmp/task-worktree" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "cline-kanban-started"
    assert "请暂停在 origin worktree 继续编辑" in latest["message"]
    assert latest["rvf_backend"] == "kanban-task"
    assert latest["rvf_backend_raw"] == "cline-kanban"
    assert latest["rvf_state_phase"] == "prepare"
    assert latest["rvf_scope_contract_path"].endswith("artifacts/inputs/scope.contract.json")
    assert latest["rvf_review_packet_path"].endswith("artifacts/review-packet.md")
    assert latest["rvf_state"]["phases"] == [
        "prepare",
        "review",
        "merge",
        "validate_fix",
        "verify",
        "handoff",
        "complete",
    ]
    assert latest["cline_kanban_task_id"] == "task-123"
    assert latest["cline_kanban_worktree_mode"] == "branch"
    assert latest["cline_kanban_prep_file_path"] == latest["rvf_dispatch_prep_file_path"]
    assert latest["workspace_path"] == "/tmp/task-worktree"
    assert "app_server_requests_path" not in latest
    assert Path(latest["startup_prepare_metadata_path"]).exists()
    assert Path(latest["worktree_bootstrap_path"]).exists()
    calls = [
        json.loads(line)
        for line in client_calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [call["argv"][0] for call in calls] == ["ensure", "create", "start"]
    create_argv = calls[1]["argv"]
    assert "--base-ref" in create_argv
    assert "--prompt" in create_argv
    assert create_argv[create_argv.index("--parent-session-id") + 1] == "parent-thread"
    assert create_argv[create_argv.index("--worktree-mode") + 1] == "branch"
    assert create_argv[create_argv.index("--prep-file-path") + 1] == latest["rvf_dispatch_prep_file_path"]
    prompt_text = create_argv[create_argv.index("--prompt") + 1]
    assert "RVF_CLINE_KANBAN_TASK" in prompt_text
    assert "RVF_FORKED_REVIEW_VALIDATE_FIX" in prompt_text
    assert "CODEX_RVF_SUPPRESS_STOP_HOOK=1" not in prompt_text
    assert "RVF_TARGET_REPO: ." in prompt_text
    assert f"RVF_PARENT_REPO: {repo}" in prompt_text
    assert f"RVF_PARENT_CWD: {repo}" in prompt_text
    assert f"RVF_TARGET_REPO: {repo}" not in prompt_text
    assert "RVF_ARTIFACTS_DIR: $RVF_RUN_DIR/artifacts" in prompt_text
    assert 'RVF_TASK_REPO="$(git rev-parse --show-toplevel)"' in prompt_text
    assert "export CODEX_RVF_LOG_ROOT=" in prompt_text
    assert "export CODEX_RVF_RUN_ID=" in prompt_text
    assert 'export CODEX_RVF_RUN_DIR="$RVF_RUN_DIR"' in prompt_text
    assert 'export RVF_ARTIFACTS_DIR="$RVF_RUN_DIR/artifacts"' in prompt_text
    assert '. "$RVF_ARTIFACTS_DIR/review-env.sh"' in prompt_text
    assert 'export RVF_REPO="$RVF_TASK_REPO"' in prompt_text
    assert '--metadata "$RVF_WORKTREE_BOOTSTRAP" --repo "$RVF_REPO"' in prompt_text
    assert "- scope contract: `$RVF_SCOPE_CONTRACT`" in prompt_text
    assert "- review packet: `$RVF_REVIEW_PACKET`" in prompt_text
    assert "- session manifest: `$RVF_SESSION_MANIFEST`" in prompt_text
    assert "review scope 只能以 `$RVF_SCOPE_CONTRACT`" in prompt_text
    assert "和 review packet 为准" not in prompt_text
    assert "review packet 仅作为冻结 reviewer 输入" in prompt_text
    assert "session manifest 只作为 ownership evidence" in prompt_text
    assert "`$RVF_ARTIFACTS_DIR/handoff.md`" in prompt_text
    # 不再指示 agent 手动打开 handoff；改由 Stop hook 在 run 结束时发 OS 系统通知。
    assert "rvf_handoff.py" not in prompt_text
    assert 'open "$RVF_ARTIFACTS_DIR/handoff.md"' not in prompt_text
    assert "OS 系统" in prompt_text
    assert "不要在当前 Cline Kanban worktree 里重新运行 `prepare_review_run.py`" not in prompt_text
    assert "由 UserPromptSubmit hook 调用 shared prepare 入口" in prompt_text
    artifacts_dir = latest["artifacts_dir"]
    assert f"{artifacts_dir}/review-packet.md" not in prompt_text
    assert f"{artifacts_dir}/session-manifest.json" not in prompt_text
    assert f"{artifacts_dir}/worktree-bootstrap.json" not in prompt_text
    startup_scope = (Path(artifacts_dir) / "startup-scope-of-work.md").read_text(encoding="utf-8")
    assert "scope 只能以本 run artifacts 中已经生成的 scope.contract.json" in startup_scope
    assert "review packet、session manifest、workspace snapshot 和 worktree bootstrap 仅作为冻结证据" in startup_scope
    assert "作为启动时 scope anchor" not in startup_scope
    task_title = create_argv[create_argv.index("--title") + 1]
    assert task_title.startswith("RVF from Codex parent-thread run ")
    assert " repo " not in task_title
    assert latest["parent_conversation_ref"] == "Codex parent-thread"
    assert latest["parent_codex_url"] == "codex://local/parent-thread"
    assert Path(latest["parent_origin_path"]).exists()
    prep = dispatch_prep_payload(latest)
    prep_token = prep["token"]
    assert isinstance(prep_token, str) and re.fullmatch(r"[0-9a-f]{16}", prep_token)
    assert f"RVF_DISPATCH=token={prep_token}" in prompt_text
    assert f"RVF_PREP_FILE: {latest['rvf_dispatch_prep_file_path']}" in prompt_text
    assert prep["origin_session_id"] == "parent-thread"
    assert Path(str(prep["origin_repo"])).resolve() == repo.resolve()
    assert prep["target_flow"] == "flow-2-branch"
    assert prep["target_worktree"] == "/tmp/task-worktree"
    assert prep["target_kanban_task_id"] == "task-123"
    assert latest["rvf_dispatch_target_worktree"] == "/tmp/task-worktree"
    assert latest["rvf_dispatch_target_kanban_task_id"] == "task-123"
    assert not stale_prep.exists()
    sweep_events = [
        event
        for event in latest_events(state)
        if event.get("event") == "dispatch_prep_file_sweep_completed"
    ]
    assert sweep_events
    assert sweep_events[-1]["removed_count"] == 1
    assert sweep_events[-1]["removed_paths"] == [str(stale_prep)]
    assert prep["rvf_run"]["run_id"] == latest["run_id"]
    assert "RVF_PARENT_CONVERSATION_REF: Codex parent-thread" in prompt_text
    assert "RVF_PARENT_CONVERSATION_NAME: Codex parent-thread" in prompt_text
    assert "RVF_PARENT_CONVERSATION_NAME_SOURCE: session_ref_fallback" in prompt_text
    assert "RVF_PARENT_CODEX_URL: codex://local/parent-thread" in prompt_text
    assert "## Origin" in prompt_text
    assert "origin metadata: `$RVF_ARTIFACTS_DIR/origin.json`" in prompt_text
    assert create_argv[create_argv.index("--agent-id") + 1] == "codex"
    assert all(call["suppress"] is None for call in calls)
    suppression_path = Path(latest["cline_kanban_stop_hook_suppression_path"])
    assert suppression_path.exists()
    suppression_marker = json.loads(suppression_path.read_text(encoding="utf-8"))
    assert suppression_marker["task_id"] == "task-123"
    assert suppression_marker["suppress_stop_hook"] is True
    assert suppression_marker["run_id"] == latest["run_id"]
    # Regression for the freeze/update race: after Cline Kanban dispatch the
    # prep file on disk must still carry the shared_workflow_state that
    # freeze_cline_kanban_dispatch_artifacts wrote. Previously the caller
    # held a stale dispatch_prep record and update_dispatch_prep_file would
    # merge over it, wiping shared_workflow_state. update_dispatch_prep_file
    # now reloads the prep payload from disk before merging.
    final_prep_path = Path(latest["rvf_dispatch_prep_file_path"])
    final_prep_payload = json.loads(final_prep_path.read_text(encoding="utf-8"))
    final_state = final_prep_payload.get("rvf_run", {}).get("shared_workflow_state")
    assert isinstance(final_state, dict), final_prep_payload
    assert final_state.get("status") == "completed"
    assert final_state.get("rvf_backend") == "kanban-task"
    assert final_state.get("target_flow") == "flow-2-branch"
    # The same payload must reflect the post-task update_dispatch_prep_file
    # write (target_worktree / target_kanban_task_id), proving the merge ran
    # against the freshly reloaded payload rather than a pre-freeze copy.
    assert final_prep_payload.get("target_worktree") == "/tmp/task-worktree"
    assert final_prep_payload.get("target_kanban_task_id") == "task-123"


def test_cline_kanban_automatic_task_ignores_base_ref_and_worktree_env(tmp_path: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    workspace = tmp_path / "task-worktree"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "action = sys.argv[1]\n"
        "if action == 'ensure':\n"
        "    print(json.dumps({'ok': True, 'started': False}))\n"
        "elif action == 'create':\n"
        f"    print(json.dumps({{'task_id': 'task-auto', 'workspacePath': {str(workspace)!r}}}))\n"
        "elif action == 'start':\n"
        f"    print(json.dumps({{'task_id': 'task-auto', 'status': 'started', 'workspacePath': {str(workspace)!r}}}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {action}')\n",
        encoding="utf-8",
    )
    original_env = {
        key: os.environ.get(key)
        for key in (
            "CODEX_RVF_STATE_DIR",
            "CODEX_RVF_FORK_MODE",
            "CODEX_RVF_CLINE_KANBAN_CLIENT",
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD",
            "CODEX_RVF_CLINE_KANBAN_BASE_REF",
            "CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE",
            "FAKE_CLIENT_CALLS",
        )
    }
    original_lookup = module.parent_thread_name_from_app_server
    try:
        module.parent_thread_name_from_app_server = lambda *_: {
            "name": None,
            "thread_found": False,
            "source": "test",
            "reason": "disabled-in-test",
        }
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        os.environ["CODEX_RVF_FORK_MODE"] = "cline-kanban"
        os.environ["CODEX_RVF_CLINE_KANBAN_CLIENT"] = str(fake_client)
        os.environ["CODEX_RVF_CLINE_KANBAN_TASK_CMD"] = "fake task"
        os.environ["CODEX_RVF_CLINE_KANBAN_BASE_REF"] = "stale-user-selected-branch"
        os.environ["CODEX_RVF_CLINE_KANBAN_WORKTREE_MODE"] = "inplace"
        os.environ["FAKE_CLIENT_CALLS"] = str(client_calls)
        transcript = write_apply_patch_transcript(tmp_path / "session.jsonl", repo)
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(repo),
            prompt="$review-validate-fix",
            log_prefix="review-validate-fix-fork",
            parent_thread_path=transcript,
        )
    finally:
        module.parent_thread_name_from_app_server = original_lookup
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert "reason=cline_kanban_task_started" in payload["systemMessage"]
    assert "pause_origin_edits=true" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "cline-kanban-started"
    assert latest["cline_kanban_worktree_mode"] == "branch"
    assert latest["workspace_path"] == str(workspace)
    calls = [
        json.loads(line)
        for line in client_calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    create_argv = calls[1]["argv"]
    expected_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert create_argv[create_argv.index("--base-ref") + 1] == expected_head
    assert create_argv[create_argv.index("--parent-session-id") + 1] == "parent-thread"
    assert create_argv[create_argv.index("--worktree-mode") + 1] == "branch"
    assert create_argv[create_argv.index("--prep-file-path") + 1] == latest["rvf_dispatch_prep_file_path"]
    prompt_text = create_argv[create_argv.index("--prompt") + 1]
    assert "独立 git worktree" in prompt_text
    assert 'RVF_TASK_REPO="$(git rev-parse --show-toplevel)"' in prompt_text
    assert "apply_worktree_bootstrap.py" in prompt_text
    assert '--metadata "$RVF_WORKTREE_BOOTSTRAP" --repo "$RVF_REPO"' in prompt_text
    prep = dispatch_prep_payload(latest)
    assert prep["target_flow"] == "flow-2-branch"
    assert prep["target_worktree"] == str(workspace)
    assert prep["target_kanban_task_id"] == "task-auto"
    assert prep["workflow_constraints"] == {
        "pause_origin_edits": True,
        "in_place_mode": False,
    }


def test_cline_kanban_mode_requires_workspace_path(tmp_path: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    fake_client.write_text(
        "import json, sys\n"
        "action = sys.argv[1]\n"
        "if action == 'ensure':\n"
        "    print(json.dumps({'ok': True, 'started': False}))\n"
        "elif action == 'create':\n"
        "    print(json.dumps({'task_id': 'task-no-workspace'}))\n"
        "elif action == 'start':\n"
        "    print(json.dumps({'task_id': 'task-no-workspace', 'status': 'started'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {action}')\n",
        encoding="utf-8",
    )
    original_env = {
        key: os.environ.get(key)
        for key in (
            "CODEX_RVF_STATE_DIR",
            "CODEX_RVF_FORK_MODE",
            "CODEX_RVF_CLINE_KANBAN_CLIENT",
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD",
        )
    }
    original_lookup = module.parent_thread_name_from_app_server
    try:
        module.parent_thread_name_from_app_server = lambda *_: {
            "name": None,
            "thread_found": False,
            "source": "test",
            "reason": "disabled-in-test",
        }
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        os.environ["CODEX_RVF_FORK_MODE"] = "cline-kanban"
        os.environ["CODEX_RVF_CLINE_KANBAN_CLIENT"] = str(fake_client)
        os.environ["CODEX_RVF_CLINE_KANBAN_TASK_CMD"] = "fake task"
        transcript = write_apply_patch_transcript(tmp_path / "session.jsonl", repo)
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(repo),
            prompt="$review-validate-fix",
            log_prefix="review-validate-fix-fork",
            parent_thread_path=transcript,
        )
    finally:
        module.parent_thread_name_from_app_server = original_lookup
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert "reason=cline_kanban_unavailable" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "cline-kanban-unavailable"
    assert "task execution workspace_path/workspacePath" in latest["error"]
    assert "workspace_path" not in latest
    assert latest["rvf_dispatch_target_worktree"] is None
    prep = dispatch_prep_payload(latest)
    assert prep["target_worktree"] is None


def test_cline_kanban_workspace_path_reads_nested_task_workspace_path(tmp_path: Path) -> None:
    module = load_hook_module()

    assert module.cline_kanban_workspace_path(
        {"task": {"workspacePath": "/tmp/task-worktree"}},
    ) == "/tmp/task-worktree"


def test_cline_kanban_branch_mode_rejects_parent_project_workspace(tmp_path: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    fake_client.write_text(
        "import json, sys\n"
        "action = sys.argv[1]\n"
        "if action == 'ensure':\n"
        "    print(json.dumps({'ok': True, 'started': False}))\n"
        "elif action == 'create':\n"
        f"    print(json.dumps({{'task_id': 'task-parent-workspace', 'task': {{'id': 'task-parent-workspace', 'workspacePath': {str(repo)!r}}}}}))\n"
        "elif action == 'start':\n"
        "    print(json.dumps({'task_id': 'task-parent-workspace', 'status': 'started'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {action}')\n",
        encoding="utf-8",
    )
    original_env = {
        key: os.environ.get(key)
        for key in (
            "CODEX_RVF_STATE_DIR",
            "CODEX_RVF_FORK_MODE",
            "CODEX_RVF_CLINE_KANBAN_CLIENT",
            "CODEX_RVF_CLINE_KANBAN_TASK_CMD",
        )
    }
    original_lookup = module.parent_thread_name_from_app_server
    try:
        module.parent_thread_name_from_app_server = lambda *_: {
            "name": None,
            "thread_found": False,
            "source": "test",
            "reason": "disabled-in-test",
        }
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        os.environ["CODEX_RVF_FORK_MODE"] = "cline-kanban"
        os.environ["CODEX_RVF_CLINE_KANBAN_CLIENT"] = str(fake_client)
        os.environ["CODEX_RVF_CLINE_KANBAN_TASK_CMD"] = "fake task"
        transcript = write_apply_patch_transcript(tmp_path / "session.jsonl", repo)
        payload = module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(repo),
            prompt="$review-validate-fix",
            log_prefix="review-validate-fix-fork",
            parent_thread_path=transcript,
        )
    finally:
        module.parent_thread_name_from_app_server = original_lookup
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert "reason=cline_kanban_unavailable" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "cline-kanban-unavailable"
    assert "parent project path" in latest["error"]
    assert "workspace_path" not in latest


def test_cline_kanban_mode_without_transcript_fail_closes_before_task_start(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "action = sys.argv[1]\n"
        "if action == 'ensure':\n"
        "    print(json.dumps({'ok': True}))\n"
        "elif action == 'create':\n"
        "    print(json.dumps({'task_id': 'task-123'}))\n"
        "elif action == 'start':\n"
        "    print(json.dumps({'task_id': 'task-123', 'status': 'started'}))\n"
        "else:\n"
        "    raise SystemExit(f'unexpected action {action}')\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
            "session_id": "parent-thread",
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "cline-kanban",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=cline_kanban_missing_scope_anchor" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "cline_kanban_missing_scope_anchor"
    assert latest["backend"] == "kanban"
    assert "startup_prepare_metadata_path" not in latest
    assert not client_calls.exists()


def test_cline_kanban_mode_blocks_expired_codex_login_before_task_start(tmp_path: Path) -> None:
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    transcript = write_apply_patch_transcript(tmp_path / "session.jsonl", repo)
    fake_codex = tmp_path / "fake_codex.py"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1:3] == ['login', 'status']:\n"
        "    print('session expired; please login again', file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "raise SystemExit(f'unexpected codex argv: {sys.argv[1:]}')\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    client_calls = tmp_path / "client-calls.jsonl"
    fake_client.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CLIENT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "print(json.dumps({'task_id': 'should-not-start'}))\n",
        encoding="utf-8",
    )

    stdout, _ = invoke(
        {
            "cwd": str(repo),
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        },
        extra_env={
            "CODEX_RVF_FORK_MODE": "cline-kanban",
            "CODEX_RVF_CLINE_KANBAN_CLIENT": str(fake_client),
            "CODEX_RVF_CODEX_BIN": str(fake_codex),
            "FAKE_CLIENT_CALLS": str(client_calls),
        },
        state_dir=state,
    )

    payload = parse_json(stdout)
    assert "reason=provider_health_failed" in payload["systemMessage"]
    latest = latest_summary(state)
    assert latest["status"] == "skipped"
    assert latest["reason_code"] == "provider_health_failed"
    assert latest["backend"] == "kanban"
    assert latest["gate_status"] == "DIRTY"
    assert "codex login" in str(latest["message"])
    assert "startup_prepare_metadata_path" not in latest
    assert not client_calls.exists()
    health = read_json_artifact(latest, "provider_health_path")
    assert isinstance(health, dict)
    results = health["results"]
    assert isinstance(results, list)
    assert results[0]["provider"] == "codex"
    assert results[0]["status"] == "failed"
    assert "session expired" in results[0]["stderr"]


def test_cline_kanban_mode_marks_unavailable_when_task_start_fails(tmp_path: Path) -> None:
    module = load_hook_module()
    repo = init_repo_with_head(tmp_path / "repo")
    state = tmp_path / "state"
    fake_client = tmp_path / "fake_cline_kanban_client.py"
    fake_client.write_text(
        "import json, sys\n"
        "if sys.argv[1] == 'ensure':\n"
        "    print(json.dumps({'ok': True}))\n"
        "elif sys.argv[1] == 'create':\n"
        "    print(json.dumps({'task_id': 'task-123'}))\n"
        "else:\n"
        "    print('start boom', file=sys.stderr)\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )
    original_env = {
        key: os.environ.get(key)
        for key in ("CODEX_RVF_STATE_DIR", "CODEX_RVF_FORK_MODE", "CODEX_RVF_CLINE_KANBAN_CLIENT")
    }
    original_lookup = module.parent_thread_name_from_app_server
    try:
        module.parent_thread_name_from_app_server = lambda *_: {
            "name": None,
            "thread_found": False,
            "source": "test",
            "reason": "disabled-in-test",
        }
        os.environ["CODEX_RVF_STATE_DIR"] = str(state)
        os.environ["CODEX_RVF_FORK_MODE"] = "cline-kanban"
        os.environ["CODEX_RVF_CLINE_KANBAN_CLIENT"] = str(fake_client)
        module.run_codex_fork(
            parent_session_id="parent-thread",
            cwd=str(repo),
            prompt="$review-validate-fix",
            log_prefix="review-validate-fix-fork",
            model=None,
            reasoning_effort=None,
            parent_thread_path=write_apply_patch_transcript(tmp_path / "session.jsonl", repo),
        )
    finally:
        module.parent_thread_name_from_app_server = original_lookup
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    latest = latest_summary(state)
    assert latest["status"] == "cline-kanban-unavailable"
    assert "start boom" in str(latest["message"])

