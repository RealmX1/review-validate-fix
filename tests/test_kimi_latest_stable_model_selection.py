"""验证 Kimi 官方目录协议、稳定模型排序、评审模型一致性和服务回收。"""
from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIRECTORY = ROOT / "plugins/review-validate-fix/skills/review-validate-fix"
SCRIPT_DIRECTORY = SKILL_DIRECTORY / "scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import kimi_latest_stable_model_selection as catalog  # noqa: E402
import run_alternative_reviewer as runner  # noqa: E402


def _model(alias: str, context: int = 1048576, **fields) -> dict:
    return {"provider": "managed:kimi-code", "model": alias, "max_context_size": context, **fields}


@pytest.mark.parametrize("seed", range(8))
def test_catalog_order_and_ineligible_variants_do_not_change_the_selected_model(seed: int):
    items = [
        _model("kimi-code/k3"), _model("kimi-code/k3-256k", 262144),
        _model("kimi-code/k3.1-pro"), _model("kimi-code/k3.1", 2097152),
        _model("kimi-code/k99-pro-highspeed"), _model("kimi-code/k99-preview"),
        _model("kimi-code/k99", display_name="K99 Preview"),
        _model("kimi-code/k99-pro", display_name="K99 Pro Highspeed"),
        _model("kimi-code/k100", provider="third-party"), _model("kimi-for-coding"),
    ]
    random.Random(seed).shuffle(items)
    assert catalog.select_latest_kimi_managed_stable_model(items) == "kimi-code/k3.1-pro"


@pytest.mark.parametrize("future", ["k3.2", "k4", "k10", "k3.10-pro"])
def test_new_numeric_generations_are_followed_without_production_code_changes(future: str):
    assert catalog.select_latest_kimi_managed_stable_model([
        _model("kimi-code/k3.1-pro"), _model("kimi-code/" + future),
    ]) == "kimi-code/" + future


def test_equal_generation_prefers_pro_then_larger_context():
    assert catalog.select_latest_kimi_managed_stable_model([
        _model("kimi-code/k3.1", 2097152), _model("kimi-code/k3.1-pro-256k", 262144),
        _model("kimi-code/k3.1.0-pro", 1048576),
    ]) == "kimi-code/k3.1.0-pro"


@pytest.mark.parametrize("items", [[], [None], [_model("kimi-code/k4", 0)], [_model("kimi-code/k4", True)],
                                        [_model("kimi-code/k4", "1048576")], [_model("kimi-code/k4-preview")]])
def test_missing_or_invalid_stable_catalog_fails(items):
    with pytest.raises(ValueError):
        catalog.select_latest_kimi_managed_stable_model(items)


