#!/usr/bin/env python3
"""cline-kanban client 与通知 测试簇。

从 tests/test_review_support_scripts.py 有界抽出（导航用拆分，行为不变）。共享 helper/常量
（run/read_jsonl/load_*_module/路径常量等）仍归 aggregator 所有，经 inject() 在注册表运行前推入
本模块 globals，避免与 __main__ 脚本循环导入。注册表 lambda 不动 -> 注册顺序 / 分片身份保持不变。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# 由 aggregator（tests/test_review_support_scripts.py）在导入后 inject 注入共享依赖。
__all__ = [
    'test_cline_kanban_client_detects_runtime_port',
    'test_cline_kanban_client_rejects_ambiguous_runtime_ports',
    'test_cline_kanban_client_reports_missing_stable_binary',
    'test_cline_kanban_client_accepts_cline_tmux_listener_from_foreign_cwd',
    'test_cline_kanban_client_accepts_cline_tmux_listener_through_parent_pane',
    'test_cline_kanban_client_rejects_listener_without_cline_tmux_session',
    'test_cline_kanban_client_accepts_workspace_payload_from_cline_tmux_listener',
    'test_cline_kanban_client_rejects_workspace_payload_without_cline_tmux_listener',
    'test_cline_kanban_client_does_not_start_when_listener_exists_but_list_fails',
    'test_cline_kanban_client_create_and_start_task',
    'test_cline_kanban_client_rejects_main_worktree_mode',
    'test_cline_kanban_client_start_task_uses_session_cwd_workspace',
    'test_cline_kanban_client_branch_mode_prefers_task_workspace_over_project_path',
    'test_cline_kanban_client_branch_mode_rejects_parent_project_workspace',
    'test_cline_kanban_client_message_accepts_response_without_task_id',
]


def inject(**deps: object) -> None:
    """把 aggregator 的共享 helper/常量绑定进本模块 globals，让被搬来的测试在调用时解析到它们。"""
    globals().update(deps)


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
    assert "npm install -g kanban@0.1.68" in message
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

