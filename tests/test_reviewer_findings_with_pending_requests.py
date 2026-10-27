"""发现、补证与完成事实分别持久化，续跑不能清除先前发现。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
sys.path.insert(0, str(SCRIPTS))
import check_review_result


@pytest.fixture(autouse=True)
def isolate_reviewer_findings_runtime_environment(tmp_path, monkeypatch):
    """清除调用者审查身份，将运行状态限定在本用例目录内。"""
    for name in tuple(os.environ):
        if name.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "rvf-home"))
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(tmp_path / "run-logs"))
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "user-local-data"))


def write_result(path, *arguments):
    return subprocess.run([sys.executable, str(SCRIPTS / "write_review_result.py"),
        arguments[0], "--out", str(path), *arguments[1:]], capture_output=True, text=True)


def test_findings_survive_request_response_and_explicit_completion(tmp_path):
    path = tmp_path / "review-result.json"
    issue = ("issue", "--path", "source.py", "--line", "1", "--kind", "REAL", "--severity", "medium", "--message")
    assert write_result(path, *issue, "第一条发现").returncode == 0
    assert write_result(path, "context-request", "--need", "file", "--reason", "缺少依赖源码").returncode == 0
    assert write_result(path, *issue, "不依赖材料的第二条发现").returncode == 0
    pending = check_review_result.classify(path)
    assert pending["valid"] and pending["kind"] == "request"
    assert pending["issue_count"] == 2 and pending["pending_request_count"] == 1
    assert pending["review_complete"] is False
    request_id = pending["requests"][0]["request_id"]
    assert write_result(path, "complete", "--audit-summary", "完整范围已查证").returncode != 0
    assert write_result(path, "respond-request", "--request-id", request_id,
        "--response", "依赖源码已保存到冻结证据入口").returncode == 0
    assert check_review_result.classify(path)["review_complete"] is False
    assert write_result(path, "resolve-request", "--request-id", request_id,
        "--resolution", "已读取补证并核实依赖").returncode == 0
    assert write_result(path, "complete", "--audit-summary", "完整范围已查证").returncode == 0
    complete = check_review_result.classify(path)
    assert complete["valid"] and complete["review_complete"] is True
    assert complete["kind"] == "issues" and complete["issue_count"] == 2
    result = json.loads(path.read_text())
    assert result["requests"][0]["response"] and result["requests"][0]["resolution"]
    assert write_result(path, "no-issues", "--audit-summary", "不能清除发现").returncode != 0


def test_unreadable_frozen_evidence_blocks_completion_without_hiding_findings(tmp_path):
    import hashlib
    index = tmp_path / "frozen-index.json"
    index.write_text(json.dumps({"units": [{"unit_id": "unit", "evidence_status": "verified",
        "patch": {"file": str(tmp_path / "missing-frozen-patch"), "sha256": "missing"}}]}))
    contract = tmp_path / "scope.contract.json"
    contract.write_text(json.dumps({"primary_units": ["unit"], "frozen_evidence_complete": True,
        "frozen_evidence_index": str(index), "frozen_evidence_index_sha256": hashlib.sha256(index.read_bytes()).hexdigest()}))
    path = tmp_path / "review-result.json"
    assert write_result(path, "issue", "--path", "source.py", "--line", "1", "--kind", "REAL",
        "--severity", "medium", "--message", "已经发现的问题").returncode == 0
    classified = check_review_result.classify(path, scope_contract=contract)
    assert classified["valid"] is True and classified["issue_count"] == 1
    assert classified["review_complete"] is False and classified["frozen_evidence_gaps"]


def test_reopened_request_reuses_content_identity_and_preserves_resolution_history(tmp_path):
    path = tmp_path / "review-result.json"
    request = ("context-request", "--need", "file", "--reason", "补证需要重新核实", "--target", "source.py")
    assert write_result(path, *request).returncode == 0
    request_id = check_review_result.classify(path)["requests"][0]["request_id"]
    assert write_result(path, "respond-request", "--request-id", request_id, "--response", "已保存原补证").returncode == 0
    assert write_result(path, "resolve-request", "--request-id", request_id, "--resolution", "已核实原补证").returncode == 0
    assert write_result(path, "complete", "--audit-summary", "已完成查证").returncode == 0
    assert write_result(path, *request).returncode == 0
    assert write_result(path, *request).returncode == 0
    reopened = check_review_result.classify(path)
    assert reopened["kind"] == "request" and reopened["pending_request_count"] == 1
    assert reopened["requests"][0]["request_id"] == request_id
    assert reopened["requests"][0]["resolution_history"][0]["resolution"] == "已核实原补证"
    assert reopened["review_complete"] is False


def test_readable_dependency_outside_repository_is_machine_evidence_not_new_permission(tmp_path, monkeypatch):
    from reviewer_leg_recovery import prepare_request_leg_recovery
    run_directory = tmp_path / "run"
    repository = tmp_path / "repository"
    repository.mkdir()
    path = run_directory / "artifacts/reviewers/alpha/review-result.json"
    context = tmp_path / "shared-dependency-evidence.txt"
    context.write_text("可只读核对的依赖证据")
    monkeypatch.setenv("RVF_RUN_DIR", str(run_directory))
    assert write_result(path, "context-request", "--need", "file", "--target", str(context),
        "--reason", "核对依赖上下文").returncode == 0
    reviewer = {"reviewer_id": "alpha"}
    assert prepare_request_leg_recovery(reviewer, path, repo=repository, run_directory=run_directory,
        skill_directory=SCRIPTS.parent) is True
    assert reviewer["recovery"]["conditions"][0]["material_observation"]["material_scope_relation"] == "dependency_context_only"
    classified = check_review_result.classify(path)
    assert classified["requests"][0]["status"] == "responded" and classified["review_complete"] is False