@pytest.fixture
def fake_kimi(tmp_path: Path):
    executable = tmp_path / "bin" / "kimi"
    executable.parent.mkdir()
    executable.write_text(f"#!{sys.executable}\n" + r'''
import json, os, signal, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

directory = Path(os.environ['FAKE_KIMI_OBSERVATIONS_DIRECTORY'])
scenario = json.loads((directory / 'scenario.json').read_text())
token = 'secret-server-token-must-never-be-logged'
if scenario.get('distinct_service_token'):
    token += '-' + str(os.getpid())
def record(value):
    with (directory / 'observations.jsonl').open('a') as output:
        output.write(json.dumps(value) + '\n')

if sys.argv[1:2] == ['web']:
    assert sys.argv[1:] == ['web', '--host', '127.0.0.1', '--port', '0', '--no-open']
    (directory / 'server.pid').write_text(str(os.getpid()))
    if scenario.get('startup') == 'exit':
        print(token, flush=True)
        sys.exit(9)
    if scenario.get('startup') == 'timeout':
        print(token, flush=True)
        time.sleep(30)
    home = Path(os.environ['KIMI_CODE_HOME'])
    home.mkdir(parents=True, exist_ok=True)
    (home / 'server.token').write_text('' if scenario.get('empty_token') else token)
    if scenario.get('track_service_lifecycle'):
        record({'event': 'service-start', 'time': time.monotonic(), 'cwd': os.getcwd(),
                'lock_held': (home / '.kanban-kimi-model-catalog-refresh.lock').is_dir()})
        def terminate_service(*_):
            record({'event': 'service-stop', 'time': time.monotonic(),
                    'lock_held': (home / '.kanban-kimi-model-catalog-refresh.lock').is_dir()})
            sys.exit(0)
        signal.signal(signal.SIGTERM, terminate_service)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def respond(self):
            if self.headers['Authorization'] != 'Bearer ' + token:
                record({'event': 'authentication-failed', 'status': 401})
                self.send_response(401)
                self.end_headers()
                return
            record({'method': self.command, 'path': self.path})
            if scenario.get('flood_service_output'):
                # 每次响应前写入远超管道容量的日志，要求 HTTP 阶段持续排空。
                for _ in range(128):
                    os.write(1, b'x' * 8192)
            refreshing = self.path == '/api/v1/providers:refresh_oauth'
            assert self.path in ['/api/v1/providers:refresh_oauth', '/api/v1/models']
            if refreshing:
                assert self.command == 'POST' and self.rfile.read(int(self.headers['Content-Length'])) == b'{}'
                payload = scenario['refresh']
            else:
                assert self.command == 'GET'
                payload = scenario['models']
            response_body = b'not JSON' if scenario.get('invalid_json') else json.dumps(payload).encode()
            if scenario.get('trickle_response_phase') == 'headers':
                self.wfile.write(b'HTTP/1.0 200 OK\r\nX-Trickle: ')
                for _ in range(7):
                    self.wfile.write(b'x')
                    self.wfile.flush()
                    time.sleep(0.2)
                self.wfile.write(b'\r\nContent-Type: application/json\r\n\r\n')
            else:
                self.send_response(scenario.get('http_status', 200))
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
            if scenario.get('trickle_response_phase') == 'body':
                for _ in range(7):
                    self.wfile.write(b' ')
                    self.wfile.flush()
                    time.sleep(0.2)
            self.wfile.write(response_body)
        do_POST = respond
        do_GET = respond
    server = HTTPServer(('127.0.0.1', 0), Handler)
    time.sleep(scenario.get('startup_delay_seconds', 0))
    if scenario.get('service_child_ignores_sigterm'):
        child_process_id = os.fork()
        if child_process_id:
            if scenario['service_child_ignores_sigterm'] == 'parent_exits_before_request':
                os._exit(0)
            time.sleep(30)
            os._exit(0)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (directory / 'server-child.pid').write_text(str(os.getpid()))
    # 一次写入多行、ANSI 和 URL；后续不再输出，钉住预读缓冲死等的回归。
    os.write(1, ('\x1b[32m启动服务\x1b[0m\n' + token + '\nhttp://127.0.0.1:' + str(server.server_port) + '/\n').encode())
    server.serve_forever()
else:
    selected = sys.argv[sys.argv.index('--model') + 1] if '--model' in sys.argv else scenario.get('session_model')
    invocation = {'invocation': 'review' if os.environ.get('RVF_REVIEW_RESULT') else 'health', 'model': selected}
    if scenario.get('native_resume_fixture'):
        resume = []
        for index, argument in enumerate(sys.argv[1:], 1):
            if argument in ['--session', '-S']:
                resume.extend([argument, sys.argv[index + 1]])
            elif argument in ['--continue', '-c'] or argument.startswith(('--session=', '-S')):
                resume.append(argument)
        invocation.update(resume=resume, prompt_present='-p' in sys.argv and bool(sys.argv[sys.argv.index('-p') + 1]))
    record(invocation)
    if scenario.get('health_call_fails') and not os.environ.get('RVF_REVIEW_RESULT'):
        print('隔离健康调用失败', file=sys.stderr)
        sys.exit(7)
    if os.environ.get('RVF_REVIEW_RESULT'):
        subprocess_command = [sys.executable, os.environ['RVF_WRITE_REVIEW_RESULT'], 'no-issues',
                              '--out', os.environ['RVF_REVIEW_RESULT'], '--audit-summary',
                              '隔离协议夹具验证模型参数一致性']
        import subprocess
        subprocess.run(subprocess_command, check=True, stdout=subprocess.DEVNULL)
    print(json.dumps({'role': 'assistant', 'content': [{'type': 'text', 'text': 'OK'}]}))
''', encoding="utf-8")
    executable.chmod(0o755)
    scenario = {
        "refresh": {"code": 0, "data": {"changed": [], "unchanged": ["managed:kimi-code"], "failed": []}},
        "models": {"code": 0, "data": {"items": [_model("kimi-code/k3"), _model("kimi-code/k3.1-pro")]}}
    }
    # 子 runner 必须脱离父评审的范围与 lease，同时保留 PATH 等非 RVF 配置。
    environment = {
        name: value for name, value in os.environ.items()
        if not name.startswith(("RVF_", "CODEX_RVF_"))
    }
    environment.update(KIMI_CODE_HOME=str(tmp_path / "kimi-home"),
                       FAKE_KIMI_OBSERVATIONS_DIRECTORY=str(tmp_path),
                       HOME=str(tmp_path / "user-home"), RVF_SUPPRESS_STOP_HOOK="1",
                       RVF_USER_LOCAL_DATA_ROOT=str(tmp_path / "rvf-state"), RVF_HOME=str(tmp_path / "rvf-home"))
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    return executable, environment, scenario, scenario_path


def test_fake_kimi_environment_discards_parent_rvf_context_without_mutating_process(monkeypatch, request, tmp_path: Path):
    inherited_parent_context = {
        "RVF_RUN_DIR": "/parent-rvf/run",
        "RVF_SCOPE_CONTRACT": "/parent-rvf/scope.contract.json",
        "RVF_REVIEWER_ID": "parent-reviewer",
        "RVF_REVIEWER_PROMPT_FILE": "/parent-rvf/prompt.md",
        "RVF_REVIEW_RESULT": "/parent-rvf/review-result.json",
        "RVF_SESSION_MANIFEST": "/parent-rvf/session.json",
        "RVF_HOME": "/parent-rvf/home",
        "RVF_USER_LOCAL_DATA_ROOT": "/parent-rvf/state",
        "RVF_SUPPRESS_STOP_HOOK": "parent-value",
        "CODEX_RVF_RUN_ID": "parent-run",
        "CODEX_RVF_LOG_ROOT": "/parent-rvf/logs",
        "CODEX_RVF_STATE_DIR": "/parent-rvf/legacy-state",
    }
    for name, value in inherited_parent_context.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KIMI_FIXTURE_REQUIRED_NON_RVF_SETTING", "preserved")
    parent_environment_before_fixture = dict(os.environ)
    _, environment, _, _ = request.getfixturevalue("fake_kimi")

    assert dict(os.environ) == parent_environment_before_fixture
    assert environment["PATH"] == parent_environment_before_fixture["PATH"]
    assert environment["KIMI_FIXTURE_REQUIRED_NON_RVF_SETTING"] == "preserved"
    assert {
        name: value for name, value in environment.items()
        if name.startswith(("RVF_", "CODEX_RVF_"))
    } == {
        "RVF_HOME": str(tmp_path / "rvf-home"),
        "RVF_USER_LOCAL_DATA_ROOT": str(tmp_path / "rvf-state"),
        "RVF_SUPPRESS_STOP_HOOK": "1",
    }


