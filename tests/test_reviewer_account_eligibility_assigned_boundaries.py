"""匿名发现对应的账户边界与原尝试恢复回归，全部使用隔离替身。"""
import sys
import signal
import time
from pathlib import Path

import pytest

from test_reviewer_account_eligibility import reviewer_configuration
from test_task_snapshot_review_workflow import repository, prepare, fake_config
from core.task_snapshot_review import reviewer_account_eligibility as eligibility
from core.task_snapshot_review import role_process_execution as roles
from core.task_snapshot_review.persistent_artifacts import read_json, write_json


@pytest.fixture
def preserved_test_process_signal_handlers():
    handlers = {kind: signal.getsignal(kind) for kind in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for kind, handler in handlers.items():
            signal.signal(kind, handler)


@pytest.mark.parametrize('selector', ['--session', '--session-id', '--session=', '--resume', '-S', '-Sattached'])
@pytest.mark.parametrize('failure_category', ['authentication', 'account_banned', 'usage_limit'])
def test_session_selection_preserves_shared_failure(tmp_path, selector, failure_category):
    executable = tmp_path / 'kimi'
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o700)
    config = reviewer_configuration(tmp_path)
    def command(session):
        tokens = [selector + session] if selector.endswith('=') else (
            ['-S' + session] if selector == '-Sattached' else [selector, session])
        return [str(executable), '-p', '--model', 'same-model', *tokens]
    config['command'] = command('first-session')
    first = eligibility.reviewer_eligibility(config)
    eligibility.record_formal_invocation_result(first, started_at=time.time(),
        failure_category=failure_category, evidence_path='original-failure')
    changed = eligibility.reviewer_eligibility({**config, 'command': command('second-session')})
    assert changed['identity_sha256'] == first['identity_sha256']
    assert not changed['can_dispatch']


@pytest.mark.parametrize('first,second', [(['claude', '-p', '-rfirst'], ['claude', '-p', '-rsecond']),
    (['omp', '-p', '-r', 'first'], ['omp', '-p', '-r', 'second']),
    (['omp', '-p', '--fork', 'first'], ['omp', '-p', '--fork', 'second']),
    (['codex', 'exec', 'resume', 'first'], ['codex', 'exec', 'resume', 'second']),
    (['codex', '--ask-for-approval', 'never', 'exec', 'resume', 'first'],
     ['codex', '--ask-for-approval', 'never', 'exec', 'resume', 'second']),
    (['codex', 'exec', '--json', '--profile', 'resume', 'resume', 'first'],
     ['codex', 'exec', '--json', '--profile', 'resume', 'resume', 'second']),
    (['codex', '--ask-for-approval', 'never', 'exec', '--json', 'fork', 'first'],
     ['codex', '--ask-for-approval', 'never', 'exec', '--json', 'fork', 'second'])])
def test_other_native_session_selectors_preserve_account_command(first, second):
    assert eligibility._account_identity_command(first) == eligibility._account_identity_command(second)


def test_codex_profile_named_resume_remains_part_of_route_identity():
    command = ['codex', 'exec', '--profile', 'resume']
    assert eligibility._account_identity_command(command) == command


@pytest.mark.parametrize('declaration_field', ['underlying_provider_account_quota_pool_identity', 'model_scope_identity'])
@pytest.mark.parametrize('original_declaration', [None, 'original-pool'])
def test_quota_declaration_change_preserves_failure_and_retry_budget(tmp_path, monkeypatch, declaration_field, original_declaration):
    config = reviewer_configuration(tmp_path)
    if original_declaration:
        config[declaration_field] = original_declaration
    epoch = [10000.0]
    monkeypatch.setattr(eligibility.time, 'time', lambda: epoch[0])
    first = eligibility.reviewer_eligibility(config)
    eligibility.record_formal_invocation_result(first, started_at=epoch[0], failure_category='usage_limit', evidence_path='quota-first')
    renamed = {**config, declaration_field: 'renamed-pool'}
    assert not eligibility.reviewer_eligibility(renamed)['can_dispatch']
    epoch[0] += 301
    retry = eligibility.reviewer_eligibility(renamed, reserve_recovery_for='one-recovery')
    assert retry['can_dispatch']
    eligibility.record_formal_invocation_result(retry, started_at=epoch[0], failure_category='usage_limit', evidence_path='quota-second')
    epoch[0] += 301
    exhausted = eligibility.reviewer_eligibility({**renamed, declaration_field: 'third-pool'})
    assert not exhausted['can_dispatch']
    assert exhausted['recovery']['maximum_unchanged_retries'] == 0


