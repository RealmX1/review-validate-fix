"""轻量 hook 受众与宿主字段的行为验证。"""
import json
import sys
from pathlib import Path

import pytest

from adapters import resolve_hook_host_adapter
from core.task_snapshot_review.hook_entrypoint import handle_hook
from test_task_snapshot_review_workflow import repository


@pytest.mark.parametrize('host',['codex','claude_code','kimi_code'])
def test_each_host_only_receives_its_supported_stop_fields(repository,monkeypatch,host):
    monkeypatch.setenv('KANBAN_TASK_ID','isolated-task')
    (repository/'task.py').write_text('value = 2\n')
    payload=handle_hook({'cwd':str(repository),'session_id':'session-1'},event_name='Stop',host_adapter=resolve_hook_host_adapter(host))
    assert payload
    if host=='codex':
        assert payload['decision']=='block'
        assert 'hookSpecificOutput' not in payload
    elif host=='claude_code':
        assert payload['hookSpecificOutput']['additionalContext']
        assert 'decision' not in payload
    else:
        assert payload['hookSpecificOutput']['permissionDecision']=='deny'
        assert 'decision' not in payload
    assert handle_hook({'cwd':str(repository),'session_id':'session-1'},event_name='Stop',host_adapter=resolve_hook_host_adapter(host)) is None


def test_unbound_or_suppressed_session_never_gets_automatic_rvf(repository,monkeypatch):
    (repository/'task.py').write_text('value = 2\n')
    adapter=resolve_hook_host_adapter('codex')
    assert handle_hook({'cwd':str(repository)},event_name='Stop',host_adapter=adapter) is None
    monkeypatch.setenv('KANBAN_TASK_ID','isolated-task')
    monkeypatch.setenv('RVF_SUPPRESS_STOP_HOOK','1')
    assert handle_hook({'cwd':str(repository)},event_name='Stop',host_adapter=adapter) is None


@pytest.mark.parametrize('host',['codex','claude_code','kimi_code'])
def test_manual_trigger_is_short_context_for_the_existing_user_turn(host):
    payload=handle_hook({'prompt':'/review-validate-fix'},event_name='UserPromptSubmit',host_adapter=resolve_hook_host_adapter(host))
    assert payload
    assert 'decision' not in payload
    if host=='kimi_code':
        assert set(payload)=={'message'}
    assert handle_hook({'prompt':'引用 /review-validate-fix 说明'},event_name='UserPromptSubmit',host_adapter=resolve_hook_host_adapter(host)) is None


def test_hook_failure_has_no_agent_driving_stdout(tmp_path):
    import subprocess
    script=Path(__file__).resolve().parents[1]/'plugins/review-validate-fix/skills/review-validate-fix/scripts/task_snapshot_review_hook.py'
    result=subprocess.run([sys.executable,str(script)],input='invalid JSON',text=True,capture_output=True)
    assert result.returncode==0
    assert not result.stdout
    assert '诊断' in result.stderr
