"""候选修正的匿名发现回归：隔离账户、模型目录与固定 Git 候选。"""
import signal
import sys
import time
from pathlib import Path

import pytest

from test_reviewer_account_eligibility import reviewer_configuration
from test_reviewer_account_eligibility_assigned_boundaries import bound_status_configuration, publish_status
from test_task_snapshot_review_workflow import repository, prepare, fake_config, canary
from core.task_snapshot_review import reviewer_account_eligibility as eligibility
from core.task_snapshot_review import workflow, harness_cli_adapters, role_process_execution
from core.task_snapshot_review.execution_lifecycle import publish_business_result
from core.task_snapshot_review.persistent_artifacts import git, read_json, write_json


@pytest.mark.parametrize('subcommand', ['resume', 'fork'])
@pytest.mark.parametrize('options', [['--model', 'same-model'], ['--model=same-model'],
    ['--json', '--profile', 'resume', '--model', 'same-model']])
def test_codex_session_after_subcommand_options_preserves_ban(tmp_path, subcommand, options):
    executable = tmp_path / 'codex'
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o700)
    config = reviewer_configuration(tmp_path)
    def command(session):
        return [str(executable), 'exec', subcommand, *options, session]
    config['command'] = command('first-session')
    first = eligibility.reviewer_eligibility(config)
    eligibility.record_formal_invocation_result(first, started_at=time.time(),
        failure_category='account_banned', evidence_path='original-ban')
    second = eligibility.reviewer_eligibility({**config, 'command': command('second-session')})
    assert second['identity_sha256'] == first['identity_sha256']
    assert second['model_scope_sha256'] == first['model_scope_sha256']
    assert not second['can_dispatch']


def test_identical_cached_failures_preserve_both_models_and_recovery_budget(tmp_path, monkeypatch):
    config = {**bound_status_configuration(tmp_path), 'underlying_provider_account_quota_pool_identity': 'shared-pool'}
    epoch = [10000.0]
    monkeypatch.setattr(eligibility.time, 'time', lambda: epoch[0])
    identities = []
    for model in ('first', 'second'):
        scoped = {**config, 'model_scope_identity': model}
        publish_status(scoped, 'usage_limit', epoch[0], model_scope_identity=model)
        identities.append(eligibility.reviewer_eligibility(scoped, resolved_command=[*config['command'], '--model', model]))
        assert not identities[-1]['can_dispatch']
        epoch[0] += 1
    evidence = Path(identities[0]['evidence_path'])
    failure = next(iter(read_json(evidence)['negative_evidence'].values()))
    assert set(failure['model_scope_sha256s']) == {item['model_scope_sha256'] for item in identities}
    assert failure['failure_count'] == 1
    scoped = {**config, 'model_scope_identity': 'first'}
    publish_status(scoped, 'available', epoch[0], model_scope_identity='first',
        quota={'remaining': 10, 'observed_at': epoch[0], 'reset_at': 10500})
    assert not eligibility.reviewer_eligibility(scoped, resolved_command=[*config['command'], '--model', 'first'])['can_dispatch']
    assert read_json(evidence)['negative_evidence']
    publish_status(config, 'unknown', epoch[0])
    assert not eligibility.reviewer_eligibility({**config, 'model_scope_identity': 'second'},
        resolved_command=[*config['command'], '--model', 'second'])['can_dispatch']
    publish_status(config, 'available', epoch[0],
        quota={'remaining': 10, 'observed_at': epoch[0], 'reset_at': 10500})
    assert eligibility.reviewer_eligibility(config, resolved_command=[*config['command'], '--model', 'first'])['can_dispatch']


def test_new_formal_failure_preserves_new_pool_and_existing_retry_budget(tmp_path, monkeypatch):
    config = {**reviewer_configuration(tmp_path), 'underlying_provider_account_quota_pool_identity': 'first-pool'}
    epoch = [20000.0]
    monkeypatch.setattr(eligibility.time, 'time', lambda: epoch[0])
    command = [*config['command'], '--model', 'first']
    initial = eligibility.reviewer_eligibility(config, resolved_command=command)
    eligibility.record_formal_invocation_result(initial, started_at=epoch[0], failure_category='usage_limit', evidence_path='first-failure')
    renamed = {**config, 'underlying_provider_account_quota_pool_identity': 'second-pool'}
    assert eligibility.reviewer_eligibility(renamed, resolved_command=[*config['command'], '--model', 'second'])['can_dispatch']
    epoch[0] += 301
    retry = eligibility.reviewer_eligibility(renamed, resolved_command=command, reserve_recovery_for='one-recovery')
    assert retry['can_dispatch']
    for _ in range(2):
        eligibility.record_formal_invocation_result(retry, started_at=epoch[0], failure_category='usage_limit', evidence_path='second-failure')
    blocked = eligibility.reviewer_eligibility(renamed, resolved_command=[*config['command'], '--model', 'second'])
    assert not blocked['can_dispatch']
    failure = next(iter(read_json(Path(initial['evidence_path']))['negative_evidence'].values()))
    assert failure['failure_count'] == 2
    assert set(initial['declared_model_or_quota_pool_scope_sha256s'] + retry['declared_model_or_quota_pool_scope_sha256s']) <= set(failure['declared_model_or_quota_pool_scope_sha256s'])


