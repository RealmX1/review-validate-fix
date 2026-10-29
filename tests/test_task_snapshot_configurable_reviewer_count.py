"""显式评审数量必须参与派发、发现合并和重启后的完成判断。"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_task_snapshot_review_workflow import ROOT, repository, fake_config
from core.task_snapshot_review.persistent_artifacts import read_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import resume_review


def prepare_review_with_count(repository, tmp_path, count):
    (repository / 'task.py').write_text('value = 2\n')
    run = tmp_path / 'configured-reviewer-count-run'
    create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'],
                    requirements='每位 reviewer 独立检查完整任务'.encode(),
                    run_directory=run, reviewer_count=count)
    return run


def reviewer_config_with_leg_findings(tmp_path, *, third_leg_needs_context=False):
    script = tmp_path / 'reviewer-with-independent-leg-findings.py'
    script.write_text(
        "import json,os,pathlib\n"
        "request=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "leg=pathlib.Path(request['input_file']).parent.parent.name\n"
        "finding={'problem_key':leg+'-finding','message':leg+' 已独立验证的问题'}\n"
        f"needs_context={third_leg_needs_context!r} and leg=='reviewer-3'\n"
        "incomplete=needs_context and not request.get('context_response_file')\n"
        "result={'status':'incomplete' if incomplete else 'completed','audit_summary':'按固定候选独立检查',"
        "'findings':[finding] if not needs_context or incomplete else []}\n"
        "if incomplete: result['requests']=['缺少第三腿的机器证据']\n"
        "print(json.dumps(result))\n"
    )
    return {'command': [sys.executable, str(script)], 'output_format': 'text'}


def test_three_reviewers_all_contribute_findings_and_are_reused_on_resume(repository, tmp_path):
    run = prepare_review_with_count(repository, tmp_path, 3)
    config = reviewer_config_with_leg_findings(tmp_path)
    result = resume_review(run, reviewer_configs=[config, config])
    assert result['status'] == 'ready_for_fix'
    assert result['reviewer_count'] == 3
    assert read_json(run / 'run.json')['reviewer_count'] == 3
    assert result['finding_count'] == 3
    attempts = sorted(run.glob('reviewer-*/attempt-*/outcome.json'))
    assert len(attempts) == 3
    workspaces = [(path.parent / 'workspace').resolve() for path in attempts]
    assert len(set(workspaces)) == 3
    original_outcomes = [path.read_bytes() for path in attempts]
    assert resume_review(run)['finding_count'] == 3
    assert sorted(run.glob('reviewer-*/attempt-*/outcome.json')) == attempts
    assert [path.read_bytes() for path in attempts] == original_outcomes


def test_incomplete_third_reviewer_prevents_completion_and_only_it_resumes(repository, tmp_path):
    run = prepare_review_with_count(repository, tmp_path, 3)
    config = reviewer_config_with_leg_findings(tmp_path, third_leg_needs_context=True)
    assert resume_review(run, reviewer_configs=[config, config])['status'] == 'review_incomplete'
    assert read_json(run / 'reviewer-3/attempt-001/result.json')['status'] == 'incomplete'
    response = tmp_path / 'third-reviewer-context-response.md'
    response.write_text('已补齐可复核的机器证据')
    result = resume_review(run, context_response_file=response)
    assert result['status'] == 'ready_for_fix'
    assert result['finding_count'] == 3
    assert len(list((run / 'reviewer-1').glob('attempt-*'))) == 1
    assert len(list((run / 'reviewer-2').glob('attempt-*'))) == 1
    assert len(list((run / 'reviewer-3').glob('attempt-*'))) == 2


@pytest.mark.parametrize('count', [0, 1, -1, True, 2.5])
def test_invalid_reviewer_counts_are_rejected_before_creating_a_run(repository, tmp_path, count):
    with pytest.raises(ValueError, match='reviewer'):
        prepare_review_with_count(repository, tmp_path, count)
    assert not (tmp_path / 'configured-reviewer-count-run').exists()


def test_cli_persists_three_reviewers_before_dispatch_and_resumes_all_three(repository, tmp_path):
    (repository / 'task.py').write_text('value = 2\n')
    requirements = tmp_path / 'review-requirements.md'
    requirements.write_text('通过正式CLI运行三路独立评审')
    run = tmp_path / 'cli-three-reviewer-run'
    entry = ROOT / 'plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py'
    environment = {**os.environ, 'RVF_USER_LOCAL_DATA_ROOT': str(tmp_path / 'isolated-user-runtime')}
    prepared = subprocess.run([sys.executable, str(entry), 'review-task-snapshot', '--repo', str(repository),
                               '--base-ref', 'HEAD', '--scope', 'task.py', '--requirements-file', str(requirements),
                               '--run-directory', str(run), '--reviewer-count', '3', '--prepare-only'],
                              env=environment, capture_output=True, text=True)
    assert prepared.returncode == 0, prepared.stderr
    assert read_json(run / 'run.json')['reviewer_count'] == 3
    config = tmp_path / 'reviewer-config.json'
    from test_temporary_mandatory_codex_external_reviewer import codex_cli_substitute, fake_config as direct_fake_config
    config.write_text(json.dumps(direct_fake_config(tmp_path)))
    codex_config = tmp_path / 'codex-reviewer-config.json'
    codex_config.write_text(json.dumps(codex_cli_substitute(tmp_path)))
    resumed = subprocess.run([sys.executable, str(entry), 'resume-task-review', '--run-dir', str(run),
                              '--reviewer-config', str(config), '--reviewer-config', str(config),
                              '--reviewer-config', str(codex_config)],
                             env=environment, capture_output=True, text=True)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)['status'] == 'ready_for_validation'
    assert len(list(run.glob('reviewer-*/attempt-*/outcome.json'))) == 3
