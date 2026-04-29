#!/usr/bin/env python3
from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
BUILD_PACKET = SCRIPT_DIR / "build_review_packet.py"
CHECK_REVIEW_OUTPUT = SCRIPT_DIR / "check_review_output.py"
COMMAND_LOCK = SCRIPT_DIR / "command_lock.py"
PREPARE_REVIEW_RUN = SCRIPT_DIR / "prepare_review_run.py"
RUN_ALTERNATIVE_REVIEWER = SCRIPT_DIR / "run_alternative_reviewer.py"
RUN_VIBE_KANBAN_RVF = SCRIPT_DIR / "run_vibe_kanban_rvf.py"
CANCEL_RVF_RUN = SCRIPT_DIR / "cancel_rvf_run.py"
VIBE_KANBAN_MCP_CLIENT = SCRIPT_DIR / "vibe_kanban_mcp_client.py"
SESSION_MANIFEST = SCRIPT_DIR / "session_manifest.py"
RVF_LOGGING = SCRIPT_DIR / "rvf_logging.py"

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


def load_vibe_kanban_client_module():
    spec = importlib.util.spec_from_file_location("rvf_vibe_kanban_mcp_client", VIBE_KANBAN_MCP_CLIENT)
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load vibe_kanban_mcp_client module")
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
    max_runtime_seconds: float | None = None,
    output_format: str | None = "text",
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
    if max_runtime_seconds is not None:
        payload["max_runtime_seconds"] = max_runtime_seconds
    if output_format is not None:
        payload["output_format"] = output_format
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def write_codex_transcript(path: Path, repo: Path) -> Path:
    apply_patch_input = (
        "*** Begin Patch\n"
        "*** Update File: tracked.txt\n"
        "@@\n"
        "-base\n"
        "+base edited by session\n"
        "*** Add File: owned-new.txt\n"
        "+owned\n"
        "*** Delete File: removed.txt\n"
        "*** End Patch\n"
    )
    records = [
        {
            "timestamp": "2026-04-27T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "session-tracking-test", "cwd": str(repo)},
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
        {
            "timestamp": "2026-04-27T00:00:02.000Z",
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


def write_fake_mcp_server(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "import json, os, sys\n"
        "calls_path = os.environ.get('FAKE_MCP_CALLS')\n"
        "def read_msg():\n"
        "    line = sys.stdin.readline()\n"
        "    if not line:\n"
        "        return None\n"
        "    return json.loads(line)\n"
        "def write_msg(payload):\n"
        "    sys.stdout.write(json.dumps(payload) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "def record(payload):\n"
        "    if calls_path:\n"
        "        with open(calls_path, 'a', encoding='utf-8') as handle:\n"
        "            handle.write(json.dumps(payload) + '\\n')\n"
        "while True:\n"
        "    msg = read_msg()\n"
        "    if msg is None:\n"
        "        break\n"
        "    if 'id' not in msg:\n"
        "        continue\n"
        "    method = msg.get('method')\n"
        "    if method == 'initialize':\n"
        "        write_msg({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'protocolVersion': '2024-11-05', 'capabilities': {}, 'serverInfo': {'name': 'fake'}}})\n"
        "    elif method == 'tools/list':\n"
        "        tools = [{'name': 'create_issue'}, {'name': 'update_issue'}, {'name': 'list_repos'}, {'name': 'list_projects'}, {'name': 'list_organizations'}]\n"
        "        if os.environ.get('FAKE_NO_CREATE_PROJECT') != '1':\n"
        "            tools.append({'name': 'create_project'})\n"
        "        write_msg({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'tools': tools}})\n"
        "    elif method == 'tools/call':\n"
        "        record(msg['params'])\n"
        "        tool = msg['params']['name']\n"
        "        args = msg['params']['arguments']\n"
        "        if os.environ.get('FAKE_TOOL_ERROR') == tool:\n"
        "            result = {'success': False, 'error': 'fake tool failure', 'details': 'diagnostic detail'}\n"
        "            write_msg({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'content': [{'type': 'text', 'text': json.dumps(result)}]}})\n"
        "            continue\n"
        "        if tool == 'list_repos':\n"
        "            result = {'repos': [{'name': 'repo', 'path': os.environ.get('FAKE_REPO_PATH', '/tmp/repo'), 'project_id': 'project-from-repo'}]}\n"
        "        elif tool == 'list_projects':\n"
        "            result = {'projects': [{'id': 'project-from-project', 'name': os.environ.get('FAKE_PROJECT_NAME', 'repo')}]}\n"
        "        elif tool == 'list_organizations':\n"
        "            result = {'organizations': [{'id': 'organization-1', 'name': 'Default'}]}\n"
        "        elif tool == 'create_project':\n"
        "            result = {'project_id': 'project-created', 'tool': tool, 'arguments': args}\n"
        "        else:\n"
        "            result = {'issue_id': 'issue-123', 'tool': tool, 'arguments': args, 'backend_url': os.environ.get('VIBE_BACKEND_URL')}\n"
        "        write_msg({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'content': [{'type': 'text', 'text': json.dumps(result)}]}})\n"
        "    else:\n"
        "        write_msg({'jsonrpc': '2.0', 'id': msg['id'], 'result': {}})\n",
        encoding="utf-8",
    )
    return path


def start_fake_vibe_api() -> tuple[str, dict[str, object], ThreadingHTTPServer]:
    state: dict[str, object] = {
        "requests": [],
        "workspaces": {},
        "next_id": 1,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_json(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0"))
            if length == 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw)

        def _write_json(self, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            body = self._read_json()
            requests = state["requests"]
            assert isinstance(requests, list)
            requests.append({"method": "POST", "path": self.path, "body": body})
            if self.path == "/api/workspaces":
                workspace_id = f"workspace-{state['next_id']}"
                state["next_id"] = int(state["next_id"]) + 1
                workspace = {"id": workspace_id, "name": body.get("name"), "branch": "vk/rvf"}
                workspaces = state["workspaces"]
                assert isinstance(workspaces, dict)
                workspaces[workspace_id] = workspace
                self._write_json({"success": True, "data": workspace})
                return
            self.send_error(404)

        def do_PUT(self) -> None:
            body = self._read_json()
            requests = state["requests"]
            assert isinstance(requests, list)
            requests.append({"method": "PUT", "path": self.path, "body": body})
            if self.path.startswith("/api/workspaces/"):
                workspace_id = self.path.rsplit("/", 1)[-1]
                workspaces = state["workspaces"]
                assert isinstance(workspaces, dict)
                workspace = dict(workspaces.get(workspace_id, {"id": workspace_id, "branch": "vk/rvf"}))
                workspace.update(body)
                workspaces[workspace_id] = workspace
                self._write_json({"success": True, "data": workspace})
                return
            if self.path.startswith("/api/scratch/WORKSPACE_NOTES/"):
                self._write_json({"success": True, "data": {"id": self.path.rsplit("/", 1)[-1], **body}})
                return
            self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_port}", state, server


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


def test_build_packet_metadata_and_scope(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
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


def test_build_packet_allows_clean_repo_with_manual_scope(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    run(["git", "add", "tracked.txt", "new.txt"], cwd=repo)
    run(["git", "commit", "-q", "-m", "settle worktree"], cwd=repo)
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：manual scoped review\n"
        "- 本 turn 主会话实际完成的工作：仓库当前 clean；本轮审查范围来自用户显式指定\n"
        "- Scope：审查 tracked.txt 的现有实现面\n",
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


def test_session_manifest_extracts_apply_patch_and_command_candidates(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    (repo / "owned-new.txt").write_text("owned\n", encoding="utf-8")
    (repo / "generated.txt").write_text("generated\n", encoding="utf-8")
    (repo / "background.txt").write_text("background contents\n", encoding="utf-8")
    transcript = write_codex_transcript(tmp / "session.jsonl", repo)
    manifest_path = tmp / "manifest.json"

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


def test_session_manifest_resolves_exec_paths_from_command_workdir(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    docs = repo / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("x\n", encoding="utf-8")
    transcript = tmp / "session.jsonl"
    records = [
        {
            "timestamp": "2026-04-27T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "session-subdir-test", "cwd": str(repo)},
        },
        {
            "timestamp": "2026-04-27T00:00:01.000Z",
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
    manifest_path = tmp / "manifest.json"

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


def test_build_packet_uses_session_manifest_as_scope_anchor(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：updated tracked.txt\n",
        encoding="utf-8",
    )
    (repo / "owned-new.txt").write_text("owned contents\n", encoding="utf-8")
    (repo / "background.txt").write_text("background contents\n", encoding="utf-8")
    transcript = write_codex_transcript(tmp / "session.jsonl", repo)
    manifest = tmp / "manifest.json"
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


def test_build_packet_rejects_session_manifest_for_different_repo(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：reject mismatched manifest\n",
        encoding="utf-8",
    )
    manifest = tmp / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": str(tmp / "other-repo"),
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


def test_build_packet_rejects_empty_session_owned_scope(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：reject empty manifest scope\n",
        encoding="utf-8",
    )
    manifest = tmp / "manifest.json"
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


def test_build_packet_requires_session_context(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
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


def test_build_packet_honors_review_validate_fix_ignore(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
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


def test_build_packet_treats_ignore_prefixes_as_literal_pathspecs(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
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


def test_prepare_review_run_and_command_lock(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
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
            str(tmp / "runs"),
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
    assert Path(payload["review_env_file"]).exists()
    assert Path(payload["review_agent_context_file"]).exists()
    assert payload["session_context"] == payload["scope_of_work_file"]
    assert payload["source_session_context"] == str(context.resolve())
    assert payload["session_context_provided"] is True
    assert payload["excluded_path_prefixes"] == ["secret.txt"]
    assert payload["review_env"]["RVF_REPO"] == str(repo.resolve())
    assert payload["review_env"]["RVF_SCOPE_OF_WORK"] == payload["scope_of_work_file"]
    assert payload["review_env"]["RVF_REVIEW_PACKET"] == payload["review_packet"]
    review_env_text = Path(payload["review_env_file"]).read_text(encoding="utf-8")
    assert "export RVF_RUN_DIR=" in review_env_text
    assert 'export RVF_ARTIFACTS_DIR="$RVF_RUN_DIR/artifacts"' in review_env_text
    assert 'export RVF_SCOPE_OF_WORK="$RVF_ARTIFACTS_DIR/scope-of-work.md"' in review_env_text
    assert 'export RVF_REVIEW_PACKET="$RVF_ARTIFACTS_DIR/review-packet.md"' in review_env_text
    review_agent_context_text = Path(payload["review_agent_context_file"]).read_text(encoding="utf-8")
    assert payload["review_agent_context"] == review_agent_context_text
    assert "## RVF Generated Reviewer Context" in review_agent_context_text
    assert f". {payload['review_env_file']}" in review_agent_context_text
    assert "- scope-of-work: `$RVF_SCOPE_OF_WORK`" in review_agent_context_text
    assert "- review packet: `$RVF_REVIEW_PACKET`" in review_agent_context_text
    assert "- command lock wrapper: `$RVF_COMMAND_LOCK`" in review_agent_context_text
    assert payload["scope_of_work_file"] not in review_agent_context_text
    assert payload["review_packet"] not in review_agent_context_text
    metadata = json.loads(Path(payload["review_packet_metadata"]).read_text(encoding="utf-8"))
    packet_text = Path(payload["review_packet"]).read_text(encoding="utf-8")
    assert metadata["excluded_path_prefixes"] == ["secret.txt"]
    assert metadata["scope_of_work_file"] == payload["scope_of_work_file"]
    assert "## Excluded Paths" in packet_text
    assert "- secret.txt" in packet_text
    assert "### secret.txt" not in packet_text

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


def test_alternative_reviewer_prompt_uses_session_env_refs(tmp: Path) -> None:
    module = load_alternative_reviewer_module()
    repo = init_repo(tmp / "repo")
    prompt_file = tmp / "review-prompt.md"
    prompt_file.write_text("# Review Prompt\n\nBody\n", encoding="utf-8")
    context = tmp / "very" / "long" / "artifacts" / "scope-of-work.md"
    context.parent.mkdir(parents=True)
    context.write_text("scope\n", encoding="utf-8")
    packet = tmp / "very" / "long" / "artifacts" / "review-packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")

    prompt = module.build_prompt(prompt_file, context, packet, repo)

    assert "$RVF_SCOPE_OF_WORK" in prompt
    assert "$RVF_REVIEW_PACKET" in prompt
    assert "$RVF_COMMAND_LOCK" in prompt
    assert "$RVF_REPO" in prompt
    assert str(context) not in prompt
    assert str(module.COMMAND_LOCK) not in prompt


def test_alternative_reviewer_subprocess_receives_session_context_alias(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "scope-of-work.md"
    context.write_text("scope\n", encoding="utf-8")
    packet = tmp / "review-packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    reviewer_code = (
        "import os, sys; "
        "sys.stdin.read(); "
        f"expected = {str(context.resolve())!r}; "
        "assert os.environ['RVF_SCOPE_OF_WORK'] == expected; "
        "assert os.environ['RVF_SESSION_CONTEXT'] == expected; "
        "print('NO_ISSUES')"
    )
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
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
        ]
    )

    assert completed.stdout.strip() == "NO_ISSUES"


def test_command_lock_writes_lifecycle_events(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    state = tmp / "state"
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


def test_command_lock_respects_env_run_dir(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    state = tmp / "state"
    run_dir = tmp / "custom-run-dir"
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


def test_command_lock_logs_timeout_with_holder_metadata(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    state = tmp / "state"
    lock_dir = tmp / "locks"
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
            "import time; time.sleep(1)",
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
                "0.05",
                "--poll-interval",
                "0.01",
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


def test_prepare_review_run_can_build_session_manifest_from_transcript(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    context = tmp / "context.md"
    context.write_text(
        "## Session context\n"
        "- 用户最初的请求 / 意图：test\n"
        "- 本 turn 主会话实际完成的工作：prepared transcript-scoped review run\n",
        encoding="utf-8",
    )
    (repo / "owned-new.txt").write_text("owned\n", encoding="utf-8")
    (repo / "background.txt").write_text("background contents\n", encoding="utf-8")
    transcript = write_codex_transcript(tmp / "session.jsonl", repo)

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
            str(tmp / "runs"),
        ]
    )
    payload = json.loads(result.stdout)
    assert Path(payload["session_manifest"]).exists()
    assert payload["session_manifest_provided"] is True
    assert payload["source_session_manifest"] == f"transcript:{transcript.resolve()}"
    packet_text = Path(payload["review_packet"]).read_text(encoding="utf-8")
    assert "## Session Manifest" in packet_text
    assert "background contents" not in packet_text


def test_prepare_review_run_requires_session_context(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    completed = subprocess.run(
        [
            sys.executable,
            str(PREPARE_REVIEW_RUN),
            "--repo",
            str(repo),
            "--base-dir",
            str(tmp / "runs"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "session context is required" in completed.stderr


def test_alternative_reviewer_idle_timeout_flag(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
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


def test_alternative_reviewer_timeout_kills_child_process_group(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    marker = tmp / "child-survived.txt"
    child_code = (
        "import pathlib, time; "
        "time.sleep(1.0); "
        f"pathlib.Path({str(marker)!r}).write_text('survived', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess, sys, time; "
        "sys.stdin.read(); "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(10.0)"
    )
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [sys.executable, "-c", parent_code],
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
    time.sleep(1.2)
    assert not marker.exists()


def test_alternative_reviewer_activity_refreshes_idle_timeout(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import sys, time; sys.stdin.read(); "
                "[print(f'tick-{i}', flush=True) or time.sleep(0.08) for i in range(4)]; "
                "print('NO_ISSUES', flush=True)"
            ),
        ],
        idle_timeout_seconds=0.12,
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


def test_alternative_reviewer_claude_bash_tool_use_suspends_idle_timeout(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import json, sys, time; sys.stdin.read(); "
                "print(json.dumps({'type':'assistant','message':{'content':["
                "{'type':'tool_use','id':'toolu_1','name':'Bash','input':{'command':'sleep 1'}}"
                "]}}), flush=True); "
                "time.sleep(0.25); "
                "print(json.dumps({'type':'user','message':{'content':["
                "{'type':'tool_result','tool_use_id':'toolu_1','content':''}"
                "]}}), flush=True); "
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)"
            ),
        ],
        idle_timeout_seconds=0.1,
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


def test_alternative_reviewer_claude_split_jsonl_preserves_tool_use(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import json, sys, time; sys.stdin.read(); "
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
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)"
            ),
        ],
        idle_timeout_seconds=0.1,
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


def test_alternative_reviewer_repeated_run_keeps_prior_artifacts(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    run_dir = tmp / "run"
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [
            sys.executable,
            "-c",
            "import sys; sys.stdin.read(); print('NO_ISSUES')",
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
    artifacts = run_dir / "artifacts"
    for name in [
        "reviewer.prompt.txt",
        "reviewer.prompt.2.txt",
        "reviewer.stdout.txt",
        "reviewer.stdout.2.txt",
        "reviewer.stderr.txt",
        "reviewer.stderr.2.txt",
        "reviewer.normalized.txt",
        "reviewer.normalized.2.txt",
    ]:
        assert (artifacts / name).exists()


def test_alternative_reviewer_long_command_wait_uses_check_interval() -> None:
    module = load_alternative_reviewer_module()
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


def test_alternative_reviewer_claude_stream_json_extracts_result(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import sys, time, json; sys.stdin.read(); "
                "print(json.dumps({'type':'system','subtype':'init'}), flush=True); "
                "time.sleep(0.08); "
                "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'working'}]}}), flush=True); "
                "time.sleep(0.08); "
                "print(json.dumps({'type':'result','subtype':'success','result':'NO_ISSUES'}), flush=True)"
            ),
        ],
        idle_timeout_seconds=0.12,
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


def test_alternative_reviewer_legacy_claude_config_gets_stream_json(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp / "claude"
    sink = tmp / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
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
        env={"PATH": f"{tmp}:{os.environ.get('PATH', '')}"},
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


def test_alternative_reviewer_respects_explicit_claude_text_output(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp / "claude"
    sink = tmp / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "print('NO_ISSUES', flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
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
        env={"PATH": f"{tmp}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert argv == ["-p", "--output-format", "text"]


def test_alternative_reviewer_respects_explicit_claude_equals_text_output(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp / "claude"
    sink = tmp / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "print('NO_ISSUES', flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
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
        env={"PATH": f"{tmp}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "NO_ISSUES", completed.stdout
    argv = json.loads(sink.read_text(encoding="utf-8"))
    assert argv == ["-p", "--output-format=text"]


def test_alternative_reviewer_non_claude_stream_json_command_is_not_patched(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    packet = tmp / "packet.md"
    packet.write_text("## Review Packet\n\nempty\n", encoding="utf-8")
    shim = tmp / "stream_wrapper"
    sink = tmp / "argv.json"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json, sys",
                "open(%r, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))" % str(sink),
                "sys.stdin.read()",
                "print(json.dumps({'type':'result','result':'NO_ISSUES'}), flush=True)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = write_alternative_reviewer_config(
        tmp / "alternative-reviewer.json",
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


def test_vibe_kanban_mcp_client_create_issue(tmp: Path) -> None:
    fake_server = write_fake_mcp_server(tmp / "fake_mcp_server.py")
    calls = tmp / "calls.jsonl"
    env = os.environ.copy()
    env["FAKE_MCP_CALLS"] = str(calls)
    completed = run(
        [
            sys.executable,
            str(VIBE_KANBAN_MCP_CLIENT),
            "create",
            "--mcp-cmd",
            f"{sys.executable} {fake_server}",
            "--backend-url",
            "http://127.0.0.1:65432",
            "--project-id",
            "project-1",
            "--title",
            "RVF test",
            "--description",
            "run_dir: /tmp/rvf",
        ],
        env=env,
    )
    payload = json.loads(completed.stdout)
    assert payload["issue_id"] == "issue-123"
    assert payload["backend_url"] == "http://127.0.0.1:65432"
    recorded = read_jsonl(calls)
    assert recorded[0]["name"] == "create_issue"
    assert recorded[0]["arguments"]["project_id"] == "project-1"


def test_vibe_kanban_mcp_client_maps_generic_issue_status(tmp: Path) -> None:
    fake_server = write_fake_mcp_server(tmp / "fake_mcp_server.py")
    calls = tmp / "calls.jsonl"
    env = os.environ.copy()
    env["FAKE_MCP_CALLS"] = str(calls)
    completed = run(
        [
            sys.executable,
            str(VIBE_KANBAN_MCP_CLIENT),
            "update",
            "--mcp-cmd",
            f"{sys.executable} {fake_server}",
            "--project-id",
            "project-1",
            "--issue-id",
            "issue-1",
            "--description",
            "running",
            "--status",
            "running",
        ],
        env=env,
    )
    payload = json.loads(completed.stdout)
    assert payload["issue_id"] == "issue-123"
    recorded = read_jsonl(calls)
    assert recorded[-1]["name"] == "update_issue"
    assert recorded[-1]["arguments"]["status"] == "In progress"


def test_vibe_kanban_mcp_client_create_workspace_uses_local_api(tmp: Path) -> None:
    backend_url, state, server = start_fake_vibe_api()
    try:
        completed = run(
            [
                sys.executable,
                str(VIBE_KANBAN_MCP_CLIENT),
                "create-workspace",
                "--backend-url",
                backend_url,
                "--title",
                "RVF test",
                "--description",
                "run_dir: /tmp/rvf",
                "--status",
                "queued",
            ],
        )
    finally:
        server.shutdown()
    payload = json.loads(completed.stdout)
    assert payload["workspace_id"] == "workspace-1"
    assert payload["backend_url"] == backend_url
    requests = state["requests"]
    assert isinstance(requests, list)
    assert requests[0]["method"] == "POST"
    assert requests[0]["path"] == "/api/workspaces"
    assert requests[0]["body"]["name"] == "RVF queued: RVF test"
    assert requests[1]["path"] == "/api/scratch/WORKSPACE_NOTES/workspace-1"


def test_vibe_kanban_mcp_client_update_workspace_uses_local_api(tmp: Path) -> None:
    backend_url, state, server = start_fake_vibe_api()
    try:
        completed = run(
            [
                sys.executable,
                str(VIBE_KANBAN_MCP_CLIENT),
                "update-workspace",
                "--backend-url",
                backend_url,
                "--workspace-id",
                "workspace-1",
                "--title",
                "RVF test",
                "--description",
                "completed",
                "--status",
                "completed",
            ],
        )
    finally:
        server.shutdown()
    payload = json.loads(completed.stdout)
    assert payload["workspace_id"] == "workspace-1"
    requests = state["requests"]
    assert isinstance(requests, list)
    assert requests[0]["method"] == "PUT"
    assert requests[0]["path"] == "/api/workspaces/workspace-1"
    assert requests[0]["body"]["name"] == "RVF completed: RVF test"


def test_run_ledger_summary_preserves_vibe_management_fields(tmp: Path) -> None:
    module = load_rvf_logging_module()
    run_dir = tmp / "run"
    ledger = module.RunLedger(component="stop-hook", repo=tmp, cwd=tmp, run_id="run-1", run_dir=run_dir)
    ledger.summary(
        status="vibe-kanban-started",
        reason_code="vibe_kanban_runner_started",
        vibe_management_mode="local-workspace",
        vibe_workspace_id="workspace-1",
        vibe_backend_url="http://127.0.0.1:62040",
        runner_pid=123,
    )

    later = ledger.summary(
        status="completed",
        reason_code="prepare_completed",
        message="later phase",
    )

    assert later["status"] == "completed"
    assert later["vibe_management_mode"] == "local-workspace"
    assert later["vibe_workspace_id"] == "workspace-1"
    assert later["vibe_backend_url"] == "http://127.0.0.1:62040"
    assert later["runner_pid"] == 123


def test_vibe_kanban_mcp_client_parses_lsof_vibe_app_ports() -> None:
    module = load_vibe_kanban_client_module()
    output = "\n".join(
        [
            "p100",
            "cvibe-kanb",
            "nlocalhost:63431",
            "n127.0.0.1:63432",
            "p101",
            "cvibe-kanban-mcp",
            "nlocalhost:73431",
            "p102",
            "cnode",
            "nlocalhost:83431",
        ]
    )
    assert module._parse_lsof_listen_ports(output) == [63431, 63432]


def test_vibe_kanban_mcp_client_prefers_discovered_backend_url() -> None:
    module = load_vibe_kanban_client_module()
    original = module.discover_backend_urls
    try:
        module.discover_backend_urls = lambda: ["http://127.0.0.1:50280"]
        assert module._candidate_backend_urls(None) == ["http://127.0.0.1:50280", None]
    finally:
        module.discover_backend_urls = original


def test_vibe_kanban_mcp_client_starts_local_app_when_probe_lacks_backend(tmp: Path) -> None:
    module = load_vibe_kanban_client_module()
    started = {"value": False}
    starts: list[dict[str, object]] = []
    original_ensure = module.ensure_vibe_kanban_mcp
    original_probe = module.probe_mcp_candidates
    original_discover = module.discover_backend_urls
    original_reachable = module._backend_url_is_reachable
    original_start = module.start_vibe_kanban_app
    try:
        module.ensure_vibe_kanban_mcp = lambda **_kwargs: {"available": True, "started": False}
        module.probe_mcp_candidates = lambda *_args, **_kwargs: {"available": True, "started": False}
        module.discover_backend_urls = (
            lambda: ["http://127.0.0.1:50280"] if started["value"] else []
        )
        module._backend_url_is_reachable = lambda _url: bool(started["value"])

        def fake_start(**kwargs: object) -> dict[str, object]:
            starts.append(kwargs)
            started["value"] = True
            return {"started": True, "launcher": "test"}

        module.start_vibe_kanban_app = fake_start
        payload = module.ensure_local_backend(
            mcp_cmd="fake mcp",
            backend_url=None,
            start_if_needed=True,
            start_cmd="fake start",
            repo=tmp,
            tmux_session="rvf-test",
            timeout_seconds=0.2,
        )
    finally:
        module.ensure_vibe_kanban_mcp = original_ensure
        module.probe_mcp_candidates = original_probe
        module.discover_backend_urls = original_discover
        module._backend_url_is_reachable = original_reachable
        module.start_vibe_kanban_app = original_start

    assert starts
    assert payload["backend_url"] == "http://127.0.0.1:50280"
    assert payload["source"] == "started_discovered_backend_url"
    assert payload["started"] is True


def test_vibe_kanban_mcp_client_ignores_stale_port_file(tmp: Path) -> None:
    module = load_vibe_kanban_client_module()
    tmp.mkdir(parents=True, exist_ok=True)
    port_file = tmp / "vibe-kanban.port"
    port_file.write_text('{"main_port": 1}', encoding="utf-8")
    assert module._backend_urls_from_port_file(port_file) == []


def test_vibe_kanban_mcp_client_surfaces_tool_errors(tmp: Path) -> None:
    fake_server = write_fake_mcp_server(tmp / "fake_mcp_server.py")
    env = os.environ.copy()
    env["FAKE_TOOL_ERROR"] = "list_repos"
    completed = subprocess.run(
        [
            sys.executable,
            str(VIBE_KANBAN_MCP_CLIENT),
            "list-repos",
            "--mcp-cmd",
            f"{sys.executable} {fake_server}",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 2
    assert "fake tool failure: diagnostic detail" in completed.stderr


def test_vibe_kanban_mcp_client_times_out_when_initialize_hangs(tmp: Path) -> None:
    fake_server = tmp / "hanging_mcp_server.py"
    fake_server.parent.mkdir(parents=True, exist_ok=True)
    fake_server.write_text(
        "import sys, time\n"
        "sys.stdin.readline()\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["CODEX_RVF_VK_MCP_REQUEST_TIMEOUT"] = "0.2"
    started = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable,
            str(VIBE_KANBAN_MCP_CLIENT),
            "list-repos",
            "--mcp-cmd",
            f"{sys.executable} {fake_server}",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=5,
    )
    elapsed = time.monotonic() - started
    assert completed.returncode == 2
    assert "MCP initialize timed out after 0.2s waiting for stdout" in completed.stderr
    assert elapsed < 3


def test_vibe_kanban_mcp_client_resolves_project_from_repo(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    fake_server = write_fake_mcp_server(tmp / "fake_mcp_server.py")
    calls = tmp / "calls.jsonl"
    env = os.environ.copy()
    env["FAKE_MCP_CALLS"] = str(calls)
    env["FAKE_REPO_PATH"] = str(repo)
    completed = run(
        [
            sys.executable,
            str(VIBE_KANBAN_MCP_CLIENT),
            "resolve-project",
            "--mcp-cmd",
            f"{sys.executable} {fake_server}",
            "--repo",
            str(repo),
            "--create-if-missing",
        ],
        env=env,
    )
    payload = json.loads(completed.stdout)
    assert payload["project_id"] == "project-from-repo"
    assert payload["source"] == "list_repos"
    recorded = read_jsonl(calls)
    assert recorded[0]["name"] == "list_repos"


def test_vibe_kanban_mcp_client_uses_single_project_fallback(tmp: Path) -> None:
    repo = init_repo(tmp / "different-repo")
    fake_server = write_fake_mcp_server(tmp / "fake_mcp_server.py")
    env = os.environ.copy()
    env["FAKE_NO_CREATE_PROJECT"] = "1"
    completed = run(
        [
            sys.executable,
            str(VIBE_KANBAN_MCP_CLIENT),
            "resolve-project",
            "--mcp-cmd",
            f"{sys.executable} {fake_server}",
            "--repo",
            str(repo),
            "--create-if-missing",
        ],
        env=env,
    )
    payload = json.loads(completed.stdout)
    assert payload["project_id"] == "project-from-project"
    assert payload["source"] == "single_project_fallback"


def test_vibe_kanban_runner_success_updates_summary_and_workspace(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    prompt = tmp / "prompt.txt"
    prompt.write_text("$review-validate-fix\n\noriginal GUI fork prompt marker\n", encoding="utf-8")
    transcript = tmp / "parent-session.jsonl"
    transcript.write_text('{"type":"session_meta","payload":{"id":"parent-thread"}}\n', encoding="utf-8")
    backend_url, api_state, api_server = start_fake_vibe_api()
    fake_codex = tmp / "fake_codex.py"
    env_capture = tmp / "codex-env.json"
    stdin_capture = tmp / "codex-stdin.md"
    fake_codex.write_text(
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "final = pathlib.Path(args[args.index('--output-last-message') + 1])\n"
        "handoff = final.with_name('handoff.md')\n"
        "handoff.write_text('# handoff\\n', encoding='utf-8')\n"
        "final.write_text(f'RVF_HANDOFF_FILE: {handoff}\\n', encoding='utf-8')\n"
        "stdin = sys.stdin.read()\n"
        "pathlib.Path(os.environ['FAKE_CODEX_STDIN']).write_text(stdin, encoding='utf-8')\n"
        "payload = {\n"
        "    'argv': args,\n"
        "    'suppress': os.environ.get('CODEX_RVF_SUPPRESS_STOP_HOOK'),\n"
        "    'run_id': os.environ.get('CODEX_RVF_RUN_ID'),\n"
        "    'run_dir': os.environ.get('CODEX_RVF_RUN_DIR'),\n"
        "    'parent_transcript': os.environ.get('CODEX_RVF_PARENT_TRANSCRIPT_PATH'),\n"
        "    'fork_mode': os.environ.get('CODEX_RVF_FORK_MODE'),\n"
        "    'session_manifest': os.environ.get('RVF_SESSION_MANIFEST'),\n"
        "}\n"
        "pathlib.Path(os.environ['FAKE_CODEX_ENV']).write_text(json.dumps(payload), encoding='utf-8')\n"
        "print(json.dumps({'event': 'complete'}))\n",
        encoding="utf-8",
    )
    run_dir = tmp / "state" / "runs" / "rvf-test"
    startup_prepare = tmp / "startup-prepare.json"
    startup_prepare.write_text(
        json.dumps(
            {
                "scope_of_work_file": str(run_dir / "artifacts" / "scope-of-work.md"),
                "session_manifest_file": str(run_dir / "artifacts" / "session-manifest.json"),
                "review_packet": str(run_dir / "artifacts" / "review-packet.md"),
                "before_workspace_snapshot": str(run_dir / "artifacts" / "before-workspace-snapshot.json"),
                "review_env_file": str(run_dir / "artifacts" / "review-env.sh"),
                "review_agent_context_file": str(run_dir / "artifacts" / "review-agent-context.md"),
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["FAKE_CODEX_ENV"] = str(env_capture)
    env["FAKE_CODEX_STDIN"] = str(stdin_capture)
    env["CODEX_RVF_FORK_MODE"] = "vibe-kanban"
    env["CODEX_RVF_RUN_DIR"] = str(tmp / "parent-run-dir")
    env["RVF_SESSION_MANIFEST"] = str(tmp / "parent-session-manifest.json")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(RUN_VIBE_KANBAN_RVF),
                "--repo",
                str(repo),
                "--prompt-file",
                str(prompt),
                "--run-id",
                "rvf-test",
                "--run-dir",
                str(run_dir),
                "--parent-session-id",
                "parent-thread",
                "--parent-transcript-path",
                str(transcript),
                "--vibe-workspace-id",
                "workspace-1",
                "--backend-url",
                backend_url,
                "--codex-bin",
                sys.executable,
                "--codex-exec-args",
                str(fake_codex),
                "--startup-prepare-metadata",
                str(startup_prepare),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    finally:
        api_server.shutdown()
    assert completed.returncode == 0, completed.stderr
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "vibe-kanban-rvf-completed"
    assert summary["returncode"] == 0
    codex_env = json.loads(env_capture.read_text(encoding="utf-8"))
    assert codex_env["suppress"] == "1"
    assert codex_env["run_id"] == "rvf-test"
    assert codex_env["run_dir"] == str(run_dir.resolve())
    assert codex_env["parent_transcript"] == str(transcript.resolve())
    assert codex_env["fork_mode"] is None
    assert codex_env["session_manifest"] is None
    codex_stdin = stdin_capture.read_text(encoding="utf-8")
    assert "RVF_HEADLESS_REVIEW_VALIDATE_FIX" in codex_stdin
    assert "Frozen startup artifacts captured before the headless runner was queued" in codex_stdin
    assert "不要因 runner 排队后 worktree 变化而重新定义 review scope" in codex_stdin
    assert "CODEX_RVF_SUPPRESS_STOP_HOOK=1" in codex_stdin
    assert "original GUI fork prompt marker" in codex_stdin
    assert str(transcript.resolve()) in codex_stdin
    assert "--rvf-run-id rvf-test" in codex_stdin
    assert f"--rvf-run-dir {run_dir.resolve()}" in codex_stdin
    assert "prepare_review_run.py" in codex_stdin
    assert (run_dir / "artifacts" / "codex-exec.prompt.md").read_text(encoding="utf-8") == codex_stdin
    requests = api_state["requests"]
    assert isinstance(requests, list)
    workspace_updates = [request for request in requests if request["path"] == "/api/workspaces/workspace-1"]
    assert [request["body"]["name"] for request in workspace_updates] == [
        "RVF running: workspace workspace-1",
        "RVF completed: workspace workspace-1",
    ]


def test_vibe_kanban_runner_failure_records_returncode(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    prompt = tmp / "prompt.txt"
    prompt.write_text("$review-validate-fix\n", encoding="utf-8")
    fake_codex = tmp / "fake_codex_fail.py"
    fake_codex.write_text(
        "import sys\n"
        "sys.stderr.write('codex failed\\n')\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )
    run_dir = tmp / "state" / "runs" / "rvf-fail"
    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_VIBE_KANBAN_RVF),
            "--repo",
            str(repo),
            "--prompt-file",
            str(prompt),
            "--run-id",
            "rvf-fail",
            "--run-dir",
            str(run_dir),
            "--parent-session-id",
            "parent-thread",
            "--codex-bin",
            sys.executable,
            "--codex-exec-args",
            str(fake_codex),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 7
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "vibe-kanban-rvf-failed"
    assert summary["returncode"] == 7
    assert "codex failed" in (run_dir / "artifacts" / "codex-exec.stderr.txt").read_text(encoding="utf-8")


def test_vibe_kanban_runner_signal_records_cancelled(tmp: Path) -> None:
    repo = init_repo(tmp / "repo")
    prompt = tmp / "prompt.txt"
    prompt.write_text("$review-validate-fix\n", encoding="utf-8")
    backend_url, api_state, api_server = start_fake_vibe_api()
    fake_codex = tmp / "fake_codex_cancel.py"
    fake_codex.write_text(
        "import os, signal, sys\n"
        "sys.stdin.read()\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n",
        encoding="utf-8",
    )
    run_dir = tmp / "state" / "runs" / "rvf-cancel"
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(RUN_VIBE_KANBAN_RVF),
                "--repo",
                str(repo),
                "--prompt-file",
                str(prompt),
                "--run-id",
                "rvf-cancel",
                "--run-dir",
                str(run_dir),
                "--parent-session-id",
                "parent-thread",
                "--vibe-workspace-id",
                "workspace-1",
                "--backend-url",
                backend_url,
                "--codex-bin",
                sys.executable,
                "--codex-exec-args",
                str(fake_codex),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        api_server.shutdown()
    assert completed.returncode != 0
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "vibe-kanban-rvf-cancelled"
    assert summary["reason_code"] == "codex_exec_cancelled"
    assert summary["returncode"] == -15
    events = read_jsonl(run_dir / "events.jsonl")
    assert any(event["event"] == "codex_exec_cancelled" for event in events)
    requests = api_state["requests"]
    assert isinstance(requests, list)
    workspace_updates = [request for request in requests if request["path"] == "/api/workspaces/workspace-1"]
    assert [request["body"]["name"] for request in workspace_updates] == [
        "RVF running: workspace workspace-1",
        "RVF cancelled: workspace workspace-1",
    ]


def test_cancel_rvf_run_marks_cancelled_and_updates_workspace(tmp: Path) -> None:
    run_dir = tmp / "state" / "runs" / "rvf-user-cancel"
    run_dir.mkdir(parents=True)
    backend_url, api_state, api_server = start_fake_vibe_api()
    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "run_id": "rvf-user-cancel",
                "status": "vibe-kanban-started",
                "repo": str(tmp / "repo"),
                "cwd": str(tmp / "repo"),
                "run_dir": str(run_dir),
                "issue_title": "RVF test run",
                "vibe_backend_url": backend_url,
                "vibe_workspace_id": "workspace-1",
                "runner_pid": 999999,
            }
        ),
        encoding="utf-8",
    )
    try:
        completed = run(
            [
                sys.executable,
                str(CANCEL_RVF_RUN),
                "--summary",
                str(summary_path),
                "--force-after",
                "0",
            ],
        )
    finally:
        api_server.shutdown()
    payload = json.loads(completed.stdout)
    assert payload["status"] == "cancelled"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "vibe-kanban-rvf-cancelled"
    assert summary["reason_code"] == "user_cancelled"
    events = read_jsonl(run_dir / "events.jsonl")
    assert any(event["event"] == "run_cancel_requested" for event in events)
    assert any(event["event"] == "run_cancelled" for event in events)
    requests = api_state["requests"]
    assert isinstance(requests, list)
    workspace_updates = [request for request in requests if request["path"] == "/api/workspaces/workspace-1"]
    assert workspace_updates[-1]["body"]["name"] == "RVF cancelled: RVF test run"


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


def test_vibe_kanban_local_api_bypasses_proxy_for_localhost() -> None:
    module = load_vibe_kanban_client_module()
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"success": true, "data": {"ok": true}}'

    class FakeOpener:
        def open(self, request: object, timeout: int) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

    original_build_opener = module.urllib_request.build_opener
    original_urlopen = module.urllib_request.urlopen
    try:
        def fake_build_opener(*handlers: object) -> FakeOpener:
            captured["handlers"] = handlers
            return FakeOpener()

        module.urllib_request.build_opener = fake_build_opener
        module.urllib_request.urlopen = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("urlopen should not be used for localhost API requests")
        )
        payload = module._api_request("http://127.0.0.1:12345", "GET", "/api/workspaces")
    finally:
        module.urllib_request.build_opener = original_build_opener
        module.urllib_request.urlopen = original_urlopen

    assert payload == {"success": True, "data": {"ok": True}}
    handlers = captured["handlers"]
    assert any(
        handler.__class__.__name__ == "ProxyHandler" and getattr(handler, "proxies", None) == {}
        for handler in handlers
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        test_check_review_output_lock_request()
        test_check_review_output_accepts_wrapped_issue_continuation()
        test_build_packet_metadata_and_scope(root / "packet")
        test_build_packet_allows_clean_repo_with_manual_scope(root / "packet-clean-manual-scope")
        test_session_manifest_extracts_apply_patch_and_command_candidates(root / "session-manifest")
        test_session_manifest_resolves_exec_paths_from_command_workdir(root / "session-manifest-workdir")
        test_build_packet_uses_session_manifest_as_scope_anchor(root / "packet-manifest")
        test_build_packet_rejects_session_manifest_for_different_repo(root / "packet-manifest-repo")
        test_build_packet_rejects_empty_session_owned_scope(root / "packet-manifest-empty")
        test_build_packet_requires_session_context(root / "packet-requires-context")
        test_build_packet_honors_review_validate_fix_ignore(root / "packet-ignore")
        test_build_packet_treats_ignore_prefixes_as_literal_pathspecs(root / "packet-literal-ignore")
        test_prepare_review_run_and_command_lock(root / "prepare")
        test_alternative_reviewer_prompt_uses_session_env_refs(root / "alternative-prompt-env")
        test_alternative_reviewer_subprocess_receives_session_context_alias(root / "alternative-session-alias")
        test_command_lock_writes_lifecycle_events(root / "command-lock-lifecycle")
        test_command_lock_respects_env_run_dir(root / "command-lock-env-run-dir")
        test_command_lock_logs_timeout_with_holder_metadata(root / "command-lock-timeout")
        test_prepare_review_run_can_build_session_manifest_from_transcript(root / "prepare-transcript")
        test_prepare_review_run_requires_session_context(root / "prepare-requires-context")
        test_alternative_reviewer_idle_timeout_flag(root / "alternative-timeout")
        test_alternative_reviewer_timeout_kills_child_process_group(root / "alternative-timeout-child")
        test_alternative_reviewer_activity_refreshes_idle_timeout(root / "alternative-activity")
        test_alternative_reviewer_claude_bash_tool_use_suspends_idle_timeout(root / "alternative-bash-tool")
        test_alternative_reviewer_claude_split_jsonl_preserves_tool_use(root / "alternative-split-jsonl")
        test_alternative_reviewer_repeated_run_keeps_prior_artifacts(root / "alternative-repeat-artifacts")
        test_alternative_reviewer_long_command_wait_uses_check_interval()
        test_alternative_reviewer_claude_stream_json_extracts_result(root / "alternative-stream-json")
        test_alternative_reviewer_legacy_claude_config_gets_stream_json(root / "alternative-legacy-config")
        test_alternative_reviewer_respects_explicit_claude_text_output(root / "alternative-text-config")
        test_alternative_reviewer_respects_explicit_claude_equals_text_output(
            root / "alternative-equals-text-config"
        )
        test_alternative_reviewer_non_claude_stream_json_command_is_not_patched(root / "alternative-wrapper")
        test_vibe_kanban_mcp_client_create_issue(root / "vibe-mcp-client")
        test_vibe_kanban_mcp_client_maps_generic_issue_status(root / "vibe-status-map")
        test_vibe_kanban_mcp_client_create_workspace_uses_local_api(root / "vibe-create-workspace")
        test_vibe_kanban_mcp_client_update_workspace_uses_local_api(root / "vibe-update-workspace")
        test_run_ledger_summary_preserves_vibe_management_fields(root / "summary-preserve-vibe")
        test_vibe_kanban_mcp_client_parses_lsof_vibe_app_ports()
        test_vibe_kanban_mcp_client_prefers_discovered_backend_url()
        test_vibe_kanban_mcp_client_starts_local_app_when_probe_lacks_backend(root / "vibe-start-local")
        test_vibe_kanban_mcp_client_ignores_stale_port_file(root / "vibe-stale-port")
        test_vibe_kanban_mcp_client_surfaces_tool_errors(root / "vibe-tool-error")
        test_vibe_kanban_mcp_client_times_out_when_initialize_hangs(root / "vibe-timeout")
        test_vibe_kanban_mcp_client_resolves_project_from_repo(root / "vibe-resolve-project")
        test_vibe_kanban_mcp_client_uses_single_project_fallback(root / "vibe-single-project")
        test_vibe_kanban_runner_success_updates_summary_and_workspace(root / "vibe-runner-success")
        test_vibe_kanban_runner_failure_records_returncode(root / "vibe-runner-failure")
        test_vibe_kanban_runner_signal_records_cancelled(root / "vibe-runner-cancel")
        test_cancel_rvf_run_marks_cancelled_and_updates_workspace(root / "cancel-rvf-run")
        test_cancel_rvf_run_ignores_stale_runner_pid_without_matching_command()
        test_vibe_kanban_local_api_bypasses_proxy_for_localhost()
    print("review support script tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
