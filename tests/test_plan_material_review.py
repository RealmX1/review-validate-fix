"""计划审阅冻结、独立执行、精确恢复与停止均在隔离仓验证。"""
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

from core.plan_material_review import create_plan_material_review, observe_plan_material_review, resume_plan_material_review
from core.task_snapshot_review.execution_lifecycle import revoke_execution
from core.task_snapshot_review.persistent_artifacts import git, read_json, write_json


@pytest.fixture
def plan_review_fixture(tmp_path):
    repo = tmp_path / "context-repository"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "RVF隔离测试")
    git(repo, "config", "user.email", "rvf-test@example.invalid")
    (repo / "context.md").write_text("已有项目上下文。")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "test: 初始化计划审阅上下文")
    material = tmp_path / "implementation-plan.md"
    material.write_text("产品目标、程序设计、架构、实施切片与验收。")
    requirements = tmp_path / "requirements.md"
    requirements.write_text("明确需求与非目标。")
    scope = tmp_path / "review-scope.md"
    scope.write_text("只审阅计划可实施性，不执行实施或授权交付。")
    marker = tmp_path / "model-invocations.jsonl"
    reviewer = tmp_path / "independent-plan-reviewer.py"
    reviewer.write_text(
        "import json,os,pathlib,sys\n"
        "sys.stdin.read()\n"
        "request=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "binding=request['plan_material_binding']\n"
        "assert len(binding['sourceMaterialManifest']) == 3\n"
        "assert all(pathlib.Path(item['snapshotPath']).is_file() for item in binding['frozenMaterialManifest'])\n"
        f"with open({str(marker)!r},'a') as handle: handle.write('invoked\\n')\n"
        "print(json.dumps({'status':'completed','audit_summary':'独立核对了全部冻结计划、需求和范围。','findings':[]}))\n"
    )
    config = {"command": [sys.executable, str(reviewer)], "output_format": "text", "pre_run_health": False,
              "prompt_delivery": "stdin", "credential_evidence_paths": []}
    arguments = {"repo": repo, "material_files": [material], "requirements_file": requirements,
                 "scope_file": scope, "run_directory": tmp_path / "plan-review-run", "reviewer_config": config}
    return arguments, material, marker


def wait_for_review(run):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = resume_plan_material_review(run)
        if result["status"] == "completed":
            return result
        if result["status"] not in {"running", "prepared"}:
            pytest.fail(str(result))
        time.sleep(0.05)
    pytest.fail("隔离 reviewer 未在测试预算内退出")


def test_explicit_materials_are_frozen_and_same_request_recovers(plan_review_fixture):
    arguments, material, marker = plan_review_fixture
    prepared = create_plan_material_review(**arguments)
    assert prepared["status"] == "prepared"
    assert prepared["authority"] == "quality_facts_only"
    assert not marker.exists()
    manifest = prepared["sourceMaterialManifest"]
    expected = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert prepared["reviewedMaterialDigest"] == expected
    assert create_plan_material_review(**arguments)["reviewRunId"] == prepared["reviewRunId"]
    material.write_text("计划已改变，应保留旧审阅对象。")
    current = observe_plan_material_review(arguments["run_directory"])
    assert current["sourceMaterialsAreCurrent"] is False
    assert current["reviewedMaterialDigest"] == prepared["reviewedMaterialDigest"]
    assert not marker.exists()
    with pytest.raises(ValueError, match="原.*材料|不同|冲突"):
        create_plan_material_review(**arguments)


def test_review_uses_independent_process_and_replays_without_dispatch(plan_review_fixture):
    arguments, material, marker = plan_review_fixture
    create_plan_material_review(**arguments)
    result = wait_for_review(arguments["run_directory"])
    assert result["findings"] == []
    assert result["validationGaps"] == []
    assert result["sourceResultFiles"]
    assert result["completedReviewEvidence"]
    repeated = resume_plan_material_review(arguments["run_directory"])
    assert repeated["reviewRunId"] == result["reviewRunId"]
    assert marker.read_text().splitlines() == ["invoked"]
    observe_plan_material_review(arguments["run_directory"])
    assert marker.read_text().splitlines() == ["invoked"]
    material.write_text("审阅完成后的材料变化。")
    assert observe_plan_material_review(arguments["run_directory"])["sourceMaterialsAreCurrent"] is False
    assert observe_plan_material_review(arguments["run_directory"])["status"] == "completed"