def _assert_server_reaped(scenario_path: Path):
    process_id = int((scenario_path.parent / "server.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)


@pytest.fixture
def independent_catalog_callers(fake_kimi, tmp_path):
    executable, environment, scenario, _ = fake_kimi
    processes = []
    caller_directories = []
    def start(name, *, home=None, timeout=3, **scenario_changes):
        caller_directory = tmp_path / name
        caller_directory.mkdir()
        caller_directories.append(caller_directory)
        caller_scenario = {**scenario, 'distinct_service_token': True, 'track_service_lifecycle': True, **scenario_changes}
        (caller_directory / 'scenario.json').write_text(json.dumps(caller_scenario))
        caller_environment = {**environment, 'FAKE_KIMI_OBSERVATIONS_DIRECTORY': str(caller_directory)}
        if home is not None:
            caller_environment['KIMI_CODE_HOME'] = str(home)
        program = '''
import json, sys
sys.path.insert(0, sys.argv[1])
from kimi_latest_stable_model_selection import resolve_latest_kimi_managed_stable_model
try:
    import os
    selected = resolve_latest_kimi_managed_stable_model(sys.argv[2], env=dict(os.environ), timeout=float(sys.argv[3]))
    print(json.dumps({'ok': True, 'selected': selected}))
except Exception as error:
    print(json.dumps({'ok': False, 'diagnostic': str(error)}))
'''
        process = subprocess.Popen([sys.executable, '-c', program, str(SCRIPT_DIRECTORY), str(executable), str(timeout)],
                                   env=caller_environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(process)
        return process, caller_directory
    yield start
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)
    for caller_directory in caller_directories:
        pid_file = caller_directory / 'server.pid'
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def _wait_for_caller_event(caller_directory, event):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        observations_path = caller_directory / 'observations.jsonl'
        if observations_path.exists():
            for line in observations_path.read_text().splitlines():
                observation = json.loads(line)
                if observation.get('event') == event:
                    return observation
        time.sleep(0.01)
    pytest.fail('未收到服务生命周期事件：' + event)


def _catalog_process_result(process):
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert 'secret-server-token' not in stdout + stderr
    return json.loads(stdout)


def test_independent_catalog_processes_serialize_shared_home_without_token_overwrite(independent_catalog_callers):
    first, first_directory = independent_catalog_callers('first', startup_delay_seconds=0.4)
    first_started = _wait_for_caller_event(first_directory, 'service-start')
    second, second_directory = independent_catalog_callers('second')
    first_result, second_result = _catalog_process_result(first), _catalog_process_result(second)

    assert first_result['ok'] and second_result['ok'], (first_result, second_result)
    first_stopped = _wait_for_caller_event(first_directory, 'service-stop')
    second_started = _wait_for_caller_event(second_directory, 'service-start')
    assert first_started['lock_held'] and first_stopped['lock_held'] and second_started['lock_held']
    assert second_started['time'] >= first_stopped['time']
    assert not Path(first_started['cwd']).exists()


def test_independent_catalog_processes_overlap_for_different_homes(independent_catalog_callers, tmp_path):
    first, first_directory = independent_catalog_callers('first', home=tmp_path / 'first-home', startup_delay_seconds=0.5)
    _wait_for_caller_event(first_directory, 'service-start')
    second, second_directory = independent_catalog_callers('second', home=tmp_path / 'second-home')
    assert _catalog_process_result(second)['ok']
    assert _catalog_process_result(first)['ok']
    assert _wait_for_caller_event(second_directory, 'service-start')['time'] < _wait_for_caller_event(first_directory, 'service-stop')['time']


def test_failed_catalog_holder_releases_home_for_waiting_independent_process(independent_catalog_callers):
    first, first_directory = independent_catalog_callers('first', startup_delay_seconds=0.3, invalid_json=True)
    _wait_for_caller_event(first_directory, 'service-start')
    second, second_directory = independent_catalog_callers('second')
    assert not _catalog_process_result(first)['ok']
    assert _catalog_process_result(second)['ok']
    assert _wait_for_caller_event(second_directory, 'service-start')['time'] >= _wait_for_caller_event(first_directory, 'service-stop')['time']


def test_waiting_for_catalog_home_lock_uses_deadline_without_starting_service(independent_catalog_callers):
    holder, holder_directory = independent_catalog_callers('holder', startup_delay_seconds=0.6)
    _wait_for_caller_event(holder_directory, 'service-start')
    started_at = time.monotonic()
    waiter, waiter_directory = independent_catalog_callers('waiter', timeout=0.15)
    waiter_result = _catalog_process_result(waiter)
    assert not waiter_result['ok'] and '超时' in waiter_result['diagnostic']
    assert time.monotonic() - started_at < 0.5
    assert not (waiter_directory / 'server.pid').exists()
    assert _catalog_process_result(holder)['ok']


@pytest.mark.parametrize('invalid_json', [False, True])
@pytest.mark.parametrize('service_has_descendant', [False, True])
def test_catalog_home_lock_is_held_until_service_and_working_directory_are_cleaned(fake_kimi, monkeypatch, invalid_json, service_has_descendant):
    executable, environment, scenario, scenario_path = fake_kimi
    scenario['invalid_json'] = invalid_json
    if service_has_descendant:
        scenario['service_child_ignores_sigterm'] = 'parent_exits_before_request'
    scenario_path.write_text(json.dumps(scenario))
    lock_path = Path(environment['KIMI_CODE_HOME']) / catalog.KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_DIRECTORY_NAME
    original_temporary_directory = catalog.tempfile.TemporaryDirectory
    cleanup_observations = []
    class VerifyingTemporaryDirectory(original_temporary_directory):
        def cleanup(self):
            assert lock_path.is_dir() and list(lock_path.iterdir()) == []
            _assert_server_reaped(scenario_path)
            if service_has_descendant:
                child_process_id = int((scenario_path.parent / 'server-child.pid').read_text())
                state = subprocess.run(['ps', '-o', 'stat=', '-p', str(child_process_id)], capture_output=True, text=True).stdout.strip()
                assert not state or state.startswith('Z'), '回收 cwd 和释放锁前，服务子进程必须已退出'
            super().cleanup()
            assert lock_path.is_dir() and not Path(self.name).exists()
            cleanup_observations.append(True)
    monkeypatch.setattr(catalog.tempfile, 'TemporaryDirectory', VerifyingTemporaryDirectory)
    if invalid_json:
        with pytest.raises(ValueError):
            catalog.resolve_latest_kimi_managed_stable_model(str(executable), env=environment, timeout=2)
    else:
        assert catalog.resolve_latest_kimi_managed_stable_model(str(executable), env=environment, timeout=2) == 'kimi-code/k3.1-pro'
    assert cleanup_observations == [True] and not lock_path.exists()


def _new_catalog_home_refresh_lock(home, *, on_compromised=lambda: None, timeout=1):
    deadline = time.monotonic() + timeout
    def remaining_seconds():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('测试目录锁等待超时')
        return remaining
    return catalog.KimiModelCatalogHomeRefreshLock(home, remaining_seconds, on_compromised)


def test_catalog_home_lock_recovers_stale_empty_directory_without_changing_protocol(tmp_path):
    lock_path = tmp_path / '.kanban-kimi-model-catalog-refresh.lock'
    lock_path.mkdir()
    stale_timestamp = time.time() - 31
    os.utime(lock_path, (stale_timestamp, stale_timestamp))
    with _new_catalog_home_refresh_lock(tmp_path) as holder:
        assert holder.path == lock_path
        assert list(lock_path.iterdir()) == []
        assert time.time() - lock_path.stat().st_mtime < 1
    assert not lock_path.exists()
    assert catalog.KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_STALE_SECONDS == 30
    assert catalog.KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_HEARTBEAT_SECONDS == 15


def test_catalog_home_lock_heartbeat_advances_mtime_and_reclaims_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, 'KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_HEARTBEAT_SECONDS', 0.02)
    existing_threads = set(threading.enumerate())
    with _new_catalog_home_refresh_lock(tmp_path) as holder:
        initial_mtime = holder.path.stat().st_mtime_ns
        deadline = time.monotonic() + 0.5
        while holder.path.stat().st_mtime_ns == initial_mtime and time.monotonic() < deadline:
            time.sleep(0.01)
        assert holder.path.stat().st_mtime_ns > initial_mtime
        holder.assert_held()
        assert list(holder.path.iterdir()) == []
    assert set(threading.enumerate()) == existing_threads


def test_old_catalog_lock_holder_does_not_refresh_or_remove_replacement_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, 'KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_HEARTBEAT_SECONDS', 0.02)
    compromised = threading.Event()
    holder = _new_catalog_home_refresh_lock(tmp_path, on_compromised=compromised.set)
    holder.acquire()
    holder.path.rmdir()
    holder.path.mkdir()
    replacement_snapshot = holder.path.stat()
    try:
        assert compromised.wait(0.5)
        with pytest.raises(ValueError, match='互斥锁已失效'):
            holder.assert_held()
        with pytest.raises(ValueError, match='互斥锁已失效'):
            holder.release()
        assert holder.path.stat().st_ino == replacement_snapshot.st_ino
        assert holder.path.stat().st_mtime_ns == replacement_snapshot.st_mtime_ns
    finally:
        if holder.directory_descriptor is not None:
            try:
                holder.release()
            except ValueError:
                pass
        holder.path.rmdir()


