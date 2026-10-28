"""真实限时审查复现后的恢复、完整树复用及 HEAD 合并验收。"""
import subprocess
import sys
import os
import signal
import time
from pathlib import Path

import pytest

from test_task_snapshot_review_workflow import repository, fake_config
from test_automatic_task_review_progression import automatic_snapshot
from core.task_snapshot_review.persistent_artifacts import git, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import resume_review


def test_failed_kanban_registration_reuses_frozen_binding_on_plain_resume(repository, tmp_path, monkeypatch, capsys):
    from adapters.cline_kanban.owned_command_execution import start_managed_review
    scripts = Path(__file__).resolve().parents[1] / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    import rvf_command_line_shortcut_entrypoint as cli
    monkeypatch.setattr(cli, "schedule_pending_issue_delivery", lambda *args: None)
    monkeypatch.delenv("KANBAN_COMMAND_EXECUTION_ID", raising=False)
    run = automatic_snapshot(repository, tmp_path)
    available = tmp_path / "kanban-supported"
    executable = tmp_path / "kanban-registration-fixture"
    executable.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport json,sys\nif not Path({str(available)!r}).exists(): sys.exit(1)\nprint(json.dumps({{'ok':True,'execution':{{'executionId':'fixture-execution'}}}}))\n")
    executable.chmod(0o755)
    with pytest.raises(RuntimeError, match="未获受理"):
        start_managed_review(run, "fixture-task", project_path=repository, kanban_cli=executable, entrypoint=scripts / "rvf_command_line_shortcut_entrypoint.py")
    available.touch()
    previous_handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        assert cli.main(["resume-task-review", "--run-dir", str(run)]) == 0
    finally:
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)
    assert "managed_execution_started" in capsys.readouterr().out
    assert len(list((run / "kanban-command-execution-registration-attempts").glob("attempt-*.json"))) == 2
    assert not list(run.glob("reviewer-*/attempt-*"))


