"""主会话追加的恢复验收；通过环境变量选择待验收的实际修复工作树。"""
import json
import os
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
sys.path.insert(0,str(SOURCE/'tests'))
from test_task_snapshot_review_workflow import repository, prepare, fake_config
from core.task_snapshot_review.persistent_artifacts import read_json,write_json
from core.task_snapshot_review.workflow import resume_review


def incrementing_fixer(tmp_path):
    script=tmp_path/'incrementing-fixer.py'
    script.write_text("import json,os,pathlib\nrequest=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
                      "path=pathlib.Path('task.py'); value=int(path.read_text().split('=')[1]); path.write_text(f'value = {value+1}\\n')\n"
                      "print(json.dumps({'status':'completed','dispositions':[{'finding_id':item['finding_id'],'status':'fixed','reason':'本轮按实际候选修复并验证'} for item in request['findings']]}))\n")
    return {'command':[sys.executable,str(script)],'output_format':'text'}


def test_three_successive_fix_rounds_are_not_limited_by_old_completed_attempts(repository,tmp_path):
    run=prepare(repository,tmp_path)
    reviewer=fake_config(tmp_path,findings=[{'path':'task.py','message':'需要继续修复的主线问题','severity':'high'}])
    resume_review(run,reviewer_configs=[reviewer,reviewer])
    fixer=incrementing_fixer(tmp_path)
    for expected in (4,5,5):
        command=[sys.executable,'-c',f"from pathlib import Path; assert Path('task.py').read_text() == 'value = {expected}\\n'"]
        result=resume_review(run,fix=True,fixer_config=fixer,validation_commands=[command])
    assert result['status']=='ready_to_commit'
    assert (repository/'task.py').read_text()=='value = 5\n'


def test_new_fixer_finding_defaults_to_current_scope_when_not_explicitly_outside(repository,tmp_path):
    run=prepare(repository,tmp_path)
    reviewer=fake_config(tmp_path,findings=[{'path':'task.py','message':'原始主线问题','severity':'high'}])
    resume_review(run,reviewer_configs=[reviewer,reviewer])
    script=tmp_path/'fixer-with-new-finding.py'
    script.write_text("import json,os,pathlib\nr=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\npathlib.Path('task.py').write_text('value = 3\\n')\n"
                      "print(json.dumps({'status':'completed','dispositions':[{'finding_id':item['finding_id'],'status':'fixed','reason':'已修复'} for item in r['findings']],"
                      "'new_findings':[{'path':'task.py','message':'尚未解决的新主线问题','severity':'high'}]}))\n")
    result=resume_review(run,fix=True,fixer_config={'command':[sys.executable,str(script)],'output_format':'text'})
    assert result['status']=='findings_unresolved'
    assert result['finding_count']==2


def test_successful_revalidation_preserves_the_original_failed_validation_output(repository,tmp_path):
    run=prepare(repository,tmp_path)
    reviewer=fake_config(tmp_path)
    resume_review(run,reviewer_configs=[reviewer,reviewer])
    marker='rvf-original-red-validation-observation'
    failed=resume_review(run,validation_commands=[[sys.executable,'-c',f"import sys; print('{marker}',file=sys.stderr); raise SystemExit(7)"]])
    assert failed['status']=='validation_failed'
    passed=resume_review(run,validation_commands=[[sys.executable,'-c','pass']])
    assert passed['status']=='ready_to_commit'
    assert any(marker in path.read_text(errors='replace') for path in run.rglob('stderr.txt'))


def test_missing_normalized_result_is_reconstructed_from_completed_native_output(repository,tmp_path):
    run=prepare(repository,tmp_path)
    reviewer=fake_config(tmp_path)
    assert resume_review(run,reviewer_configs=[reviewer,reviewer])['status']=='ready_for_validation'
    attempt=run/'reviewer-1/attempt-001'
    outcome=read_json(attempt/'outcome.json');outcome.update({'status':'failed','error':'原始格式误判'})
    write_json(attempt/'outcome.json',outcome)
    assert resume_review(run)['status']=='ready_for_validation'
    (attempt/'result.json').unlink()
    assert resume_review(run)['status']=='ready_for_validation'
    assert len(list(run.glob('reviewer-*/attempt-*')))==2
    assert read_json(attempt/'outcome.json')==outcome