def test_catalog_lock_holder_rejects_changed_mtime_without_removing_directory(tmp_path):
    holder = _new_catalog_home_refresh_lock(tmp_path)
    holder.acquire()
    observed = holder.path.stat()
    os.utime(holder.path, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000))
    with pytest.raises(ValueError, match='互斥锁已失效'):
        holder.release()
    assert holder.path.is_dir()
    holder.path.rmdir()


def test_catalog_lock_acquired_after_budget_exhaustion_is_released_without_spawning(fake_kimi, monkeypatch):
    executable, environment, _, _ = fake_kimi
    lock_path = Path(environment['KIMI_CODE_HOME']) / catalog.KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_DIRECTORY_NAME
    lock_path.mkdir(parents=True)
    original_mkdir = Path.mkdir
    lock_acquisition_attempts = []
    def mkdir_after_wait_consumes_remaining_budget(path, *args, **kwargs):
        if path == lock_path and not kwargs.get('exist_ok'):
            lock_acquisition_attempts.append(True)
            if len(lock_acquisition_attempts) == 1:
                raise FileExistsError('现有 holder 尚未释放')
            lock_path.rmdir()
            original_mkdir(path, *args, **kwargs)
            time.sleep(0.06)
            return
        return original_mkdir(path, *args, **kwargs)
    def unexpected_spawn(*_args, **_kwargs):
        pytest.fail('锁等待耗尽预算后不得启动服务')
    monkeypatch.setattr(Path, 'mkdir', mkdir_after_wait_consumes_remaining_budget)
    monkeypatch.setattr(catalog.subprocess, 'Popen', unexpected_spawn)
    with pytest.raises(ValueError, match='超时'):
        catalog.resolve_latest_kimi_managed_stable_model(str(executable), env=environment, timeout=0.08)
    assert not lock_path.exists()
    assert len(lock_acquisition_attempts) == 2


