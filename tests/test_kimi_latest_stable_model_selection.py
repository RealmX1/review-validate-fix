"""验证 Kimi 官方目录协议、稳定模型排序、评审模型一致性和服务回收。"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
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
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

directory = Path(os.environ['FAKE_KIMI_OBSERVATIONS_DIRECTORY'])
scenario = json.loads((directory / 'scenario.json').read_text())
token = 'secret-server-token-must-never-be-logged'
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
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def respond(self):
            assert self.headers['Authorization'] == 'Bearer ' + token
            record({'method': self.command, 'path': self.path})
            refreshing = self.path == '/api/v1/providers:refresh_oauth'
            assert self.path in ['/api/v1/providers:refresh_oauth', '/api/v1/models']
            if refreshing:
                assert self.command == 'POST' and self.rfile.read(int(self.headers['Content-Length'])) == b'{}'
                payload = scenario['refresh']
            else:
                assert self.command == 'GET'
                payload = scenario['models']
            self.send_response(scenario.get('http_status', 200))
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'not JSON' if scenario.get('invalid_json') else json.dumps(payload).encode())
        do_POST = respond
        do_GET = respond
    server = HTTPServer(('127.0.0.1', 0), Handler)
    # 一次写入多行、ANSI 和 URL；后续不再输出，钉住预读缓冲死等的回归。
    os.write(1, ('\x1b[32m启动服务\x1b[0m\n' + token + '\nhttp://127.0.0.1:' + str(server.server_port) + '/\n').encode())
    server.serve_forever()
else:
    selected = sys.argv[sys.argv.index('--model') + 1]
    record({'invocation': 'review' if os.environ.get('RVF_REVIEW_RESULT') else 'health', 'model': selected})
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
    environment = dict(os.environ, KIMI_CODE_HOME=str(tmp_path / "kimi-home"),
                       FAKE_KIMI_OBSERVATIONS_DIRECTORY=str(tmp_path),
                       HOME=str(tmp_path / "user-home"), RVF_SUPPRESS_STOP_HOOK="1",
                       RVF_USER_LOCAL_DATA_ROOT=str(tmp_path / "rvf-state"), RVF_HOME=str(tmp_path / "rvf-home"))
    for overriding_root_name in ("CODEX_RVF_LOG_ROOT", "CODEX_RVF_STATE_DIR"):
        environment.pop(overriding_root_name, None)
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    return executable, environment, scenario, scenario_path


def _assert_server_reaped(scenario_path: Path):
    process_id = int((scenario_path.parent / "server.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)


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
