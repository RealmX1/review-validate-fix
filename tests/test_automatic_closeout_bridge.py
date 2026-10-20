import hashlib
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/review-validate-fix/skills/review-validate-fix/scripts'
sys.path.insert(0, str(SCRIPTS))
import rvf_automatic_closeout_bridge as bridge


def test_completion_is_exact_bound_run_and_durable(tmp_path, monkeypatch):
    monkeypatch.setenv('RVF_HOME', str(tmp_path))
    receipt = {'decision_token': '0123456789abcdef', 'session_id': 'source', 'worktree': str(tmp_path / 'repo')}
    directory = tmp_path / 'receipts'
    directory.mkdir()
    receipt_path = directory / 'receipt.json'
    receipt_path.write_text(json.dumps(receipt))
    run = tmp_path / 'runs' / 'run-id'
    (run / 'artifacts').mkdir(parents=True)
    handoff = run / 'artifacts' / 'handoff.md'
    handoff.write_text('RVF 已完成')
    bridge.bind_run(receipt, run)
    finalized = {'run_id': 'run-id', 'run_dir': str(run), 'repo': receipt['worktree'], 'decision_kind': 'handoff', 'completed_at': 'now', 'errors': []}
    bridge.freeze_completion_notification(run, finalized)
    bridge.publish_completion(run, finalized)
    result = json.loads((tmp_path / 'automatic-closeout-completion-notifications' / '0123456789abcdef.json').read_text())
    assert result['handoff_sha256'] == hashlib.sha256(handoff.read_bytes()).hexdigest()
    assert result['session_id'] == 'source'
    assert result['run_id'] == 'run-id'


def test_errors_and_non_handoff_do_not_trigger_closeout(tmp_path, monkeypatch):
    monkeypatch.setenv('RVF_HOME', str(tmp_path))
    run = tmp_path / 'run'
    (run / 'artifacts').mkdir(parents=True)
    (run / 'artifacts' / 'handoff.md').write_text('RVF 已完成')
    receipt = {'decision_token': '0123456789abcdef', 'session_id': 'source', 'worktree': str(tmp_path / 'repo')}
    bridge.bind_run(receipt, run)
    finalized = {'run_id': run.name, 'run_dir': str(run), 'repo': receipt['worktree'], 'completed_at': 'now'}
    bridge.publish_completion(run, {**finalized, 'errors': ['failed'], 'decision_kind': 'handoff'})
    bridge.publish_completion(run, {**finalized, 'errors': [], 'decision_kind': 'cancelled'})
    assert not (tmp_path / 'automatic-closeout-completion-notifications').exists()


def test_host_capture_failure_happens_before_acknowledgement(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import rvf_agent_decision_cli as cli
    monkeypatch.setenv('RVF_HOME', str(tmp_path))
    monkeypatch.setenv('KANBAN_TASK_ID', 'task')
    payload = {'token': '0123456789abcdef', 'session_id': 'source', 'worktree': str(tmp_path), 'candidate_summary': {}}
    monkeypatch.setattr(cli.token_store, 'read_decision_token', lambda *args, **kwargs: SimpleNamespace(payload=payload, state='injected'))
    def refuse(*args, **kwargs):
        raise ValueError('没有独立开审分界')
    monkeypatch.setattr(bridge, 'capture_before_start', refuse)
    def cannot_acknowledge(*args, **kwargs):
        raise AssertionError('不应推进 token')
    monkeypatch.setattr(cli.token_store, 'acknowledge_decision_token', cannot_acknowledge)
    result = cli.run_start(token=payload['token'], reason='测试')
    assert result.exit_code != 0
    assert result.detail['phase'] == 'capture_independent_review_start_boundary'


def test_codex_capture_uses_main_checkout_and_explicit_runtime_operation(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setenv('RVF_HOME', str(tmp_path))
    transcript = tmp_path / 'rollout.jsonl'
    transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {'originator': 'codex-tui', 'source': 'cli'}}) + '\n')
    payload = {'token': '0123456789abcdef', 'session_id': 'source', 'worktree': str(tmp_path / 'worktree')}
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        output = str(tmp_path / 'main' / '.git') if command[0] == 'git' else json.dumps({'receipt': {'decision_token': payload['token'], 'session_id': 'source', 'worktree': payload['worktree']}})
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')
    monkeypatch.setattr(bridge.kanban, 'run_command', run)
    monkeypatch.setattr(bridge.kanban, 'task_runtime_port', lambda **kwargs: 12345)
    receipt = bridge.capture_before_start(payload, transcript_path=str(transcript), task_id='task')
    assert receipt['decision_token'] == payload['token']
    assert 'capture-rvf-review-start-boundary' in calls[1]
    assert str(tmp_path / 'main') in calls[1]