@pytest.mark.parametrize('initialization_failure', ['directory_descriptor', 'heartbeat_start'])
def test_catalog_home_lock_releases_directory_if_acquisition_initialization_fails(tmp_path, monkeypatch, initialization_failure):
    holder = _new_catalog_home_refresh_lock(tmp_path)
    if initialization_failure == 'directory_descriptor':
        def fail_open(*_args, **_kwargs):
            raise OSError('目录 fd 初始化失败')
        monkeypatch.setattr(catalog.os, 'open', fail_open)
        expected_error = OSError
    else:
        def fail_start(*_args, **_kwargs):
            raise RuntimeError('heartbeat 初始化失败')
        monkeypatch.setattr(catalog.threading.Thread, 'start', fail_start)
        expected_error = RuntimeError
    with pytest.raises(expected_error):
        with holder:
            pytest.fail('初始化失败不得进入持锁代码')
    assert not holder.path.exists()
    assert holder.directory_descriptor is None


@pytest.mark.parametrize('concurrent_change', ['heartbeat', 'replacement'])
def test_stale_catalog_lock_recovery_preserves_changed_holder(tmp_path, monkeypatch, concurrent_change):
    holder = _new_catalog_home_refresh_lock(tmp_path, timeout=0.1)
    holder.path.mkdir()
    stale_timestamp = time.time() - 31
    os.utime(holder.path, (stale_timestamp, stale_timestamp))
    original_open = os.open
    replacement_snapshots = []
    def change_holder_before_stale_identity_check(path, *args, **kwargs):
        if path == holder.path and not replacement_snapshots:
            if concurrent_change == 'replacement':
                holder.path.rmdir()
                holder.path.mkdir()
            else:
                os.utime(holder.path, None)
            replacement_snapshots.append(holder.path.stat())
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(catalog.os, 'open', change_holder_before_stale_identity_check)
    with pytest.raises(ValueError, match='等待超时'):
        with holder:
            pytest.fail('不能取得刚刷新或被替换的锁')
    assert holder.path.stat().st_ino == replacement_snapshots[0].st_ino
    assert holder.path.stat().st_mtime_ns == replacement_snapshots[0].st_mtime_ns
    holder.path.rmdir()


def test_catalog_heartbeat_uses_old_directory_descriptor_if_replaced_after_identity_check(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, 'KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_HEARTBEAT_SECONDS', 0.02)
    compromised = threading.Event()
    holder = _new_catalog_home_refresh_lock(tmp_path, on_compromised=compromised.set)
    original_utime = os.utime
    replacement_snapshots = []
    def replace_directory_before_heartbeat_write(path, *args, **kwargs):
        if path == holder.directory_descriptor and not replacement_snapshots:
            holder.path.rmdir()
            holder.path.mkdir()
            replacement_snapshots.append(holder.path.stat())
        return original_utime(path, *args, **kwargs)
    monkeypatch.setattr(catalog.os, 'utime', replace_directory_before_heartbeat_write)
    holder.acquire()
    try:
        assert compromised.wait(0.5)
        with pytest.raises(ValueError, match='互斥锁已失效'):
            holder.release()
        assert holder.path.stat().st_ino == replacement_snapshots[0].st_ino
        assert holder.path.stat().st_mtime_ns == replacement_snapshots[0].st_mtime_ns
    finally:
        if holder.directory_descriptor is not None:
            try:
                holder.release()
            except ValueError:
                pass
        holder.path.rmdir()


def test_compromised_catalog_lock_interrupts_http_request_and_preserves_replacement(fake_kimi, monkeypatch):
    executable, environment, scenario, scenario_path = fake_kimi
    scenario['trickle_response_phase'] = 'body'
    scenario_path.write_text(json.dumps(scenario))
    monkeypatch.setattr(catalog, 'KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_HEARTBEAT_SECONDS', 0.02)
    lock_path = Path(environment['KIMI_CODE_HOME']) / catalog.KIMI_MODEL_CATALOG_HOME_REFRESH_LOCK_DIRECTORY_NAME
    replacement_snapshots = []
    def replace_lock_after_request_starts():
        observation_path = scenario_path.parent / 'observations.jsonl'
        deadline = time.monotonic() + 1
        while not observation_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if observation_path.exists():
            lock_path.rmdir()
            lock_path.mkdir()
            replacement_snapshots.append(lock_path.stat())
    replacement_thread = threading.Thread(target=replace_lock_after_request_starts)
    replacement_thread.start()
    try:
        started_at = time.monotonic()
        with pytest.raises(ValueError, match='互斥锁已失效'):
            catalog.resolve_latest_kimi_managed_stable_model(str(executable), env=environment, timeout=2)
        assert time.monotonic() - started_at < 0.7
        _assert_server_reaped(scenario_path)
        assert replacement_snapshots
        assert lock_path.stat().st_ino == replacement_snapshots[0].st_ino
        assert lock_path.stat().st_mtime_ns == replacement_snapshots[0].st_mtime_ns
    finally:
        replacement_thread.join(timeout=2)
        if lock_path.exists():
            lock_path.rmdir()