def test_declared_pool_still_shares_failures_between_actual_models(tmp_path):
    config = {**reviewer_configuration(tmp_path), 'underlying_provider_account_quota_pool_identity': 'shared-pool'}
    first = eligibility.reviewer_eligibility(config, resolved_command=[*config['command'], '--model', 'first'])
    eligibility.record_formal_invocation_result(first, started_at=time.time(), failure_category='usage_limit', evidence_path='quota')
    assert not eligibility.reviewer_eligibility(config, resolved_command=[*config['command'], '--model', 'second'])['can_dispatch']


def bound_status_configuration(tmp_path):
    return {**reviewer_configuration(tmp_path), 'account_identity': 'account', 'credential_source_identity': 'credentials',
        'route_identity': 'route', 'account_status_evidence_path': str(tmp_path / 'account-status.json')}


def publish_status(config, status, observed_at, **fields):
    write_json(Path(config['account_status_evidence_path']), {
        **{key: config[key] for key in ('account_identity', 'credential_source_identity', 'route_identity')},
        'status': status, 'observed_at': observed_at, 'stale': False, **fields})


def test_reconfirmed_ban_rejects_older_recovery_without_spending_retry(tmp_path, monkeypatch):
    config = bound_status_configuration(tmp_path)
    epoch = [10000.0]
    monkeypatch.setattr(eligibility.time, 'time', lambda: epoch[0])
    publish_status(config, 'account_banned', epoch[0])
    first = eligibility.reviewer_eligibility(config)
    epoch[0] += 20
    publish_status(config, 'account_banned', epoch[0])
    eligibility.reviewer_eligibility(config)
    failure = read_json(Path(first['evidence_path']))['negative_evidence']['account']
    assert failure['observed_at'] == epoch[0]
    assert failure['failure_count'] == 1
    publish_status(config, 'available', epoch[0] - 10)
    assert not eligibility.reviewer_eligibility(config)['can_dispatch']
    epoch[0] += 1
    publish_status(config, 'available', epoch[0])
    assert eligibility.reviewer_eligibility(config)['can_dispatch']


@pytest.mark.parametrize('quota_sample_offset,expected_dispatch', [(-10, False), (1, True)])
def test_quota_recovery_requires_sample_newer_than_formal_failure(tmp_path, monkeypatch, quota_sample_offset, expected_dispatch):
    config = bound_status_configuration(tmp_path)
    epoch = [10000.0]
    monkeypatch.setattr(eligibility.time, 'time', lambda: epoch[0])
    initial = eligibility.reviewer_eligibility(config)
    eligibility.record_formal_invocation_result(initial, started_at=epoch[0], failure_category='usage_limit', evidence_path='quota')
    epoch[0] += 2
    publish_status(config, 'available', epoch[0], quota={'remaining': 10, 'observed_at': 10000 + quota_sample_offset, 'reset_at': 10500})
    assert eligibility.reviewer_eligibility(config)['can_dispatch'] is expected_dispatch