def _start_fixture(tmp_path, monkeypatch):
    import rvf_agent_decision_cli as cli
    monkeypatch.setenv('RVF_HOME', str(tmp_path / 'rvf-home'))
    monkeypatch.setenv('RVF_USER_LOCAL_DATA_ROOT', str(tmp_path / 'rvf-user-local-data'))
    monkeypatch.setenv('KANBAN_TASK_ID', 'task')
    roots = cli.AgentDecisionRoots(token_root=tmp_path / 'decision-tokens')
    minted = cli.token_store.mint_decision_token(
        worktree=str(tmp_path), session_id='source', candidate_fingerprint='candidate',
        root=roots.token_root, cooldown_root=tmp_path / 'cooldown',
        circuit_breaker_root_dir=tmp_path / 'circuit-breaker',
    )
    receipt = {'decision_token': minted.token, 'session_id': 'source', 'worktree': str(tmp_path), 'kanban_task_id': 'task'}
    monkeypatch.setattr(bridge, 'capture_before_start', lambda *args, **kwargs: receipt)
    monkeypatch.setattr(cli, 'resolve_agent_initiation_backend', lambda: 'manual')
    arguments = {
        'token': minted.token, 'reason': '测试', 'roots': roots,
        'launch_preflight': lambda **kwargs: (True, None),
        'provider_health_requirements_resolver': lambda backend: [],
    }
    return cli, arguments, receipt


@pytest.mark.parametrize('start_outcome', ['launch_preflight', 'provider_health', 'prepare_raised', 'prepare_failed', 'no_unreviewed_scope', 'bootstrap_waiting'])
def test_incomplete_start_never_activates_automatic_closeout(tmp_path, monkeypatch, start_outcome):
    cli, arguments, receipt = _start_fixture(tmp_path, monkeypatch)
    activations = []
    monkeypatch.setattr(bridge, 'activate_bound_run', lambda *args: activations.append(args))
    def prepare(**kwargs):
        if start_outcome == 'prepare_raised':
            raise ValueError('准备异常')
        if start_outcome == 'bootstrap_waiting':
            return {'status': cli.rvf_bootstrap_gate.WAITING_STATUS, 'bootstrap_gate': {'required_skill': 'sync-local-base-into-task-worktree-only'}}
        return {'status': 'no_unreviewed_scope' if start_outcome == 'no_unreviewed_scope' else 'failed'}
    arguments['prepare_pipeline'] = prepare
    if start_outcome == 'launch_preflight':
        arguments['launch_preflight'] = lambda **kwargs: (False, '启动不可用')
    if start_outcome == 'provider_health':
        arguments['provider_health_requirements_resolver'] = lambda backend: ['provider']
        arguments['provider_health_runner'] = lambda *args: {'status': 'failed', 'provider': 'provider'}
    if start_outcome == 'bootstrap_waiting':
        monkeypatch.setattr(cli.rvf_bootstrap_gate, 'reconcile_bootstrap_gate', lambda *args, **kwargs: cli.rvf_bootstrap_gate.BootstrapGateReconciliation(
            outcome=cli.rvf_bootstrap_gate.OUTCOME_STILL_WAITING,
            phase=cli.rvf_bootstrap_gate.PHASE_WAITING_FOR_LOCAL_BASE_BOOTSTRAP,
            reason='等待同步', diagnostic='等待 local base 同步',
        ))
    result = cli.run_start(**arguments)
    assert result.exit_code != 0
    assert result.token_state == cli.token_store.STATE_ACKNOWLEDGED
    assert activations == []
    assert not (tmp_path / 'rvf-home' / 'review-start-independent-user-turn-boundary-receipts' / f"{receipt['decision_token']}.run-binding.json").exists()
    assert not (tmp_path / 'rvf-home' / 'automatic-closeout-completion-notifications').exists()


