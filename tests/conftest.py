"""所有自动验收默认隔离本机配置、数据库及运行材料。"""
import pytest


@pytest.fixture(autouse=True)
def isolated_user_runtime_environment(tmp_path, monkeypatch):
    home=tmp_path/'isolated-user-home'
    home.mkdir(exist_ok=True)
    for key,value in {'HOME':home,'CODEX_HOME':home/'.codex','CODEX_SQLITE_HOME':home/'.codex-sqlite',
                      'RVF_USER_LOCAL_DATA_ROOT':home/'.rvf/user-local-data'}.items():
        monkeypatch.setenv(key,str(value))
    for key in ('KANBAN_TASK_ID','KANBAN_PROJECT_PATH','CLINE_KANBAN_TASK_ID','CLINE_KANBAN_PROJECT_PATH','RVF_SUPPRESS_STOP_HOOK'):
        monkeypatch.delenv(key,raising=False)