@pytest.mark.parametrize("changed", [["managed:kimi-code"], [{"provider_id": "managed:kimi-code", "added": 1, "removed": 0}]])
def test_official_refresh_accepts_changed_provider_shapes_and_reaps_service(fake_kimi, changed):
    executable, environment, scenario, scenario_path = fake_kimi
    scenario["refresh"]["data"].update(changed=changed, unchanged=[])
    scenario_path.write_text(json.dumps(scenario))
    assert catalog.resolve_latest_kimi_managed_stable_model(str(executable), env=environment, timeout=2) == "kimi-code/k3.1-pro"
    _assert_server_reaped(scenario_path)
    observations = [json.loads(line) for line in (scenario_path.parent / "observations.jsonl").read_text().splitlines()]
    assert observations == [
        {"method": "POST", "path": "/api/v1/providers:refresh_oauth"},
        {"method": "GET", "path": "/api/v1/models"},
    ]


@pytest.mark.parametrize('invalid_json', [False, True])
def test_catalog_requests_drain_flooding_service_output_and_release_reader_resources(fake_kimi, monkeypatch, invalid_json):
    executable, environment, scenario, scenario_path = fake_kimi
    scenario['flood_service_output'] = True
    scenario['invalid_json'] = invalid_json
    scenario_path.write_text(json.dumps(scenario))
    service_processes = []
    original_popen = subprocess.Popen
    def record_service_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        service_processes.append(process)
        return process
    monkeypatch.setattr(catalog.subprocess, 'Popen', record_service_process)
    existing_threads = set(threading.enumerate())

    if invalid_json:
        with pytest.raises(ValueError, match='JSONDecodeError'):
            catalog.resolve_latest_kimi_managed_stable_model(str(executable), env=environment, timeout=3)
    else:
        assert catalog.resolve_latest_kimi_managed_stable_model(str(executable), env=environment, timeout=3) == 'kimi-code/k3.1-pro'

    _assert_server_reaped(scenario_path)
    assert service_processes and all(process.stdout.closed for process in service_processes)
    assert set(threading.enumerate()) == existing_threads
    observations = [json.loads(line) for line in (scenario_path.parent / 'observations.jsonl').read_text().splitlines()]
    expected_observations = [
        {'method': 'POST', 'path': '/api/v1/providers:refresh_oauth'},
        {'method': 'GET', 'path': '/api/v1/models'},
    ]
    if invalid_json:
        expected_observations = expected_observations[:1]
    assert observations == expected_observations


@pytest.mark.parametrize('response_phase', ['headers', 'body'])
def test_catalog_absolute_deadline_interrupts_trickling_headers_and_body(fake_kimi, response_phase):
    executable, environment, scenario, scenario_path = fake_kimi
    scenario['trickle_response_phase'] = response_phase
    scenario_path.write_text(json.dumps(scenario))
    existing_threads = set(threading.enumerate())
    started_at = time.monotonic()
    with pytest.raises(ValueError) as failure:
        catalog.resolve_latest_kimi_managed_stable_model(str(executable), env=environment, timeout=0.5)
    elapsed_seconds = time.monotonic() - started_at

    assert elapsed_seconds < 1.0, f'绝对时限未生效：耗时 {elapsed_seconds:.3f} 秒'
    assert '超时' in str(failure.value)
    assert 'secret-server-token' not in str(failure.value)
    _assert_server_reaped(scenario_path)
    assert set(threading.enumerate()) == existing_threads


