"""一次启动、验证修复、持久预算与提交后收尾的行为验收。"""
import sys

import pytest

from test_task_snapshot_review_workflow import repository, fake_config
from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import resume_review


def automatic_snapshot(repository, tmp_path, *, commands=None, maximum_rounds=3):
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "automatic-review"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements="将值修复为 3，并保留背景改动。".encode(),
                    run_directory=run, execution_plan={"fix": True, "validation_commands": commands or [
                        [sys.executable, "-c", "from pathlib import Path; assert Path('task.py').read_text() == 'value = 3\\n'"]],
                        "maximum_semantic_repair_rounds": maximum_rounds})
    return run


def validation_fixer(tmp_path, *, changes=True, elevation_impact=None):
    script = tmp_path / "validation-repair-fixer.py"
    script.write_text("import json,os,pathlib,subprocess\n"
        "request=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "if request.get('validation_failure'):\n"
        " assert request['candidate_commit'] == request['validation_failure']['tested_commit']\n"
        " assert pathlib.Path(request['validation_failure']['attempt_directory']).is_dir()\n"
        + ("pathlib.Path('task.py').write_text('value = 3\\n')\n" if changes else "")
        + f"impact={elevation_impact!r}\n"
        "print(json.dumps({'status':'completed','dispositions':[{'finding_id':item['finding_id'], 'status':'elevated' if impact else 'fixed', 'reason':'可复核处置依据', **({'elevation_impact':impact} if impact else {})} for item in request['findings']]}))\n")
    return {"command": [sys.executable, str(script)], "output_format": "text", "timeout_seconds": 20}


def test_empty_findings_validation_failure_repairs_actual_tested_commit_automatically(repository, tmp_path):
    run = automatic_snapshot(repository, tmp_path)
    (repository / "background.txt").write_text("背景 staged\n")
    git(repository, "add", "background.txt")
    (repository / "background.txt").write_text("背景 unstaged\n")
    result = resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)], fixer_config=validation_fixer(tmp_path))
    assert result["status"] == "ready_to_commit"
    assert result["disposition"] == "agent_review_ready"
    assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == 2
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 1
    request = read_json(run / "fixer/attempt-001/request.json")
    application = read_json(run / "fixer/attempt-001/fix-application.json")
    assert application["before"] == request["validation_failure"]["tested_commit"]
    assert application["paths"] == ["task.py"]
    assert (repository / "background.txt").read_text() == "背景 unstaged\n"
    assert git_text(repository, "show", ":background.txt") == "背景 staged"


def test_no_finding_passing_validation_skips_fixer_and_resume_uses_frozen_material(repository, tmp_path):
    run = automatic_snapshot(repository, tmp_path, commands=[[sys.executable, "-c", "pass"]])
    assert resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)])["status"] == "ready_to_commit"
    assert not (run / "fixer").exists()
    assert resume_review(run)["status"] == "ready_to_commit"
    assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == 2


def test_repair_budget_survives_resume_and_configuration_changes(repository, tmp_path):
    run = automatic_snapshot(repository, tmp_path, maximum_rounds=3)
    fixer = validation_fixer(tmp_path, changes=False)
    result = resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)], fixer_config=fixer)
    assert result["disposition"] == "agent_judgment_required"
    assert read_json(run / "run.json")["semantic_repair_rounds_used"] == 3
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 3
    result = resume_review(run, fixer_config={**fixer, "timeout_seconds": 21})
    assert result["disposition"] == "agent_judgment_required"
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 3


@pytest.mark.parametrize("impact,allowed", [("none", True), ("rollback", False), ("broad_rewrite", False)])
def test_elevation_commit_eligibility_and_completed_recap(repository, tmp_path, impact, allowed):
    run = automatic_snapshot(repository, tmp_path, commands=[[sys.executable, "-c", "pass"]])
    config = fake_config(tmp_path, findings=[{"message": "任务关联观察", "path": "task.py"}])
    result = resume_review(run, reviewer_configs=[config, config], fixer_config=validation_fixer(tmp_path, changes=False, elevation_impact=impact))
    assert result["closeout"]["automatic_commit_allowed"] is allowed
    if allowed:
        git(repository, "commit", "--only", "-qm", "fix: 验证提交后收尾", "--", "task.py")
        result = resume_review(run)
        assert result["status"] == "completed"
        assert result["closeout"]["implementation_plan_completion"] == "unknown"
        assert result["closeout"]["work_recap"]
        assert result["closeout"]["commit"] == git_text(repository, "rev-parse", "HEAD")


def test_persistent_revocation_prevents_restart_dispatch(repository, tmp_path):
    run = automatic_snapshot(repository, tmp_path)
    write_json(run / "execution-revocation.json", {"revoked_at": 1, "reason": "用户 Stop"})
    assert resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)])["disposition"] == "cancelled"
    assert not list(run.glob("reviewer-*/attempt-*"))


def test_expired_role_deadline_does_not_create_fallback_attempts(repository, tmp_path):
    from core.task_snapshot_review.workflow import _start_role
    run = automatic_snapshot(repository, tmp_path)
    role_directory = run / "reviewer-1"
    role_directory.mkdir()
    write_json(role_directory / "role-deadline.json", {"deadline_at": 1})
    config = fake_config(tmp_path)
    result, process = _start_role(run, read_json(run / "run.json"), role_directory, "reviewer", [config], [])
    assert result is None and process is None
    assert not list(role_directory.glob("attempt-*"))


def test_completed_closeout_covers_every_frozen_implementation_item(repository, tmp_path):
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "implementation-plan-review"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements="完成两项行为并展示收尾。".encode(), run_directory=run,
        execution_plan={"fix": True, "validation_commands": [[sys.executable, "-c", "pass"]], "implementation_plan": ["主行为", "回归验证"]})
    assert resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)])["status"] == "ready_to_commit"
    report = tmp_path / "closeout-report.json"
    write_json(report, {"work_recap": "已实现主行为并完成回归验证", "implementation_items": [{"description": "主行为", "status": "completed", "evidence": "task.py"}]})
    with pytest.raises(ValueError, match="全部条目"):
        resume_review(run, closeout_report_file=report)
    write_json(report, {"work_recap": "已实现主行为并完成回归验证", "implementation_items": [
        {"description": item, "status": "completed", "evidence": "隔离行为测试通过"} for item in ["主行为", "回归验证"]]})
    git(repository, "commit", "--only", "-qm", "feat: 实施与收尾", "--", "task.py")
    result = resume_review(run, closeout_report_file=report)
    assert result["status"] == "completed"
    assert result["closeout"]["implementation_plan_completion"] == "complete"
    assert result["closeout"]["work_recap"]["summary"] == "已实现主行为并完成回归验证"


def test_unchanged_active_run_and_fixer_evolution_do_not_repeat_hook_decision(repository, tmp_path, monkeypatch):
    from adapters import resolve_hook_host_adapter
    from core.task_snapshot_review.hook_entrypoint import handle_hook
    monkeypatch.setenv("KANBAN_TASK_ID", "isolated-task")
    run = automatic_snapshot(repository, tmp_path)
    assert handle_hook({"cwd": str(repository)}, event_name="Stop", host_adapter=resolve_hook_host_adapter("codex")) is None
    resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)], fixer_config=validation_fixer(tmp_path))
    assert handle_hook({"cwd": str(repository)}, event_name="Stop", host_adapter=resolve_hook_host_adapter("codex")) is None
    (repository / "new-unreviewed-task.py").write_text("new_behavior = True\n")
    assert handle_hook({"cwd": str(repository)}, event_name="Stop", host_adapter=resolve_hook_host_adapter("codex"))