def test_retry_activates_only_the_successfully_prepared_exact_bound_run(tmp_path, monkeypatch):
    cli, arguments, receipt = _start_fixture(tmp_path, monkeypatch)
    run = tmp_path / 'runs' / 'prepared-run'
    activations = []
    def activate(actual_receipt):
        binding_path = tmp_path / 'rvf-home' / 'review-start-independent-user-turn-boundary-receipts' / f"{receipt['decision_token']}.run-binding.json"
        binding = json.loads(binding_path.read_text())
        assert binding['run_id'] == 'prepared-run'
        assert binding['run_dir'] == str(run)
        assert actual_receipt == receipt
        activations.append(binding)
    monkeypatch.setattr(bridge, 'activate_bound_run', activate)
    arguments['prepare_pipeline'] = lambda **kwargs: {'status': 'failed', 'error': '首次失败'}
    assert cli.run_start(**arguments).status == 'start_failed'
    assert activations == []
    arguments['prepare_pipeline'] = lambda **kwargs: {'status': 'completed', 'run_id': run.name, 'run_dir': str(run), 'artifacts': {}}
    result = cli.run_start(**arguments, retry=True)
    assert result.status == 'ok'
    assert len(activations) == 1
    token = cli.token_store.read_decision_token(receipt['decision_token'], root=arguments['roots'].token_root)
    assert token.payload['start_attempt_count'] == 2


def test_activate_bound_run_requests_exact_identity_from_main_checkout(tmp_path, monkeypatch):
    import subprocess
    receipt = {'decision_token': '0123456789abcdef', 'session_id': 'source', 'worktree': str(tmp_path / 'worktree'), 'kanban_task_id': 'task'}
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        output = str(tmp_path / 'main' / '.git') if command[0] == 'git' else json.dumps({'receipt': receipt})
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')
    monkeypatch.setattr(bridge.kanban, 'run_command', execute)
    monkeypatch.setattr(bridge.kanban, 'task_runtime_port', lambda **kwargs: 12345)
    bridge.activate_bound_run(receipt)
    assert 'activate-rvf-automatic-closeout-after-run-binding' in calls[1]
    assert '--run-dir' not in calls[1]
    assert calls[1][calls[1].index('--task-id') + 1] == 'task'
    assert calls[1][calls[1].index('--decision-token') + 1] == receipt['decision_token']
    assert calls[1][calls[1].index('--project-path') + 1] == str(tmp_path / 'main')


def test_host_activation_refusal_stays_retryable_without_claiming_success(tmp_path, monkeypatch):
    cli, arguments, receipt = _start_fixture(tmp_path, monkeypatch)
    run = tmp_path / 'runs' / 'prepared-run'
    arguments['prepare_pipeline'] = lambda **kwargs: {'status': 'completed', 'run_id': run.name, 'run_dir': str(run), 'artifacts': {}}
    def refuse_activation(actual_receipt):
        assert actual_receipt == receipt
        raise ValueError('宿主记录已被明确停止')
    monkeypatch.setattr(bridge, 'activate_bound_run', refuse_activation)
    result = cli.run_start(**arguments)
    assert result.status == 'start_failed'
    assert result.detail['phase'] == 'activate_automatic_closeout_after_run_binding'
    assert result.token_state == cli.token_store.STATE_ACKNOWLEDGED
    assert not (tmp_path / 'rvf-home' / 'automatic-closeout-completion-notifications').exists()


def test_host_cannot_activate_a_different_review_start_receipt(tmp_path, monkeypatch):
    import subprocess
    receipt = {'decision_token': '0123456789abcdef', 'session_id': 'source', 'worktree': str(tmp_path / 'worktree'), 'kanban_task_id': 'task'}
    def execute(command, **kwargs):
        output = str(tmp_path / 'main' / '.git') if command[0] == 'git' else json.dumps({'receipt': {**receipt, 'session_id': 'other'}})
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')
    monkeypatch.setattr(bridge.kanban, 'run_command', execute)
    monkeypatch.setattr(bridge.kanban, 'task_runtime_port', lambda **kwargs: 12345)
    with pytest.raises(ValueError, match='开审分界与已绑定 receipt 不一致'):
        bridge.activate_bound_run(receipt)


