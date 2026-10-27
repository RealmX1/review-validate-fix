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


def test_packaged_cli_runs_without_source_checkout_imports(repository,tmp_path):
    installer=_load_installer()
    release=installer.build_developer_marketplace_release(source_repository=ROOT,home_directory=tmp_path/'packaged-home',source_commit='b'*40,source_commit_count=2)
    payload=release.release_root/'plugins/review-validate-fix'
    entry=payload/'skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py'
    run=prepare(repository,tmp_path)
    config_file=tmp_path/'reviewer-config.json';config_file.write_text(json.dumps(fake_config(tmp_path)))
    environment={**os.environ,'PYTHONPATH':'','RVF_USER_LOCAL_DATA_ROOT':str(tmp_path/'isolated-materials')}
    completed=subprocess.run([sys.executable,str(entry),'resume-task-review','--run-dir',str(run),
                              '--reviewer-config',str(config_file),'--reviewer-config',str(config_file)],
                             cwd=tmp_path,env=environment,capture_output=True,text=True)
    assert completed.returncode==0,completed.stderr
    assert json.loads(completed.stdout)['status']=='ready_for_validation'
    prompts=list(run.glob('reviewer-*/attempt-*/prompt.md'))
    assert len(prompts)==2
    assert all('独立 Reviewer' in path.read_text() for path in prompts)