def test_stop_prevents_original_and_late_resume(plan_review_fixture):
    arguments, _, marker = plan_review_fixture
    create_plan_material_review(**arguments)
    revoke_execution(arguments["run_directory"], "明确停止本次计划审阅。")
    assert resume_plan_material_review(arguments["run_directory"])["status"] == "stopped"
    assert observe_plan_material_review(arguments["run_directory"])["status"] == "stopped"
    assert not marker.exists()


def test_changed_frozen_original_cannot_be_accepted(plan_review_fixture):
    arguments, _, marker = plan_review_fixture
    create_plan_material_review(**arguments)
    record = read_json(arguments["run_directory"] / "run.json")
    Path(record["frozenMaterialManifest"][0]["snapshotPath"]).write_text("篡改冻结材料。")
    with pytest.raises(ValueError, match="冻结.*改变|冻结.*损坏"):
        resume_plan_material_review(arguments["run_directory"])
    assert not marker.exists()


def test_concurrent_resume_keeps_one_native_execution(plan_review_fixture):
    from concurrent.futures import ThreadPoolExecutor
    arguments, _, marker = plan_review_fixture
    create_plan_material_review(**arguments)
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda _: resume_plan_material_review(arguments["run_directory"]), range(2)))
    wait_for_review(arguments["run_directory"])
    assert marker.read_text().splitlines() == ["invoked"]


def test_observation_does_not_create_or_modify_runtime_files(plan_review_fixture):
    arguments, _, marker = plan_review_fixture
    create_plan_material_review(**arguments)
    run = arguments["run_directory"]
    # 模拟派发前已登记 request 但 worker 尚未取得锁；只读查询不补建执行锁。
    attempt = run / "independent-plan-reviewer" / "attempt-001"
    attempt.mkdir(parents=True)
    before = {str(path): (path.stat().st_mtime_ns, path.read_bytes()) for path in run.rglob("*") if path.is_file()}
    assert observe_plan_material_review(run)["status"] == "unknown"
    after = {str(path): (path.stat().st_mtime_ns, path.read_bytes()) for path in run.rglob("*") if path.is_file()}
    assert before == after
    assert not marker.exists()


def test_original_structured_stdout_restores_missing_result_without_second_model(plan_review_fixture):
    arguments, _, marker = plan_review_fixture
    create_plan_material_review(**arguments)
    wait_for_review(arguments["run_directory"])
    attempt = arguments["run_directory"] / "independent-plan-reviewer" / "attempt-001"
    payload = read_json(attempt / "result.json")
    request = read_json(attempt / "request.json")
    request["config"]["output_format"] = "codex_json"
    write_json(attempt / "request.json", request)
    (attempt / "stdout.txt").write_text(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}}) + "\n" +
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}) + "\n")
    (attempt / "result.json").unlink()
    (attempt / "outcome.json").unlink()
    (attempt / "role-output.json").unlink(missing_ok=True)
    recovered = resume_plan_material_review(arguments["run_directory"])
    assert recovered["status"] == "completed", recovered
    assert any(item["path"].endswith("reconciled-native-completion.json") for item in recovered["completedReviewEvidence"])
    stdout_evidence = next(item for item in recovered["completedReviewEvidence"] if item["path"].endswith("stdout.txt"))
    assert stdout_evidence["sha256"] == hashlib.sha256((attempt / "stdout.txt").read_bytes()).hexdigest()
    assert marker.read_text().splitlines() == ["invoked"]
    (attempt / "stdout.txt").write_text(json.dumps({"type": "turn.failed"}) + "\n")
    assert observe_plan_material_review(arguments["run_directory"])["status"] != "completed"
    resumed = resume_plan_material_review(arguments["run_directory"])
    assert resumed["status"] != "completed"
    assert marker.read_text().splitlines() == ["invoked"]