@pytest.mark.parametrize('final_status,expected_dispatch', [('unknown', True), ('unavailable', False)])
def test_status_queries_cover_original_deadline_at_normal_frequency(tmp_path, monkeypatch, final_status, expected_dispatch):
    config = reviewer_configuration(tmp_path)
    epoch = [10000.0]
    monkeypatch.setattr(eligibility.time, 'time', lambda: epoch[0])
    status_file = tmp_path / 'health-response-status'
    calls_file = tmp_path / 'non-inference-query-executions'
    executable = tmp_path / 'curl'
    executable.write_text(f'#!{sys.executable}\nfrom pathlib import Path\n'
        f'with Path({str(calls_file)!r}).open("a") as calls: calls.write("checked\\n")\n'
        f'print("{{}}\\n" + Path({str(status_file)!r}).read_text(), end="")\n')
    executable.chmod(0o700)
    config['health_command'] = [str(executable), '-fsS', 'http://127.0.0.1/health']
    run = tmp_path / 'original-run'
    write_json(run / 'run.json', {})
    statuses = (200, 200, 200, 503, 200) if final_status == 'unknown' else (200, 200, 200, 200, 503)
    for offset, http_status in zip((0, 31, 62, 93, 394), statuses):
        epoch[0] = 10000 + offset
        status_file.write_text(str(http_status))
        result = eligibility.reviewer_eligibility(config, run_directory=run, deadline_at=10900)
    assert len(calls_file.read_text().splitlines()) == 5
    assert result['can_dispatch'] is expected_dispatch


@pytest.mark.parametrize('terminal_condition', [None, 'stop', 'deadline', 'already_started'])
def test_worker_resumes_own_claim_before_invocation_without_new_attempt(repository, tmp_path, terminal_condition, preserved_test_process_signal_handlers):
    run = prepare(repository, tmp_path)
    config = fake_config(tmp_path)
    attempt = run / 'reviewer-1/attempt-001'
    first = eligibility.reviewer_eligibility(config)
    eligibility.record_formal_invocation_result(first, started_at=time.time(), failure_category='authentication', evidence_path='auth')
    record = read_json(Path(first['evidence_path']))
    record['negative_evidence']['account']['retry_not_before'] = time.time() - 1
    write_json(Path(first['evidence_path']), record)
    claimed = eligibility.reviewer_eligibility(config, reserve_recovery_for=str(attempt))
    assert claimed['can_dispatch']
    assert not eligibility.reviewer_eligibility(config, reserve_recovery_for='another-attempt')['can_dispatch']
    write_json(attempt / 'request.json', {'role': 'reviewer', 'config': config, 'run_directory': str(run),
        'repository': str(repository), 'workspace': str(attempt / 'workspace'),
        'candidate_commit': read_json(run / 'run.json')['candidate_commit'],
        'input_file': str(attempt / 'request.json'), 'result_file': str(attempt / 'role-output.json')})
    (attempt / 'prompt.md').write_text('验证原尝试恢复。')
    if terminal_condition == 'stop':
        from core.task_snapshot_review.execution_lifecycle import persist_execution_revocation
        persist_execution_revocation(run, '验证 Stop 优先于恢复认领')
    elif terminal_condition == 'deadline':
        write_json(attempt.parent / 'role-deadline.json', {'deadline_at': time.time() - 1})
    elif terminal_condition == 'already_started':
        write_json(attempt / 'model-invocation.json', {'started_at': time.time()})
    roles.execute_role(attempt)
    if terminal_condition:
        assert read_json(attempt / 'outcome.json')['status'] != 'completed'
        assert not (attempt / 'stdout.txt').exists()
        return
    assert read_json(attempt / 'outcome.json')['status'] == 'completed'
    invocation = (attempt / 'model-invocation.json').read_bytes()
    roles.execute_role(attempt)
    assert (attempt / 'model-invocation.json').read_bytes() == invocation
    assert len(list(attempt.parent.glob('attempt-*'))) == 1


def test_shorter_seat_budget_does_not_exhaust_another_original_deadline(tmp_path, monkeypatch):
    config = reviewer_configuration(tmp_path)
    epoch = [10000.0]
    monkeypatch.setattr(eligibility.time, 'time', lambda: epoch[0])
    calls = []
    def query(*args):
        calls.append(epoch[0])
        return {'status': 'unknown', 'reason': 'route_reachable_account_not_verified'}
    monkeypatch.setattr(eligibility, '_query_non_inference_status', query)
    run = tmp_path / 'shared-run'
    eligibility.reviewer_eligibility(config, run_directory=run, deadline_at=10060)
    epoch[0] += 31
    eligibility.reviewer_eligibility(config, run_directory=run, deadline_at=10060)
    for offset in (62, 93, 124, 155):
        epoch[0] = 10000 + offset
        eligibility.reviewer_eligibility(config, run_directory=run, deadline_at=10900)
    assert len(calls) == 6