@pytest.mark.parametrize('timeout_seconds', [20, 900])
@pytest.mark.parametrize('error_type', [ValueError, RuntimeError])
def test_catalog_failure_schedules_bounded_recovery_without_role_runtime_limit(repository, tmp_path, monkeypatch, timeout_seconds, error_type):
    run = prepare(repository, tmp_path)
    config = {**fake_config(tmp_path), 'timeout_seconds': timeout_seconds, 'reviewer_model_selection_source': 'oh_my_pi_default_model_role'}
    # 固定秒与毫秒往返会向上舍入的原失败时间，按接口单位核验严格上限。
    epoch = [1793385482.123886]
    monkeypatch.setattr(workflow.time, 'time', lambda: epoch[0])
    available = [False]
    queries = []
    def resolve(*args, **kwargs):
        queries.append(epoch[0])
        if not available[0]:
            raise error_type('隔离目录暂时失败')
        return config['command']
    monkeypatch.setattr(harness_cli_adapters, 'resolve_formal_reviewer_model_command', resolve)
    launched = []
    monkeypatch.setattr(workflow, 'launch_role', lambda attempt, **kwargs: launched.append(attempt))
    record = read_json(run / 'run.json')
    workflow._start_role(run, record, run / 'reviewer-1', 'reviewer', [config], [])
    assert not launched
    result = publish_business_result(run, {'status': 'review_incomplete'})
    retry_not_before_epoch_ms = result['recovery']['retry_not_before_epoch_ms']
    assert epoch[0] * 1000 < retry_not_before_epoch_ms <= (epoch[0] + 30) * 1000
    retry_at = retry_not_before_epoch_ms / 1000
    assert not (run / 'reviewer-1/role-deadline.json').exists()
    workflow._start_role(run, record, run / 'reviewer-1', 'reviewer', [config], [])
    assert len(queries) == 1
    available[0] = True
    epoch[0] = retry_at
    workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config], [])
    assert len(queries) == 2
    assert len(launched) == 1
    assert not (run / 'reviewer-1/role-deadline.json').exists()


@pytest.mark.parametrize('terminal_condition', ['stop', 'legacy_deadline'])
def test_persisted_catalog_recovery_preserves_stop_and_ignores_retired_deadline(repository, tmp_path, monkeypatch, terminal_condition):
    from core.task_snapshot_review.execution_lifecycle import ExecutionRevoked, persist_execution_revocation
    run = prepare(repository, tmp_path)
    config = {**fake_config(tmp_path), 'timeout_seconds': 900, 'reviewer_model_selection_source': 'oh_my_pi_default_model_role'}
    epoch = [time.time()]
    monkeypatch.setattr(workflow.time, 'time', lambda: epoch[0])
    queries = []
    available = [False]
    def unavailable_catalog(*args, **kwargs):
        queries.append(epoch[0])
        if available[0]:
            return config['command']
        raise ValueError('隔离目录暂时失败')
    monkeypatch.setattr(harness_cli_adapters, 'resolve_formal_reviewer_model_command', unavailable_catalog)
    launched = []
    monkeypatch.setattr(workflow, 'launch_role', lambda attempt, **kwargs: launched.append(attempt))
    workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config], [])
    epoch[0] += 31
    if terminal_condition == 'stop':
        persist_execution_revocation(run, '目录等待期间用户 Stop')
        with pytest.raises(ExecutionRevoked):
            workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config], [])
        assert len(queries) == 1
        assert not launched
    else:
        write_json(run / 'reviewer-1/role-deadline.json', {'deadline_at': 1})
        available[0] = True
        workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config], [])
        assert len(queries) == 2
        assert len(launched) == 1
        assert not (run / 'reviewer-1/deadline-exhausted.json').exists()
        assert read_json(run / 'reviewer-1/role-deadline.json') == {'deadline_at': 1}