@pytest.mark.parametrize("ignore_sigterm", [False, True])
def test_exited_group_leader_does_not_hide_observed_owned_descendant(tmp_path, ignore_sigterm):
    from core.task_snapshot_review.execution_lifecycle import register_owned_process, observe_owned_process_group_descendants, revoke_execution, process_start_identity
    run = tmp_path / "orphan-revocation"
    write_json(run / "run.json", {"status": "review_pending"})
    child_pid = tmp_path / "child-pid"
    child_ready = tmp_path / "child-ready"
    release = tmp_path / "release-parent"
    unexpected_work = tmp_path / "work-after-stop"
    child_program = f"from pathlib import Path; import time,signal; {'signal.signal(signal.SIGTERM, signal.SIG_IGN); ' if ignore_sigterm else ''}Path({str(child_ready)!r}).touch(); time.sleep(8); Path({str(unexpected_work)!r}).touch()"
    parent_program = f"import subprocess,sys,time; from pathlib import Path; child=subprocess.Popen([sys.executable,'-c',{child_program!r}]); Path({str(child_pid)!r}).write_text(str(child.pid));\nwhile not Path({str(release)!r}).exists(): time.sleep(0.01)"
    parent = subprocess.Popen([sys.executable, "-c", parent_program], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not child_pid.exists() or not child_ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        register_owned_process(run, parent.pid, "final_validation", owns_process_group=True)
        observe_owned_process_group_descendants(run, parent.pid)
        descendant_pid = int(child_pid.read_text())
        descendant_identity = process_start_identity(descendant_pid)
        assert any(item["pid"] == descendant_pid for item in read_json(run / "owned-processes.json")["processes"])
        release.touch()
        assert parent.wait(timeout=5) == 0
        cancellation = revoke_execution(run, "验证已观察的孤儿取消")
        assert any(item["pid"] == descendant_pid for item in cancellation["processes"])
        deadline = time.monotonic() + 4
        while process_start_identity(descendant_pid) == descendant_identity and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process_start_identity(descendant_pid) != descendant_identity
        assert not unexpected_work.exists()
    finally:
        try:
            os.killpg(parent.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        parent.wait(timeout=5)


def test_new_credentials_change_configuration_evidence_without_storing_secret(tmp_path):
    from core.task_snapshot_review.workflow import configuration_digest, persistent_configuration
    credentials = tmp_path / "native-credentials.json"
    credentials.write_text("第一份凭据")
    config = {"command": [sys.executable, "-c", "pass"], "credential_evidence_paths": [str(credentials)]}
    first = configuration_digest(config)
    credentials.write_text("已刷新凭据")
    assert configuration_digest(config) != first
    assert "已刷新凭据" not in str(persistent_configuration(config))


def test_changed_shared_facts_resume_only_the_incomplete_reviewer(repository, tmp_path):
    (repository / "task.py").write_text("value = 2\n")
    marker = tmp_path / "machine-ready"
    run = tmp_path / "shared-fact-recovery"
    collect = [sys.executable, "-c", f"from pathlib import Path; assert Path({str(marker)!r}).exists()"]
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"resume incomplete leg", run_directory=run,
        execution_plan={"fix": True, "machine_evidence_commands": [collect], "validation_commands": [[sys.executable, "-c", "pass"]]})
    script = tmp_path / "fact-aware-reviewer.py"
    script.write_text("import json,os\nfrom pathlib import Path\nr=json.loads(Path(os.environ['RVF_ROLE_INPUT']).read_text()); e=json.loads(Path(r['shared_machine_evidence']).read_text())\nincomplete=Path(r['input_file']).parent.parent.name=='reviewer-2' and e['status']=='failed'\nprint(json.dumps({'status':'incomplete' if incomplete else 'completed','audit_summary':'独立审查并核对机器事实','findings':[], **({'requests':['等待机器验证证据']} if incomplete else {})}))\n")
    config = {"command": [sys.executable, str(script)], "output_format": "text"}
    assert resume_review(run, reviewer_configs=[config, config])["status"] == "review_incomplete"
    marker.touch()
    assert resume_review(run)["status"] == "ready_to_commit"
    assert len(list((run / "reviewer-1").glob("attempt-*"))) == 1
    assert len(list((run / "reviewer-2").glob("attempt-*"))) == 2


def test_missing_process_identity_preserves_unknown_without_aborting_authorized_work(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review import execution_lifecycle
    def unavailable(_pid):
        raise PermissionError("沙箱禁止 ps")
    monkeypatch.setattr(execution_lifecycle, "process_start_identity", unavailable)
    run = automatic_snapshot(repository, tmp_path, commands=[[sys.executable, "-c", "pass"]])
    result = resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)])
    assert result["status"] == "ready_to_commit"
    controller = next(item for item in read_json(run / "owned-processes.json")["processes"] if item["purpose"] == "workflow_controller")
    assert controller["start_and_command"] is None
    assert controller["identity_evidence_unavailable"]


def test_failed_shared_preparation_resumes_in_new_attempt_after_dependency_recovers(repository, tmp_path):
    (repository / "task.py").write_text("value = 2\n")
    marker = tmp_path / "dependency-ready"
    run = tmp_path / "recover-machine-evidence"
    preparation = [sys.executable, "-c", f"from pathlib import Path; assert Path({str(marker)!r}).exists()"]
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"recover same run", run_directory=run,
                    execution_plan={"fix": True, "preparation_commands": [preparation], "machine_evidence_commands": [[sys.executable, "-c", "pass"]], "validation_commands": [[sys.executable, "-c", "pass"]]})
    config = fake_config(tmp_path)
    assert resume_review(run, reviewer_configs=[config, config])["disposition"] == "machine_evidence_pending"
    marker.touch()
    assert resume_review(run)["status"] == "ready_to_commit"
    assert (run / "machine-evidence/attempt-002/evidence.json").exists()