def test_late_success_after_pool_rename_does_not_override_newer_failure(tmp_path, monkeypatch):
    config = {**reviewer_configuration(tmp_path), 'underlying_provider_account_quota_pool_identity': 'original-pool'}
    epoch = [10000.0]
    monkeypatch.setattr(eligibility.time, 'time', lambda: epoch[0])
    older = eligibility.reviewer_eligibility(config)
    epoch[0] += 1
    eligibility.record_formal_invocation_result(older, started_at=10000, failure_category='usage_limit', evidence_path='quota')
    renamed = eligibility.reviewer_eligibility({**config, 'underlying_provider_account_quota_pool_identity': 'renamed-pool'})
    epoch[0] += 1
    eligibility.record_formal_invocation_result(renamed, started_at=10000, success=True, evidence_path='late-success')
    assert not eligibility.reviewer_eligibility(config)['can_dispatch']


def test_exhausted_status_budget_never_reuses_an_expired_positive_observation(tmp_path, monkeypatch):
    config = reviewer_configuration(tmp_path)
    epoch = [10000.0]
    monkeypatch.setattr(eligibility.time, 'time', lambda: epoch[0])
    run = tmp_path / 'original-run'
    first = eligibility.reviewer_eligibility(config, run_directory=run, deadline_at=10900)
    record = read_json(Path(first['evidence_path']))
    for budget in record['non_inference_check_budgets_by_run'].values():
        budget['maximum_attempts'] = budget['attempt_count']
    write_json(Path(first['evidence_path']), record)
    epoch[0] += 31
    result = eligibility.reviewer_eligibility(config, run_directory=run, deadline_at=10900)
    assert result['status'] == 'unknown'
    assert not result['can_dispatch']
    assert result['non_inference_observation']['stale']


