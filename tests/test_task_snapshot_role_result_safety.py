"""真实角色进程的结果脱敏、协议保全与失败恢复验收。"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json, write_json
from core.task_snapshot_review.role_process_execution import launch_role, reconcile_completed_role_attempt, role_environment
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import resume_review


SECRET_NAME = "ISOLATED_ROLE_SAFETY_KEY"
SECRET = "isolated-invalid-role-safety-secret-never-for-real-use"


@pytest.fixture
def role_repository(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "RVF test")
    git(repo, "config", "user.email", "rvf-test@example.invalid")
    (repo / "task.py").write_text("value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "test: 初始化角色结果验收仓库")
    return repo


def _request(attempt, repo, program, *, role="reviewer", output_format="text", env=None):
    attempt.mkdir()
    request = {
        "role": role,
        "repository": str(repo),
        "workspace": str(attempt / "workspace"),
        "candidate_commit": git_text(repo, "rev-parse", "HEAD"),
        "input_file": str(attempt / "request.json"),
        "result_file": str(attempt / "role-output.json"),
        "findings": [],
        "config": {
            "command": [sys.executable, "-c", program],
            "output_format": output_format,
            "credential_environment_variable_names": sorted(env or {}),
            "timeout_seconds": 20,
        },
    }
    write_json(attempt / "request.json", request)
    (attempt / "prompt.md").write_text("检查固定候选并返回角色结果。\n")
    return request


def _launch(attempt, env=None):
    process = launch_role(attempt, environment_overrides=env)
    assert process.wait(timeout=30) == 0
    return read_json(attempt / "outcome.json")


def _assert_no_secret(directory):
    for path in directory.rglob("*"):
        if path.is_file() and "workspace" not in path.relative_to(directory).parts:
            assert SECRET.encode() not in path.read_bytes(), str(path)


@pytest.mark.parametrize("source", ["file", "stdout", "structured-stream"])
def test_two_role_legs_sanitize_both_result_sources_and_preserve_numeric_protocol(role_repository, tmp_path, source):
    (role_repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "review"
    create_snapshot(role_repository, base_ref="HEAD", scope_paths=["task.py"],
                    requirements="保全角色结果协议并脱敏".encode(), run_directory=run)
    program = (
        "import json,os,pathlib\n"
        f"p={{'status':'completed','audit_summary':os.environ[{SECRET_NAME!r}],'findings':[],'reviewed_lines':1}}\n"
    )
    if source == "file":
        program += "pathlib.Path(os.environ['RVF_REVIEW_RESULT']).write_text(json.dumps(p,indent=2)); print('角色完成')\n"
    elif source == "structured-stream":
        program += (
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(p)}}))\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))\n"
        )
    else:
        program += "print(json.dumps(p,indent=2))\n"
    config = {"command": [sys.executable, "-c", program],
              "output_format": "codex_json" if source == "structured-stream" else "text",
              "env_set": {SECRET_NAME: SECRET, "NO_COLOR": "1"}}
    assert resume_review(run, reviewer_configs=[config, config])["status"] == "ready_for_validation"
    attempts = list(run.glob("reviewer-*/attempt-*"))
    assert len(attempts) == 2
    for attempt in attempts:
        result = read_json(attempt / "result.json")
        assert result["status"] == "completed"
        assert result["reviewed_lines"] == 1
        assert isinstance(result["audit_summary"], str) and result["audit_summary"].strip()
        if source == "file":
            assert read_json(attempt / "role-output.json") == result
        if source == "structured-stream":
            records = [json.loads(line) for line in (attempt / "stdout.txt").read_text().splitlines()]
            assert records[-1] == {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}
            assert json.loads(records[0]["item"]["text"])["reviewed_lines"] == 1
    _assert_no_secret(run)
    assert resume_review(run)["status"] == "ready_for_validation"
    _assert_no_secret(run)


@pytest.mark.parametrize("role", ["reviewer", "fixer"])
@pytest.mark.parametrize("problem_key", [17, ["问题"], {"问题": "身份"}, None])
def test_invalid_problem_key_never_completes_or_reconciles(role_repository, tmp_path, role, problem_key):
    finding = {"problem_key": problem_key, "message": "需要合法身份的发现"}
    payload = ({"status": "completed", "audit_summary": "已检查", "findings": [finding]}
               if role == "reviewer" else
               {"status": "completed", "dispositions": [], "new_findings": [finding]})
    attempt = tmp_path / "attempt"
    program = f"import json; print(json.dumps({payload!r}))"
    _request(attempt, role_repository, program, role=role)
    outcome = _launch(attempt)
    assert outcome["status"] == "failed"
    assert outcome["returncode"] == 0
    assert not (attempt / "result.json").exists()
    reconcile_completed_role_attempt(attempt)
    assert not (attempt / "reconciled-native-completion.json").exists()
    assert not (attempt / "result.json").exists()


@pytest.mark.parametrize("failure", ["exit", "schema", "unterminated", "provider-error"])
def test_failure_paths_also_sanitize_raw_role_files(role_repository, tmp_path, failure):
    attempt = tmp_path / "attempt"
    program = (
        "import json,os,pathlib,sys\n"
        f"secret=os.environ[{SECRET_NAME!r}]\n"
        "p={'status':'completed','audit_summary':secret,'findings':[]}\n"
    )
    if failure == "schema":
        program += "p['findings']=[{'problem_key':17,'message':secret}]\n"
    program += (
        "pathlib.Path(os.environ['RVF_REVIEW_RESULT']).write_text(json.dumps(p,indent=2))\n"
        "print(secret,file=sys.stderr)\n"
    )
    program += ("print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(p)}}))\n"
                if failure == "unterminated" else "print(json.dumps(p))\n")
    if failure == "provider-error":
        program += (
            "print(json.dumps({'type':'error','message':'usage limit '+secret}))\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))\n"
        )
    if failure == "exit":
        program += "sys.exit(7)\n"
    env = {SECRET_NAME: SECRET, "NO_COLOR": "1"}
    _request(attempt, role_repository, program,
             output_format="codex_json" if failure in ("unterminated", "provider-error") else "text", env=env)
    outcome = _launch(attempt, env)
    assert outcome["status"] == "failed"
    assert not (attempt / "result.json").exists()
    _assert_no_secret(attempt)
    reconcile_completed_role_attempt(attempt)
    assert not (attempt / "result.json").exists()
    assert not (attempt / "reconciled-native-completion.json").exists()
    _assert_no_secret(attempt)


def test_reconciliation_sanitizes_original_native_files_before_publishing_result(role_repository, tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    program = (
        "import json,os,pathlib\n"
        f"p={{'status':'completed','audit_summary':os.environ[{SECRET_NAME!r}],'findings':[],'reviewed_lines':1}}\n"
        "pathlib.Path(os.environ['RVF_REVIEW_RESULT']).write_text(json.dumps(p,indent=2))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(p)}}))\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))\n"
    )
    env = {SECRET_NAME: SECRET, "NO_COLOR": "1"}
    request = _request(attempt, role_repository, program, output_format="codex_json", env=env)
    # 直接执行真实原生进程，留下 worker 尚未发布结果时的持久材料。
    git(role_repository, "worktree", "add", "--detach", request["workspace"], request["candidate_commit"])
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    process = subprocess.run(request["config"]["command"], input="", text=True, capture_output=True,
                             cwd=request["workspace"], env=role_environment(request), timeout=20)
    assert process.returncode == 0
    (attempt / "stdout.txt").write_text(process.stdout)
    (attempt / "stderr.txt").write_text(process.stderr)
    write_json(attempt / "outcome.json", {"status": "failed", "returncode": 0, "error": "结果尚未发布：" + SECRET})
    assert SECRET in (attempt / "role-output.json").read_text()
    reconcile_completed_role_attempt(attempt)
    assert read_json(attempt / "reconciled-native-completion.json")["status"] == "completed"
    result = read_json(attempt / "result.json")
    assert result["status"] == "completed"
    assert result["reviewed_lines"] == 1
    assert isinstance(result["audit_summary"], str) and result["audit_summary"].strip()
    _assert_no_secret(attempt)


def test_invalid_reviewer_problem_key_uses_available_alternative(role_repository, tmp_path):
    (role_repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "review"
    create_snapshot(role_repository, base_ref="HEAD", scope_paths=["task.py"],
                    requirements="无效发现应使用可用替代腿".encode(), run_directory=run)
    bad_payload = {"status": "completed", "audit_summary": "已检查",
                   "findings": [{"problem_key": 17, "message": "无效身份"}]}
    good_payload = {"status": "completed", "audit_summary": "已检查", "findings": []}
    configurations = [
        {"command": [sys.executable, "-c", f"import json; print(json.dumps({payload!r}))"],
         "output_format": "text"}
        for payload in (bad_payload, good_payload)
    ]
    assert resume_review(run, reviewer_configs=configurations)["status"] == "ready_for_validation"
    outcomes = [read_json(path) for path in run.glob("reviewer-*/attempt-*/outcome.json")]
    assert sum(outcome["status"] == "completed" for outcome in outcomes) == 2
    assert any(outcome["status"] == "failed" for outcome in outcomes)
    assert resume_review(run)["status"] == "ready_for_validation"
