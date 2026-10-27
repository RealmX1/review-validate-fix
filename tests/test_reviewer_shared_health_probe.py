"""真实 CLI 边界计数：选路预检和正式派发共用同一有效探针。"""

import json
import fcntl
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from test_review_start_independent_user_turn_boundary import valid_boundary, isolate_runtime_state

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
sys.path.insert(0, str(SCRIPTS))
import run_alternative_reviewer as runner


def test_preflight_and_dispatch_run_only_one_health_model_call(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    probe_count = tmp_path / "health-model-call-count.txt"
    reviewer = tmp_path / "isolated-reviewer.py"
    reviewer.write_text(
        "import os,sys,json\nfrom pathlib import Path\n"
        "if '--probe' in sys.argv:\n"
        f" with Path({str(probe_count)!r}).open('a') as handle: handle.write('probe\\n')\n"
        "else:\n"
        " sys.stdin.read()\n"
        " path=Path(os.environ['RVF_REVIEW_RESULT']); path.parent.mkdir(parents=True,exist_ok=True)\n"
        " path.write_text(json.dumps({'schema_version':1,'kind':'no_issues','issues':[],'requests':[],'audit_summary':'已完整审查'}))\n"
    )
    config = tmp_path / "alternative-reviewer.hermetic.json"
    config.write_text(json.dumps({"enabled": True, "label": "alternative-reviewer:hermetic",
        "command": [sys.executable, str(reviewer)], "health_command": [sys.executable, str(reviewer), "--probe"],
        "pre_run_health": True, "allow_repo_cwd": True,
        "health_probe_parity_exemption": "inline_script_fixture_has_no_shared_cli_entrypoint"}))
    packet = tmp_path / "packet.md"
    packet.write_text("审查隔离测试仓库。")
    environment = {**os.environ, "RVF_USER_LOCAL_DATA_ROOT": str(tmp_path / "state"),
        "HOME": str(tmp_path / "isolated-home"), "CODEX_HOME": str(tmp_path / "isolated-codex-home"),
        "CODEX_SQLITE_HOME": str(tmp_path / "isolated-codex-sqlite")}
    command = [sys.executable, str(SCRIPTS / "run_alternative_reviewer.py"), "--config", str(config), "--repo", str(repo)]
    preflight = subprocess.run([*command, "--preflight"], env=environment, text=True, capture_output=True)
    assert preflight.returncode == 0, preflight.stderr
    assert json.loads(preflight.stdout.strip().splitlines()[-1])["reason_code"] == "reviewer_health_completed"
    dispatched = subprocess.run([*command, "--review-packet", str(packet), "--rvf-run-dir", str(tmp_path / "run")],
                               env=environment, text=True, capture_output=True)
    assert dispatched.returncode == 0, dispatched.stderr
    assert probe_count.read_text().splitlines() == ["probe"]


def test_resolved_dynamic_model_and_execution_identity_change_probe_digest(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config = {"command": [sys.executable], "label": "alternative-reviewer:dynamic",
              "reviewer_model_selection_source": "isolated-default"}
    config_path.write_text(json.dumps(config))
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "state"))
    def identity(model):
        return runner.effective_reviewer_probe_identity(config_path, config,
            [sys.executable, "--model", model], [sys.executable, "--model", model, "probe"], tmp_path)
    first = identity("model-a")
    assert first == identity("model-a")
    assert first != identity("model-b")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "another-execution-identity"))
    assert first != identity("model-a")


