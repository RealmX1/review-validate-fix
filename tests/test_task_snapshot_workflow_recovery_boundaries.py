"""最终验证内容绑定、未完成修复接续与派发取消的行为回归。"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time

import pytest

from test_task_snapshot_review_workflow import ROOT, canary, fake_config, plain_reviewer_config, prepare, repository
from core.task_snapshot_review.persistent_artifacts import git, git_text, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import default_reviewer_configurations, resume_review


def dependency_task(repository, tmp_path, *, expected=1, background=None):
    (repository / 'dependency.py').write_text('value = 1\n')
    git(repository, 'add', 'dependency.py')
    git(repository, 'commit', '-qm', 'test: 初始化验证依赖')
    (repository / 'task.py').write_text(f'from dependency import value\nassert value == {expected}, value\n')
    if background is not None:
        (repository / 'dependency.py').write_text(f'value = {background}\n')
    run = tmp_path / 'dependency-run'
    create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements='验证真实候选'.encode(), run_directory=run)
    config = plain_reviewer_config(tmp_path)
    assert resume_review(run, reviewer_configs=[config, config])['status'] == 'ready_for_validation'
    return run


def test_background_dependency_cannot_hide_a_red_candidate(repository, tmp_path):
    run = dependency_task(repository, tmp_path, expected=2, background=2)
    before = canary(repository)
    result = resume_review(run, validation_commands=[[sys.executable, '-B', 'task.py']])
    assert result['status'] == 'validation_failed'
    validation = read_json(run / 'validation.json')
    assert validation['returncode'] == 1
    assert (Path(validation['workspace']) / 'dependency.py').read_text() == 'value = 1\n'
    assert canary(repository) == before


@pytest.mark.parametrize('dependency_value', [0, 1])
def test_head_tree_drift_revalidates_without_rerunning_reviewers(repository, tmp_path, dependency_value):
    run = dependency_task(repository, tmp_path)
    command = [sys.executable, '-B', 'task.py']
    assert resume_review(run, validation_commands=[command])['status'] == 'ready_to_commit'
    original_validation = read_json(run / 'validation.json')
    reviewer_attempts = sorted(run.glob('reviewer-*/attempt-*/outcome.json'))
    (repository / 'dependency.py').write_text(f'value = {dependency_value}\n# base 漂移\n')
    git(repository, 'commit', '--only', '-qm', 'test: 提交依赖漂移', '--', 'dependency.py')
    git(repository, 'commit', '--only', '-qm', 'test: 提交任务', '--', 'task.py')
    result = resume_review(run)
    assert result['status'] == ('validation_failed' if dependency_value == 0 else 'completed')
    validation = read_json(run / 'validation.json')
    assert validation['tested_tree'] != original_validation['tested_tree']
    assert validation['tested_tree'] == git_text(repository, 'rev-parse', 'HEAD^{tree}')
    assert sorted(run.glob('reviewer-*/attempt-*/outcome.json')) == reviewer_attempts
    assert validation['returncode'] == (1 if dependency_value == 0 else 0)


@pytest.mark.parametrize('disabled_content', [None, '{not valid json'])
def test_disabled_reviewer_does_not_load_unavailable_configuration(repository, tmp_path, monkeypatch, disabled_content):
    import core.rvf_user_local_data_paths as paths
    disabled = tmp_path / 'disabled.json'
    if disabled_content is not None:
        disabled.write_text(disabled_content)
    available = tmp_path / 'available.json'
    write_json(available, plain_reviewer_config(tmp_path))
    registry = tmp_path / 'registry.json'
    write_json(registry, {'harnesses': {
        'disabled': {'enabled': False, 'priority_default': 100, 'config_path': str(disabled)},
        'available': {'enabled': True, 'priority_default': 1, 'config_path': str(available)},
    }})
    monkeypatch.setattr(paths, 'resolved_rvf_reviewer_registry_path', lambda bundled: registry)
    run = prepare(repository, tmp_path)
    assert resume_review(run, reviewer_configs=default_reviewer_configurations())['status'] == 'ready_for_validation'


@pytest.mark.parametrize('partial_changes', [False, True])
def test_incomplete_fixer_preserves_new_findings_dispositions_and_repairs(repository, tmp_path, partial_changes):
    run = prepare(repository, tmp_path)
    reviewer = fake_config(tmp_path, findings=[{'problem_key': 'original', 'message': '原问题'}])
    assert resume_review(run, reviewer_configs=[reviewer, reviewer])['status'] == 'ready_for_fix'
    first = tmp_path / 'partial-fixer.py'
    first.write_text(
        'import json,os,pathlib\nr=json.loads(pathlib.Path(os.environ["RVF_ROLE_INPUT"]).read_text())\n'
        + ("pathlib.Path('task.py').write_text('value = 3\\n')\n" if partial_changes else '')
        + f"print(json.dumps({{'status':'incomplete','dispositions':[{{'finding_id':f['finding_id'],'status':{'fixed' if partial_changes else 'false_positive'!r},'reason':'原问题已验证'}} for f in r['findings']], 'new_findings':[{{'problem_key':'remaining','message':'新发现尚未修复','scope_relation':'in_task_scope'}}]}}))\n"
    )
    config = {'command': [sys.executable, str(first)], 'output_format': 'text'}
    assert resume_review(run, fix=True, fixer_config=config)['status'] == 'fix_incomplete'
    assert {f['problem_key'] for f in read_json(run / 'findings.json')['findings']} == {'original', 'remaining'}
    assert (repository / 'task.py').read_text() == 'value = 2\n'
    second = tmp_path / 'resume-fixer.py'
    second.write_text(
        'import json,os,pathlib\nr=json.loads(pathlib.Path(os.environ["RVF_ROLE_INPUT"]).read_text())\n'
        f"assert pathlib.Path('task.py').read_text() == {'value = 3' + chr(10) if partial_changes else 'value = 2' + chr(10)!r}\n"
        "assert {f['problem_key'] for f in r['findings']} == {'original','remaining'}\n"
        "prior=json.loads(pathlib.Path(r['previous_result_files'][-1]).read_text())\nassert prior['dispositions'][0]['reason']=='原问题已验证'\n"
        f"print(json.dumps({{'status':'completed','dispositions':[{{'finding_id':f['finding_id'],'status':('unresolved' if f['problem_key']=='remaining' else {'fixed' if partial_changes else 'false_positive'!r}),'reason':'接续验证结果'}} for f in r['findings']]}}))\n"
    )
    config = {'command': [sys.executable, str(second)], 'output_format': 'text'}
    assert resume_review(run, fix=True, fixer_config=config)['status'] == 'findings_unresolved'
    assert (repository / 'task.py').read_text() == ('value = 3\n' if partial_changes else 'value = 2\n')
    third = tmp_path / 'finish-fixer.py'
    third.write_text(
        'import json,os,pathlib\nr=json.loads(pathlib.Path(os.environ["RVF_ROLE_INPUT"]).read_text())\n'
        "pathlib.Path('task.py').write_text('value = 4\\n')\n"
        "print(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'fixed','reason':'已验证并修复'} for f in r['findings']]}))\n"
    )
    result = resume_review(run, fix=True, fixer_config={'command': [sys.executable, str(third)], 'output_format': 'text'},
                           validation_commands=[[sys.executable, '-c', "from task import value; assert value == 4"]])
    assert result['status'] == 'ready_to_commit'
    assert (repository / 'task.py').read_text() == 'value = 4\n'
    assert (repository / 'background.txt').read_text() == 'unstaged background\n'
    assert git_text(repository, 'diff', '--cached', '--name-only') == 'background.txt'


def test_cancellation_during_initial_three_reviewer_dispatch_reaps_started_worker(repository, tmp_path):
    (repository / 'task.py').write_text('value = 2\n')
    run = tmp_path / 'initial-dispatch-run'
    create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'cancel dispatch', run_directory=run, reviewer_count=3)
    started = tmp_path / 'child-started'
    finished = tmp_path / 'child-finished'
    config = tmp_path / 'slow-reviewer.json'
    write_json(config, {'command': [sys.executable, '-c',
        f"import pathlib,time,json; pathlib.Path({str(started)!r}).touch(); time.sleep(3); pathlib.Path({str(finished)!r}).touch(); print(json.dumps({{'status':'completed','audit_summary':'已检查','findings':[]}}))"], 'output_format': 'text'})
    driver = tmp_path / 'cancel-driver.py'
    driver.write_text(
        f'import sys,time\nfrom pathlib import Path\nsys.path.insert(0,{str(ROOT)!r})\n'
        f'sys.path.insert(0,{str(ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts")!r})\n'
        'import core.task_snapshot_review.workflow as workflow\nfrom rvf_command_line_shortcut_entrypoint import main\n'
        'original=workflow._start_role\ndef pause(*args,**kwargs):\n'
        "    if args[2].name=='reviewer-2':\n"
        f'        deadline=time.monotonic()+10\n        while not Path({str(started)!r}).exists():\n            assert time.monotonic()<deadline\n            time.sleep(0.01)\n'
        "        print('STARTUP_WINDOW',flush=True)\n        time.sleep(20)\n"
        '    return original(*args,**kwargs)\nworkflow._start_role=pause\n'
        f"raise SystemExit(main(['resume-task-review','--run-dir',{str(run)!r},'--reviewer-config',{str(config)!r},'--reviewer-config',{str(config)!r}]))\n"
    )
    process = subprocess.Popen([sys.executable, str(driver)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'STARTUP_WINDOW'
        process.terminate()
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 130, (stdout, stderr)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert read_json(run / 'run.json')['status'] == 'interrupted'
    outcome = read_json(run / 'reviewer-1/attempt-001/outcome.json')
    assert outcome['status'] == 'cancelled'
    assert not finished.exists()
    config = plain_reviewer_config(tmp_path)
    assert resume_review(run, reviewer_configs=[config, config])['status'] == 'ready_for_validation'


def test_interrupted_validation_reuses_build_outputs_and_only_runs_remaining_commands(repository, tmp_path):
    run = dependency_task(repository, tmp_path)
    counter = tmp_path / 'build-invocations'
    started = tmp_path / 'validation-started'
    proceed = tmp_path / 'proceed'
    commands = [
        [sys.executable, '-c',
         f"from pathlib import Path; p=Path({str(counter)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); Path('build-result').write_text('built')"],
        [sys.executable, '-c',
         f"from pathlib import Path; import time; Path({str(started)!r}).touch(); time.sleep(0 if Path({str(proceed)!r}).exists() else 20); assert Path('build-result').read_text()=='built'"],
    ]
    commands_file = tmp_path / 'commands.json'
    write_json(commands_file, commands)
    entry = ROOT / 'plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py'
    process = subprocess.Popen([sys.executable, str(entry), 'resume-task-review', '--run-dir', str(run),
                                '--validation-commands', str(commands_file)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not started.exists():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        process.terminate()
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 130, (stdout, stderr)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    before = read_json(run / 'validation.json')
    assert before['status'] == 'interrupted'
    assert before['completed_commands'] == 1
    proceed.touch()
    assert resume_review(run)['status'] == 'ready_to_commit'
    assert counter.read_text() == 'x'
    assert read_json(run / 'validation.json')['completed_commands'] == 2
