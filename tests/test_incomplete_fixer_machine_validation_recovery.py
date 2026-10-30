"""机器缺证由执行器补验保留修复树，角色仍独立收束语义结论。"""
import sys
from pathlib import Path

import pytest

from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import resume_review
from test_task_snapshot_review_workflow import fake_config, repository


def machine_blocked_fixer(tmp_path, mode="machine", final_value=3):
    script = tmp_path / "machine-blocked-fixer.py"
    script.write_text(
        "import json,os,pathlib\n"
        "request=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "evidence=request.get('incomplete_fixer_machine_validation_evidence')\n"
        "pathlib.Path('task.py').write_text('value = 3\\n')\n"
        f"mode={mode!r}\n"
        "status='incomplete'\n"
        "if evidence:\n"
        " proof=json.loads(pathlib.Path(evidence).read_text())\n"
        " assert proof['tested_tree']\n"
        " assert pathlib.Path(proof['workspace']).joinpath('task.py').read_text() == 'value = 3\\n'\n"
        f" pathlib.Path('task.py').write_text('value = {final_value}\\n')\n"
        " status='incomplete' if mode=='always_machine' else 'completed'\n"
        "payload={'status':status,'dispositions':[{'finding_id':item['finding_id'],'status':'fixed','reason':'已修复，保留机器证据与语义判断'} for item in request['findings']]}\n"
        "if status=='incomplete' and mode!='semantic':payload['completion_blocker']='registered_machine_validation'\n"
        "print(json.dumps(payload))\n"
    )
    return {"command": [sys.executable, str(script)], "output_format": "text"}


def review_with_registered_validation(repository, tmp_path, expected_value=3):
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "machine-incomplete-review"
    command = [sys.executable, "-c", f"from pathlib import Path; assert Path('task.py').read_text() == 'value = {expected_value}\\n'"]
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements="修复实际任务并保全背景工作。".encode(),
        run_directory=run, execution_plan={"fix": True, "validation_commands": [command]})
    reviewer = fake_config(tmp_path, findings=[{"path": "task.py", "message": "值应符合最终验证"}])
    return run, reviewer


@pytest.mark.parametrize("expected_value", [3, 4])
def test_incomplete_machine_validation_is_collected_and_fixer_resumes_in_one_call(repository, tmp_path, expected_value):
    run, reviewer = review_with_registered_validation(repository, tmp_path, expected_value)
    (repository / "background.txt").write_text("背景 staged\n")
    git(repository, "add", "background.txt")
    (repository / "background.txt").write_text("背景 unstaged\n")
    result = resume_review(run, reviewer_configs=[reviewer, reviewer],
        fixer_config=machine_blocked_fixer(tmp_path, final_value=expected_value))
    assert result["status"] == "ready_to_commit"
    assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == 2
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 2
    assert read_json(run / "fixer/attempt-001/outcome.json")["status"] == "incomplete"
    evidence_path = read_json(run / "fixer/attempt-002/request.json")["incomplete_fixer_machine_validation_evidence"]
    evidence = read_json(Path(evidence_path))
    assert evidence["status"] == ("passed" if expected_value == 3 else "failed")
    assert evidence["tested_tree"] == git_text(repository, "rev-parse", evidence["tested_commit"] + "^{tree}")
    assert evidence["role_outcome_status"] == "incomplete"
    assert (repository / "background.txt").read_text() == "背景 unstaged\n"
    assert git_text(repository, "show", ":background.txt") == "背景 staged"


def test_machine_validation_recovery_is_bounded_across_resume(repository, tmp_path):
    run, reviewer = review_with_registered_validation(repository, tmp_path)
    fixer = machine_blocked_fixer(tmp_path, mode="always_machine")
    result = resume_review(run, reviewer_configs=[reviewer, reviewer], fixer_config=fixer)
    for _ in range(2):
        assert result["status"] == "fix_incomplete"
        result = resume_review(run)
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 4
    assert len(list(run.glob("incomplete-fixer-machine-validation/round-*/attempt-*/evidence.json"))) == 3
    assert read_json(run / "run.json")["semantic_repair_rounds_used"] == 1
    assert not (run / "fixer/role-deadline.json").exists()
    assert any(path.endswith("incomplete-fixer-machine-validation/round-001/evidence.json") for path in result["recovery"]["evidence_paths"])


def test_semantic_incomplete_does_not_request_machine_validation(repository, tmp_path):
    run, reviewer = review_with_registered_validation(repository, tmp_path)
    result = resume_review(run, reviewer_configs=[reviewer, reviewer], fixer_config=machine_blocked_fixer(tmp_path, mode="semantic"))
    assert result["status"] == "fix_incomplete"
    assert not (run / "incomplete-fixer-machine-validation").exists()
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 1


@pytest.mark.parametrize("status,blocker", [("completed", "registered_machine_validation"), ("incomplete", "run_other_commands")])
def test_invalid_machine_blocker_is_not_accepted(status, blocker):
    from core.task_snapshot_review.role_process_execution import validate_role_result
    with pytest.raises(ValueError, match="机器补证请求"):
        validate_role_result({"status": status, "completion_blocker": blocker, "dispositions": []}, "fixer", [])