@pytest.mark.parametrize("failure_text,expected_reason,expected_returncode", [
    ("usage limit exceeded; retry after 120 seconds", "usage_limit_exhausted", 125),
    ("context_length_exceeded", "reviewer_input_context_limit", 2),
])
def test_preflight_classifies_quota_and_input_failures(tmp_path, failure_text, expected_reason, expected_returncode):
    config = tmp_path / "probe.json"
    config.write_text(json.dumps({"enabled": True, "label": "alternative-reviewer:isolated-quota",
        "reviewer_harness_id": "isolated-quota-pool", "command": [sys.executable, "-c", "pass"],
        "health_command": [sys.executable, "-c", f"import sys;sys.stderr.write({failure_text!r});sys.exit(1)"],
        "health_probe_parity_exemption": "inline_script_fixture_has_no_shared_cli_entrypoint"}))
    environment = {**os.environ, "HOME": str(tmp_path / "isolated-home"),
        "CODEX_HOME": str(tmp_path / "isolated-codex-home"), "CODEX_SQLITE_HOME": str(tmp_path / "isolated-sqlite-home"),
        "RVF_USER_LOCAL_DATA_ROOT": str(tmp_path / "state"), "RVF_HARNESS_LIMIT_COOLDOWN_ROOT": str(tmp_path / "cooldown")}
    completed = subprocess.run([sys.executable, str(SCRIPTS / "run_alternative_reviewer.py"),
        "--config", str(config), "--preflight"], text=True, capture_output=True, env=environment)
    assert completed.returncode == expected_returncode, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["reason_code"] == expected_reason
    assert bool(list((tmp_path / "cooldown").glob("*.json"))) == (expected_reason == "usage_limit_exhausted")
    cache_entries = list((tmp_path / "state/runtime-state/reviewer-harness-probe-result-cache").glob("*.json"))
    assert bool(cache_entries) == (expected_reason == "usage_limit_exhausted")
    if expected_reason == "usage_limit_exhausted":
        # 跨重启缺少 cooldown 时，仍须从有效探针恢复额度事实，不能错误进入 R3。
        original_expiry = json.loads(next((tmp_path / "cooldown").glob("*.json")).read_text())["expires_at"]
        for marker in (tmp_path / "cooldown").glob("*.json"):
            marker.unlink()
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"harnesses": {"isolated-quota-pool": {"enabled": True,
        "config_path": str(config), "dispatch_mode": "external_cli"}}}))
    routed = subprocess.run([sys.executable, str(SCRIPTS / "dispatch_reviewers.py"), "--registry", str(registry),
        "--dry-run", "--rvf-run-id", "isolated-routing", "--rvf-run-dir", str(tmp_path / "routing-run")],
        text=True, capture_output=True, env=environment)
    plan = json.loads(routed.stdout)
    assert plan["needs_last_resort_fallback"] is False
    if expected_reason == "usage_limit_exhausted":
        assert plan["status"] == "failed" and plan["reason"] == "all_harnesses_usage_limited"
        from datetime import datetime
        restored_expiry = json.loads(next((tmp_path / "cooldown").glob("*.json")).read_text())["expires_at"]
        assert abs((datetime.fromisoformat(restored_expiry.replace("Z", "+00:00"))
                    - datetime.fromisoformat(original_expiry.replace("Z", "+00:00"))).total_seconds()) < .25
    else:
        assert plan["status"] == "waiting_for_reviewer_probe_evidence"