def test_reconciled_completion_rejects_different_clean_stdout_and_keeps_original_snapshot(plan_review_fixture):
    arguments, _, marker = plan_review_fixture
    create_plan_material_review(**arguments)
    wait_for_review(arguments["run_directory"])
    attempt = arguments["run_directory"] / "independent-plan-reviewer" / "attempt-001"
    payload = read_json(attempt / "result.json")
    request = read_json(attempt / "request.json")
    request["config"]["output_format"] = "codex_json"
    write_json(attempt / "request.json", request)
    original_stdout = (
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}}) + "\n" +
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}) + "\n")
    (attempt / "stdout.txt").write_text(original_stdout)
    (attempt / "result.json").unlink()
    (attempt / "outcome.json").unlink()
    (attempt / "role-output.json").unlink(missing_ok=True)
    recovered = resume_plan_material_review(arguments["run_directory"])
    assert recovered["status"] == "completed", recovered
    original_sha = hashlib.sha256(original_stdout.encode()).hexdigest()
    assert any(item["sha256"] == original_sha for item in recovered["completedReviewEvidence"])
    snapshot = attempt / "accepted-original-stdout.txt"
    assert snapshot.read_text() == original_stdout
    changed = {"status": "completed", "audit_summary": "另一份有效但不同的审阅。",
               "findings": [{"message": "这里存在新发现。"}]}
    (attempt / "stdout.txt").write_text(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(changed)}}) + "\n" +
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}) + "\n")
    observed = observe_plan_material_review(arguments["run_directory"])
    changed_sha = hashlib.sha256((attempt / "stdout.txt").read_bytes()).hexdigest()
    assert observed["status"] != "completed"
    assert observed["findings"] == []
    assert observed["auditSummary"] == payload["audit_summary"]
    assert observed["currentNativeStdoutMatchesAcceptedCompletion"] is False
    assert all(item["sha256"] != changed_sha for item in observed.get("completedReviewEvidence", []))
    assert any(item["sha256"] == original_sha for item in observed["acceptedNativeCompletionEvidence"])
    assert observed["historicalCompletion"]["status"] == "completed"
    assert any(item["sha256"] == original_sha for item in observed["historicalCompletion"]["completedReviewEvidence"])
    assert snapshot.read_bytes() == original_stdout.encode()
    assert read_json(attempt / "result.json")["findings"] == []
    assert not (attempt / "outcome.json").exists()
    resumed = resume_plan_material_review(arguments["run_directory"])
    assert resumed["status"] != "completed"
    assert resumed["historicalCompletion"]["status"] == "completed"
    assert any(item["sha256"] == original_sha for item in resumed["historicalCompletion"]["completedReviewEvidence"])
    assert all(item["sha256"] != changed_sha for item in resumed.get("completedReviewEvidence", []))
    saved = read_json(arguments["run_directory"] / "plan-material-review-result.json")
    assert saved["status"] != "completed"
    assert saved["historicalCompletion"]["status"] == "completed"
    assert marker.read_text().splitlines() == ["invoked"]
    assert snapshot.read_bytes() == original_stdout.encode()