@pytest.mark.parametrize('first_notification_fails', [False, True])
def test_cached_finalize_and_restarted_publish_keep_original_handoff_digest(tmp_path, monkeypatch, first_notification_fails):
    import rvf_run_finalize as finalize
    monkeypatch.setenv('RVF_HOME', str(tmp_path / 'rvf-home'))
    receipt = {'decision_token': '0123456789abcdef', 'session_id': 'source', 'worktree': str(tmp_path / 'repo')}
    Path(receipt['worktree']).mkdir()
    run = tmp_path / 'runs' / 'run-id'
    (run / 'artifacts').mkdir(parents=True)
    (run / 'summary.json').write_text(json.dumps({'run_id': run.name, 'repo': receipt['worktree']}))
    bridge.bind_run(receipt, run)
    handoff = run / 'artifacts' / 'handoff.md'
    handoff.write_text('首次完成时的 handoff')
    original_digest = hashlib.sha256(handoff.read_bytes()).hexdigest()
    notification_directory = tmp_path / 'rvf-home' / 'automatic-closeout-completion-notifications'
    notification_path = notification_directory / f"{receipt['decision_token']}.json"
    for name in ('capture_run', '_write_usage_summary', '_release_tracker_lease', '_advance_review_highwater', '_scaffold_analysis'):
        monkeypatch.setattr(finalize, name, lambda *args, **kwargs: {})
    if first_notification_fails:
        with monkeypatch.context() as notification_failure:
            original_mkdir = Path.mkdir
            def fail_notification_directory_creation(path, *args, **kwargs):
                if path == notification_directory:
                    raise OSError('通知目录不可写')
                return original_mkdir(path, *args, **kwargs)
            notification_failure.setattr(Path, 'mkdir', fail_notification_directory_creation)
            with pytest.raises(OSError, match='通知'):
                finalize.finalize_run(run_dir=run)
        original_notification = None
    else:
        finalize.finalize_run(run_dir=run)
        original_notification = (notification_path.read_bytes(), notification_path.stat().st_mtime_ns)
    lock_path = run / 'artifacts' / '.finalize.lock'
    original_lock = (lock_path.read_bytes(), lock_path.stat().st_mtime_ns)
    handoff.write_text('完成后被修改的 handoff')

    # 独立进程模拟重启后的缓存完成；并发进程也只能发布同一份冻结凭据。
    command = [sys.executable, str(SCRIPTS / 'rvf_run_finalize.py'), '--run-dir', str(run)]
    processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        assert json.loads(stdout)['already_finalized'] is True
    notification = json.loads(notification_path.read_text())
    assert notification['handoff_sha256'] == original_digest
    assert notification['handoff_sha256'] != hashlib.sha256(handoff.read_bytes()).hexdigest()
    assert (lock_path.read_bytes(), lock_path.stat().st_mtime_ns) == original_lock
    if original_notification is not None:
        assert (notification_path.read_bytes(), notification_path.stat().st_mtime_ns) == original_notification


def test_cached_finalize_without_frozen_evidence_cannot_endorse_current_handoff(tmp_path, monkeypatch):
    import rvf_run_finalize as finalize
    monkeypatch.setenv('RVF_HOME', str(tmp_path))
    run = tmp_path / 'run-id'
    (run / 'artifacts').mkdir(parents=True)
    receipt = {'decision_token': '0123456789abcdef', 'session_id': 'source', 'worktree': str(tmp_path / 'repo')}
    bridge.bind_run(receipt, run)
    (run / 'artifacts' / 'handoff.md').write_text('没有冻结证据的当前 handoff')
    finalized = {'run_id': run.name, 'run_dir': str(run), 'repo': receipt['worktree'], 'decision_kind': 'handoff', 'completed_at': 'now', 'errors': []}
    (run / 'artifacts' / '.finalize.lock').write_text(json.dumps(finalized))
    assert finalize.finalize_run(run_dir=run)['already_finalized'] is True
    assert not (tmp_path / 'automatic-closeout-completion-notifications').exists()


