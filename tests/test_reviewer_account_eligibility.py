"""零推理资格检查、共享故障记忆及真实调用证据的行为验收。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.task_snapshot_review import reviewer_account_eligibility as eligibility
from core.task_snapshot_review.persistent_artifacts import read_json, write_json


def reviewer_configuration(tmp_path):
    credential = tmp_path / 'account-credentials.json'
    credential.write_text('first-credential')
    return {'command': [sys.executable, '-c', 'pass'], 'output_format': 'text',
            'credential_evidence_paths': [str(credential)]}


def test_formal_success_expires_after_one_hour_and_identity_changes(tmp_path, monkeypatch):
    config = reviewer_configuration(tmp_path)
    now = time.time()
    first = eligibility.reviewer_eligibility(config)
    assert first['status'] == 'unknown'
    eligibility.record_formal_invocation_result(first, started_at=now, success=True,
                                              evidence_path='synthetic-formal-invocation')
    assert eligibility.reviewer_eligibility(config)['status'] == 'available'
    monkeypatch.setattr(eligibility.time, 'time', lambda: now + 3601)
    assert eligibility.reviewer_eligibility(config)['status'] == 'unknown'
    monkeypatch.setattr(eligibility.time, 'time', lambda: now + 1)
    Path(config['credential_evidence_paths'][0]).write_text('second-credential')
    assert eligibility.reviewer_eligibility(config)['status'] == 'unknown'


def test_new_authentication_failure_overrides_success_across_processes(tmp_path):
    config = reviewer_configuration(tmp_path)
    first = eligibility.reviewer_eligibility(config)
    now = time.time()
    eligibility.record_formal_invocation_result(first, started_at=now, success=True, evidence_path='success')
    eligibility.record_formal_invocation_result(first, started_at=now, failure_category='authentication', evidence_path='failure')
    assert eligibility.reviewer_eligibility(config)['status'] == 'unavailable'
    program = ('import json; from core.task_snapshot_review.reviewer_account_eligibility import reviewer_eligibility; '
               f'print(json.dumps(reviewer_eligibility({config!r})))')
    child = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True, check=True)
    assert json.loads(child.stdout)['status'] == 'unavailable'
    # 先开始的并发调用不能用晚到的成功覆盖新故障。
    eligibility.record_formal_invocation_result(first, started_at=now, success=True, evidence_path='late-success')
    assert eligibility.reviewer_eligibility(config)['status'] == 'unavailable'


def test_authentication_failure_spans_models_but_quota_does_not(tmp_path):
    config = reviewer_configuration(tmp_path)
    first = eligibility.reviewer_eligibility(config, resolved_command=[*config['command'], '--model', 'first'])
    second_command = [*config['command'], '--model', 'second']
    eligibility.record_formal_invocation_result(first, started_at=time.time(), failure_category='usage_limit', evidence_path='quota')
    assert eligibility.reviewer_eligibility(config, resolved_command=second_command)['status'] == 'unknown'
    eligibility.record_formal_invocation_result(first, started_at=time.time(), failure_category='authentication', evidence_path='auth')
    assert eligibility.reviewer_eligibility(config, resolved_command=second_command)['status'] == 'unavailable'


def test_only_one_process_can_claim_the_bounded_recovery_attempt(tmp_path, monkeypatch):
    config = reviewer_configuration(tmp_path)
    first = eligibility.reviewer_eligibility(config)
    eligibility.record_formal_invocation_result(first, started_at=time.time(), failure_category='authentication', evidence_path='auth')
    record = read_json(Path(first['evidence_path']))
    record['negative_evidence']['account']['retry_not_before'] = time.time() - 1
    write_json(Path(first['evidence_path']), record)
    program = ('import json; from core.task_snapshot_review.reviewer_account_eligibility import reviewer_eligibility; '
               f'print(json.dumps(reviewer_eligibility({config!r}, reserve_recovery_for=__import__("sys").argv[1])))')
    children = [subprocess.Popen([sys.executable, '-c', program, str(index)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for index in range(4)]
    results = []
    for child in children:
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert sum(result['can_dispatch'] for result in results) == 1
    assert eligibility.reviewer_eligibility(config, reserve_recovery_for='after-restart')['status'] == 'unavailable'


def test_legacy_inference_health_command_is_never_executed(tmp_path):
    config = reviewer_configuration(tmp_path)
    marker = tmp_path / 'inference-probe-was-executed'
    config.update(pre_run_health=True, health_command=[sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).touch()'])
    result = eligibility.reviewer_eligibility(config)
    assert result['status'] == 'unknown'
    assert result['non_inference_observation']['reason'] == 'inference_or_unverified_health_command_skipped'
    assert not marker.exists()


def test_cached_account_status_requires_matching_identity_and_fresh_quota(tmp_path):
    config = reviewer_configuration(tmp_path)
    status_file = tmp_path / 'account-manager-cached-status.json'
    config.update(account_identity='fixture-account', credential_source_identity='fixture-credentials',
                  route_identity='fixture-route', account_status_evidence_path=str(status_file))
    def publish_status(account_identity, *, stale=False, observed_at=None):
        write_json(status_file, {'account_identity': account_identity, 'credential_source_identity': 'fixture-credentials',
            'route_identity': 'fixture-route', 'status': 'available', 'observed_at': observed_at or time.time(),
            'stale': stale, 'quota': {'remaining': 0, 'reset_at': time.time() + 300}})
    publish_status('another-account')
    assert eligibility.reviewer_eligibility(config)['status'] == 'unknown'
    publish_status('fixture-account', stale=True)
    stale = eligibility.reviewer_eligibility(config)
    assert stale['status'] == 'unknown'
    assert stale['account_status_observation']['stale'] is True
    publish_status('fixture-account', observed_at=time.time() - 120)
    assert eligibility.reviewer_eligibility(config)['status'] == 'unknown'
    publish_status('fixture-account')
    assert eligibility.reviewer_eligibility(config)['status'] == 'unavailable'


@pytest.mark.parametrize('changed_field', ['account_identity', 'route_identity', 'credential_source_identity'])
def test_related_configuration_changes_invalidate_formal_success(tmp_path, changed_field):
    config = reviewer_configuration(tmp_path)
    before = eligibility.reviewer_eligibility(config)
    eligibility.record_formal_invocation_result(before, started_at=time.time(), success=True, evidence_path='success')
    assert eligibility.reviewer_eligibility({**config, changed_field: 'changed'})['status'] == 'unknown'


def test_session_metadata_does_not_reset_account_failure(tmp_path, monkeypatch):
    config = reviewer_configuration(tmp_path)
    initial = eligibility.reviewer_eligibility(config)
    eligibility.record_formal_invocation_result(initial, started_at=time.time(), failure_category='authentication', evidence_path='auth')
    monkeypatch.setenv('CODEX_THREAD_ID', 'another-conversation')
    monkeypatch.setenv('CODEX_TURN_ID', 'another-turn')
    assert eligibility.reviewer_eligibility(config)['status'] == 'unavailable'


def test_replaying_same_native_failure_does_not_consume_another_retry(tmp_path):
    config = reviewer_configuration(tmp_path)
    initial = eligibility.reviewer_eligibility(config)
    invocation_started = time.time()
    for _ in range(3):
        eligibility.record_formal_invocation_result(initial, started_at=invocation_started,
            failure_category='authentication', evidence_path='one-immutable-outcome')
    record = read_json(Path(initial['evidence_path']))
    assert record['negative_evidence']['account']['failure_count'] == 1


def test_confirmed_ban_never_uses_unchanged_formal_recovery(tmp_path, monkeypatch):
    config = reviewer_configuration(tmp_path)
    initial = eligibility.reviewer_eligibility(config)
    now = time.time()
    eligibility.record_formal_invocation_result(initial, started_at=now, failure_category='account_banned', evidence_path='ban')
    monkeypatch.setattr(eligibility.time, 'time', lambda: now + 3600)
    result = eligibility.reviewer_eligibility(config, reserve_recovery_for='forbidden-retry')
    assert not result['can_dispatch']
    assert result['recovery']['maximum_unchanged_retries'] == 0


def test_native_claude_keychain_rotation_changes_identity_without_persisting_secrets(tmp_path, monkeypatch):
    from types import SimpleNamespace
    config = {**reviewer_configuration(tmp_path), 'credential_identity_source': 'claude_secure_storage'}
    secret = ['synthetic-keychain-credential-one']
    monkeypatch.setattr(eligibility.sys, 'platform', 'darwin')
    monkeypatch.setattr(eligibility.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=secret[0].encode()))
    first = eligibility.reviewer_eligibility(config)
    eligibility.record_formal_invocation_result(first, started_at=time.time(), success=True, evidence_path='success')
    assert eligibility.reviewer_eligibility(config)['status'] == 'available'
    secret[0] = 'synthetic-keychain-credential-two'
    assert eligibility.reviewer_eligibility(config)['status'] == 'unknown'
    assert 'synthetic-keychain-credential' not in Path(first['evidence_path']).read_text()


def test_free_local_login_success_does_not_override_native_authentication_failure(tmp_path):
    config = reviewer_configuration(tmp_path)
    executable = tmp_path / 'claude'
    executable.write_text(f'#!{sys.executable}\nimport json\nprint(json.dumps({{"loggedIn": True}}))\n')
    executable.chmod(0o700)
    config.update(command=[str(executable), '-p'], health_command=[str(executable), 'auth', 'status', '--json'], pre_run_health=True)
    initial = eligibility.reviewer_eligibility(config)
    assert initial['status'] == 'unknown'
    eligibility.record_formal_invocation_result(initial, started_at=time.time(), failure_category='authentication', evidence_path='auth')
    assert eligibility.reviewer_eligibility(config)['status'] == 'unavailable'


def test_cached_negative_evidence_survives_status_expiry_and_requires_new_recovery_fact(tmp_path, monkeypatch):
    config = reviewer_configuration(tmp_path)
    status_file = tmp_path / 'account-status.json'
    config.update(account_identity='account', credential_source_identity='credentials', route_identity='route',
                  account_status_evidence_path=str(status_file))
    now = time.time()
    snapshot = {key: config[key] for key in ('account_identity', 'credential_source_identity', 'route_identity')}
    write_json(status_file, {**snapshot, 'status': 'authentication', 'stale': False, 'observed_at': now})
    assert eligibility.reviewer_eligibility(config)['status'] == 'unavailable'
    monkeypatch.setattr(eligibility.time, 'time', lambda: now + 61)
    assert eligibility.reviewer_eligibility(config)['status'] == 'unavailable'
    write_json(status_file, {**snapshot, 'status': 'available', 'stale': False, 'observed_at': now + 61})
    assert eligibility.reviewer_eligibility(config)['can_dispatch']


def test_stale_quota_does_not_clear_new_formal_quota_failure(tmp_path):
    config = reviewer_configuration(tmp_path)
    status_file = tmp_path / 'account-status.json'
    config.update(account_identity='account', credential_source_identity='credentials', route_identity='route',
                  account_status_evidence_path=str(status_file))
    initial = eligibility.reviewer_eligibility(config)
    eligibility.record_formal_invocation_result(initial, started_at=time.time(), failure_category='usage_limit', evidence_path='quota')
    write_json(status_file, {**{key: config[key] for key in ('account_identity', 'credential_source_identity', 'route_identity')},
        'status': 'available', 'stale': False, 'observed_at': time.time(),
        'quota': {'remaining': 10, 'observed_at': time.time() - 120}})
    assert eligibility.reviewer_eligibility(config)['status'] == 'unavailable'


def test_unobservable_credential_source_never_claims_cached_account_health(tmp_path):
    config = {'command': [sys.executable, '-c', 'pass']}
    initial = eligibility.reviewer_eligibility(config)
    eligibility.record_formal_invocation_result(initial, started_at=time.time(), success=True, evidence_path='success')
    result = eligibility.reviewer_eligibility(config)
    assert not result['credential_identity_observable']
    assert result['status'] == 'unknown'
    assert result['can_dispatch']


def test_route_settings_alone_do_not_prove_credential_identity(tmp_path):
    settings = tmp_path / 'settings.json'
    settings.write_text('{}')
    result = eligibility.reviewer_eligibility({'command': [sys.executable, '-c', 'pass'],
        'credential_evidence_paths': [str(tmp_path / 'missing-auth.json'), str(settings)]})
    assert not result['credential_identity_observable']


@pytest.mark.parametrize('health_update', [{'pre_run_health': False}, {'health_command': ['unverified-probe']},
    {'underlying_provider_account_quota_pool_identity': 'new-quota-pool'}])
def test_changing_health_check_settings_does_not_erase_native_account_failure(tmp_path, health_update):
    config = reviewer_configuration(tmp_path)
    initial = eligibility.reviewer_eligibility(config)
    eligibility.record_formal_invocation_result(initial, started_at=time.time(), failure_category='account_banned', evidence_path='ban')
    assert not eligibility.reviewer_eligibility({**config, **health_update})['can_dispatch']


def test_cached_quota_negatives_are_persisted_for_each_bound_model_scope(tmp_path, monkeypatch):
    config = reviewer_configuration(tmp_path)
    status_file = tmp_path / 'account-status.json'
    config.update(account_identity='account', credential_source_identity='credentials', route_identity='route',
                  account_status_evidence_path=str(status_file))
    now = time.time()
    write_json(status_file, {**{key: config[key] for key in ('account_identity', 'credential_source_identity', 'route_identity')},
        'status': 'usage_limit', 'stale': False, 'observed_at': now})
    first_model = [*config['command'], '--model', 'first']
    second_model = [*config['command'], '--model', 'second']
    assert not eligibility.reviewer_eligibility(config, resolved_command=first_model)['can_dispatch']
    assert not eligibility.reviewer_eligibility(config, resolved_command=second_model)['can_dispatch']
    monkeypatch.setattr(eligibility.time, 'time', lambda: now + 61)
    assert not eligibility.reviewer_eligibility(config, resolved_command=second_model)['can_dispatch']


def test_new_negative_metadata_does_not_reset_exhausted_formal_retry_budget(tmp_path):
    config = reviewer_configuration(tmp_path)
    status_file = tmp_path / 'account-status.json'
    config.update(account_identity='account', credential_source_identity='credentials', route_identity='route',
                  account_status_evidence_path=str(status_file))
    initial = eligibility.reviewer_eligibility(config)
    for index in range(2):
        eligibility.record_formal_invocation_result(initial, started_at=time.time(), failure_category='authentication', evidence_path=str(index))
    write_json(status_file, {**{key: config[key] for key in ('account_identity', 'credential_source_identity', 'route_identity')},
        'status': 'authentication', 'stale': False, 'observed_at': time.time()})
    eligibility.reviewer_eligibility(config)
    assert read_json(Path(initial['evidence_path']))['negative_evidence']['account']['failure_count'] == 2


@pytest.mark.parametrize('source', ['kimi_latest_managed_stable_model', 'oh_my_pi_default_model_role'])
@pytest.mark.parametrize('obsolete_health_model', [None, 'old-probe-model'])
def test_dynamic_model_selection_does_not_require_a_health_prompt(tmp_path, monkeypatch, source, obsolete_health_model):
    from types import SimpleNamespace
    from core.task_snapshot_review import harness_cli_adapters as adapters
    if source == 'kimi_latest_managed_stable_model':
        command = ['kimi', '-p']
        monkeypatch.setattr(adapters, 'resolve_latest_kimi_managed_stable_model', lambda *args, **kwargs: 'kimi-code/k3')
        expected = 'kimi-code/k3'
    else:
        command = ['omp', '--print']
        expected = 'openai-codex/gpt-fixture'
        monkeypatch.setattr(adapters.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0,
            stdout=json.dumps({'key': 'modelRoles', 'value': {'default': expected}})))
    config = {'reviewer_model_selection_source': source}
    if obsolete_health_model:
        config['health_command'] = [command[0], '--model', obsolete_health_model, '-p', 'Reply with exactly: OK']
    resolved = adapters.resolve_formal_reviewer_model_command(config,
        command, env=dict(os.environ), cwd=tmp_path, timeout=5)
    assert resolved[:3] == [command[0], '--model', expected]
