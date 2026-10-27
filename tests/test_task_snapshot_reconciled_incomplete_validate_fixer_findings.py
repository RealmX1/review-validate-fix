"""补证后的未完成结果仍须保留发现，原失败历史不被改写。"""
import json
from pathlib import Path
import sys

from test_task_snapshot_review_workflow import repository
from core.task_snapshot_review.persistent_artifacts import read_json, write_json
from core.task_snapshot_review.role_process_execution import reconcile_completed_role_attempt
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import _role_result, _run_roles, resume_review


def test_reconciled_incomplete_validate_fixer_keeps_new_findings_and_original_outcome(repository, tmp_path):
    (repository / 'task.py').write_text('value = 2\n')
    run = tmp_path / 'review-with-reconciled-incomplete-validate-fixer'
    create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'],
                    requirements='保留未解决的新发现和原始失败事实'.encode(), run_directory=run)
    reviewer = {'command': [sys.executable, '-c', "import json; print(json.dumps({'status':'completed','audit_summary':'已检查','findings':[{'problem_key':'original','message':'原始发现'}]}))"], 'output_format': 'text'}
    assert resume_review(run, reviewer_configs=[reviewer, reviewer])['status'] == 'ready_for_fix'
    program = (
        "import json,os,pathlib\n"
        "r=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "pathlib.Path('task.py').write_text('value = 3\\n')\n"
        "print(json.dumps({'status':'incomplete','dispositions':[{'finding_id':i['finding_id'],'status':'fixed','reason':'已验证并修复原问题'} for i in r['findings']],"
        "'new_findings':[{'problem_key':'remaining-current-scope','message':'已报告但未解决的新主线问题','scope_relation':'in_task_scope'}]}))\n"
    )
    config = {'command': [sys.executable, '-c', program], 'output_format': 'text'}
    findings = read_json(run / 'findings.json')['findings']
    _run_roles(run, read_json(run / 'run.json'), [('fixer', 'fixer', [config])], findings)
    attempt = run / 'fixer/attempt-001'
    outcome = read_json(attempt / 'outcome.json')
    assert outcome['status'] == 'incomplete' and outcome['returncode'] == 0
    outcome.update(status='failed', error='隔离模拟：原解析器未识别正常退出的未完成报告')
    write_json(attempt / 'outcome.json', outcome)
    original_failure_bytes = (attempt / 'outcome.json').read_bytes()
    reconcile_completed_role_attempt(attempt)
    assert read_json(attempt / 'reconciled-native-completion.json')['status'] == 'incomplete'
    assert _role_result(run / 'fixer', latest=True) is None

    continuation = (
        "import json,os,pathlib\n"
        "r=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "print(json.dumps({'status':'incomplete','dispositions':[{'finding_id':i['finding_id'],'status':'unresolved','reason':'仍需接续验证'} for i in r['findings']],"
        "'new_findings':[]}))\n"
    )
    resumed_config = {'command': [sys.executable, '-c', continuation], 'output_format': 'text'}

    result = resume_review(run, fix=True, fixer_config=resumed_config)

    assert result['status'] == 'fix_incomplete'
    assert 'remaining-current-scope' in {finding['problem_key'] for finding in read_json(run / 'findings.json')['findings']}
    assert (attempt / 'outcome.json').read_bytes() == original_failure_bytes
    next_attempt = run / 'fixer/attempt-002'
    request = read_json(next_attempt / 'request.json')
    assert {finding['problem_key'] for finding in request['findings']} == {'original', 'remaining-current-scope'}
    assert request['previous_result_files'] == [str(attempt / 'result.json')]
    assert (Path(request['workspace']) / 'task.py').read_text() == 'value = 3\n'
    assert read_json(next_attempt / 'outcome.json')['status'] == 'incomplete'
    assert read_json(attempt / 'reconciled-native-completion.json')['status'] == 'incomplete'
    assert (repository / 'task.py').read_text() == 'value = 2\n'
    assert not (run / 'validation.json').exists()