def test_dynamic_model_quota_failure_does_not_dispatch_repeated_unavailable_attempts(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review import workflow, harness_cli_adapters
    run = prepare(repository, tmp_path)
    config = fake_config(tmp_path)
    executable = tmp_path / 'omp'
    executable.write_text(f'#!{sys.executable}\nraise SystemExit("只能通过隔离派发替身调用")\n')
    executable.chmod(0o700)
    config.update(command=[str(executable), '-p'], output_format='oh_my_pi_json',
                  reviewer_model_selection_source='oh_my_pi_default_model_role')
    resolved = [str(executable), '--model', 'openai-codex/fixture-model', '-p']
    monkeypatch.setattr(harness_cli_adapters, 'resolve_formal_reviewer_model_command', lambda *args, **kwargs: resolved)
    launched = []
    def launch_role_substitute(attempt, **kwargs):
        selected = eligibility.reviewer_eligibility(read_json(attempt / 'request.json')['config'], resolved_command=resolved)
        if not launched:
            eligibility.record_formal_invocation_result(selected, started_at=time.time(), failure_category='usage_limit',
                evidence_path=str(attempt / 'synthetic-native-failure.json'))
            recovery = {'category': 'usage_limit'}
        else:
            assert not selected['can_dispatch']
            recovery = {'category': 'reviewer_unavailable'}
        launched.append(attempt)
        write_json(attempt / 'outcome.json', {'status': 'failed', 'retryable': False, 'recovery': recovery})
    monkeypatch.setattr(workflow, 'launch_role', launch_role_substitute)
    record = read_json(run / 'run.json')
    for _ in range(3):
        workflow._start_role(run, record, run / 'reviewer-1', 'reviewer', [config], [])
    assert len(launched) == 1
    assert len(list((run / 'reviewer-1').glob('attempt-*'))) == 1


def test_model_selection_interface_error_keeps_unknown_and_uses_another_reviewer(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review import workflow, harness_cli_adapters
    run = prepare(repository, tmp_path)
    good = fake_config(tmp_path)
    pending = {**good, 'reviewer_model_selection_source': 'oh_my_pi_default_model_role'}
    def unavailable_model_catalog(*args, **kwargs):
        raise ValueError('隔离模型目录接口暂不可用')
    monkeypatch.setattr(harness_cli_adapters, 'resolve_formal_reviewer_model_command', unavailable_model_catalog)
    launched = []
    monkeypatch.setattr(workflow, 'launch_role', lambda attempt, **kwargs: launched.append(attempt))
    workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [pending, good], [])
    assert len(launched) == 1
    assert read_json(launched[0] / 'request.json')['config'].get('reviewer_model_selection_source') is None
    observations = read_json(run / 'reviewer-1/reviewer-eligibility.json')['observations']
    assert observations[0]['status'] == 'unknown'
    assert not observations[0]['can_dispatch']


def test_pool_rename_does_not_relabel_unrelated_model_failures(tmp_path):
    config = {**reviewer_configuration(tmp_path), 'underlying_provider_account_quota_pool_identity': 'original-pool'}
    first_command = [*config['command'], '--model', 'first']
    second_command = [*config['command'], '--model', 'second']
    first = eligibility.reviewer_eligibility(config, resolved_command=first_command)
    eligibility.record_formal_invocation_result(first, started_at=time.time(), failure_category='usage_limit', evidence_path='quota')
    renamed = {**config, 'underlying_provider_account_quota_pool_identity': 'another-pool'}
    assert not eligibility.reviewer_eligibility(renamed, resolved_command=first_command)['can_dispatch']
    assert eligibility.reviewer_eligibility(renamed, resolved_command=second_command)['can_dispatch']


def test_model_scope_and_pool_with_same_label_are_distinct_evidence(tmp_path):
    config = reviewer_configuration(tmp_path)
    first = eligibility.reviewer_eligibility({**config, 'model_scope_identity': 'same-label'},
        resolved_command=[*config['command'], '--model', 'first'])
    eligibility.record_formal_invocation_result(first, started_at=time.time(), failure_category='usage_limit', evidence_path='quota')
    second = eligibility.reviewer_eligibility({**config, 'underlying_provider_account_quota_pool_identity': 'same-label'},
        resolved_command=[*config['command'], '--model', 'second'])
    assert second['can_dispatch']


def test_model_specific_quota_does_not_clear_another_models_failure(tmp_path, monkeypatch):
    config = {**bound_status_configuration(tmp_path), 'underlying_provider_account_quota_pool_identity': 'shared-pool',
              'model_scope_identity': 'first-model'}
    epoch = [10000.0]
    monkeypatch.setattr(eligibility.time, 'time', lambda: epoch[0])
    first_command = [*config['command'], '--model', 'first']
    first = eligibility.reviewer_eligibility(config, resolved_command=first_command)
    eligibility.record_formal_invocation_result(first, started_at=epoch[0], failure_category='usage_limit', evidence_path='quota')
    epoch[0] += 1
    second_config = {**config, 'model_scope_identity': 'second-model'}
    publish_status(second_config, 'available', epoch[0], model_scope_identity='second-model',
        quota={'remaining': 10, 'observed_at': epoch[0], 'reset_at': 10500})
    eligibility.reviewer_eligibility(second_config, resolved_command=[*config['command'], '--model', 'second'])
    assert not eligibility.reviewer_eligibility(config, resolved_command=first_command)['can_dispatch']


@pytest.mark.parametrize('query_raises_revocation', [False, True])
def test_stop_during_model_selection_is_never_downgraded_to_metadata_wait(repository, tmp_path, monkeypatch, query_raises_revocation):
    from core.task_snapshot_review import workflow, harness_cli_adapters
    from core.task_snapshot_review.execution_lifecycle import ExecutionRevoked, persist_execution_revocation
    run = prepare(repository, tmp_path)
    config = {**fake_config(tmp_path), 'reviewer_model_selection_source': 'oh_my_pi_default_model_role'}
    def stop_during_metadata_query(*args, **kwargs):
        persist_execution_revocation(run, '隔离测试：模型目录查询期间用户 Stop')
        if query_raises_revocation:
            raise ExecutionRevoked('目录查询已观察到撤销')
        return config['command']
    monkeypatch.setattr(harness_cli_adapters, 'resolve_formal_reviewer_model_command', stop_during_metadata_query)
    with pytest.raises(ExecutionRevoked):
        workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config], [])
    assert not list((run / 'reviewer-1').glob('attempt-*'))
