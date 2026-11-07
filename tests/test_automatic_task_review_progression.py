"""一次启动、验证修复、持久预算与无交付权限的评审结果验收。"""
import sys

import pytest

from test_task_snapshot_review_workflow import repository, fake_config
from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import resume_review
from core.task_snapshot_review.execution_lifecycle import normalized_execution_plan


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
    return {"command": [sys.executable, str(script)], "output_format": "text"}


def test_empty_findings_validation_failure_repairs_actual_tested_commit_automatically(repository, tmp_path):
    run = automatic_snapshot(repository, tmp_path)
    (repository / "background.txt").write_text("背景 staged\n")
    git(repository, "add", "background.txt")
    (repository / "background.txt").write_text("背景 unstaged\n")
    result = resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)], fixer_config=validation_fixer(tmp_path))
    assert result["status"] == "review_and_validation_completed"
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
    assert resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)])["status"] == "review_and_validation_completed"
    assert not (run / "fixer").exists()
    assert resume_review(run)["status"] == "review_and_validation_completed"
    assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == 2


def test_repair_budget_survives_resume_and_configuration_changes(repository, tmp_path):
    run = automatic_snapshot(repository, tmp_path, maximum_rounds=3)
    fixer = validation_fixer(tmp_path, changes=False)
    result = resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)], fixer_config=fixer)
    assert result["disposition"] == "agent_judgment_required"
    assert read_json(run / "run.json")["semantic_repair_rounds_used"] == 3
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 3
    result = resume_review(run, fixer_config={**fixer, "env_unset": ["UNRELATED_ENVIRONMENT"]})
    assert result["disposition"] == "agent_judgment_required"
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 3


@pytest.mark.parametrize("impact,requires_judgment", [("none", False), ("rollback", True), ("broad_rewrite", True)])
def test_elevation_returns_bound_review_facts_without_delivery_authority(repository, tmp_path, impact, requires_judgment):
    run = automatic_snapshot(repository, tmp_path, commands=[[sys.executable, "-c", "pass"]])
    original_head = git_text(repository, "rev-parse", "HEAD")
    config = fake_config(tmp_path, findings=[{"message": "任务关联观察", "path": "task.py"}])
    result = resume_review(run, reviewer_configs=[config, config], fixer_config=validation_fixer(tmp_path, changes=False, elevation_impact=impact))
    facts = result["task_snapshot_review_result_facts"]
    assert facts["elevations"][0]["elevation_impact"] == impact
    assert facts["validation"] == read_json(run / "validation.json")
    record = read_json(run / "run.json")
    assert facts["candidate_commit"] == record.get("validated_candidate_commit", record["candidate_commit"])
    assert facts["validation"]["candidate_commit"] == facts["candidate_commit"]
    assert facts["scope_paths"] == ["task.py"]
    assert result["disposition"] == ("agent_judgment_required" if requires_judgment else "agent_review_ready")
    assert "closeout" not in result and "commit" not in result
    assert "automatic_commit_allowed" not in str(result)
    assert git_text(repository, "rev-parse", "HEAD") == original_head
    if not requires_judgment:
        assert result["status"] == "review_and_validation_completed"
        assert read_json(run / "run.json")["status"] == "ready_to_commit"
        git(repository, "commit", "--only", "-qm", "fix: 独立调用方提交已验证改动", "--", "task.py")
        result = resume_review(run)
        assert result["status"] == "review_and_validation_completed"
        assert "commit" not in result
        record = read_json(run / "run.json")
        assert record["status"] == "completed"
        assert record["commit"] == git_text(repository, "rev-parse", "HEAD")


def test_persistent_revocation_prevents_restart_dispatch(repository, tmp_path):
    run = automatic_snapshot(repository, tmp_path)
    write_json(run / "execution-revocation.json", {"revoked_at": 1, "reason": "用户 Stop"})
    assert resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)])["disposition"] == "cancelled"
    assert not list(run.glob("reviewer-*/attempt-*"))


def test_expired_role_deadline_preserves_history_and_allows_current_execution(repository, tmp_path):
    run = automatic_snapshot(repository, tmp_path, commands=[[sys.executable, "-c", "pass"]])
    historical_files = {}
    for role in ("reviewer-1", "reviewer-2"):
        path = run / role / "role-deadline.json"
        write_json(path, {"deadline_at": 1})
        historical_files[path] = path.read_bytes()
    config = fake_config(tmp_path)
    assert resume_review(run, reviewer_configs=[config, config])["status"] == "review_and_validation_completed"
    assert all(path.read_bytes() == original for path, original in historical_files.items())
    assert resume_review(run)["status"] == "review_and_validation_completed"
    assert len(list(run.glob("reviewer-*/attempt-*"))) == 2


def test_new_review_execution_plan_rejects_workflow_implementation_closeout():
    with pytest.raises(ValueError, match="未知字段"):
        normalized_execution_plan({"implementation_plan": ["主行为", "回归验证"]})


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
