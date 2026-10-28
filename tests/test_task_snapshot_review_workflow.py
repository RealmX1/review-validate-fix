"""固定快照、独立上下文、有界恢复与主工作树保全的行为验收。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import resume_review


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "isolated-codex-home"))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(tmp_path / "isolated-codex-sqlite-home"))
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "RVF test")
    git(repo, "config", "user.email", "rvf-test@example.invalid")
    (repo / "task.py").write_text("value = 1\n")
    (repo / "background.txt").write_text("original\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "test: 初始化隔离验收仓库")
    return repo


def canary(repo):
    return (git(repo, "diff", "--binary", "--full-index", "HEAD"),
            git(repo, "diff", "--cached", "--binary", "--full-index"),
            git(repo, "ls-files", "--others", "--exclude-standard", "-z"))


def fake_config(tmp_path, *, findings=None, fail_first=False, malformed=False, fixer=False):
    script = tmp_path / ("fixer.py" if fixer else "reviewer.py")
    script.write_text(
        "import json, os, pathlib, sys\n"
        "prompt = sys.stdin.read()\n"
        "assert 'RVF_DECISION_TOKEN' not in os.environ\n"
        "assert os.environ['RVF_SUPPRESS_STOP_HOOK'] == '1'\n"
        "request = json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "assert pathlib.Path('task.py').read_text() == 'value = 2\\n'\n"
        "assert pathlib.Path('background.txt').read_text() == 'original\\n'\n"
        + ("assert 'reviewer' not in json.dumps(request['findings']).lower()\n"
           "pathlib.Path('task.py').write_text('value = 3\\n')\n"
           "result = {'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'fixed','reason':'已复现并修复'} for f in request['findings']]}\n"
           if fixer else
           f"result = {{'status':'completed','findings':{findings or []!r},'audit_summary':'已检查固定快照及边界条件'}}\n")
        + ("marker=pathlib.Path(os.environ['RVF_ROLE_INPUT']).parent.parent/'first-failure'\n"
           "if not marker.exists():\n marker.write_text('failed'); print('transient failure',file=sys.stderr); sys.exit(7)\n" if fail_first else "")
        + ("print('malformed result')\n" if malformed else "print(json.dumps(result,ensure_ascii=False))\n")
    )
    config = {"command": [sys.executable, str(script)], "output_format": "text", "timeout_seconds": 20}
    return config


def prepare(repository, tmp_path):
    (repository / "task.py").write_text("value = 2\n")
    (repository / "background.txt").write_text("staged background\n")
    git(repository, "add", "background.txt")
    (repository / "background.txt").write_text("unstaged background\n")
    (repository / "background-untracked.txt").write_text("untracked background\n")
    run = tmp_path / "persistent-review-run"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements="用户原话：修复任务。".encode(), run_directory=run)
    return run


def test_fixed_snapshot_preserves_all_background_wip_and_resolves_baseline_once(repository, tmp_path):
    run = prepare(repository, tmp_path)
    original = canary(repository)
    record = read_json(run / "run.json")
    assert git_text(repository, "show", f"{record['candidate_commit']}:task.py") == "value = 2"
    assert git_text(repository, "show", f"{record['candidate_commit']}:background.txt") == "original"
    assert record["changed_paths"] == ["task.py"]
    assert canary(repository) == original
    assert (run / "requirements.md").read_text() == "用户原话：修复任务。"


def test_two_independent_reviewers_then_resume_does_not_rerun_completed_work(repository, tmp_path):
    run = prepare(repository, tmp_path)
    original = canary(repository)
    result = resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)])
    assert result["status"] == "ready_for_validation"
    attempts = sorted(run.glob("reviewer-*/attempt-*/outcome.json"))
    assert len(attempts) == 2
    resume_review(run)
    assert sorted(run.glob("reviewer-*/attempt-*/outcome.json")) == attempts
    assert canary(repository) == original


def test_failed_leg_uses_the_other_primary_cli_as_bounded_fallback(repository, tmp_path):
    run = prepare(repository, tmp_path)
    good = fake_config(tmp_path)
    broken = {"command": [sys.executable, "-c", "print('invalid')"], "output_format": "text"}
    result = resume_review(run, reviewer_configs=[good, broken])
    assert result["status"] == "ready_for_validation"
    assert len(list((run / "reviewer-2").glob("attempt-*"))) == 2
    resume_review(run)
    assert len(list((run / "reviewer-2").glob("attempt-*"))) == 2
    assert len(list((run / "reviewer-1").glob("attempt-*"))) == 1


def test_anonymous_combined_fixer_safe_apply_validation_and_commit_recovery(repository, tmp_path):
    run = prepare(repository, tmp_path)
    finding = {"problem_key":"task-value-invariant", "path":"task.py", "line":1,"message":"值应为 3", "severity":"high"}
    config = fake_config(tmp_path, findings=[finding])
    resume_review(run, reviewer_configs=[config, config])
    result = resume_review(run, fix=True, fixer_config=fake_config(tmp_path, fixer=True))
    assert result["status"] == "ready_for_validation"
    assert (repository / "task.py").read_text() == "value = 3\n"

    assert (repository / "background.txt").read_text() == "unstaged background\n"
    assert (repository / "background-untracked.txt").read_text() == "untracked background\n"
    assert git_text(repository, "diff", "--cached", "--name-only") == "background.txt"
    result = resume_review(run, validation_commands=[[sys.executable,"-c","from pathlib import Path; assert Path('task.py').read_text() == 'value = 3\\n'"]])
    assert result["status"] == "ready_to_commit"
    git(repository, "commit", "--only", "-qm", "fix: 修复隔离验收任务", "--", "task.py")
    result = resume_review(run)
    assert result["status"] == "completed"
    assert result["commit"] == git_text(repository, "rev-parse", "HEAD")


def test_completed_review_of_already_committed_changes_is_recorded_without_reset(repository, tmp_path):
    baseline = git_text(repository,"rev-parse","HEAD")
    prepare(repository,tmp_path)
    git(repository,"commit","--only","-qm","feat: 已提交任务改动","--","task.py")
    second_run = tmp_path/'review-committed-task'
    create_snapshot(repository,base_ref=baseline,scope_paths=['task.py'],requirements=b'committed task',run_directory=second_run)
    result = resume_review(second_run,reviewer_configs=[fake_config(tmp_path),fake_config(tmp_path)],validation_commands=[[sys.executable,'-c','pass']])
    assert result['status'] == 'completed'
    assert result['commit'] == git_text(repository,'rev-parse','HEAD')
    assert git_text(repository,'diff','--cached','--name-only') == 'background.txt'


def test_fixed_baseline_survives_unrelated_base_and_head_drift(repository,tmp_path):
    run = prepare(repository,tmp_path)
    baseline = read_json(run/'run.json')['base_commit']
    (repository/'unrelated.txt').write_text('new base content\n')
    git(repository,'add','unrelated.txt')
    git(repository,'commit','--only','-qm','feat: 无关 base 漂移','--','unrelated.txt')
    before = canary(repository)
    result = resume_review(run,reviewer_configs=[fake_config(tmp_path),fake_config(tmp_path)])
    assert result['status'] == 'ready_for_validation'
    assert read_json(run/'run.json')['base_commit'] == baseline
    assert canary(repository) == before


def test_original_workflow_process_can_die_while_roles_finish_without_duplicate_dispatch(repository,tmp_path,monkeypatch):
    run = prepare(repository,tmp_path)
    config = fake_config(tmp_path)
    script = Path(config['command'][1])
    script.write_text('import time\ntime.sleep(1)\n'+script.read_text())
    configs = tmp_path/'configurations.json'
    write_json(configs,{'configurations':[config,config]})
    startup=tmp_path/'isolated-python-startup';startup.mkdir()
    (startup/'sitecustomize.py').write_text("import sys,time\nif 'execute_snapshot_review_role' in sys.argv[0]: time.sleep(0.8)\n")
    monkeypatch.setenv('PYTHONPATH',str(startup))
    code = "from pathlib import Path; from core.task_snapshot_review.workflow import resume_review; from core.task_snapshot_review.persistent_artifacts import read_json; import sys; resume_review(Path(sys.argv[1]),reviewer_configs=read_json(Path(sys.argv[2]))['configurations'])"
    process = subprocess.Popen([sys.executable,'-c',code,str(run),str(configs)],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    deadline = time.monotonic()+10
    while len(list(run.glob('reviewer-*/attempt-*/request.json'))) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.03)
    process.kill()
    process.wait()
    result = resume_review(run)
    assert result['status'] in ('review_incomplete','ready_for_validation')
    while len(list(run.glob('reviewer-*/attempt-*/outcome.json'))) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.03)
    assert resume_review(run)['status'] == 'ready_for_validation'
    assert len(list(run.glob('reviewer-*/attempt-*'))) == 2


def test_symlinks_deletions_binary_and_untracked_task_files_are_in_fixed_snapshot(repository,tmp_path):
    (repository/'deleted.txt').write_text('delete me\n')
    git(repository,'add','deleted.txt')
    git(repository,'commit','-qm','test: 初始化删除路径')
    (repository/'deleted.txt').unlink()
    (repository/'new-binary.bin').write_bytes(bytes(range(256)))
    (repository/'task-link').symlink_to('task.py')
    run=tmp_path/'binary-snapshot'
    before=canary(repository)
    record=create_snapshot(repository,base_ref='HEAD',scope_paths=['deleted.txt','new-binary.bin','task-link'],requirements=b'binary task',run_directory=run)
    assert set(record['changed_paths']) == {'deleted.txt','new-binary.bin','task-link'}
    assert git(repository,'show',record['candidate_commit']+':new-binary.bin') == bytes(range(256))
    assert git_text(repository,'ls-tree',record['candidate_commit'],'task-link').startswith('120000')
    assert canary(repository) == before


def test_real_failed_validation_is_not_reported_as_ready_to_commit(repository, tmp_path):
    run = prepare(repository, tmp_path)
    resume_review(run, reviewer_configs=[fake_config(tmp_path), fake_config(tmp_path)])
    result = resume_review(run, validation_commands=[[sys.executable,"-c","raise SystemExit(9)"]])
    assert result["status"] == "validation_failed"
    assert read_json(run / "validation.json")["returncode"] == 9


def test_role_credentials_are_delivered_without_persisting_values_in_run_materials(repository,tmp_path):
    run=prepare(repository,tmp_path)
    config=fake_config(tmp_path)
    secret='isolated-test-api-credential-value'
    config['env_set']={'ISOLATED_ROLE_API_KEY':secret}
    script=Path(config['command'][1])
    script.write_text(script.read_text()+"\nprint(os.environ['ISOLATED_ROLE_API_KEY'],file=sys.stderr)\n")
    result=resume_review(run,reviewer_configs=[config,config])
    assert result['status']=='ready_for_validation'
    for path in run.rglob('*'):
        if path.is_file() and 'workspace' not in path.parts:
            assert secret.encode() not in path.read_bytes(),str(path)


def test_transient_failure_retries_once_and_deterministic_failure_uses_alternative(repository,tmp_path):
    run=prepare(repository,tmp_path)
    good=fake_config(tmp_path,fail_first=True)
    bad={'command':[sys.executable,'-c',"print('invalid JSON')"],'output_format':'text'}
    result=resume_review(run,reviewer_configs=[good,bad,good])
    assert result['status']=='ready_for_validation'
    assert len(list((run/'reviewer-1').glob('attempt-*')))==2
    assert len(list((run/'reviewer-2').glob('attempt-*')))==3


def test_context_response_only_resumes_missing_legs_and_preserves_their_prior_findings(repository,tmp_path):
    run=prepare(repository,tmp_path)
    script=tmp_path/'reviewer-with-context-gap.py'
    script.write_text("import json,os,pathlib\nr=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
                      "result={'status':'incomplete','audit_summary':'缺少机器证据','findings':[{'path':'task.py','message':'已有发现'}],'requests':['需要 fixture']}\n"
                      "if r.get('context_response_file'): result={'status':'completed','audit_summary':'已接续检查','findings':[]}\nprint(json.dumps(result))\n")
    missing={'command':[sys.executable,str(script)],'output_format':'text'}
    assert resume_review(run,reviewer_configs=[missing,missing])['status']=='review_incomplete'
    response=tmp_path/'machine-context.md';response.write_text('fixture 已补齐；不包含另一腿结论。')
    result=resume_review(run,context_response_file=response)
    assert result['status']=='ready_for_fix'
    assert result['finding_count']==1
    assert len(list((run/'reviewer-1').glob('attempt-*')))==2


def test_explicit_process_stop_cancels_owned_roles_but_retains_restart_materials(repository,tmp_path):
    run=prepare(repository,tmp_path)
    config=fake_config(tmp_path)
    script=Path(config['command'][1]);script.write_text('import time\ntime.sleep(20)\n'+script.read_text())
    config_file=tmp_path/'reviewer-config.json';write_json(config_file,config)
    entry=ROOT/'plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py'
    before=canary(repository)
    process=subprocess.Popen([sys.executable,str(entry),'resume-task-review','--run-dir',str(run),
                              '--reviewer-config',str(config_file),'--reviewer-config',str(config_file)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    deadline=time.monotonic()+10
    while len(list(run.glob('reviewer-*/attempt-*/model-invocation.json')))<2:
        assert time.monotonic()<deadline
        time.sleep(0.03)
    process.terminate()
    stdout,stderr=process.communicate(timeout=15)
    assert process.returncode==130,(stdout,stderr)
    assert read_json(run/'run.json')['status']=='cancelled'
    assert all(read_json(path)['status']=='cancelled' for path in run.glob('reviewer-*/attempt-*/outcome.json'))
    assert canary(repository)==before
    config=fake_config(tmp_path)
    assert resume_review(run,reviewer_configs=[config,config])['status']=='cancelled'


def test_changed_main_worktree_blocks_only_patch_application_and_retains_fixer_result(repository, tmp_path):
    run = prepare(repository, tmp_path)
    finding = {"path":"task.py", "line":1,"message":"值应为 3", "severity":"high"}
    config = fake_config(tmp_path, findings=[finding])
    resume_review(run, reviewer_configs=[config, config])
    (repository / "task.py").write_text("concurrent work\n")
    result = resume_review(run, fix=True, fixer_config=fake_config(tmp_path, fixer=True))
    assert result["status"] == "fix_application_pending"
    assert (repository / "task.py").read_text() == "concurrent work\n"
    assert (run / "fixer.patch").is_file()
    (repository / "task.py").write_text("value = 2\n")
    result = resume_review(run)
    assert result["status"] == "ready_for_validation"
    assert (repository / "task.py").read_text() == "value = 3\n"


def plain_reviewer_config(tmp_path, name="plain-reviewer.py"):
    script = tmp_path / name
    script.write_text("import json\nprint(json.dumps({'status':'completed','findings':[],'audit_summary':'已检查'}))\n")
    return {"command": [sys.executable, str(script)], "output_format": "text", "timeout_seconds": 20}


def test_deleted_scoped_file_survives_snapshot_validation_and_completion(repository, tmp_path):
    (repository / "task.py").unlink()
    run = tmp_path / "deleted-file-run"
    record = create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements=b"delete task", run_directory=run)
    assert record["changed_paths"] == ["task.py"]
    config = plain_reviewer_config(tmp_path)
    result = resume_review(run, reviewer_configs=[config, config], validation_commands=[[sys.executable, "-c", "pass"]])
    assert result["status"] == "ready_to_commit"


def test_only_an_explicit_ignored_file_is_forced_into_the_snapshot(repository, tmp_path):
    (repository / ".gitignore").write_text("ignored-task.py\nignored-runtime/\n")
    git(repository, "add", ".gitignore")
    git(repository, "commit", "-qm", "test: 忽略任务与运行材料")
    (repository / "ignored-task.py").write_text("task = True\n")
    (repository / "ignored-runtime").mkdir()
    (repository / "ignored-runtime" / "state.json").write_text("{}")
    explicit = create_snapshot(repository, base_ref="HEAD", scope_paths=["ignored-task.py"], requirements=b"explicit", run_directory=tmp_path / "explicit-ignored")
    assert explicit["changed_paths"] == ["ignored-task.py"]
    assert git_text(repository, "show", explicit["candidate_commit"] + ":ignored-task.py") == "task = True"
    directory = create_snapshot(repository, base_ref="HEAD", scope_paths=["."], requirements=b"directory", run_directory=tmp_path / "directory-ignored")
    assert directory["changed_paths"] == []
    assert not git(repository, "ls-tree", "-r", "--name-only", directory["candidate_commit"], "--", "ignored-task.py", "ignored-runtime")


@pytest.mark.parametrize("output_format", ["codex_json", "claude_stream_json", "cursor_stream_json"])
def test_shipped_structured_formats_accept_their_success_terminal_events(repository, tmp_path, output_format):
    script = tmp_path / f"{output_format}.py"
    script.write_text(
        "import json\nresult=json.dumps({'status':'completed','findings':[],'audit_summary':'已检查'})\n"
        + ("print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':result}}))\nprint(json.dumps({'type':'turn.completed'}))\n"
           if output_format == "codex_json" else
           "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':result}))\n")
    )
    config = {"command": [sys.executable, str(script)], "output_format": output_format}
    run = prepare(repository, tmp_path)
    assert resume_review(run, reviewer_configs=[config, config])["status"] == "ready_for_validation"


def test_role_output_file_does_not_hide_structured_provider_failure(repository, tmp_path):
    script = tmp_path / "omp-provider-error.py"
    script.write_text(
        "import json,os,pathlib\nrequest=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "pathlib.Path(request['result_file']).write_text(json.dumps({'status':'completed','findings':[],'audit_summary':'伪造完成'}))\n"
        "print(json.dumps({'type':'turn_end','message':{'stopReason':'error','errorMessage':'usage limit exhausted'}}))\n"
        "print(json.dumps({'type':'agent_end'}))\n"
    )
    config = {"command": [sys.executable, str(script)], "output_format": "oh_my_pi_json"}
    run = prepare(repository, tmp_path)
    assert resume_review(run, reviewer_configs=[config, config])["status"] == "review_incomplete"
    assert all(read_json(path)["status"] == "failed" for path in run.glob("reviewer-*/attempt-*/outcome.json"))


def test_original_stdout_is_reconciled_without_redispatch_or_rewriting_failure_history(repository, tmp_path):
    script = tmp_path / "native-kimi.py"
    script.write_text(
        "import json\nprint(json.dumps({'role':'assistant','content':json.dumps({'status':'completed','findings':[],'audit_summary':'已检查'})}))\n"
        "print(json.dumps({'type':'session.resume_hint','role':'meta','content':'kimi -r native'}))\n"
    )
    config = {"command": [sys.executable, str(script)], "output_format": "kimi_stream_json"}
    run = prepare(repository, tmp_path)
    assert resume_review(run, reviewer_configs=[config, config])["status"] == "ready_for_validation"
    outcomes = sorted(run.glob("reviewer-*/attempt-*/outcome.json"))
    for outcome in outcomes:
        original = read_json(outcome)
        write_json(outcome, {**original, "status": "failed", "retryable": False, "error": "旧解释器不识别原生终态"})
        (outcome.parent / "result.json").unlink()
    assert resume_review(run)["status"] == "ready_for_validation"
    assert sorted(run.glob("reviewer-*/attempt-*/outcome.json")) == outcomes
    assert len(list(run.glob("reviewer-*/attempt-*"))) == 2
    assert all(read_json(path)["status"] == "failed" for path in outcomes)
    assert all(read_json(path.parent / "reconciled-native-completion.json")["status"] == "completed" for path in outcomes)


def test_empty_patch_cannot_accept_a_fixed_disposition(repository, tmp_path):
    run = prepare(repository, tmp_path)
    finding = {"problem_key": "must-change", "path": "task.py", "message": "必须修改", "severity": "high"}
    reviewer = fake_config(tmp_path, findings=[finding])
    resume_review(run, reviewer_configs=[reviewer, reviewer])
    script = tmp_path / "empty-fixer.py"
    script.write_text("import json,os,pathlib\nr=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\nprint(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'fixed','reason':'声称修复'} for f in r['findings']]}))\n")
    result = resume_review(run, fix=True, fixer_config={"command": [sys.executable, str(script)], "output_format": "text"})
    assert result["status"] == "fix_incomplete"
    assert (repository / "task.py").read_text() == "value = 2\n"
    assert read_json(run / "fix-application.json")["applied"] is False


def test_new_in_scope_finding_blocks_completion_and_is_assigned_to_the_next_fix(repository, tmp_path):
    run = prepare(repository, tmp_path)
    finding = {"problem_key": "original", "path": "task.py", "message": "原问题", "severity": "high"}
    reviewer = fake_config(tmp_path, findings=[finding])
    resume_review(run, reviewer_configs=[reviewer, reviewer])
    first = tmp_path / "new-finding-fixer.py"
    first.write_text(
        "import json,os,pathlib\nr=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\npathlib.Path('task.py').write_text('value = 3\\n')\n"
        "print(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'fixed','reason':'已修复'} for f in r['findings']],'new_findings':[{'problem_key':'remaining','path':'task.py','message':'仍需修复','severity':'high','scope_relation':'in_task_scope'}]}))\n"
    )
    config = {"command": [sys.executable, str(first)], "output_format": "text"}
    assert resume_review(run, fix=True, fixer_config=config)["status"] == "findings_unresolved"
    assert read_json(run / "run.json")["finding_count"] == 2
    second = tmp_path / "second-fixer.py"
    second.write_text(
        "import json,os,pathlib\nr=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\nassert len(r['findings'])==2\nassert pathlib.Path('task.py').read_text()=='value = 3\\n'\npathlib.Path('task.py').write_text('value = 4\\n')\n"
        "print(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'fixed','reason':'全部修复'} for f in r['findings']]}))\n"
    )
    result = resume_review(run, fix=True, fixer_config={"command": [sys.executable, str(second)], "output_format": "text"})
    assert result["status"] == "ready_for_validation"
    assert (repository / "task.py").read_text() == "value = 4\n"
    assert len(list((run / "fixer").glob("attempt-*"))) == 2


def test_unresolved_fix_can_resume_with_new_evidence_from_the_current_candidate(repository, tmp_path):
    run = prepare(repository, tmp_path)
    finding = {"problem_key": "permission", "path": "task.py", "message": "需要授权", "severity": "high"}
    reviewer = fake_config(tmp_path, findings=[finding])
    resume_review(run, reviewer_configs=[reviewer, reviewer])
    unresolved = tmp_path / "unresolved-fixer.py"
    unresolved.write_text("import json,os,pathlib\nr=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\npathlib.Path('task.py').write_text('value = 3\\n')\nprint(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'unresolved','reason':'等待授权'} for f in r['findings']]}))\n")
    config = {"command": [sys.executable, str(unresolved)], "output_format": "text"}
    assert resume_review(run, fix=True, fixer_config=config)["status"] == "findings_unresolved"
    context = tmp_path / "permission.md"
    context.write_text("已授权")
    fixed_script = tmp_path / "authorized-fixer.py"
    fixed_script.write_text("import json,os,pathlib\nr=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\nassert pathlib.Path('task.py').read_text()=='value = 3\\n'\npathlib.Path('task.py').write_text('value = 4\\n')\nprint(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'fixed','reason':'授权后修复'} for f in r['findings']]}))\n")
    fixed = {"command": [sys.executable, str(fixed_script)], "output_format": "text"}
    assert resume_review(run, fix=True, fixer_config=fixed, context_response_file=context)["status"] == "ready_for_validation"
    attempts = sorted((run / "fixer").glob("attempt-*"))
    assert len(attempts) == 2
    first_intent = read_json(attempts[0] / "fix-application.json")
    assert read_json(attempts[1] / "request.json")["candidate_commit"] == first_intent["after"]


def test_validation_failure_can_be_fixed_and_revalidated_in_the_same_run(repository, tmp_path):
    run = prepare(repository, tmp_path)
    finding = {"problem_key": "red-test", "path": "task.py", "message": "红测", "severity": "high"}
    reviewer = fake_config(tmp_path, findings=[finding])
    resume_review(run, reviewer_configs=[reviewer, reviewer])
    fixer = fake_config(tmp_path, fixer=True)
    assert resume_review(run, fix=True, fixer_config=fixer, validation_commands=[[sys.executable, "-c", "raise SystemExit(9)"]])["status"] == "validation_failed"
    script = tmp_path / "fix-red-test.py"
    script.write_text("import json,os,pathlib\nr=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\nassert pathlib.Path('task.py').read_text()=='value = 3\\n'\npathlib.Path('task.py').write_text('value = 4\\n')\nprint(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'fixed','reason':'修复红测'} for f in r['findings']]}))\n")
    result = resume_review(run, fix=True, fixer_config={"command": [sys.executable, str(script)], "output_format": "text"}, validation_commands=[[sys.executable, "-c", "from pathlib import Path; assert Path('task.py').read_text() == 'value = 4\\n'"]])
    assert result["status"] == "ready_to_commit"
    assert len(list((run / "fixer").glob("attempt-*"))) == 2


def test_stopping_final_validation_reaps_its_process_before_unlocking(repository, tmp_path):
    run = prepare(repository, tmp_path)
    config = plain_reviewer_config(tmp_path)
    resume_review(run, reviewer_configs=[config, config])
    pid_file = tmp_path / "validation.pid"
    marker = tmp_path / "validation-finished"
    validation = [sys.executable, "-c", f"import os,pathlib,time; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); print('interrupted-validation-marker',flush=True); time.sleep(20); pathlib.Path({str(marker)!r}).write_text('finished')"]
    commands = tmp_path / "validation.json"
    write_json(commands, [validation])
    entry = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py"
    process = subprocess.Popen([sys.executable, str(entry), "resume-task-review", "--run-dir", str(run), "--validation-commands", str(commands)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 10
    while not pid_file.exists():
        assert time.monotonic() < deadline
        time.sleep(0.03)
    child_pid = int(pid_file.read_text())
    process.terminate()
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 130, (stdout, stderr)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert not marker.exists()
    assert read_json(run / "validation.json")["status"] == "interrupted"
    assert resume_review(run, validation_commands=[[sys.executable, "-c", "pass"]])["status"] == "cancelled"
    attempts = sorted((run / "final-validation").glob("attempt-*"))
    assert len(attempts) == 1
    assert read_json(attempts[0] / "validation.json")["status"] == "interrupted"
    assert "interrupted-validation-marker" in (attempts[0] / "command-001" / "stdout.txt").read_text()


def test_failed_leg_fallback_starts_before_the_healthy_leg_finishes(repository, tmp_path):
    events = tmp_path / "events.jsonl"
    healthy = tmp_path / "slow-reviewer.py"
    healthy.write_text(
        "import json,os,pathlib,time\nr=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\nrole=pathlib.Path(r['input_file']).parent.parent.name\n"
        f"def log(event):\n with pathlib.Path({str(events)!r}).open('a') as f: f.write(json.dumps({{'role':role,'event':event,'time':time.monotonic()}})+'\\n')\n"
        "log('start'); time.sleep(1); log('end')\nprint(json.dumps({'status':'completed','findings':[],'audit_summary':'已检查'}))\n"
    )
    good = {"command": [sys.executable, str(healthy)], "output_format": "text"}
    broken = {"command": [sys.executable, "-c", "print('invalid')"], "output_format": "text"}
    run = prepare(repository, tmp_path)
    assert resume_review(run, reviewer_configs=[good, broken])["status"] == "ready_for_validation"
    records = [json.loads(line) for line in events.read_text().splitlines()]
    reviewer_1_end = next(item["time"] for item in records if item["role"] == "reviewer-1" and item["event"] == "end")
    reviewer_2_starts = [item["time"] for item in records if item["role"] == "reviewer-2" and item["event"] == "start"]
    assert len(reviewer_2_starts) == 1
    assert reviewer_2_starts[0] < reviewer_1_end