def test_same_tested_tree_is_revalidated_without_dependency_identity_proof(repository, tmp_path):
    counter = tmp_path / "validation-invocations"
    command = [sys.executable, "-c", f"from pathlib import Path; p=Path({str(counter)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]
    run = automatic_snapshot(repository, tmp_path, commands=[command])
    config = fake_config(tmp_path)
    assert resume_review(run, reviewer_configs=[config, config])["status"] == "ready_to_commit"
    git(repository, "commit", "--only", "-qm", "fix: 核验提交后收尾", "--", "task.py")
    assert resume_review(run)["status"] == "completed"
    assert resume_review(run)["status"] == "completed"
    # 相同源码不能证明外部依赖未变；每次执行资格检查保留独立的当前验证。
    assert counter.read_text() == "xxx"
    assert len(list(run.glob("final-validation/attempt-*"))) == 3


def test_validation_repair_resumes_incomplete_tree_instead_of_original_red_tree(repository, tmp_path):
    run = automatic_snapshot(repository, tmp_path, commands=[[sys.executable, "-c", "from task import value; assert value == 4"]])
    partial = tmp_path / "partial-validation-repair.py"
    partial.write_text("from pathlib import Path\nimport json\nPath('task.py').write_text('value = 3\\n')\nprint(json.dumps({'status':'incomplete','dispositions':[]}))\n")
    config = fake_config(tmp_path)
    assert resume_review(run, reviewer_configs=[config, config], fixer_config={"command": [sys.executable, str(partial)], "output_format": "text"})["status"] == "fix_incomplete"
    complete = tmp_path / "complete-validation-repair.py"
    complete.write_text("from pathlib import Path\nimport json\nassert Path('task.py').read_text() == 'value = 3\\n'\nPath('task.py').write_text('value = 4\\n')\nprint(json.dumps({'status':'completed','dispositions':[]}))\n")
    assert resume_review(run, fixer_config={"command": [sys.executable, str(complete)], "output_format": "text"})["status"] == "ready_to_commit"
    assert (repository / "task.py").read_text() == "value = 4\n"


def test_safe_external_head_hunk_is_preserved_before_fix_application(repository, tmp_path, monkeypatch):
    import core.task_snapshot_review.workflow as workflow
    path = repository / "task.py"
    original = "value = 1\n" + "# 独立上下文\n" * 40 + "external_value = 1\n"
    path.write_text(original)
    git(repository, "commit", "--only", "-qm", "test: 初始化分离 hunk", "--", "task.py")
    path.write_text(original.replace("value = 1\n", "value = 2\n", 1))
    run = tmp_path / "head-before-application"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"repair first line", run_directory=run,
                    execution_plan={"fix": True, "validation_commands": [[sys.executable, "-c", "from task import value,external_value; assert (value,external_value)==(3,2)"]]})
    actual_apply = workflow._prepare_and_apply_fix
    def advance_head(*args):
        path.write_text(original.replace("external_value = 1", "external_value = 2"))
        git(repository, "commit", "--only", "-qm", "test: 外部 HEAD 独立前移", "--", "task.py")
        path.write_text(path.read_text().replace("value = 1\n", "value = 2\n", 1))
        return actual_apply(*args)
    monkeypatch.setattr(workflow, "_prepare_and_apply_fix", advance_head)
    fixer = tmp_path / "hunk-fixer.py"
    fixer.write_text("import json,os\nfrom pathlib import Path\nr=json.loads(Path(os.environ['RVF_ROLE_INPUT']).read_text())\np=Path('task.py'); p.write_text(p.read_text().replace('value = 2\\n','value = 3\\n',1))\nprint(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'fixed','reason':'首行修复'} for f in r['findings']]}))\n")
    config = fake_config(tmp_path, findings=[{"message": "首行需要修复", "path": "task.py"}])
    reviewer = tmp_path / "reviewer.py"
    reviewer.write_text(reviewer.read_text().replace(".read_text() == 'value = 2\\n'", ".read_text().startswith('value = 2\\n')"))
    assert resume_review(run, reviewer_configs=[config, config], fixer_config={"command": [sys.executable, str(fixer)], "output_format": "text"})["status"] == "ready_to_commit"
    assert path.read_text().startswith("value = 3\n") and path.read_text().endswith("external_value = 2\n")