@pytest.mark.parametrize('parent_exit', ['parent_exits_before_request', 'parent_exits_after_sigterm'])
def test_service_cleanup_kills_ignoring_descendant_after_parent_exit_without_affecting_other_groups(fake_kimi, parent_exit):
    executable, environment, scenario, scenario_path = fake_kimi
    scenario['service_child_ignores_sigterm'] = parent_exit
    scenario_path.write_text(json.dumps(scenario))
    unrelated_process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    existing_threads = set(threading.enumerate())
    try:
        started_at = time.monotonic()
        assert catalog.resolve_latest_kimi_managed_stable_model(str(executable), env=environment, timeout=2) == 'kimi-code/k3.1-pro'
        assert time.monotonic() - started_at < 1.0
        _assert_server_reaped(scenario_path)
        child_process_id = int((scenario_path.parent / 'server-child.pid').read_text())
        exit_deadline = time.monotonic() + 1
        while time.monotonic() < exit_deadline:
            state = subprocess.run(['ps', '-o', 'stat=', '-p', str(child_process_id)], capture_output=True, text=True).stdout.strip()
            # 被系统收养的子进程可能短暂处于僵尸态；它已经退出，无法继续提供服务。
            if not state or state.startswith('Z'):
                break
            time.sleep(0.01)
        else:
            pytest.fail('忽略 SIGTERM 的服务子进程仍在运行')
        assert unrelated_process.poll() is None
        assert set(threading.enumerate()) == existing_threads
    finally:
        # 基线验证失败也必须回收夹具自己创建的服务组与无关进程组。
        if (scenario_path.parent / 'server.pid').exists():
            service_group_id = int((scenario_path.parent / 'server.pid').read_text())
            try:
                os.killpg(service_group_id, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        unrelated_process.kill()
        unrelated_process.wait(timeout=2)


@pytest.mark.parametrize("mutation", [
    {"refresh": {"code": 9, "msg": "secret-server-token-must-never-be-logged", "data": {}}},
    {"refresh": {"code": False, "data": {}}},
    {"refresh": {"code": 0, "data": {"changed": [], "unchanged": [], "failed": []}}},
    {"refresh": {"code": 0, "data": {"changed": [], "unchanged": ["managed:kimi-code"], "failed": [{"reason": "secret-server-token-must-never-be-logged"}]}}},
    {"refresh": {"code": 0, "data": {"changed": "managed:kimi-code", "unchanged": [], "failed": []}}},
    {"refresh": {"code": 0, "data": {"changed": [{}], "unchanged": [], "failed": []}}},
    {"models": {"code": 0, "data": {"items": {}}}},
    {"models": {"code": 0, "data": {"items": []}}},
    {"models": {"code": 1, "data": {}}},
    {"http_status": 500}, {"http_status": 302}, {"invalid_json": True},
    {"startup": "exit"}, {"startup": "timeout"}, {"empty_token": True},
])
def test_catalog_failures_are_visible_without_leaking_secrets_and_reap_service(fake_kimi, mutation):
    executable, environment, scenario, scenario_path = fake_kimi
    scenario.update(mutation)
    scenario_path.write_text(json.dumps(scenario))
    with pytest.raises(ValueError) as failure:
        catalog.resolve_latest_kimi_managed_stable_model(str(executable), env=environment, timeout=1 if mutation.get("startup") == "timeout" else 3)
    assert "secret-server-token" not in str(failure.value)
    _assert_server_reaped(scenario_path)


def _template():
    return json.loads((SKILL_DIRECTORY / "config/alternative-reviewer.kimi.json").read_text())


def test_model_is_resolved_once_and_shared_by_health_and_review(monkeypatch, tmp_path: Path):
    template = _template()
    calls = []
    def resolve(executable, **options):
        calls.append((executable, options))
        return "kimi-code/k4-pro"
    monkeypatch.setattr(runner, "resolve_latest_kimi_managed_stable_model", resolve)
    command, health = runner.resolve_reviewer_model_commands(
        template, template["command"], template["health_command"], env={"SAME_ENV": "yes"}, cwd=tmp_path, timeout=45,
    )
    assert len(calls) == 1 and calls[0][1]["env"] == {"SAME_ENV": "yes"}
    for argv in (command, health):
        assert argv[argv.index("--model") + 1] == "kimi-code/k4-pro"
    assert "--model" not in template["command"]
    runner.assert_health_command_shares_dispatch_flag_set(command, health, prompt_flag="-p", run_directory_grant_flag="--add-dir")


@pytest.mark.parametrize("explicit", [["--model", "kimi-code/k3"], ["-m", "custom-model"], ["--model=custom-model"], ["-mcustom-model"]])
def test_explicit_model_override_bypasses_catalog_and_is_shared_with_health(monkeypatch, tmp_path: Path, explicit):
    template = _template()
    def unexpected(*_args, **_kwargs):
        pytest.fail("显式模型不应刷新目录")
    monkeypatch.setattr(runner, "resolve_latest_kimi_managed_stable_model", unexpected)
    original = ["kimi", *explicit, *template["command"][1:]]
    command, health = runner.resolve_reviewer_model_commands(
        template, original, template["health_command"], env={}, cwd=tmp_path, timeout=5,
    )
    expected = explicit[-1].split("=", 1)[-1] if explicit[0].startswith("--model=") else explicit[-1].removeprefix("-m") if len(explicit) == 1 else explicit[-1]
    assert command[command.index("--model") + 1] == expected
    assert health[health.index("--model") + 1] == expected
    runner.assert_health_command_shares_dispatch_flag_set(command, health, prompt_flag="-p", run_directory_grant_flag="--add-dir")


@pytest.mark.parametrize("resume", [["--session", "existing"], ["-S", "existing"], ["--session=existing"], ["--continue"], ["-c"]])
def test_native_resume_keeps_its_existing_model_without_catalog_refresh(monkeypatch, tmp_path: Path, resume):
    template = _template()
    monkeypatch.setattr(runner, "resolve_latest_kimi_managed_stable_model", lambda *_a, **_k: pytest.fail("恢复会话不应刷新目录"))
    original = [*template["command"], *resume]
    assert runner.resolve_reviewer_model_commands(template, original, template["health_command"], env={}, cwd=tmp_path, timeout=5)[0] == original


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_real_runner_entrypoint_resolves_once_before_health_and_fixture_dispatch(fake_kimi, tmp_path: Path, refresh_fails: bool):
    executable, environment, scenario, scenario_path = fake_kimi
    if refresh_fails:
        scenario["refresh"]["data"]["failed"] = [{"provider": "managed:kimi-code", "reason": "secret-server-token-must-never-be-logged"}]
        scenario_path.write_text(json.dumps(scenario))
    template = _template()
    template["command"][0] = template["health_command"][0] = str(executable)
    config_path = tmp_path / "reviewer.json"
    config_path.write_text(json.dumps(template))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    completed = subprocess.run([
        sys.executable, str(SCRIPT_DIRECTORY / "run_alternative_reviewer.py"),
        "--config", str(config_path), "--repo", str(repo),
        "--rvf-run-dir", str(tmp_path / "run"), "--rvf-run-id", "kimi-model-contract-fixture",
    ], env=environment, capture_output=True, text=True, timeout=15)
    observations = [json.loads(line) for line in (tmp_path / "observations.jsonl").read_text().splitlines()]
    if refresh_fails:
        assert completed.returncode == 2
        assert "目录刷新失败" in completed.stderr
        assert "secret-server-token" not in completed.stderr + completed.stdout
        assert observations == [{"method": "POST", "path": "/api/v1/providers:refresh_oauth"}]
        _assert_server_reaped(scenario_path)
        return
    assert completed.returncode == 0, completed.stderr
    assert observations == [
        {"method": "POST", "path": "/api/v1/providers:refresh_oauth"},
        {"method": "GET", "path": "/api/v1/models"},
        {"invocation": "health", "model": "kimi-code/k3.1-pro"},
        {"invocation": "review", "model": "kimi-code/k3.1-pro"},
    ]
    _assert_server_reaped(scenario_path)


@pytest.mark.parametrize("resume", [["--session", "existing"], ["-S", "existing"], ["--session=existing"], ["-Sexisting"], ["--continue"], ["-c"]])
@pytest.mark.parametrize("explicit_model", [None, "custom-model"])
@pytest.mark.parametrize("health_call_fails", [False, True])
def test_complete_runner_native_resume_preserves_session_model_and_real_health_call(fake_kimi, tmp_path: Path, resume, explicit_model, health_call_fails):
    executable, environment, scenario, scenario_path = fake_kimi
    scenario["native_resume_fixture"] = True
    scenario["health_call_fails"] = health_call_fails
    scenario["session_model"] = "kimi-code/persisted-session-model"
    scenario_path.write_text(json.dumps(scenario))
    template = _template()
    template["command"][0] = template["health_command"][0] = str(executable)
    template["command"].extend(resume)
    if explicit_model is not None:
        template["command"].extend(["-m", explicit_model])
    config_path = tmp_path / "reviewer.json"
    config_path.write_text(json.dumps(template))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    completed = subprocess.run([
        sys.executable, str(SCRIPT_DIRECTORY / "run_alternative_reviewer.py"),
        "--config", str(config_path), "--repo", str(repo),
        "--rvf-run-dir", str(tmp_path / "run"), "--rvf-run-id", "kimi-native-resume-fixture",
    ], env=environment, capture_output=True, text=True, timeout=15)
    assert completed.returncode == (1 if health_call_fails else 0), completed.stderr
    observations = [json.loads(line) for line in (tmp_path / "observations.jsonl").read_text().splitlines()]
    expected_model = explicit_model or scenario["session_model"]
    expected_observations = [
        {"invocation": "health", "model": expected_model, "resume": resume, "prompt_present": True},
        {"invocation": "review", "model": expected_model, "resume": resume, "prompt_present": True},
    ]
    assert observations == (expected_observations[:1] if health_call_fails else expected_observations)
    assert not (tmp_path / "server.pid").exists(), "恢复会话不得启动默认目录服务"


@pytest.mark.parametrize("health_resume", [["-S", "existing"], ["--session=existing"], ["-Sexisting"]])
def test_native_resume_equivalent_health_selector_uses_dispatch_spelling(monkeypatch, tmp_path: Path, health_resume):
    template = _template()
    monkeypatch.setattr(runner, "resolve_latest_kimi_managed_stable_model", lambda *_a, **_k: pytest.fail("恢复会话不应刷新目录"))
    command, health = runner.resolve_reviewer_model_commands(
        template, [*template["command"], "--session", "existing"],
        [*template["health_command"], *health_resume], env={}, cwd=tmp_path, timeout=5,
    )
    runner.assert_health_command_shares_dispatch_flag_set(command, health, prompt_flag="-p", run_directory_grant_flag="--add-dir")


@pytest.mark.parametrize("dispatch_resume, health_resume", [
    (["--session", "existing"], ["--session", "other"]),
    (["--continue"], ["--session", "existing"]),
    ([], ["--continue"]),
])
def test_native_resume_different_health_session_is_rejected_before_any_call(monkeypatch, tmp_path: Path, dispatch_resume, health_resume):
    template = _template()
    monkeypatch.setattr(runner, "resolve_latest_kimi_managed_stable_model", lambda *_a, **_k: pytest.fail("无效恢复参数不应刷新目录"))
    with pytest.raises(ValueError, match="原生恢复会话不一致"):
        runner.resolve_reviewer_model_commands(
            template, [*template["command"], *dispatch_resume],
            [*template["health_command"], *health_resume], env={}, cwd=tmp_path, timeout=5,
        )


@pytest.mark.parametrize("health_model", ["different-model", "custom-model"])
def test_native_resume_health_cannot_override_session_or_explicit_dispatch_model(monkeypatch, tmp_path: Path, health_model):
    template = _template()
    monkeypatch.setattr(runner, "resolve_latest_kimi_managed_stable_model", lambda *_a, **_k: pytest.fail("无效恢复模型不应刷新目录"))
    command = [*template["command"], "--session", "existing"]
    if health_model == "different-model":
        command.extend(["--model", "custom-model"])
    with pytest.raises(ValueError, match="模型"):
        runner.resolve_reviewer_model_commands(
            template, command, [*template["health_command"], "--model", health_model], env={}, cwd=tmp_path, timeout=5,
        )
