"""多轮补证跨进程恢复时保留原件、已有发现和已完成的评审腿。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_task_snapshot_review_workflow import ROOT, canary, prepare, repository
from core.task_snapshot_review.persistent_artifacts import read_json, write_json


def reviewer_with_two_context_requests(tmp_path: Path) -> Path:
    program = tmp_path / "reviewer-with-two-context-requests.py"
    program.write_text(
        "import json,os,pathlib\n"
        "request=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "leg=pathlib.Path(request['input_file']).parent.parent.name\n"
        "result={'status':'completed','audit_summary':'已独立检查固定候选','findings':[]}\n"
        "if leg=='reviewer-2':\n"
        " response=pathlib.Path(request['context_response_file']).read_text() if request.get('context_response_file') else None\n"
        " if response is None:\n"
        "  result.update(status='incomplete',findings=[{'problem_key':'first-finding','message':'已确认的第一个问题'}],requests=['需要第一份机器证据'])\n"
        " elif response=='第一份机器证据\\n':\n"
        "  result.update(status='incomplete',findings=[{'problem_key':'second-finding','message':'继续检查发现的第二个问题'}],requests=['需要第二份机器证据'])\n"
        " else:\n"
        "  assert response=='第二份机器证据\\n'\n"
        "print(json.dumps(result,ensure_ascii=False))\n"
    )
    config = tmp_path / "reviewer-with-two-context-requests.json"
    write_json(config, {"command": [sys.executable, str(program)], "output_format": "text"})
    return config


def resume_in_new_controller_process(run: Path, tmp_path: Path, *arguments: str):
    entry = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py"
    completed = subprocess.run(
        [sys.executable, str(entry), "resume-task-review", "--run-dir", str(run), *arguments],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "RVF_USER_LOCAL_DATA_ROOT": str(tmp_path / "isolated-task-review-state")},
    )
    return completed, json.loads(completed.stdout) if completed.stdout.strip() else None


def start_two_context_request_review(repository: Path, tmp_path: Path):
    run = prepare(repository, tmp_path)
    config = reviewer_with_two_context_requests(tmp_path)
    before = canary(repository)
    execution, result = resume_in_new_controller_process(
        run, tmp_path, "--reviewer-config", str(config), "--reviewer-config", str(config),
    )
    assert execution.returncode == 1, execution.stderr
    assert result["status"] == "review_incomplete"
    assert len(list(run.glob("reviewer-*/attempt-*"))) == 2
    assert read_json(run / "reviewer-2/attempt-001/result.json")["requests"] == ["需要第一份机器证据"]
    assert not (run / "validation.json").exists()
    source_response = tmp_path / "mutable-machine-context-source.md"
    source_response.write_text("第一份机器证据\n")
    execution, result = resume_in_new_controller_process(
        run, tmp_path, "--context-response-file", str(source_response),
    )
    assert execution.returncode == 1, execution.stderr
    assert result["status"] == "review_incomplete"
    first_response_request = read_json(run / "reviewer-2/attempt-002/request.json")
    assert read_json(run / "reviewer-2/attempt-002/result.json")["requests"] == ["需要第二份机器证据"]
    assert Path(first_response_request["context_response_file"]).read_bytes() == source_response.read_bytes()
    assert not (run / "validation.json").exists()
    assert canary(repository) == before
    return run, source_response, first_response_request, before


def test_two_context_responses_survive_restarts_without_rerunning_completed_reviewer(repository, tmp_path):
    run, source_response, first_request, before = start_two_context_request_review(repository, tmp_path)
    healthy_outcome = run / "reviewer-1/attempt-001/outcome.json"
    healthy_outcome_bytes = healthy_outcome.read_bytes()
    first_original = Path(first_request["context_response_file"])
    first_bytes = first_original.read_bytes()
    previous_requests_and_outcomes = {
        path: path.read_bytes()
        for path in run.glob("reviewer-*/attempt-*/*.json")
        if path.name in {"request.json", "outcome.json", "result.json"}
    }

    source_response.write_text("第二份机器证据\n")
    execution, result = resume_in_new_controller_process(
        run, tmp_path, "--context-response-file", str(source_response),
    )

    assert execution.returncode == 0, execution.stderr
    assert result["status"] == "ready_for_fix"
    assert first_original.read_bytes() == first_bytes
    second_request = read_json(run / "reviewer-2/attempt-003/request.json")
    second_original = Path(second_request["context_response_file"])
    assert second_original != first_original
    assert second_original.read_bytes() == "第二份机器证据\n".encode()
    for request in (first_request, second_request):
        assert hashlib.sha256(Path(request["context_response_file"]).read_bytes()).hexdigest() == request["context_response_sha256"]
    assert {finding["problem_key"] for finding in read_json(run / "findings.json")["findings"]} == {
        "first-finding", "second-finding",
    }
    assert not (run / "validation.json").exists()
    assert all(path.read_bytes() == original for path, original in previous_requests_and_outcomes.items())
    for _ in range(2):
        execution, result = resume_in_new_controller_process(run, tmp_path)
        assert execution.returncode == 0, execution.stderr
        assert result["status"] == "ready_for_fix"
    assert len(list((run / "reviewer-1").glob("attempt-*"))) == 1
    assert len(list((run / "reviewer-2").glob("attempt-*"))) == 3
    assert healthy_outcome.read_bytes() == healthy_outcome_bytes
    assert canary(repository) == before


@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_damaged_context_response_original_stops_before_redispatch(repository, tmp_path, damage):
    run, source_response, first_request, before = start_two_context_request_review(repository, tmp_path)
    original = Path(first_request["context_response_file"])
    original_bytes = original.read_bytes()
    if damage == "changed":
        original.write_text("被改写的机器证据\n")
    else:
        original.unlink()
    attempts_before = sorted(run.glob("reviewer-*/attempt-*"))

    execution, _ = resume_in_new_controller_process(run, tmp_path)

    assert execution.returncode == 2
    assert "补证原件" in execution.stderr
    assert sorted(run.glob("reviewer-*/attempt-*")) == attempts_before
    assert not (run / "validation.json").exists()
    assert canary(repository) == before

    run_before_replacement = read_json(run / "run.json")
    source_response.write_text("第二份机器证据\n")
    execution, _ = resume_in_new_controller_process(
        run, tmp_path, "--context-response-file", str(source_response),
    )
    assert execution.returncode == 2
    assert "补证原件" in execution.stderr
    assert sorted(run.glob("reviewer-*/attempt-*")) == attempts_before
    assert read_json(run / "run.json") == run_before_replacement
    assert canary(repository) == before

    if original.exists():
        (tmp_path / "preserved-damaged-context-original.md").write_bytes(original.read_bytes())
    assert hashlib.sha256(original_bytes).hexdigest() == first_request["context_response_sha256"]
    original.write_bytes(original_bytes)
    source_response.write_text("第二份机器证据\n")
    execution, result = resume_in_new_controller_process(
        run, tmp_path, "--context-response-file", str(source_response),
    )
    assert execution.returncode == 0, execution.stderr
    assert result["status"] == "ready_for_fix"
    assert len(list((run / "reviewer-1").glob("attempt-*"))) == 1
    assert len(list((run / "reviewer-2").glob("attempt-*"))) == 3
    assert canary(repository) == before