def test_freeze_failure_cannot_be_retried_using_changed_handoff(tmp_path, monkeypatch):
    import rvf_run_finalize as finalize
    monkeypatch.setenv('RVF_HOME', str(tmp_path / 'rvf-home'))
    receipt = {'decision_token': '0123456789abcdef', 'session_id': 'source', 'worktree': str(tmp_path / 'repo')}
    Path(receipt['worktree']).mkdir()
    run = tmp_path / 'run-id'
    (run / 'artifacts').mkdir(parents=True)
    (run / 'summary.json').write_text(json.dumps({'run_id': run.name, 'repo': receipt['worktree']}))
    bridge.bind_run(receipt, run)
    handoff = run / 'artifacts' / 'handoff.md'
    handoff.write_text('首次完成时的 handoff')
    for name in ('capture_run', '_write_usage_summary', '_release_tracker_lease', '_advance_review_highwater', '_scaffold_analysis'):
        monkeypatch.setattr(finalize, name, lambda *args, **kwargs: {})
    frozen_path = run / 'artifacts' / 'automatic-closeout-frozen-completion-notification.json'
    with monkeypatch.context() as freeze_failure:
        original_link = bridge.os.link
        def fail_frozen_notification_installation(source, destination, *args, **kwargs):
            if destination == frozen_path:
                raise OSError('冻结凭据无法落盘')
            return original_link(source, destination, *args, **kwargs)
        freeze_failure.setattr(bridge.os, 'link', fail_frozen_notification_installation)
        record = finalize.finalize_run(run_dir=run)
    assert any(error['stage'] == 'automatic_closeout_completion_freeze' for error in record['errors'])
    assert not frozen_path.exists()
    assert not list(frozen_path.parent.glob('*.writing'))
    handoff.write_text('完成后被修改的 handoff')
    assert finalize.finalize_run(run_dir=run)['already_finalized'] is True
    assert not frozen_path.exists()
    assert not (tmp_path / 'rvf-home' / 'automatic-closeout-completion-notifications').exists()


def test_concurrent_freezes_and_publications_keep_one_complete_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv('RVF_HOME', str(tmp_path))
    run = tmp_path / 'run-id'
    (run / 'artifacts').mkdir(parents=True)
    receipt = {'decision_token': '0123456789abcdef', 'session_id': 'source', 'worktree': str(tmp_path / 'repo')}
    bridge.bind_run(receipt, run)
    finalized = {'run_id': run.name, 'run_dir': str(run), 'repo': receipt['worktree'], 'decision_kind': 'handoff', 'completed_at': 'now', 'errors': []}
    handoff = run / 'artifacts' / 'handoff.md'
    handoff.write_text('并发完成的 handoff')
    original_read_bytes = Path.read_bytes
    simultaneous_candidates = threading.Barrier(2)
    def read_distinct_concurrent_handoff_candidates(path):
        if path == handoff:
            simultaneous_candidates.wait(timeout=10)
            return threading.current_thread().name.encode()
        return original_read_bytes(path)
    monkeypatch.setattr(Path, 'read_bytes', read_distinct_concurrent_handoff_candidates)
    def freeze_and_publish(_):
        bridge.freeze_completion_notification(run, finalized)
        bridge.publish_completion(run, finalized)
        return hashlib.sha256(threading.current_thread().name.encode()).hexdigest()
    with ThreadPoolExecutor(max_workers=2) as publishers:
        candidate_digests = set(publishers.map(freeze_and_publish, range(2)))
    frozen_path = run / 'artifacts' / 'automatic-closeout-frozen-completion-notification.json'
    frozen_bytes = frozen_path.read_bytes()
    frozen = json.loads(frozen_bytes)
    assert frozen['handoff_sha256'] in candidate_digests
    notification_path = tmp_path / 'automatic-closeout-completion-notifications' / f"{receipt['decision_token']}.json"
    assert notification_path.read_bytes() == frozen_bytes
    handoff.write_text('并发完成后修改的 handoff')
    bridge.freeze_completion_notification(run, finalized)
    bridge.publish_completion(run, finalized)
    assert frozen_path.read_bytes() == frozen_bytes
    assert notification_path.read_bytes() == frozen_bytes
