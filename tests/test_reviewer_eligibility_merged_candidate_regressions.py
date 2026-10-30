"""整合候选的模型身份、目录恢复、席位恢复提示及故障落盘回归。"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

from test_reviewer_account_eligibility import reviewer_configuration
from test_reviewer_account_eligibility_assigned_boundaries import preserved_test_process_signal_handlers
from test_task_snapshot_review_workflow import repository, prepare, fake_config, canary
from test_temporary_mandatory_codex_external_reviewer import codex_cli_substitute, fake_config as native_claude_configuration
from core.task_snapshot_review import reviewer_account_eligibility as eligibility
from core.task_snapshot_review import workflow, role_process_execution
from core.task_snapshot_review.execution_lifecycle import publish_business_result
from core.task_snapshot_review.persistent_artifacts import read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot


@pytest.fixture(autouse=True)
def isolated_native_model_configuration_environment(monkeypatch):
    for name in ('PI_CODING_AGENT_DIR', 'PI_CONFIG_DIR', 'PI_CONFIG_FILES', 'OMP_PROFILE', 'PI_PROFILE', 'KIMI_CODE_HOME'):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize('selector', [['-msecond'], ['-m', 'second'], ['--model', 'second'], ['--model=second']])
@pytest.mark.parametrize('category', ['authentication', 'account_banned'])
def test_kimi_all_native_model_selectors_preserve_account_failure(tmp_path, selector, category):
    executable = tmp_path / 'kimi'
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o700)
    config = {**reviewer_configuration(tmp_path), 'command': [str(executable), '-mfirst']}
    first = eligibility.reviewer_eligibility(config)
    eligibility.record_formal_invocation_result(first, started_at=time.time(), failure_category=category, evidence_path='native-failure')
    changed = eligibility.reviewer_eligibility({**config, 'command': [str(executable), *selector]})
    assert changed['identity_sha256'] == first['identity_sha256']
    assert changed['can_dispatch'] is False


@pytest.mark.parametrize('executable_name,selector', [('codex', ['--model=fixed']), ('codex', ['-m', 'fixed']),
    ('kimi', ['-mfixed']), ('kimi', ['--model=fixed'])])
def test_equivalent_model_selectors_preserve_exhausted_quota_budget(tmp_path, monkeypatch, executable_name, selector):
    executable = tmp_path / executable_name
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o700)
    config = {**reviewer_configuration(tmp_path), 'command': [str(executable), '--model', 'fixed']}
    epoch = [time.time()]
    monkeypatch.setattr(eligibility.time, 'time', lambda: epoch[0])
    first = eligibility.reviewer_eligibility(config)
    for index in range(2):
        eligibility.record_formal_invocation_result(first, started_at=epoch[0], failure_category='usage_limit', evidence_path=f'quota-{index}')
        epoch[0] += 301
    changed = eligibility.reviewer_eligibility({**config, 'command': [str(executable), *selector]})
    assert changed['identity_sha256'] == first['identity_sha256']
    assert changed['model_scope_sha256'] == first['model_scope_sha256']
    assert changed['can_dispatch'] is False
    assert changed['recovery']['maximum_unchanged_retries'] == 0


@pytest.mark.parametrize('boundary', ['kimi_account', 'codex_quota'])
def test_worker_rechecks_normalized_model_identity_after_controller_selection(repository, tmp_path, monkeypatch,
        preserved_test_process_signal_handlers, boundary):
    run = prepare(repository, tmp_path)
    marker = tmp_path / 'unexpected-formal-invocation'
    executable = tmp_path / ('kimi' if boundary == 'kimi_account' else 'codex')
    executable.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n')
    executable.chmod(0o700)
    config = {**reviewer_configuration(tmp_path), 'command': [str(executable), '-mfirst']}
    changed = {**config, 'command': [str(executable), '-msecond'],
        'reviewer_model_selection_source': 'kimi_latest_managed_stable_model'}
    if boundary == 'codex_quota':
        config['command'] = [str(executable), 'exec', '--model', 'fixed', '-']
        changed = {**config, 'command': [str(executable), 'exec', '--model=fixed', '-']}
    launched = []
    monkeypatch.setattr(workflow, 'launch_role', lambda attempt, **kwargs: launched.append(attempt))
    workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [changed], [])
    original = eligibility.reviewer_eligibility(config)
    eligibility.record_formal_invocation_result(original, started_at=time.time(),
        failure_category='account_banned' if boundary == 'kimi_account' else 'usage_limit', evidence_path='native-failure-after-selection')
    role_process_execution.execute_role(launched[0])
    assert read_json(launched[0] / 'outcome.json')['recovery']['category'] == 'reviewer_unavailable'
    assert not (launched[0] / 'model-invocation.json').exists()
    assert not marker.exists()


@pytest.mark.parametrize('changed_source', ['HOME', 'XDG_CONFIG_HOME', 'OPENAI_BASE_URL', 'native_configuration',
    'absolute_native_configuration_overlay', 'relative_external_native_configuration_overlay'])
def test_exhausted_catalog_budget_recovers_only_for_changed_effective_source(repository, tmp_path, monkeypatch, changed_source):
    run = prepare(repository, tmp_path)
    executable = tmp_path / 'omp'
    query_log = tmp_path / 'native-model-catalog-queries.jsonl'
    executable.write_text(f'#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n'
        'assert sys.argv[1:] == ["config", "get", "modelRoles", "--json"]\n'
        f'with Path({str(query_log)!r}).open("a") as output: output.write(json.dumps({{"cwd": str(Path.cwd()), "home": os.environ["HOME"]}}) + "\\n")\n'
        'configured = Path(os.environ["PI_CONFIG_FILES"]) if os.environ.get("PI_CONFIG_FILES") else Path(os.environ["HOME"]) / ".omp/agent/config.yml"\n'
        'if not configured.exists(): sys.exit(1)\n'
        'print(json.dumps({"key": "modelRoles", "value": {"default": "openai-codex/" + configured.read_text()}}))\n')
    executable.chmod(0o700)
    config = {'command': [str(executable), '-p'], 'output_format': 'text',
              'reviewer_model_selection_source': 'oh_my_pi_default_model_role'}
    external_configuration = tmp_path / 'external-native-model-configuration.yml'
    if changed_source == 'absolute_native_configuration_overlay':
        monkeypatch.setenv('PI_CONFIG_FILES', str(external_configuration))
    elif changed_source == 'relative_external_native_configuration_overlay':
        candidate_workspace = run / 'formal-model-selection-candidate-workspaces' / read_json(run / 'run.json')['candidate_commit']
        monkeypatch.setenv('PI_CONFIG_FILES', os.path.relpath(external_configuration, candidate_workspace))
    original_digest = workflow.configuration_digest(config)
    original_identity = eligibility.reviewer_eligibility(config)['identity_sha256']
    launched = []
    monkeypatch.setattr(workflow, 'launch_role', lambda attempt, **kwargs: launched.append(attempt))
    epoch = [time.time()]
    monkeypatch.setattr(workflow.time, 'time', lambda: epoch[0])
    original = canary(repository)
    for _ in range(4):
        workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config], [])
        epoch[0] += 31
    assert len(query_log.read_text().splitlines()) == 2
    assert not launched
    waiting = read_json(run / 'reviewer-1/reviewer-eligibility.json')['observations'][0]
    if changed_source == 'HOME':
        monkeypatch.setenv('HOME', str(tmp_path / 'recovered-native-home'))
    elif changed_source in ('XDG_CONFIG_HOME', 'OPENAI_BASE_URL'):
        monkeypatch.setenv(changed_source, str(tmp_path / 'changed-routing-environment'))
    configuration_path = (external_configuration if changed_source.endswith('configuration_overlay') else
        Path(os.environ['HOME']) / '.omp/agent/config.yml')
    configuration_path.parent.mkdir(parents=True, exist_ok=True)
    configuration_path.write_text('fixture-model')
    if changed_source == 'native_configuration' or changed_source.endswith('configuration_overlay'):
        assert any(Path(path).resolve() == configuration_path for path in waiting['configuration_evidence_paths'])
        assert eligibility.reviewer_eligibility(config)['identity_sha256'] == original_identity
    else:
        assert eligibility.reviewer_eligibility(config)['identity_sha256'] != original_identity
    assert workflow.configuration_digest(config) == original_digest
    workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config], [])
    assert len(query_log.read_text().splitlines()) == 3
    assert len(launched) == 1
    request = read_json(launched[0] / 'request.json')
    assert request['preselected_formal_model_command'] == [str(executable), '--model', 'openai-codex/fixture-model', '-p']
    assert canary(repository) == original


@pytest.mark.parametrize('failure_category', ['authentication', 'route_unavailable'])
@pytest.mark.parametrize('pending_disabled', [False, True])
def test_completed_fallback_seat_does_not_publish_stale_recovery(repository, tmp_path, monkeypatch, failure_category, pending_disabled):
    (repository / 'task.py').write_text('value = 2\n')
    run = tmp_path / 'pending-reviewer-recovery-run'
    create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'fixed recovery', run_directory=run,
        reviewer_harness_constraints=['any_external_cli', 'codex_external_cli'])
    good = fake_config(tmp_path)
    unavailable = {**good, 'command': [sys.executable, '-c', 'pass']}
    first = eligibility.reviewer_eligibility(unavailable)
    if failure_category == 'authentication':
        eligibility.record_formal_invocation_result(first, started_at=time.time(), failure_category='authentication', evidence_path='native-auth-failure')
    else:
        evidence = read_json(Path(first['evidence_path']))
        evidence['non_inference_observation'].update(status='unavailable', reason='route_health_unavailable', next_check_at=time.time() + 300)
        write_json(Path(first['evidence_path']), evidence)
    pending = codex_cli_substitute(tmp_path)
    if pending_disabled:
        pending['enabled'] = False
    else:
        blocked = eligibility.reviewer_eligibility(pending)
        eligibility.record_formal_invocation_result(blocked, started_at=time.time(), failure_category='account_banned', evidence_path='native-ban')
    result = workflow.resume_review(run, reviewer_configs=[unavailable, good, pending])
    assert result['status'] == 'review_incomplete'
    completed_file = run / 'reviewer-1/attempt-001/outcome.json'
    completed = completed_file.read_bytes()
    assert read_json(completed_file)['status'] == 'completed'
    assert not list((run / 'reviewer-2').glob('attempt-*'))
    original_epoch = time.time()
    monkeypatch.setattr(workflow.time, 'time', lambda: original_epoch + 400)
    for _ in range(2):
        result = workflow.resume_review(run)
        assert result['status'] == 'review_incomplete'
        assert result['recovery']['maximum_unchanged_retries'] == 0
        assert 'retry_not_before_epoch_ms' not in result['recovery']
        assert str(run / 'reviewer-1/reviewer-eligibility.json') in result['recovery']['evidence_paths']
        assert completed_file.read_bytes() == completed


@pytest.mark.parametrize('recoverable_pending_seat', [False, True])
def test_default_pool_recovery_uses_only_pending_harness_candidates(repository, tmp_path, monkeypatch, recoverable_pending_seat):
    import core.rvf_user_local_data_paths as paths
    (repository / 'task.py').write_text('value = 2\n')
    run = tmp_path / 'default-pool-constrained-recovery-run'
    create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'constrained recovery', run_directory=run,
        reviewer_harness_constraints=['non_codex_external_cli', 'codex_external_cli'])
    ordinary = native_claude_configuration(tmp_path)
    unavailable = {**ordinary, 'command': [*ordinary['command'], '--route', 'unavailable']}
    pending = codex_cli_substitute(tmp_path)
    registry = tmp_path / 'isolated-default-reviewer-registry.json'
    registry_entries = {}
    for index, config in enumerate([unavailable, ordinary, pending]):
        configuration_file = tmp_path / f'default-reviewer-{index}.json'
        write_json(configuration_file, config)
        registry_entries[str(index)] = {'enabled': True, 'priority_default': 3 - index, 'config_path': str(configuration_file)}
    write_json(registry, {'harnesses': registry_entries})
    monkeypatch.setattr(paths, 'resolved_rvf_reviewer_registry_path', lambda bundled: registry)
    for config, category in [(unavailable, 'authentication'),
            (pending, 'authentication' if recoverable_pending_seat else 'account_banned')]:
        before = eligibility.reviewer_eligibility(config)
        eligibility.record_formal_invocation_result(before, started_at=time.time(), failure_category=category, evidence_path=config['command'][0])
    result = workflow.resume_review(run)
    assert result['status'] == 'review_incomplete'
    assert read_json(run / 'reviewer-1/attempt-001/outcome.json')['status'] == 'completed'
    assert not list((run / 'reviewer-2').glob('attempt-*'))
    assert result['recovery']['maximum_unchanged_retries'] == int(recoverable_pending_seat)
    assert ('retry_not_before_epoch_ms' in result['recovery']) is recoverable_pending_seat


@pytest.mark.parametrize('status', ['review_incomplete', 'machine_evidence_pending'])
def test_reviewer_recovery_recalculation_preserves_other_machine_recovery_deadlines(repository, tmp_path, status):
    run = prepare(repository, tmp_path)
    record = read_json(run / 'run.json')
    retry_at = (time.time() + 30) * 1000
    record['recovery'] = {'maximum_unchanged_retries': 1, 'retry_not_before_epoch_ms': retry_at}
    write_json(run / 'run.json', record)
    result = publish_business_result(run, {'status': status})
    if status == 'review_incomplete':
        assert result['recovery']['maximum_unchanged_retries'] == 0
        assert 'retry_not_before_epoch_ms' not in result['recovery']
    else:
        assert result['recovery']['maximum_unchanged_retries'] == 1
        assert result['recovery']['retry_not_before_epoch_ms'] == retry_at


@pytest.mark.parametrize('http_status', [200, 429, 500, 503])
def test_route_health_status_and_fallback_without_listening_socket(repository, tmp_path, http_status):
    run = prepare(repository, tmp_path)
    curl = tmp_path / 'curl'
    query_arguments_file = tmp_path / 'non-inference-route-health-query.json'
    curl.write_text(f'#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n'
        f'Path({str(query_arguments_file)!r}).write_text(json.dumps(sys.argv[1:]))\n'
        f'sys.stdout.write("\\n{http_status}")\n')
    curl.chmod(0o700)
    good = fake_config(tmp_path)
    checked = {**good, 'health_command': [str(curl), '-fsS', 'http://127.0.0.1/health']}
    current = eligibility.reviewer_eligibility(checked)
    assert current['status'] == ('unavailable' if http_status == 503 else 'unknown')
    assert current['can_dispatch'] is (http_status != 503)
    assert workflow.resume_review(run, reviewer_configs=[checked, good])['status'] == 'ready_for_validation'
    arguments = json.loads(query_arguments_file.read_text())
    assert arguments[arguments.index('--request') + 1] == 'GET'
    assert arguments[arguments.index('--max-time') + 1] == '5'
    if http_status == 503:
        assert all(not read_json(path)['config'].get('health_command') for path in run.glob('reviewer-*/attempt-*/request.json'))


@pytest.mark.parametrize('failure_message,category', [('Invalid API key', 'authentication'),
    ('Account banned', 'account_banned'), ('usage limit reached', 'usage_limit')])
def test_existing_readonly_evidence_blocks_native_failure_and_recovers_without_replay(repository, tmp_path, monkeypatch,
        preserved_test_process_signal_handlers, failure_message, category):
    run = prepare(repository, tmp_path)
    calls = tmp_path / 'formal-invocation-count'
    config = {'command': [sys.executable, '-c', 'from pathlib import Path; import sys; '
        f'p=Path({str(calls)!r}); p.write_text(p.read_text()+"x" if p.exists() else "x"); '
        f'print({failure_message!r}, file=sys.stderr); sys.exit(1)'], 'output_format': 'text'}
    initial = eligibility.reviewer_eligibility(config, run_directory=run)
    evidence_directory = Path(initial['evidence_path']).parent
    evidence_directory.chmod(0o500)
    launched = []
    monkeypatch.setattr(workflow, 'launch_role', lambda attempt, **kwargs: launched.append(attempt))
    try:
        workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config], [])
        role_process_execution.execute_role(launched[0])
        outcome = read_json(launched[0] / 'outcome.json')
        assert outcome['recovery']['category'] == category
        assert outcome['availability_evidence_persistence_error'] == 'PermissionError'
        unchanged_outcome = (launched[0] / 'outcome.json').read_bytes()
        stale = eligibility.reviewer_eligibility(config)
        assert stale['can_dispatch'] and not stale.get('storage_error')
        for _ in range(3):
            workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config], [])
            if len(launched) > 1:
                role_process_execution.execute_role(launched[-1])
        assert len(launched) == 1
        assert calls.read_text() == 'x'
        current = eligibility.reviewer_eligibility(config, run_directory=run)
        assert current['can_dispatch'] is False
        fallback = fake_config(tmp_path)
        assert eligibility.reviewer_eligibility(fallback, run_directory=run)['can_dispatch']
    finally:
        evidence_directory.chmod(0o700)
    recovered_storage = eligibility.reviewer_eligibility(config, run_directory=run)
    assert recovered_storage['can_dispatch'] is False
    failure = next(iter(read_json(Path(initial['evidence_path']))['negative_evidence'].values()))
    assert failure['failure_count'] == 1
    assert (launched[0] / 'outcome.json').read_bytes() == unchanged_outcome
    workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config], [])
    assert len(launched) == 1
    fallback = fake_config(tmp_path)
    workflow._start_role(run, read_json(run / 'run.json'), run / 'reviewer-1', 'reviewer', [config, fallback], [])
    assert len(launched) == 2
    assert read_json(launched[-1] / 'request.json')['config']['command'] == fallback['command']