def test_cli_prepare_observe_resume_and_cancel(plan_review_fixture):
    import subprocess
    arguments, _, marker = plan_review_fixture
    entrypoint = Path(__file__).resolve().parents[1] / "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py"
    config = arguments["run_directory"].parent / "reviewer-config.json"
    write_json(config, arguments["reviewer_config"])
    command = [sys.executable, str(entrypoint)]
    prepared = subprocess.run([*command, "review-plan-materials", "--repo", str(arguments["repo"]),
        "--material-file", str(arguments["material_files"][0]), "--requirements-file", str(arguments["requirements_file"]),
        "--scope-file", str(arguments["scope_file"]), "--run-directory", str(arguments["run_directory"]),
        "--reviewer-config", str(config), "--prepare-only"], capture_output=True, text=True, timeout=15)
    assert prepared.returncode == 0, prepared.stderr
    assert json.loads(prepared.stdout)["status"] == "prepared"
    observed = subprocess.run([*command, "observe-plan-material-review", "--run-dir", str(arguments["run_directory"])],
        capture_output=True, text=True, timeout=15)
    assert observed.returncode == 0, observed.stderr
    assert json.loads(observed.stdout)["reviewRunId"] == json.loads(prepared.stdout)["reviewRunId"]
    cancelled = subprocess.run([*command, "cancel-plan-material-review", "--run-dir", str(arguments["run_directory"]),
        "--reason", "已批准范围内的隔离停止验证。"], capture_output=True, text=True, timeout=15)
    assert cancelled.returncode == 0, cancelled.stderr
    assert json.loads(cancelled.stdout)["status"] == "stopped"
    assert not marker.exists()


def _wait_for_status(run, status):
    deadline = time.monotonic() + 20
    last = None
    while time.monotonic() < deadline:
        last = resume_plan_material_review(run)
        if last["status"] == status:
            return last
        if last["status"] not in {"running", "prepared"}:
            return last
        time.sleep(0.05)
    pytest.fail(str(last))


