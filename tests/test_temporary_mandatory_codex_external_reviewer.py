"""临时三席外审在创建时冻结，恢复和 fallback 都遵守原分配。"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
import shlex
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_task_snapshot_review_workflow import ROOT, repository, fake_config as python_fake_config
from core.task_snapshot_review import task_snapshot, workflow
from core.task_snapshot_review.persistent_artifacts import read_json, write_json


DEADLINE = datetime(2026, 11, 2, 5, tzinfo=timezone.utc).timestamp()
ENTRYPOINT = ROOT / 'plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py'


def fake_config(tmp_path, **kwargs):
    config = python_fake_config(tmp_path, **kwargs)
    script = Path(config['command'][1])
    executable = tmp_path / 'claude'
    executable.write_text(f'#!{sys.executable}\n' + script.read_text())
    executable.chmod(0o700)
    return {**config, 'command': [str(executable), '-p']}


def create_via_cli(repository, tmp_path, monkeypatch, *, now=DEADLINE - 1, count=None, managed=False):
    monkeypatch.syspath_prepend(str(ENTRYPOINT.parent))
    spec = importlib.util.spec_from_file_location('temporary_policy_cli', ENTRYPOINT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'schedule_pending_issue_delivery', lambda *args: False)
    (repository / 'task.py').write_text('value = 2\n')
    requirements = tmp_path / 'temporary-policy-requirements.md'
    requirements.write_text('两路普通外审加必选外部 Codex，综合全部发现')
    run = tmp_path / 'temporary-policy-run'
    arguments = ['review-task-snapshot', '--repo', str(repository), '--base-ref', 'HEAD',
                 '--scope', 'task.py', '--requirements-file', str(requirements), '--run-directory', str(run)]
    if count is not None:
        arguments.extend(['--reviewer-count', str(count)])
    if managed:
        import adapters.cline_kanban.owned_command_execution as managed_execution
        def start_managed(run_directory, *args, **kwargs):
            record = read_json(run_directory / 'run.json')
            assert record['reviewer_count'] == 3
            assert record['reviewer_harness_constraints'] == ['non_codex_external_cli'] * 2 + ['codex_external_cli']
            return {'status': 'managed_execution_started'}
        monkeypatch.setattr(managed_execution, 'start_managed_review', start_managed)
        arguments.extend(['--kanban-task-id', 'fixture-task', '--kanban-project-path', str(repository)])
    else:
        arguments.append('--prepare-only')
    with monkeypatch.context() as creation_clock:
        creation_clock.setattr(task_snapshot, 'time', SimpleNamespace(time=lambda: now))
        assert module.main(arguments) == 0
    return run


@pytest.mark.parametrize('now,count,expected', [
    (DEADLINE - 1, None, 3), (DEADLINE - 1, 2, 3), (DEADLINE - 1, 5, 5),
    (DEADLINE, None, 2), (DEADLINE + 1, None, 2), (DEADLINE, 5, 5),
])
def test_formal_creation_freezes_expiring_allocation(repository, tmp_path, monkeypatch, now, count, expected):
    run = create_via_cli(repository, tmp_path, monkeypatch, now=now, count=count)
    record = read_json(run / 'run.json')
    assert record['reviewer_count'] == expected
    expected_constraints = (['non_codex_external_cli'] * (expected - 1) + ['codex_external_cli']
                            if now < DEADLINE else ['any_external_cli'] * expected)
    assert record['reviewer_harness_constraints'] == expected_constraints


def test_managed_creation_freezes_the_same_allocation(repository, tmp_path, monkeypatch):
    create_via_cli(repository, tmp_path, monkeypatch, managed=True)


def codex_cli_substitute(tmp_path, *, failure=None, findings=None):
    directory = tmp_path / 'codex-cli-substitute'
    directory.mkdir(exist_ok=True)
    executable = directory / 'codex'
    executable.write_text(f'#!{sys.executable}\n' +
        'import json,os,pathlib,sys\n'
        "if '--version' in sys.argv: print('isolated-codex-cli'); sys.exit(0)\n"
        "assert 'exec' in sys.argv\nassert os.environ['RVF_SUPPRESS_STOP_HOOK']=='1'\n"
        "request=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "assert pathlib.Path.cwd()==pathlib.Path(request['workspace'])\n"
        "assert pathlib.Path('task.py').read_text()=='value = 2\\n'\n"
        + (f"print({failure!r},file=sys.stderr); sys.exit(1)\n" if failure else
           f"print(json.dumps({dict(status='completed', audit_summary='独立 Codex 外部执行', findings=findings or [])!r}))\n"))
    executable.chmod(0o700)
    return {'command': [str(executable), 'exec', '-'], 'output_format': 'text', 'timeout_seconds': 40}


def test_expired_prepared_run_keeps_codex_and_merges_every_report(repository, tmp_path, monkeypatch):
    run = create_via_cli(repository, tmp_path, monkeypatch)
    monkeypatch.setattr(task_snapshot, 'time', SimpleNamespace(time=lambda: DEADLINE + 1))
    ordinary = fake_config(tmp_path, findings=[{'problem_key': 'ordinary', 'message': '普通发现'},
                                               {'problem_key': 'shared', 'message': '共同问题', 'evidence': '普通证据'}])
    codex = codex_cli_substitute(tmp_path, findings=[{'problem_key': 'codex', 'message': 'Codex 独有发现'},
                                                  {'problem_key': 'shared', 'message': '共同问题', 'evidence': 'Codex 补充证据'}])
    monkeypatch.setenv('RVF_HOST_ADAPTER', 'codex')
    result = workflow.resume_review(run, reviewer_configs=[ordinary, codex])
    assert result['status'] == 'ready_for_fix'
    findings = read_json(run / 'findings.json')['findings']
    assert {finding['problem_key'] for finding in findings} == {'ordinary', 'shared', 'codex'}
    assert next(f for f in findings if f['problem_key'] == 'shared')['additional_evidence'][0]['evidence'] == 'Codex 补充证据'
    attempts = sorted(run.glob('reviewer-*/attempt-*/request.json'))
    assert len(attempts) == 3
    assert len({read_json(path)['workspace'] for path in attempts}) == 3
    assert read_json(attempts[0])['config']['command'] == ordinary['command']
    assert read_json(attempts[1])['config']['command'] == ordinary['command']
    assert read_json(attempts[2])['config']['command'] == codex['command']
    originals = {path: path.read_bytes() for path in run.glob('reviewer-*/attempt-*/outcome.json')}
    assert workflow.resume_review(run)['status'] == 'ready_for_fix'
    assert {path: path.read_bytes() for path in run.glob('reviewer-*/attempt-*/outcome.json')} == originals


@pytest.mark.parametrize('failure', ['authentication failed', 'rate limit exceeded'])
def test_required_codex_failure_cannot_fallback_and_only_missing_seat_recovers(repository, tmp_path, monkeypatch, failure):
    run = create_via_cli(repository, tmp_path, monkeypatch)
    ordinary = fake_config(tmp_path)
    codex = codex_cli_substitute(tmp_path, failure=failure)
    assert workflow.resume_review(run, reviewer_configs=[ordinary, codex])['status'] == 'review_incomplete'
    outcomes = sorted(run.glob('reviewer-*/attempt-*/outcome.json'))
    assert len(outcomes) == 3
    assert read_json(outcomes[-1])['recovery']['category'] in {'authentication', 'usage_limit'}
    assert workflow.resume_review(run)['status'] == 'review_incomplete'
    assert sorted(run.glob('reviewer-*/attempt-*/outcome.json')) == outcomes
    codex = codex_cli_substitute(tmp_path)
    assert workflow.resume_review(run, reviewer_configs=[ordinary, codex])['status'] == 'ready_for_validation'
    assert len(list((run / 'reviewer-1').glob('attempt-*'))) == 1
    assert len(list((run / 'reviewer-2').glob('attempt-*'))) == 1
    assert len(list((run / 'reviewer-3').glob('attempt-*'))) == 2


def test_all_ordinary_fallbacks_exclude_codex(repository, tmp_path, monkeypatch):
    run = create_via_cli(repository, tmp_path, monkeypatch)
    codex = codex_cli_substitute(tmp_path)
    broken = {'command': [sys.executable, '-c', "print('invalid')"], 'output_format': 'text'}
    ordinary = fake_config(tmp_path)
    assert workflow.resume_review(run, reviewer_configs=[broken, codex, ordinary])['status'] == 'ready_for_validation'
    requests = [read_json(path) for role in ('reviewer-1', 'reviewer-2') for path in (run / role).glob('attempt-*/request.json')]
    assert all(Path(request['config']['command'][0]).name != 'codex' for request in requests)
    assert any(request['config']['command'] == ordinary['command'] for request in requests)


def test_missing_codex_cli_leaves_recovery_and_allows_ordinary_reports(repository, tmp_path, monkeypatch):
    run = create_via_cli(repository, tmp_path, monkeypatch)
    original_default = workflow.default_reviewer_configurations
    monkeypatch.setattr(workflow.shutil, 'which', lambda executable: None if executable == 'codex' else executable)
    ordinary = fake_config(tmp_path)
    assert workflow.resume_review(run, reviewer_configs=[ordinary, ordinary])['status'] == 'review_incomplete'
    assert read_json(run / 'reviewer-1/attempt-001/outcome.json')['status'] == 'completed'
    assert read_json(run / 'reviewer-2/attempt-001/outcome.json')['status'] == 'completed'
    assert not list((run / 'reviewer-3').glob('attempt-*'))
    assert read_json(run / 'run.json')['reviewer_configuration_recovery'][0]['role'] == 'reviewer-3'
    assert original_default(harness_constraint='codex_external_cli') == []


@pytest.mark.parametrize('wrapper', ['environment', 'shell', 'python', 'node', 'custom'])
def test_wrapped_codex_cannot_fill_ordinary_seats(repository, tmp_path, monkeypatch, wrapper):
    run = create_via_cli(repository, tmp_path, monkeypatch)
    codex = codex_cli_substitute(tmp_path)
    command = ['/usr/bin/env', *codex['command']] if wrapper == 'environment' else ['/bin/sh', '-c', 'exec ' + shlex.join(codex['command'])]
    if wrapper in {'python', 'node', 'custom'}:
        command = [sys.executable if wrapper == 'python' else wrapper, '-c', 'import os; os.execv(' + repr(codex['command'][0]) + ', ' + repr(codex['command']) + ')']
    wrapped = {**codex, 'command': command}
    assert workflow.resume_review(run, reviewer_configs=[wrapped, codex])['status'] == 'review_incomplete'
    assert not list((run / 'reviewer-1').glob('attempt-*'))
    assert not list((run / 'reviewer-2').glob('attempt-*'))
    assert read_json(run / 'reviewer-3/attempt-001/outcome.json')['status'] == 'completed'


def test_default_pool_rediscovers_recovered_ordinary_cli_without_repeating_codex(repository, tmp_path, monkeypatch):
    import core.rvf_user_local_data_paths as paths
    run = create_via_cli(repository, tmp_path, monkeypatch)
    codex = codex_cli_substitute(tmp_path)
    ordinary_executable = tmp_path / 'recoverable' / 'claude'
    ordinary_executable.parent.mkdir()
    ordinary = {'command': [str(ordinary_executable), '-p'], 'output_format': 'text'}
    registry = tmp_path / 'recovery-reviewer-registry.json'
    harnesses = {}
    for name, config in [('ordinary', ordinary), ('codex', codex)]:
        path = tmp_path / f'{name}-configuration.json'
        write_json(path, config)
        harnesses[name] = {'enabled': True, 'config_path': str(path)}
    write_json(registry, {'harnesses': harnesses})
    monkeypatch.setattr(paths, 'resolved_rvf_reviewer_registry_path', lambda bundled: registry)
    assert workflow.resume_review(run)['status'] == 'review_incomplete'
    codex_result = (run / 'reviewer-3/attempt-001/outcome.json').read_bytes()
    script = Path(python_fake_config(tmp_path)['command'][1])
    ordinary_executable.write_text(f'#!{sys.executable}\n' + script.read_text())
    ordinary_executable.chmod(0o700)
    assert workflow.resume_review(run)['status'] == 'ready_for_validation'
    assert len(list((run / 'reviewer-3').glob('attempt-*'))) == 1
    assert (run / 'reviewer-3/attempt-001/outcome.json').read_bytes() == codex_result


def test_default_ordinary_pool_preserves_oh_my_pi_and_quota_preference(tmp_path, monkeypatch):
    import core.rvf_user_local_data_paths as paths
    registry = tmp_path / 'reviewer-registry.json'
    harnesses = {}
    for name, command, pool, priority in [('codex', ['codex', 'exec', '-'], 'openai', 200),
                                          ('oh_my_pi', ['omp', '--print'], 'openai', 100),
                                          ('claude', ['claude', '--print'], 'anthropic', 50)]:
        config = tmp_path / f'{name}.json'
        write_json(config, {'command': command, 'output_format': 'text'})
        harnesses[name] = {'enabled': True, 'priority_default': priority, 'config_path': str(config),
                           'underlying_provider_account_quota_pool_identity': pool}
    write_json(registry, {'harnesses': harnesses})
    monkeypatch.setattr(paths, 'resolved_rvf_reviewer_registry_path', lambda bundled: registry)
    monkeypatch.setattr(workflow.shutil, 'which', lambda executable: executable)
    selected = workflow.default_reviewer_configurations(harness_constraint='non_codex_external_cli')
    assert [config['command'][0] for config in selected] == ['omp', 'claude']
    assert [config['command'][0] for config in workflow.default_reviewer_configurations()] == ['codex', 'claude', 'omp']


def test_missing_codex_deadline_survives_late_configuration(repository, tmp_path, monkeypatch):
    run = create_via_cli(repository, tmp_path, monkeypatch)
    ordinary = fake_config(tmp_path)
    monkeypatch.setattr(workflow, 'default_reviewer_configurations', lambda **kwargs: [])
    assert workflow.resume_review(run, reviewer_configs=[ordinary, ordinary])['status'] == 'review_incomplete'
    deadline = read_json(run / 'reviewer-3/role-deadline.json')
    with monkeypatch.context() as clock:
        clock.setattr(workflow, 'time', SimpleNamespace(time=lambda: deadline['deadline_at'] + 1))
        assert workflow.resume_review(run, reviewer_configs=[ordinary, codex_cli_substitute(tmp_path)])['status'] == 'review_incomplete'
    assert read_json(run / 'reviewer-3/role-deadline.json') == deadline
    assert not list((run / 'reviewer-3').glob('attempt-*'))