@pytest.fixture
def shared_probe_wait_dispatch(valid_boundary, tmp_path, monkeypatch):
    import prepare_review_run
    from core.reviewer_harness_probe_result_cache import resolved_probe_result_cache_entry_path

    for variable, directory in (("HOME", "isolated-home"), ("CODEX_HOME", "isolated-codex-home"),
                                ("CODEX_SQLITE_HOME", "isolated-sqlite-home"),
                                ("RVF_HARNESS_LIMIT_COOLDOWN_ROOT", "isolated-cooldowns")):
        monkeypatch.setenv(variable, str(tmp_path / directory))
    monkeypatch.setenv("RVF_REVIEWER_REQUEST_CONDITION_WAIT_SECONDS", "0")
    context = tmp_path / "scope-of-work.md"
    context.write_text("审查 a.py 的实现。")
    prepared = prepare_review_run.prepare_run(repo=valid_boundary["repo"], session_context=context,
        session_manifest=None, transcript=None, base_dir=tmp_path / "runs", max_file_bytes=10000,
        max_packet_bytes=0, primary_files=["a.py"], background_files=[], exclude_path_prefixes=[],
        decision_token=valid_boundary["token"])
    run_directory = Path(prepared["run_dir"])
    reviewer_script = tmp_path / "isolated-shared-probe-wait-reviewer.py"
    reviewer_script.write_text(
        "import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
        "label,run,scenario=sys.argv[1:4]; run=Path(run)\n"
        "if '--probe' in sys.argv:\n"
        " with (run/(label+'-health-count.txt')).open('a') as handle: handle.write('probe\\n')\n"
        " raise SystemExit(0)\n"
        "reviewer_prompt=sys.stdin.read()\n"
        "count=run/(label+'-execution-count.txt')\n"
        "with count.open('a') as handle: handle.write('execution\\n')\n"
        "result=Path(os.environ['RVF_REVIEW_RESULT'])\n"
        f"writer={str(SCRIPTS / 'write_review_result.py')!r}\n"
        "def write(command,*arguments):\n"
        " subprocess.run([sys.executable,writer,command,'--out',str(result),*arguments],check=True)\n"
        "if label=='alpha' and scenario=='request-once':\n"
        " if len(count.read_text().splitlines())==1:\n"
        "  write('context-request','--need','file','--target',str(run/'required-evidence.txt'),'--reason','等待隔离证据')\n"
        " else:\n"
        "  request=json.loads(result.read_text())['requests'][0]\n"
        "  assert request['status']=='responded'\n"
        "  write('resolve-request','--request-id',request['request_id'],'--resolution','证据已查证')\n"
        "  write('complete','--audit-summary','完整范围已独立查证')\n"
        "elif label=='alpha' and scenario.startswith('context-limit-probe-wait'):\n"
        " with (run/'alpha-native-prompt-presentations.jsonl').open('a') as output:\n"
        "  output.write(json.dumps({'prompt_presentation':'file_references' if reviewer_prompt.startswith('负责完整正式 Scope') else 'inline'})+'\\n')\n"
        " execution_number=len(count.read_text().splitlines())\n"
        " if execution_number==1:\n"
        "  (run/'first-input-failure-ready.txt').write_text('首次真实执行即将报告输入超限。')\n"
        "  deadline=time.monotonic()+10\n"
        "  while not (run/'input-retry-probe-lock-acquired.txt').exists():\n"
        "   if time.monotonic()>deadline: raise SystemExit(8)\n"
        "   time.sleep(.01)\n"
        " if execution_number==1 or scenario=='context-limit-probe-wait-permanent':\n"
        "  print('context_length_exceeded',file=sys.stderr)\n"
        "  raise SystemExit(1)\n"
        " write('no-issues','--audit-summary','完整引用 Scope 已独立查证')\n"
        "else: write('no-issues','--audit-summary','完整范围已独立查证')\n"
    )
    registry = tmp_path / "isolated-shared-probe-registry.json"
    config_paths = {}

    def configure(*, pre_run_health, scenario="complete"):
        harnesses = {}
        for harness_id, label in (("codex", "alpha"), ("cursor", "beta")):
            config = tmp_path / (label + ".json")
            command = [sys.executable, str(reviewer_script), label, str(run_directory), scenario]
            config.write_text(json.dumps({"enabled": True, "label": "alternative-reviewer:" + label,
                "command": command, "health_command": [*command, "--probe"],
                "pre_run_health": pre_run_health, "allow_repo_cwd": True,
                "health_probe_parity_exemption": "inline_script_fixture_has_no_shared_cli_entrypoint"}))
            config_paths[harness_id] = config
            harnesses[harness_id] = {"enabled": True, "config_path": str(config),
                "dispatch_mode": "external_cli", "label_prefix": "alternative-reviewer:" + label}
        registry.write_text(json.dumps({"harnesses": harnesses}))

    command = [sys.executable, str(SCRIPTS / "dispatch_reviewers.py"), "--execute", "--registry", str(registry),
        "--repo", str(valid_boundary["repo"]), "--review-packet", prepared["review_packet"],
        "--scope-contract", prepared["scope_contract"], "--session-context", prepared["scope_of_work_file"],
        "--rvf-run-id", prepared["run_id"], "--rvf-run-dir", prepared["run_dir"]]

    def dispatch():
        completed = subprocess.run(command, capture_output=True, text=True, timeout=35)
        plan = json.loads((run_directory / "artifacts/reviewers/reviewer-plan.json").read_text())
        return completed, plan

    return {"configure": configure, "dispatch": dispatch, "run": run_directory,
            "repo": valid_boundary["repo"], "config_paths": config_paths,
            "cache_path": resolved_probe_result_cache_entry_path}


