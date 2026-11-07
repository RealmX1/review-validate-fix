"""实际分发包入口与复制入口的隔离验收。"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from test_official_cli_developer_plugin_installation import _load_installer, _minimal_source_repository
from test_task_snapshot_review_workflow import repository, fake_config, prepare, ROOT


def test_packaging_never_traverses_a_runtime_state_symlink(tmp_path):
    installer=_load_installer()
    source=_minimal_source_repository(tmp_path)
    state=source/'plugins/review-validate-fix/skills/review-validate-fix/state'
    if state.exists():
        shutil.rmtree(state)
    state.symlink_to(tmp_path/'unavailable-runtime-state',target_is_directory=True)
    release=installer.build_developer_marketplace_release(source_repository=source,home_directory=tmp_path/'home',source_commit='a'*40,source_commit_count=1)
    assert not (release.release_root/'plugins/review-validate-fix/skills/review-validate-fix/state').exists()


def test_packaged_review_guidance_does_not_assign_delivery_control(tmp_path):
    installer=_load_installer()
    release=installer.build_developer_marketplace_release(source_repository=ROOT,home_directory=tmp_path/'packaged-home',source_commit='c'*40,source_commit_count=3)
    payload=release.release_root/'plugins/review-validate-fix'
    guidance_files=[release.release_root/'.claude-plugin/marketplace.json',
        *(payload/directory/'plugin.json' for directory in ('.claude-plugin','.codex-plugin','.kimi-plugin')),
        payload/'skills/review-validate-fix/agents/openai.yaml',
        payload/'skills/review-validate-fix/prompts/validate-fix.md']
    obsolete_delivery_guidance=('主会话收尾','主会话直接收尾','主会话完成最终验证和提交',
        '普通 elevation 不阻止顺利完成后的提交','主 Agent 保留最终审阅和按授权提交',
        'handoff generation','no handoff','Cline Kanban follow-up workflow')
    for guidance_file in guidance_files:
        guidance=guidance_file.read_text()
        assert guidance.strip(),guidance_file
        for obsolete in obsolete_delivery_guidance:
            assert obsolete not in guidance,(guidance_file,obsolete)


def test_packaged_cli_runs_without_source_checkout_imports(repository,tmp_path):
    installer=_load_installer()
    release=installer.build_developer_marketplace_release(source_repository=ROOT,home_directory=tmp_path/'packaged-home',source_commit='b'*40,source_commit_count=2)
    payload=release.release_root/'plugins/review-validate-fix'
    entry=payload/'skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py'
    run=prepare(repository,tmp_path)
    config_file=tmp_path/'reviewer-config.json';config_file.write_text(json.dumps(fake_config(tmp_path)))
    validation_commands=tmp_path/'validation-commands.json';validation_commands.write_text(json.dumps([[sys.executable,'-c','pass']]))
    environment={**os.environ,'PYTHONPATH':'','RVF_USER_LOCAL_DATA_ROOT':str(tmp_path/'isolated-materials')}
    completed=subprocess.run([sys.executable,str(entry),'resume-task-review','--run-dir',str(run),
                              '--reviewer-config',str(config_file),'--reviewer-config',str(config_file),
                              '--validation-commands',str(validation_commands)],
                             cwd=tmp_path,env=environment,capture_output=True,text=True)
    assert completed.returncode==0,completed.stderr
    result=json.loads(completed.stdout)
    assert result['status']=='review_and_validation_completed'
    assert 'closeout' not in result and 'commit' not in result
    business_file=run/'business-result.json'
    business_bytes=business_file.read_bytes()
    assert json.loads(business_bytes)['task_snapshot_review_result_facts']['validation']['status']=='passed'
    prompts=list(run.glob('reviewer-*/attempt-*/prompt.md'))
    assert len(prompts)==2
    assert all('独立 Reviewer' in path.read_text() for path in prompts)
    removed_arguments=[
        ['resume-task-review','--run-dir',str(run),'--closeout-report',str(tmp_path/'retired-closeout.json')],
        ['review-task-snapshot','--repo',str(repository),'--base-ref','HEAD','--scope','task.py',
         '--requirements-file',str(tmp_path/'retired-requirements.md'),'--implementation-plan-file',str(tmp_path/'retired-plan.json')],
    ]
    for arguments in removed_arguments:
        rejected=subprocess.run([sys.executable,str(entry),*arguments],cwd=tmp_path,env=environment,capture_output=True,text=True)
        assert rejected.returncode==2
        assert 'unrecognized arguments' in rejected.stderr
    assert business_file.read_bytes()==business_bytes