def test_cancel_persists_when_frozen_material_and_result_are_unreadable(plan_review_fixture):
    import os
    import subprocess
    arguments, _, _ = plan_review_fixture
    parent = arguments["run_directory"].parent
    pid_file = parent / "reviewer.pid"
    script = parent / "sleeping-reviewer.py"
    script.write_text(
        "import os,time,pathlib,sys\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    arguments["reviewer_config"] = {**arguments["reviewer_config"], "command": [sys.executable, str(script)]}
    create_plan_material_review(**arguments)
    resume_plan_material_review(arguments["run_directory"])
    deadline = time.monotonic() + 10
    pid = None
    while time.monotonic() < deadline:
        if pid_file.exists():
            pid = int(pid_file.read_text())
            owned = arguments["run_directory"] / "owned-processes.json"
            if owned.exists() and any(item.get("pid") == pid and item.get("is_active_registration") is True
                                       for item in read_json(owned).get("processes", [])):
                break
        time.sleep(0.05)
    else:
        pytest.fail("隔离 reviewer 尚未登记为所属进程")
    record = read_json(arguments["run_directory"] / "run.json")
    Path(record["frozenMaterialManifest"][0]["snapshotPath"]).write_text("损坏冻结原件")
    attempt = arguments["run_directory"] / "independent-plan-reviewer" / "attempt-001"
    (attempt / "result.json").write_text("{")
    entrypoint = Path(__file__).resolve().parents[1] / "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py"
    try:
        cancelled = subprocess.run([sys.executable, str(entrypoint), "cancel-plan-material-review",
            "--run-dir", str(arguments["run_directory"]), "--reason", "材料损坏后仍须停止。"],
            capture_output=True, text=True, timeout=15)
        assert cancelled.returncode == 0, cancelled.stderr
        assert json.loads(cancelled.stdout)["status"] == "stopped"
        assert (arguments["run_directory"] / "execution-revocation.json").is_file()
        gone = time.monotonic() + 5
        while time.monotonic() < gone:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("损坏材料后的取消没有回收原 reviewer")
    finally:
        try:
            os.kill(pid, 15)
        except (ProcessLookupError, TypeError):
            pass


def test_credential_change_uses_remaining_dispatch_budget(plan_review_fixture):
    arguments, _, marker = plan_review_fixture
    credential = arguments["run_directory"].parent / "credential.json"
    credential.write_text('{"token":"expired"}')
    script = arguments["run_directory"].parent / "credential-reviewer.py"
    script.write_text(
        "import json,os,pathlib,sys\n"
        "sys.stdin.read()\n"
        f"marker=pathlib.Path({str(marker)!r})\n"
        "count=marker.read_text().count('invoked') if marker.exists() else 0\n"
        "marker.write_text((marker.read_text() if marker.exists() else '')+'invoked\\n')\n"
        "if count==0:\n"
        "    sys.stderr.write('authentication failed\\n')\n"
        "    raise SystemExit(1)\n"
        "print(json.dumps({'status':'completed','audit_summary':'凭据恢复后完成独立审阅。','findings':[]}))\n"
    )
    arguments["reviewer_config"] = {**arguments["reviewer_config"], "command": [sys.executable, str(script)],
                                    "credential_evidence_paths": [str(credential)]}
    create_plan_material_review(**arguments)
    failed = _wait_for_status(arguments["run_directory"], "incomplete")
    assert failed["status"] == "incomplete", failed
    attempt = arguments["run_directory"] / "independent-plan-reviewer" / "attempt-001"
    outcome = read_json(attempt / "outcome.json")
    assert outcome["retryable"] is False
    assert outcome["recovery"]["category"] == "authentication"
    assert _wait_for_status(arguments["run_directory"], "incomplete")["status"] == "incomplete"
    assert not (attempt.parent / "attempt-002").exists()
    credential.write_text('{"token":"restored"}')
    recovered = _wait_for_status(arguments["run_directory"], "completed")
    assert recovered["status"] == "completed", recovered
    assert marker.read_text().splitlines() == ["invoked", "invoked"]
    assert read_json(attempt / "outcome.json")["retryable"] is False


def test_incomplete_result_keeps_semantic_gaps_and_result_file(plan_review_fixture):
    arguments, _, _ = plan_review_fixture
    script = arguments["run_directory"].parent / "incomplete-reviewer.py"
    script.write_text(
        "import json,sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'status':'incomplete','audit_summary':'依赖契约尚未核对。','findings':[],"
        "'validation_gaps':['缺少依赖 API 的版本与实际响应示例。']}))\n"
    )
    arguments["reviewer_config"] = {**arguments["reviewer_config"], "command": [sys.executable, str(script)]}
    create_plan_material_review(**arguments)
    result = _wait_for_status(arguments["run_directory"], "incomplete")
    assert result["status"] == "incomplete", result
    assert result["auditSummary"] == "依赖契约尚未核对。"
    assert result["validationGaps"] == ["缺少依赖 API 的版本与实际响应示例。"]
    assert result["sourceResultFiles"]
    assert result["sourceResultFiles"][0].endswith("result.json")


def test_inline_environment_without_recoverable_source_is_rejected(plan_review_fixture):
    arguments, _, marker = plan_review_fixture
    name = "PLAN_REVIEW_UNRESTORABLE_SETTING"
    arguments["reviewer_config"] = {**arguments["reviewer_config"], "env_set": {name: "enabled"}}
    with pytest.raises(ValueError, match="无法恢复"):
        create_plan_material_review(**arguments)
    assert not (arguments["run_directory"] / "run.json").exists()
    assert not marker.exists()


def test_configuration_file_environment_reaches_reviewer(plan_review_fixture):
    arguments, _, _ = plan_review_fixture
    observed = arguments["run_directory"].parent / "observed-environment.txt"
    script = arguments["run_directory"].parent / "environment-reviewer.py"
    name = "PLAN_REVIEW_ISOLATED_REQUIRED_SETTING"
    script.write_text(
        "import json,os,pathlib,sys\n"
        "sys.stdin.read()\n"
        f"pathlib.Path({str(observed)!r}).write_text(os.environ.get({name!r}) or '')\n"
        "print(json.dumps({'status':'completed','audit_summary':'已观察隔离环境变量。','findings':[]}))\n"
    )
    config_path = arguments["run_directory"].parent / "reviewer-with-environment.json"
    config = {**arguments["reviewer_config"], "command": [sys.executable, str(script)], "env_set": {name: "enabled"}}
    write_json(config_path, config)
    arguments["reviewer_config"] = {**config, "configuration_file_path": str(config_path)}
    create_plan_material_review(**arguments)
    result = wait_for_review(arguments["run_directory"])
    assert result["status"] == "completed"
    assert observed.read_text() == "enabled"