def test_existing_plan_waits_for_shared_probe_without_executing_unverified_leg(shared_probe_wait_dispatch):
    fixture = shared_probe_wait_dispatch
    fixture["configure"](pre_run_health=False, scenario="request-once")
    completed, initial_plan = fixture["dispatch"]()
    assert completed.returncode == 3, completed.stderr
    run = fixture["run"]
    (run / "required-evidence.txt").write_text("恢复原 reviewer 所需的隔离证据。")
    beta_result = run / "artifacts/reviewers/beta/review-result.json"
    beta_before = beta_result.read_bytes()
    beta_cache_before = fixture["cache_path"]("cursor").read_bytes()
    cache_path = fixture["cache_path"]("codex")
    cache_path.unlink()
    with cache_path.with_suffix(".probe-execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for _ in range(2):
            waiting, plan = fixture["dispatch"]()
            assert waiting.returncode == 5, waiting.stderr
            assert plan["status"] == "waiting_for_reviewer_probe_evidence"
            assert plan["probe_evidence_pending_reviewer_ids"] == ["alpha"]
            alpha = next(reviewer for reviewer in plan["reviewers"] if reviewer["reviewer_id"] == "alpha")
            assert alpha["probe_evidence_wait"]["reason_code"] == "reviewer_probe_in_flight"
            assert alpha["probe_evidence_wait"]["recovery_condition"] == "reviewer_probe_execution_lock_available"
            assert plan["fallbacks"] == [] and plan["effective_reviewer_count"] == 1
            assert (run / "alpha-execution-count.txt").read_text().splitlines() == ["execution"]
            assert (run / "alpha-health-count.txt").read_text().splitlines() == ["probe"]
            assert beta_result.read_bytes() == beta_before
            assert fixture["cache_path"]("cursor").read_bytes() == beta_cache_before
            assert not cache_path.exists()
    resumed, plan = fixture["dispatch"]()
    assert resumed.returncode == 0, resumed.stderr
    assert plan["status"] == "completed"
    assert [reviewer["reviewer_id"] for reviewer in plan["reviewers"]] == [reviewer["reviewer_id"] for reviewer in initial_plan["reviewers"]]
    assert (run / "alpha-execution-count.txt").read_text().splitlines() == ["execution", "execution"]
    assert (run / "alpha-health-count.txt").read_text().splitlines() == ["probe", "probe"]
    assert beta_result.read_bytes() == beta_before
    duplicate, _ = fixture["dispatch"]()
    assert duplicate.returncode == 0, duplicate.stderr
    assert len((run / "beta-execution-count.txt").read_text().splitlines()) == 1
    assert len((run / "alpha-execution-count.txt").read_text().splitlines()) == 2


def test_pre_run_probe_wait_preserves_same_leg_cache_and_completed_peer(shared_probe_wait_dispatch):
    fixture = shared_probe_wait_dispatch
    fixture["configure"](pre_run_health=True)
    run = fixture["run"]
    seeded = subprocess.run([sys.executable, str(SCRIPTS / "run_alternative_reviewer.py"),
        "--config", str(fixture["config_paths"]["cursor"]), "--repo", str(fixture["repo"]),
        "--reviewer-harness-id", "cursor", "--preflight"], capture_output=True, text=True)
    assert seeded.returncode == 0, seeded.stderr
    cache_path = fixture["cache_path"]("cursor")
    cache_before = cache_path.read_bytes()
    with cache_path.with_suffix(".probe-execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        waiting, plan = fixture["dispatch"]()
        assert waiting.returncode == 5, waiting.stderr
        assert plan["status"] == "waiting_for_reviewer_probe_evidence"
        assert plan["probe_evidence_pending_reviewer_ids"] == ["beta"]
        beta = next(reviewer for reviewer in plan["reviewers"] if reviewer["reviewer_id"] == "beta")
        attempt_id = beta["execution_attempt_id"]
        assert beta["probe_evidence_wait"]["reason_code"] == "reviewer_probe_in_flight"
        assert cache_path.read_bytes() == cache_before
        assert plan["fallbacks"] == [] and plan["cooldown_recorded"] == []
        alpha_result = run / "artifacts/reviewers/alpha/review-result.json"
        alpha_before = alpha_result.read_bytes()
        duplicate, plan = fixture["dispatch"]()
        assert duplicate.returncode == 5, duplicate.stderr
        beta = next(reviewer for reviewer in plan["reviewers"] if reviewer["reviewer_id"] == "beta")
        assert beta["execution_attempt_id"] == attempt_id
        assert not beta.get("recovery", {}).get("attempts_by_restart_condition")
        assert plan["effective_reviewer_count"] == 1
        assert not (run / "beta-execution-count.txt").exists()
        assert alpha_result.read_bytes() == alpha_before
        assert cache_path.read_bytes() == cache_before
    resumed, plan = fixture["dispatch"]()
    assert resumed.returncode == 0, resumed.stderr
    assert plan["status"] == "completed" and plan["probe_evidence_pending_reviewer_ids"] == []
    assert {reviewer["reviewer_id"] for reviewer in plan["reviewers"]} == {"alpha", "beta"}
    assert alpha_result.read_bytes() == alpha_before
    assert cache_path.read_bytes() == cache_before
    assert (run / "beta-health-count.txt").read_text().splitlines() == ["probe"]
    duplicate, _ = fixture["dispatch"]()
    assert duplicate.returncode == 0, duplicate.stderr
    for label in ("alpha", "beta"):
        assert (run / (label + "-execution-count.txt")).read_text().splitlines() == ["execution"]


def test_pre_run_probe_wait_does_not_consume_request_recovery_attempt(shared_probe_wait_dispatch):
    fixture = shared_probe_wait_dispatch
    fixture["configure"](pre_run_health=True, scenario="request-once")
    first, _ = fixture["dispatch"]()
    assert first.returncode == 3, first.stderr
    run = fixture["run"]
    (run / "required-evidence.txt").write_text("恢复 request 腿所需证据。")
    with fixture["cache_path"]("codex").with_suffix(".probe-execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        waiting, plan = fixture["dispatch"]()
        assert waiting.returncode == 5, waiting.stderr
        alpha = next(reviewer for reviewer in plan["reviewers"] if reviewer["reviewer_id"] == "alpha")
        assert not alpha["recovery"]["attempts_by_condition"]
        assert (run / "alpha-execution-count.txt").read_text().splitlines() == ["execution"]
    resumed, plan = fixture["dispatch"]()
    assert resumed.returncode == 0, resumed.stderr
    assert plan["status"] == "completed"
    assert (run / "alpha-execution-count.txt").read_text().splitlines() == ["execution", "execution"]
    assert (run / "beta-execution-count.txt").read_text().splitlines() == ["execution"]


@pytest.mark.parametrize("retry_completes", [False, True], ids=["retry-still-exceeds-input-limit", "retry-completes"])
def test_deferred_input_retry_survives_repeated_probe_wait_without_extra_retry(shared_probe_wait_dispatch, retry_completes):
    fixture = shared_probe_wait_dispatch
    fixture["configure"](pre_run_health=True,
        scenario="context-limit-probe-wait-completes" if retry_completes else "context-limit-probe-wait-permanent")
    run = fixture["run"]
    lock_path = fixture["cache_path"]("codex").with_suffix(".probe-execution.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path = run / "artifacts/reviewers/reviewer-plan.json"

    def hold_probe_lock_after_condition(condition, *, signal_native_reviewer=False):
        release = threading.Event()
        acquired = threading.Event()

        def hold_lock():
            deadline = time.monotonic() + 15
            while not condition():
                if time.monotonic() > deadline:
                    return
                time.sleep(.001)
            with lock_path.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if signal_native_reviewer:
                    (run / "input-retry-probe-lock-acquired.txt").write_text("真实共享探针锁已持有。")
                acquired.set()
                release.wait(timeout=25)

        thread = threading.Thread(target=hold_lock)
        thread.start()
        return thread, acquired, release

    first_holder, first_acquired, first_release = hold_probe_lock_after_condition(
        lambda: (run / "first-input-failure-ready.txt").exists(), signal_native_reviewer=True)
    try:
        first, first_plan = fixture["dispatch"]()
        assert first_acquired.is_set()
        assert first.returncode == 5, first.stderr
        alpha = next(reviewer for reviewer in first_plan["reviewers"] if reviewer["reviewer_id"] == "alpha")
        condition = alpha["recovery"]["input_failure_condition"]
        assert not alpha["recovery"]["attempts_by_failure_condition"]
        assert alpha["recovery"]["deferred_input_failure_retry_condition"] == condition
        first_wait_attempt_id = alpha["execution_attempt_id"]
        peer_result = run / "artifacts/reviewers/beta/review-result.json"
        peer_result_before = peer_result.read_bytes()
        duplicate, duplicate_plan = fixture["dispatch"]()
        assert duplicate.returncode == 5, duplicate.stderr
        alpha = next(reviewer for reviewer in duplicate_plan["reviewers"] if reviewer["reviewer_id"] == "alpha")
        assert alpha["execution_attempt_id"] == first_wait_attempt_id
        assert not alpha["recovery"]["attempts_by_failure_condition"]
    finally:
        first_release.set()
        first_holder.join(timeout=16)

    def resumed_attempt_is_running():
        if not plan_path.exists():
            return False
        plan = json.loads(plan_path.read_text())
        return any(reviewer["reviewer_id"] == "alpha" and reviewer.get("execution_state") == "running"
                   and reviewer.get("execution_attempt_id") != first_wait_attempt_id for reviewer in plan["reviewers"])

    second_holder, second_acquired, second_release = hold_probe_lock_after_condition(resumed_attempt_is_running)
    try:
        second_wait, second_plan = fixture["dispatch"]()
        assert second_acquired.is_set()
        assert second_wait.returncode == 5, second_wait.stderr
        alpha = next(reviewer for reviewer in second_plan["reviewers"] if reviewer["reviewer_id"] == "alpha")
        assert alpha["execution_attempt_id"] != first_wait_attempt_id
        assert not alpha["recovery"]["attempts_by_failure_condition"]
        assert alpha["recovery"]["input_failure_condition"] == condition
        assert alpha["recovery"]["deferred_input_failure_retry_condition"] == condition
        assert (run / "alpha-execution-count.txt").read_text().splitlines() == ["execution"]
        assert peer_result.read_bytes() == peer_result_before
    finally:
        second_release.set()
        second_holder.join(timeout=16)

    resumed, resumed_plan = fixture["dispatch"]()
    alpha = next(reviewer for reviewer in resumed_plan["reviewers"] if reviewer["reviewer_id"] == "alpha")
    assert (run / "alpha-execution-count.txt").read_text().splitlines() == ["execution", "execution"]
    assert alpha["recovery"]["attempts_by_failure_condition"][condition] == 1
    assert not alpha["recovery"].get("deferred_input_failure_retry_condition")
    assert resumed_plan["effective_probe_identities"]["codex"] == first_plan["effective_probe_identities"]["codex"]
    presentations = [json.loads(line)["prompt_presentation"] for line in (run / "alpha-native-prompt-presentations.jsonl").read_text().splitlines()]
    assert presentations == ["inline", "file_references"]
    assert (resumed.returncode == 0) == retry_completes, resumed.stderr
    assert resumed_plan["status"] == ("completed" if retry_completes else "failed")
    duplicate, _ = fixture["dispatch"]()
    assert (duplicate.returncode == 0) == retry_completes, duplicate.stderr
    assert (run / "alpha-execution-count.txt").read_text().splitlines() == ["execution", "execution"]
    assert (run / "beta-execution-count.txt").read_text().splitlines() == ["execution"]
    assert peer_result.read_bytes() == peer_result_before
    (run / "deferred-input-retry-budget-verification.json").write_text(json.dumps({
        "retry_completes": retry_completes, "pre_run_probe_wait_count": 2,
        "unexecuted_retry_count_after_each_wait": 0, "native_alpha_execution_count": 2,
        "native_alpha_file_reference_retry_count": 1, "native_beta_execution_count": 1,
        "consumed_retry_count": alpha["recovery"]["attempts_by_failure_condition"][condition],
        "probe_identity_unchanged": True, "input_failure_condition": condition,
    }, ensure_ascii=False, indent=2))
