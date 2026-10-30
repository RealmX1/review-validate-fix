"""真实 Git 工作树及替身 CLI 验证健康门禁的派发与恢复边界。"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from test_task_snapshot_review_workflow import repository, prepare, fake_config, canary
from test_temporary_mandatory_codex_external_reviewer import codex_cli_substitute
from core.task_snapshot_review import workflow
from core.task_snapshot_review.persistent_artifacts import read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot


@pytest.fixture
def proxy_health_endpoint():
    observations = {'status': 503, 'requests': []}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observations['requests'].append((self.command, self.path))
            self.send_response(observations['status'])
            self.end_headers()
            self.wfile.write(b'{"healthy":false}')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/health', observations
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_proxy_503_filters_candidates_and_does_not_dispatch_unhealthy_reviewer(repository, tmp_path, monkeypatch, proxy_health_endpoint):
    import core.rvf_user_local_data_paths as paths
    url, observations = proxy_health_endpoint
    bad = {**fake_config(tmp_path), 'pre_run_health': True, 'health_command': ['curl', '-fsS', url]}
    good = {**fake_config(tmp_path), 'label': 'synthetic-good-reviewer'}
    bad_path, good_path = tmp_path / 'unavailable.json', tmp_path / 'available.json'
    write_json(bad_path, bad)
    write_json(good_path, good)
    registry = tmp_path / 'reviewer-registry.json'
    write_json(registry, {'harnesses': {
        'unavailable': {'enabled': True, 'priority_default': 2, 'config_path': str(bad_path)},
        'available': {'enabled': True, 'priority_default': 1, 'config_path': str(good_path)}}})
    monkeypatch.setattr(paths, 'resolved_rvf_reviewer_registry_path', lambda bundled: registry)
    assert all(config.get('label') == 'synthetic-good-reviewer' for config in workflow.default_reviewer_configurations())
    run = prepare(repository, tmp_path)
    before = canary(repository)
    assert workflow.resume_review(run, reviewer_configs=[bad, good])['status'] == 'ready_for_validation'
    requests = [read_json(path) for path in run.glob('reviewer-*/attempt-*/request.json')]
    assert len(requests) == 2
    assert all(not request['config'].get('health_command') for request in requests)
    assert observations['requests'] and set(observations['requests']) == {('GET', '/health')}
    assert canary(repository) == before


def test_formal_authentication_failure_is_not_dispatched_again_in_a_new_run(repository, tmp_path):
    run = prepare(repository, tmp_path)
    marker = tmp_path / 'formal-authentication-attempts'
    bad = {'command': [sys.executable, '-c', f"from pathlib import Path; import sys; p=Path({str(marker)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('Invalid API key', file=sys.stderr); sys.exit(1)"],
           'output_format': 'text', 'timeout_seconds': 30}
    good = fake_config(tmp_path)
    assert workflow.resume_review(run, reviewer_configs=[bad, good])['status'] == 'ready_for_validation'
    original_count = len(marker.read_text())
    other_run = tmp_path / 'second-independent-run'
    create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'another run', run_directory=other_run)
    assert workflow.resume_review(other_run, reviewer_configs=[bad, good])['status'] == 'ready_for_validation'
    assert len(marker.read_text()) == original_count
    assert all(read_json(path)['config']['command'] == good['command'] for path in other_run.glob('reviewer-*/attempt-*/request.json'))


def publish_account_status(config, status):
    write_json(Path(config['account_status_evidence_path']), {
        'account_identity': config['account_identity'], 'credential_source_identity': config['credential_source_identity'],
        'route_identity': config['route_identity'], 'status': status, 'stale': False, 'observed_at': time.time()})


def recovery_snapshot(repository, tmp_path):
    (repository / 'task.py').write_text('value = 2\n')
    run = tmp_path / 'pending-account-recovery-run'
    create_snapshot(repository, base_ref='HEAD', scope_paths=['task.py'], requirements=b'account recovery', run_directory=run,
                    reviewer_harness_constraints=['any_external_cli', 'codex_external_cli'])
    good = fake_config(tmp_path)
    pending = {**codex_cli_substitute(tmp_path), 'timeout_seconds': 30, 'account_identity': 'fixture-account',
               'credential_source_identity': 'fixture-credentials', 'route_identity': 'fixture-route',
               'account_status_evidence_path': str(tmp_path / 'account-manager-cached-status.json')}
    publish_account_status(pending, 'authentication')
    return run, good, pending


def test_bound_account_status_restores_same_run_without_repeating_completed_seat(repository, tmp_path):
    run, good, pending = recovery_snapshot(repository, tmp_path)
    first = workflow.resume_review(run, reviewer_configs=[good, pending])
    assert first['status'] == 'review_incomplete'
    assert first['disposition'] == 'machine_evidence_pending'
    completed = run / 'reviewer-1/attempt-001/outcome.json'
    completed_bytes = completed.read_bytes()
    assert not (run / 'reviewer-2/role-deadline.json').exists()
    assert str(Path(pending['account_status_evidence_path'])) in first['recovery']['evidence_paths']
    assert not list((run / 'reviewer-2').glob('attempt-*'))
    publish_account_status(pending, 'available')
    assert workflow.resume_review(run)['status'] == 'ready_for_validation'
    assert completed.read_bytes() == completed_bytes
    assert len(list((run / 'reviewer-1').glob('attempt-*'))) == 1
    assert not (run / 'reviewer-2/role-deadline.json').exists()


@pytest.mark.parametrize('terminal_condition', ['stop', 'legacy_deadline'])
def test_recovered_account_preserves_stop_and_ignores_retired_deadline(repository, tmp_path, terminal_condition):
    from core.task_snapshot_review.execution_lifecycle import persist_execution_revocation
    run, good, pending = recovery_snapshot(repository, tmp_path)
    assert workflow.resume_review(run, reviewer_configs=[good, pending])['status'] == 'review_incomplete'
    if terminal_condition == 'stop':
        persist_execution_revocation(run, '测试用户 Stop')
    else:
        write_json(run / 'reviewer-2/role-deadline.json', {'deadline_at': time.time() - 1})
    publish_account_status(pending, 'available')
    result = workflow.resume_review(run)
    assert result['status'] == ('cancelled' if terminal_condition == 'stop' else 'ready_for_validation')
    assert len(list((run / 'reviewer-2').glob('attempt-*'))) == (0 if terminal_condition == 'stop' else 1)


def test_worker_rechecks_status_after_candidate_selection_before_formal_dispatch(repository, tmp_path, monkeypatch):
    run, good, pending = recovery_snapshot(repository, tmp_path)
    publish_account_status(pending, 'available')
    launch = workflow.launch_role
    def invalidate_after_selection(attempt, **kwargs):
        if attempt.parent.name == 'reviewer-2':
            publish_account_status(pending, 'authentication')
        return launch(attempt, **kwargs)
    monkeypatch.setattr(workflow, 'launch_role', invalidate_after_selection)
    assert workflow.resume_review(run, reviewer_configs=[good, pending])['status'] == 'review_incomplete'
    attempt = run / 'reviewer-2/attempt-001'
    assert not (attempt / 'model-invocation.json').exists()
    assert read_json(attempt / 'outcome.json')['recovery']['category'] == 'reviewer_unavailable'


@pytest.mark.parametrize('http_status', [200, 429, 500])
def test_metadata_interface_errors_do_not_claim_account_health_or_ban(tmp_path, proxy_health_endpoint, http_status):
    from core.task_snapshot_review.reviewer_account_eligibility import reviewer_eligibility
    url, observed = proxy_health_endpoint
    observed['status'] = http_status
    result = reviewer_eligibility({'command': [sys.executable, '-c', 'pass'], 'health_command': ['curl', '-fsS', url]})
    assert result['status'] == 'unknown'
    assert result['can_dispatch']
    assert observed['requests'] == [('GET', '/health')]


def test_two_restarted_cli_controllers_restore_a_single_pending_seat(repository, tmp_path):
    from test_task_snapshot_review_workflow import ROOT
    run, good, pending = recovery_snapshot(repository, tmp_path)
    assert workflow.resume_review(run, reviewer_configs=[good, pending])['status'] == 'review_incomplete'
    completed = (run / 'reviewer-1/attempt-001/outcome.json').read_bytes()
    publish_account_status(pending, 'available')
    entrypoint = ROOT / 'plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py'
    children = [subprocess.Popen([sys.executable, str(entrypoint), 'resume-task-review', '--run-dir', str(run)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(2)]
    assert any(child.wait(timeout=30) == 0 for child in children)
    for child in children:
        child.wait(timeout=30)
    assert read_json(run / 'run.json')['status'] == 'ready_for_validation'
    assert len(list((run / 'reviewer-2').glob('attempt-*'))) == 1
    assert (run / 'reviewer-1/attempt-001/outcome.json').read_bytes() == completed


def test_interrupted_native_error_repairs_shared_negative_evidence_without_a_new_invocation(repository, tmp_path):
    from core.task_snapshot_review.reviewer_account_eligibility import reviewer_eligibility
    from core.task_snapshot_review.role_process_execution import reconcile_completed_role_attempt
    run = prepare(repository, tmp_path)
    config = {'command': [sys.executable, '-c', 'pass'], 'output_format': 'claude_stream_json'}
    initial = reviewer_eligibility(config)
    attempt = run / 'reviewer-1/attempt-001'
    write_json(attempt / 'request.json', {'config': config, 'run_directory': str(run), 'role': 'reviewer',
        'input_file': str(attempt / 'request.json'), 'result_file': str(attempt / 'role-output.json')})
    write_json(attempt / 'reviewer-eligibility.json', initial)
    write_json(attempt / 'model-invocation.json', {'started_at': time.time()})
    (attempt / 'stdout.txt').write_text(json.dumps({'type': 'result', 'is_error': True, 'result': 'Invalid API key'}))
    reconcile_completed_role_attempt(attempt)
    reconcile_completed_role_attempt(attempt)
    assert reviewer_eligibility(config)['status'] == 'unavailable'
    assert read_json(Path(initial['evidence_path']))['negative_evidence']['account']['failure_count'] == 1
    assert not (attempt / 'outcome.json').exists()
