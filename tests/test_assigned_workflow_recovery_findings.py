"""独立修复发现的撤销、凭据、准备与恢复边界回归。"""
import concurrent.futures
import json
import sys
import time
from pathlib import Path

import pytest

from test_task_snapshot_review_workflow import repository, fake_config
from core.task_snapshot_review import workflow
from core.task_snapshot_review.execution_lifecycle import ExecutionRevoked, persist_execution_revocation, publish_business_result
from core.task_snapshot_review.machine_evidence import WorkspacePreparationFailed, prepare_isolated_workspace
from core.task_snapshot_review.persistent_artifacts import exclusive_lock, git, read_json, write_json
from core.task_snapshot_review.task_snapshot import capture_worktree_tree, create_snapshot


@pytest.mark.parametrize("executable,environment,expected", [
    ("codex", {"CODEX_HOME": "configured-codex"}, "configured-codex/auth.json"),
    ("claude", {"CLAUDE_CONFIG_DIR": "configured-claude"}, "configured-claude/.credentials.json"),
    ("codex", {"HOME": "configured-home"}, "configured-home/.codex/auth.json"),
])
def test_credentials_follow_effective_role_environment(tmp_path, executable, environment, expected):
    configuration_file = tmp_path / "role-configuration.json"
    write_json(configuration_file, {"command": [executable], "env_set": {key: str(tmp_path / value) for key, value in environment.items()},
                                    "env_unset": ["CODEX_HOME"] if "HOME" in environment else []})
    configuration = workflow.load_reviewer_configuration(configuration_file)
    credentials = tmp_path / expected
    assert configuration['credential_evidence_paths'][0] == str(credentials)
    assert all(Path(path).parent == credentials.parent for path in configuration['credential_evidence_paths'])
    original_digest = workflow.configuration_digest(configuration)
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text("已刷新测试凭据")
    assert workflow.configuration_digest(configuration) != original_digest


@pytest.mark.parametrize("category", ["configuration", "deterministic"])
def test_permanent_configuration_failure_survives_unrelated_evidence(repository, tmp_path, monkeypatch, category):
    run = tmp_path / "configuration-recovery"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"test", run_directory=run)
    bad = {"command": [sys.executable, "--unknown-reviewer-argument"]}
    good = {"command": [sys.executable, "-c", "pass"]}
    attempt = run / "reviewer-1/attempt-001"
    record = read_json(run / "run.json")
    write_json(attempt / "request.json", {"candidate_commit": record["candidate_commit"], "configuration_digest": workflow.configuration_digest(bad),
                                          "findings": [], "config": bad, "shared_machine_evidence_recovery_sha256": "old"})
    write_json(attempt / "outcome.json", {"status": "failed", "retryable": False, "recovery": {"category": category}})
    record["shared_machine_evidence_recovery_sha256"] = "new"
    monkeypatch.setattr(workflow, "launch_role", lambda *_arguments, **_keywords: None)
    workflow._start_role(run, record, attempt.parent, "reviewer", [bad, good], [])
    assert read_json(attempt.parent / "attempt-002/request.json")["config"]["command"] == good["command"]


def test_pending_recovery_watches_native_output(repository, tmp_path):
    run = tmp_path / "native-recovery"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"test", run_directory=run)
    attempt = run / "reviewer-1/attempt-001"
    workspace = attempt / "workspace"
    candidate = read_json(run / "run.json")["candidate_commit"]
    write_json(attempt / "request.json", {"config": {"output_format": "codex_json"}, "role": "reviewer",
        "run_directory": str(run), "candidate_commit": candidate, "workspace": str(workspace),
        "input_file": str(attempt / "request.json"), "result_file": str(attempt / "role-output.json")})
    git(repository, "worktree", "add", "--detach", str(workspace), candidate)
    write_json(attempt / "model-invocation.json", {"output_format": "codex_json"})
    result = publish_business_result(run, {"status": "review_incomplete"})
    paths = result["recovery"]["evidence_paths"]
    assert str(attempt / "stdout.txt") in paths
    before = [(path, Path(path).read_bytes() if Path(path).is_file() else None) for path in paths]
    payload = {"status": "completed", "audit_summary": "原生完成后自主补证", "findings": []}
    (attempt / "stdout.txt").write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}})
        + "\n" + json.dumps({"type": "turn.completed"}))
    assert before != [(path, Path(path).read_bytes() if Path(path).is_file() else None) for path in paths]
    assert workflow._role_result(attempt.parent)["result"] == payload
    assert not (attempt / "outcome.json").exists()


