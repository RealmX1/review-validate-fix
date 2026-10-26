"""终态请求接受点、索引双写窗口及同代预算前置检查。"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from _rvf_test_support.loader import load_script_module


TERMINAL_ARCHIVE_INDEX_NATIVE_WORKER = r'''
import json, os, sys
from pathlib import Path
scripts, root, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(scripts))
import review_run_terminal_archive as archive
import rvf_run_finalize as finalizer
configuration=json.loads((root/'native-index-worker-configuration.json').read_text())
run, repo=Path(configuration['run_dir']), Path(configuration['repo'])
session=configuration['session_id']
def side_effect(kind):
    with (root/'native-finalize-side-effects.jsonl').open('a') as output:
        output.write(json.dumps({'kind':kind,'pid':os.getpid()})+'\n')
    return {}
finalizer.capture_run=lambda **kwargs:side_effect('capture')
finalizer._release_tracker_lease=lambda *args,**kwargs:side_effect('release')
finalizer._write_usage_summary=lambda *args:{}
finalizer._scaffold_analysis=lambda *args:{}
original_write=archive.atomic_write_json
def interrupted_registration_write(path,payload):
    is_index=Path(path).parent==archive._pending_archive_directory(repo,session) and payload.get('request_path')==str(run/'artifacts'/archive.ARCHIVE_REQUEST_FILENAME)
    if mode=='fail_first_index' and is_index: raise OSError('隔离首次索引写入失败')
    if mode=='fail_first_canonical' and Path(path).name==archive.ARCHIVE_REQUEST_FILENAME: raise OSError('隔离首次canonical投影写入失败')
    original_write(path,payload)
    if mode=='fail_after_index_commit' and is_index: raise OSError('隔离索引已提交但调用仍报错')
    if mode=='crash_after_canonical' and Path(path).name==archive.ARCHIVE_REQUEST_FILENAME: os._exit(73)
    if mode=='crash_after_index' and is_index: os._exit(74)
archive.atomic_write_json=interrupted_registration_write
if mode in ('resume','resume_forbid_finalize'):
    if mode=='resume_forbid_finalize':
        def forbidden_finalize(**kwargs): raise AssertionError('正式恢复不得重复执行已完成finalize')
        archive.finalize_run=forbidden_finalize
    outcome=archive.resume_pending_review_run_terminal_archives(repo=repo,session_id=session)
else:
    try:
        outcome=archive.request_review_run_terminal_archive(run_dir=run,
            decision_kind='cancelled' if mode=='register_later_cancel' else 'interrupted',
            reason='后到取消不得替换首个接受身份' if mode=='register_later_cancel' else '隔离验证索引接受点',
            event={'session_id':session,'suppress_review_validate_fix':True},defer_resume=True)
    except OSError as registration_error:
        (root/'native-registration-error.json').write_text(json.dumps({'error':str(registration_error)}))
        raise SystemExit(75)
(root/(mode+'-result.json')).write_text(json.dumps(outcome,ensure_ascii=False,indent=2))
'''


@pytest.fixture
def terminal_archive_index_recovery_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "isolated-user-local"))
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "isolated-rvf-home"))
    archive = load_script_module("review_run_terminal_archive")
    run = tmp_path / "rvf-review-start-0270270270270270"
    repo = tmp_path / "isolated-repository"
    repo.mkdir()
    identity = {"run_id": run.name, "run_dir": str(run), "repo": str(repo), "session_id": "rvf027-session"}
    for path in (run / "summary.json", run / "artifacts/inputs/scope.contract.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(identity))
    configuration = {"run_dir": str(run), "repo": str(repo), "session_id": identity["session_id"]}
    (tmp_path / "native-index-worker-configuration.json").write_text(json.dumps(configuration))
    worker = tmp_path / "terminal-archive-index-native-worker.py"
    worker.write_text(TERMINAL_ARCHIVE_INDEX_NATIVE_WORKER)
    receipts = []
    def execute_native_worker(mode, expected=0):
        argv = [sys.executable, "-B", str(worker), str(Path(archive.__file__).parent), str(tmp_path), mode]
        completed = subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=15)
        receipts.append({"argv": argv, "cwd": str(repo), "exit_code": completed.returncode,
            "stdout": completed.stdout, "stderr": completed.stderr})
        (tmp_path / "native-process-command-receipts.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2))
        assert completed.returncode == expected, receipts[-1]
        result_path = tmp_path / (mode + "-result.json")
        return json.loads(result_path.read_text()) if result_path.exists() else None
    return {"root": tmp_path, "run": run, "repo": repo, "archive": archive, "execute": execute_native_worker,
        "request": run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME,
        "index_directory": archive._pending_archive_directory(repo, identity["session_id"])}


def test_accepted_canonical_request_is_discovered_after_actual_process_exit(terminal_archive_index_recovery_fixture):
    fixture = terminal_archive_index_recovery_fixture
    fixture["execute"]("crash_after_canonical", expected=73)
    assert fixture["request"].exists()
    recovered = fixture["execute"]("resume")
    (fixture["root"] / "native-canonical-before-index-observation.json").write_text(json.dumps({
        "canonical_request_exists": True, "formal_resume_outcome": recovered}, ensure_ascii=False, indent=2))
    assert recovered and recovered[0]["status"] == "archived"
    fixture["execute"]("resume_forbid_finalize")
    effects = [json.loads(line)["kind"] for line in (fixture["root"] / "native-finalize-side-effects.jsonl").read_text().splitlines()]
    assert effects == ["capture", "release"]


def test_accepted_index_before_canonical_recovers_projection_without_reregistration(terminal_archive_index_recovery_fixture):
    fixture = terminal_archive_index_recovery_fixture
    fixture["execute"]("crash_after_index", expected=74)
    assert not fixture["request"].exists()
    evidence = load_script_module("dispatcher_writer_execution_evidence")
    intent = evidence.read_bound_terminal_archive_intent_prohibiting_dispatcher_launch(
        fixture["run"] / "artifacts/reviewers/.dispatch-thread.status.json",
        {"kind": "dispatch", "run_id": fixture["run"].name, "run_dir": str(fixture["run"]), "repo": str(fixture["repo"]), "session_id": "rvf027-session"})
    assert intent and intent["run_id"] == fixture["run"].name
    assert not fixture["request"].exists()
    recovered = fixture["execute"]("resume")
    assert recovered and recovered[0]["status"] == "archived"
    assert fixture["request"].exists()
    fixture["execute"]("resume_forbid_finalize")
    effects = [json.loads(line)["kind"] for line in (fixture["root"] / "native-finalize-side-effects.jsonl").read_text().splitlines()]
    assert effects == ["capture", "release"]


def test_first_index_failure_cannot_leave_an_accepted_unindexed_intent(terminal_archive_index_recovery_fixture):
    fixture = terminal_archive_index_recovery_fixture
    fixture["execute"]("fail_first_index", expected=75)
    assert not fixture["request"].exists()
    assert fixture["execute"]("resume") == []
    assert "首次索引写入失败" in json.loads((fixture["root"] / "native-registration-error.json").read_text())["error"]


@pytest.mark.parametrize("write_failure", ["fail_first_canonical", "fail_after_index_commit"])
def test_first_canonical_projection_failure_is_recovered_from_accepted_index(terminal_archive_index_recovery_fixture, write_failure):
    fixture = terminal_archive_index_recovery_fixture
    fixture["execute"](write_failure, expected=75)
    assert not fixture["request"].exists()
    assert list(fixture["index_directory"].glob("*.json"))
    recovered = fixture["execute"]("resume")
    assert recovered and recovered[0]["status"] == "archived"
    fixture["execute"]("resume_forbid_finalize")
    effects = [json.loads(line)["kind"] for line in (fixture["root"] / "native-finalize-side-effects.jsonl").read_text().splitlines()]
    assert effects == ["capture", "release"]


def test_registered_snapshot_preserves_later_canonical_facts(terminal_archive_index_recovery_fixture):
    fixture = terminal_archive_index_recovery_fixture
    fixture["execute"]("register")
    request = json.loads(fixture["request"].read_text())
    request["successor_review_runs"] = [{"run_id": "exact-successor-fact"}]
    request["independently_persisted_fact"] = "登记后另一拥有者追加的事实"
    fixture["request"].write_text(json.dumps(request))
    recovered = fixture["execute"]("resume")
    assert recovered and recovered[0]["status"] == "archived"
    final_request = json.loads(fixture["request"].read_text())
    assert final_request["successor_review_runs"] == [{"run_id": "exact-successor-fact"}]
    assert final_request["independently_persisted_fact"] == "登记后另一拥有者追加的事实"


def test_accepted_policy_strengthening_survives_projection_interruption(terminal_archive_index_recovery_fixture):
    fixture = terminal_archive_index_recovery_fixture
    fixture["archive"].request_review_run_terminal_archive(run_dir=fixture["run"], decision_kind="interrupted",
        reason="先前已接受请求", event={"session_id": "rvf027-session"}, defer_resume=True)
    assert not json.loads(fixture["request"].read_text())["event"].get("suppress_review_validate_fix")
    fixture["execute"]("crash_after_index", expected=74)
    assert not json.loads(fixture["request"].read_text())["event"].get("suppress_review_validate_fix")
    recovered = fixture["execute"]("resume")
    assert recovered and recovered[0]["status"] == "archived"
    assert json.loads(fixture["request"].read_text())["event"]["suppress_review_validate_fix"] is True


def test_request_api_reentry_preserves_first_accepted_index_identity(terminal_archive_index_recovery_fixture):
    fixture = terminal_archive_index_recovery_fixture
    fixture["execute"]("crash_after_index", expected=74)
    index_path = next(fixture["index_directory"].glob("*.json"))
    first = json.loads(index_path.read_text())["accepted_request_snapshot"]
    assert not fixture["request"].exists()
    repeated = fixture["execute"]("register_later_cancel")
    for field in fixture["archive"].ARCHIVE_REQUEST_IDENTITY_FIELDS:
        assert repeated.get(field) == first.get(field)
    assert json.loads(fixture["request"].read_text())["decision_kind"] == "interrupted"
    assert fixture["execute"]("resume")[0]["status"] == "archived"


@pytest.mark.parametrize("missing_evidence", ["run_id", "snapshot_digest", "scope_contract", "index_symlink", "request_symlink"])
def test_accepted_registration_keeps_identity_scope_and_symlink_gates(terminal_archive_index_recovery_fixture, missing_evidence):
    fixture = terminal_archive_index_recovery_fixture
    fixture["execute"]("crash_after_index", expected=74)
    index_path = next(fixture["index_directory"].glob("*.json"))
    index_bytes = index_path.read_bytes()
    contract_path = fixture["run"] / "artifacts/inputs/scope.contract.json"
    contract_bytes = contract_path.read_bytes()
    target = fixture["root"] / "foreign-registration-target.json"
    if missing_evidence == "index_symlink":
        index_path.rename(target)
        index_path.symlink_to(target)
    elif missing_evidence == "request_symlink":
        target.write_text('{"unrelated":true}')
        fixture["request"].symlink_to(target)
    elif missing_evidence == "scope_contract":
        contract_path.write_text('{"run_id":"foreign-run"}')
    else:
        index = json.loads(index_bytes)
        index["run_id" if missing_evidence == "run_id" else "accepted_request_snapshot_sha256"] = "foreign"
        index_path.write_text(json.dumps(index))
    waiting = fixture["execute"]("resume")
    assert waiting and waiting[0]["status"] == "waiting_for_evidence"
    assert not (fixture["root"] / "native-finalize-side-effects.jsonl").exists()
    if missing_evidence == "request_symlink":
        assert fixture["request"].is_symlink() and target.read_text() == '{"unrelated":true}'
        fixture["request"].unlink()
    if index_path.is_symlink():
        index_path.unlink()
    index_path.write_bytes(index_bytes)
    contract_path.write_bytes(contract_bytes)
    recovered = fixture["execute"]("resume")
    assert recovered and recovered[0]["status"] == "archived"


def test_known_exhausted_generation_stops_before_namespace_query(terminal_archive_index_recovery_fixture, monkeypatch):
    fixture = terminal_archive_index_recovery_fixture
    evidence = load_script_module("dispatcher_writer_execution_evidence")
    detached = load_script_module("rvf_detached_thread")
    script = fixture["root"] / "missing-dispatcher-entrypoint.py"
    status = fixture["run"] / "artifacts/reviewers/.dispatch-thread.status.json"
    payload = {"kind": "dispatch", "run_id": fixture["run"].name, "run_dir": str(fixture["run"]), "repo": str(fixture["repo"]),
        "session_id": "rvf027-session", "tmux_session": "rvf027-private-dispatch", "command": [sys.executable, str(script)], "returncode": None}
    parameters = {"session_name": payload["tmux_session"], "argv": payload["command"], "log_path": status.with_name("dispatcher.log"),
        "status_path": status, "lock_path": status.with_name(".dispatch-thread.lock"), "status_payload": payload,
        "total_timeout_seconds": 10, "require_actual_dispatcher_writer_execution_evidence": True}
    query_count = []
    def absent_namespace(*args):
        query_count.append(1)
        return {"status": "absent"}
    monkeypatch.setattr(detached, "probe_dispatcher_tmux_session_before_new_execution", absent_namespace)
    for _ in range(3):
        assert detached.launch_detached(**parameters)["launch_status"] == "launch_failed"
    def query_must_not_run(*args):
        raise AssertionError("已耗尽同代预算不应再查询namespace")
    monkeypatch.setattr(detached, "probe_dispatcher_tmux_session_before_new_execution", query_must_not_run)
    exhausted = detached.launch_detached(**parameters)
    assert exhausted.get("recovery_state") == "waiting_for_dispatcher_prelaunch_input_change"
    receipts = list((status.parent / "dispatcher-writer-executions").glob("*/dispatcher-writer-execution-evidence.json"))
    assert len(receipts) == len(query_count) == 3
    assert not evidence.observe_dispatcher_writer_execution_delivery_or_boot_exit_evidence(status)["dispatcher_writers_may_still_write"]