def test_catalog_failure_budget_survives_restarts_without_a_role_deadline(repository, tmp_path, monkeypatch):
    run = prepare(repository, tmp_path)
    config = {**fake_config(tmp_path), 'reviewer_model_selection_source': 'oh_my_pi_default_model_role'}
    epoch = [time.time()]
    monkeypatch.setattr(workflow.time, 'time', lambda: epoch[0])
    queries = []
    def unavailable_catalog(*args, **kwargs):
        queries.append(epoch[0])
        raise ValueError('隔离目录持续失败')
    monkeypatch.setattr(harness_cli_adapters, 'resolve_formal_reviewer_model_command', unavailable_catalog)
    for _ in range(4):
        workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config], [])
        epoch[0] += 31
    assert len(queries) == 2
    assert not list((run / 'reviewer-1').glob('attempt-*'))
    assert not (run / 'reviewer-1/role-deadline.json').exists()
    result = publish_business_result(run, {'status': 'review_incomplete'})
    assert result['recovery']['maximum_unchanged_retries'] == 0
    assert result['recovery'].get('retry_not_before_epoch_ms') is None


def test_dynamic_model_uses_candidate_configuration_and_skips_blocked_model(repository, tmp_path, monkeypatch):
    configuration_path = repository / '.omp/config.yml'
    configuration_path.parent.mkdir()
    configuration_path.write_text('blocked-model')
    git(repository, 'add', '.omp/config.yml')
    git(repository, 'commit', '-qm', 'test: 固定候选模型配置')
    configuration_path.write_text('background-model')
    run = prepare(repository, tmp_path)
    executable = tmp_path / 'omp'
    query_observations = tmp_path / 'model-catalog-query-observations.jsonl'
    executable.write_text(f'#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n'
        'assert sys.argv[1:] == ["config", "get", "modelRoles", "--json"]\n'
        f'with Path({str(query_observations)!r}).open("a") as observations: observations.write(json.dumps(str(Path.cwd())) + "\\n")\n'
        'print(json.dumps({"key": "modelRoles", "value": {"default": "openai-codex/" + Path(".omp/config.yml").read_text()}}))\n')
    executable.chmod(0o700)
    config = {'command': [str(executable), '-p'], 'output_format': 'text', 'timeout_seconds': 900,
        'reviewer_model_selection_source': 'oh_my_pi_default_model_role'}
    blocked_command = [str(executable), '--model', 'openai-codex/blocked-model', '-p']
    blocked = eligibility.reviewer_eligibility(config, resolved_command=blocked_command)
    eligibility.record_formal_invocation_result(blocked, started_at=time.time(), failure_category='usage_limit', evidence_path='blocked-model-quota')
    launched = []
    monkeypatch.setattr(workflow, 'launch_role', lambda attempt, **kwargs: launched.append(attempt))
    original = canary(repository)
    for _ in range(3):
        workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config], [])
    assert not launched
    assert len(query_observations.read_text().splitlines()) == 3
    fallback = fake_config(tmp_path)
    workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config, fallback], [])
    assert len(launched) == 1
    assert read_json(launched[0] / 'request.json')['config'].get('reviewer_model_selection_source') is None
    assert canary(repository) == original


def test_worker_uses_persisted_model_selection_without_requery(repository, tmp_path, monkeypatch):
    run = prepare(repository, tmp_path)
    config = {**fake_config(tmp_path), 'reviewer_model_selection_source': 'oh_my_pi_default_model_role'}
    resolved = config['command']
    monkeypatch.setattr(harness_cli_adapters, 'resolve_formal_reviewer_model_command', lambda *args, **kwargs: resolved)
    launched = []
    monkeypatch.setattr(workflow, 'launch_role', lambda attempt, **kwargs: launched.append(attempt))
    workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config], [])
    def unexpected_query(*args, **kwargs):
        raise AssertionError('worker 不应重新解析已固定模型')
    monkeypatch.setattr(harness_cli_adapters, 'resolve_formal_reviewer_model_command', unexpected_query)
    handlers = {kind: signal.getsignal(kind) for kind in (signal.SIGTERM, signal.SIGINT)}
    try:
        role_process_execution.execute_role(launched[0])
    finally:
        for kind, handler in handlers.items():
            signal.signal(kind, handler)
    assert read_json(launched[0] / 'outcome.json')['status'] == 'completed'
    assert read_json(launched[0] / 'model-invocation.json')['command'] == resolved