def test_preparation_provenance_survives_resume_and_partial_failure(repository, tmp_path):
    run = tmp_path / "preparation-provenance"
    write_json(run / "run.json", {"status": "review_pending"})
    artifacts = run / "fixer/attempt-001"
    command = [sys.executable, "-c", "from pathlib import Path; Path('.prepared-environment').touch()"]
    for _ in range(2):
        git(repository, "reset", "--hard", "HEAD")
        prepare_isolated_workspace(run, repository, [command], artifacts)
        assert read_json(artifacts / "preparation-created-untracked-paths.json")["paths"] == [".prepared-environment"]
    partial = [sys.executable, "-c", "from pathlib import Path; Path('.partially-prepared-environment').touch(); raise SystemExit(1)"]
    with pytest.raises(WorkspacePreparationFailed):
        prepare_isolated_workspace(run, repository, [partial], artifacts)
    assert read_json(artifacts / "preparation-created-untracked-paths.json")["paths"] == [".partially-prepared-environment", ".prepared-environment"]
    (repository / "task.py").write_text("value = 3\n")
    fixed_tree = capture_worktree_tree(repository, ["."], artifacts,
        excluded_preparation_paths=read_json(artifacts / "preparation-created-untracked-paths.json")["paths"])
    assert git(repository, "diff", "--name-only", "HEAD", fixed_tree).decode().splitlines() == ["task.py"]


def test_role_only_preparation_failure_recovers_without_configuration_change(repository, tmp_path):
    (repository / "task.py").write_text("value = 2\n")
    marker = tmp_path / "dependency-ready"
    preparation = [sys.executable, "-c", f"from pathlib import Path; assert Path({str(marker)!r}).exists()"]
    run = tmp_path / "role-preparation-recovery"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"test", run_directory=run,
                    execution_plan={"fix": True, "preparation_commands": [preparation], "validation_commands": [[sys.executable, "-c", "pass"]]})
    config = fake_config(tmp_path)
    assert workflow.resume_review(run, reviewer_configs=[config, config])["status"] == "review_incomplete"
    assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == 2
    assert not list(run.glob("reviewer-*/attempt-*/model-invocation.json"))
    deadlines = {path: read_json(path) for path in run.glob("reviewer-*/role-deadline.json")}
    repeated = workflow.resume_review(run)
    assert repeated["status"] == "review_incomplete"
    assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == 4
    assert repeated["recovery"]["retry_not_before_epoch_ms"] > time.time() * 1000
    marker.touch()
    assert workflow.resume_review(run)["status"] == "ready_to_commit"
    assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == 6
    assert all(read_json(path) == deadline for path, deadline in deadlines.items())


def test_revocation_and_real_patch_entry_are_serialized(repository, tmp_path, monkeypatch):
    run = tmp_path / "patch-revocation"
    write_json(run / "run.json", {"status": "fix_pending"})
    (repository / "task.py").write_text("value = 2\n")
    patch = git(repository, "diff", "--binary", "--full-index")
    git(repository, "checkout", "--", "task.py")
    attempt = run / "fixer/attempt-001"
    attempt.mkdir(parents=True)
    (attempt / "fixer.patch").write_bytes(patch)
    actual_git = workflow.git
    write_started = False
    revocation_future = None
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    def inspect_apply(repo, *arguments, **kwargs):
        nonlocal write_started, revocation_future
        if arguments[:2] == ("apply", "-"):
            try:
                with exclusive_lock(run / "execution-revocation.lock", blocking=False):
                    pass
            except RuntimeError:
                write_started = True
            assert write_started, "补丁副作用入口必须持有撤销协调锁"
            assert kwargs.get("pass_fds"), "Git 必须继承协调锁以覆盖 controller 崩溃窗口"
            revocation_future = executor.submit(persist_execution_revocation, run, "真实补丁执行期间的 Stop")
            with pytest.raises(concurrent.futures.TimeoutError):
                revocation_future.result(timeout=0.1)
            assert not (run / "execution-revocation.json").exists()
        return actual_git(repo, *arguments, **kwargs)
    monkeypatch.setattr(workflow, "git", inspect_apply)
    monkeypatch.setattr(workflow, "_snapshot_fixer_progress", lambda *_: {"before": "before", "after": "after", "paths": ["task.py"], "requires_changes": True})
    monkeypatch.setattr(workflow, "_matches_tree", lambda _repo, commit, *_: commit == "before")
    record = {"repo": str(repository), "original_head": workflow.git_text(repository, "rev-parse", "HEAD"), "scope_paths": ["task.py"]}
    try:
        assert workflow._prepare_and_apply_fix(run, record, {"attempt_directory": str(attempt)})
        revocation_future.result(timeout=5)
        assert (repository / "task.py").read_text() == "value = 2\n"
    finally:
        executor.shutdown(wait=True)
    with pytest.raises(ExecutionRevoked):
        workflow._prepare_and_apply_fix(run, record, {"attempt_directory": str(attempt)})
