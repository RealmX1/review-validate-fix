"""原始输入恢复、正式输出重验与精确 run 完成计数均可隔离恢复。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from _rvf_test_support.loader import load_script_module


@pytest.fixture
def analysis_recovery_fixture(tmp_path, monkeypatch):
    wrapper = load_script_module("rvf_analyze_formal_archive_verified_execution")
    guardian = load_script_module("agentic_initiation_guardian_dispatch")
    guardian_root = tmp_path / "isolated-retired-guardian-root"
    guardian_root.mkdir()
    counter_path = guardian_root / guardian.ANALYZE_COMPLETION_COUNTER_FILENAME
    counter_path.write_text(json.dumps({
        "analyze_completion_count": 0, "guardian_retired": True,
        "dispatched_milestones": [1, 2, 4, 8, 16],
    }))
    for name, path in {
        "HOME": tmp_path / "isolated-user-home",
        "CODEX_HOME": tmp_path / "isolated-codex-home",
        "RVF_USER_LOCAL_DATA_ROOT": tmp_path / "isolated-user-local-root",
        "RVF_AGENTIC_INITIATION_GUARDIAN_ROOT": guardian_root,
    }.items():
        monkeypatch.setenv(name, str(path))
    run_directory = tmp_path / "isolated-review-run"
    analysis_directory = run_directory / "artifacts/analysis"
    analysis_directory.mkdir(parents=True)
    (analysis_directory / "summary.md").write_text("TODO(rvf-analyze): 自产待完成分析")
    (analysis_directory / "causality.json").write_text(json.dumps({
        "schema_version": 2, "run_id": run_directory.name, "issues": [], "patches": [],
    }))
    (analysis_directory / ".analyze-thread.prompt.md").write_text("自产冻结分析指令")
    status_path = analysis_directory / ".analyze-thread.status.json"
    status_path.write_text(json.dumps({"run_dir": str(run_directory), "automatic_analysis_attempt_limit": 3}))
    original_input = run_directory / "artifacts/trajectory/rvf/trajectory.jsonl"
    original_input.parent.mkdir(parents=True)
    original_input.write_text('{"synthetic_input":true}\n')
    process_count_path = tmp_path / "synthetic-analysis-process-count.txt"
    return {
        "wrapper": wrapper, "guardian": guardian, "guardian_root": guardian_root,
        "counter_path": counter_path, "run_directory": run_directory,
        "analysis_directory": analysis_directory, "status_path": status_path,
        "original_input": original_input, "process_count_path": process_count_path,
        "agent_path": tmp_path / "synthetic-analysis-agent.py", "monkeypatch": monkeypatch,
    }


def write_synthetic_recovery_agent(fixture, *, exit_code=0, unreadable_output=None):
    fixture["agent_path"].write_text(
        "from pathlib import Path\nimport sys\n"
        "assert sys.stdin.read() == '自产冻结分析指令'\n"
        f"counter=Path({str(fixture['process_count_path'])!r})\n"
        "counter.write_text(str(int(counter.read_text())+1 if counter.exists() else 1))\n"
        f"original=Path({str(fixture['original_input'])!r})\n"
        "try: original.read_text()\n"
        "except PermissionError: raise SystemExit(7)\n"
        + (f"raise SystemExit({exit_code})\n" if exit_code else (
            f"summary=Path({str(fixture['analysis_directory'] / 'summary.md')!r})\n"
            "summary.chmod(0o600)\nsummary.write_text('已完成的自产分析输出。')\n"
            + (f"Path({str(fixture['analysis_directory'] / unreadable_output)!r}).chmod(0)\n"
               if unreadable_output else "")
        ))
    )


def execute_synthetic_analysis(fixture):
    fixture["monkeypatch"].setattr(sys, "stdin", io.StringIO("自产冻结分析指令"))
    return fixture["wrapper"].execute_and_verify_analysis(
        fixture["run_directory"], [sys.executable, str(fixture["agent_path"])],
    )


def read_analysis_attempt_budget(fixture):
    return json.loads((fixture["run_directory"] / "artifacts"
                       / fixture["wrapper"].ANALYSIS_ATTEMPT_BUDGET_FILENAME).read_text())


@pytest.mark.parametrize("recovery_succeeds", [True, False])
def test_original_input_readability_recovery_is_automatic_and_bounded(analysis_recovery_fixture, recovery_succeeds):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture, exit_code=0 if recovery_succeeds else 7)
    fixture["original_input"].chmod(0)
    try:
        assert execute_synthetic_analysis(fixture) == 7
        original_budget = read_analysis_attempt_budget(fixture)
    finally:
        fixture["original_input"].chmod(0o600)
    assert execute_synthetic_analysis(fixture) == (0 if recovery_succeeds else 7)
    expected_process_count = "4" if recovery_succeeds else "6"
    assert fixture["process_count_path"].read_text() == expected_process_count
    recovered_budget = read_analysis_attempt_budget(fixture)
    assert recovered_budget["attempt_history"][:3] == original_budget["attempt_history"]
    assert recovered_budget["attempts_started"] == (1 if recovery_succeeds else 3)
    assert execute_synthetic_analysis(fixture) == (0 if recovery_succeeds else 1)
    assert fixture["process_count_path"].read_text() == expected_process_count
    assert read_analysis_attempt_budget(fixture) == recovered_budget


@pytest.mark.parametrize("output_filename", ["summary.md", "causality.json"])
def test_formal_output_readability_recovery_revalidates_without_new_agent(analysis_recovery_fixture, output_filename):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture, unreadable_output=output_filename)
    assert execute_synthetic_analysis(fixture) == 1
    exhausted_budget = read_analysis_attempt_budget(fixture)
    (fixture["analysis_directory"] / output_filename).chmod(0o600)
    assert execute_synthetic_analysis(fixture) == 0
    assert fixture["process_count_path"].read_text() == "1"
    assert read_analysis_attempt_budget(fixture) == exhausted_budget
    assert json.loads(fixture["status_path"].read_text())["formal_analysis_completion_status"] == "completed"
    assert execute_synthetic_analysis(fixture) == 0
    assert fixture["process_count_path"].read_text() == "1"
    assert json.loads(fixture["counter_path"].read_text())["analyze_completion_count"] == 1


@pytest.mark.parametrize("output_filename", ["summary.md", "causality.json"])
def test_model_output_content_changes_never_renew_a_failed_agent_budget(analysis_recovery_fixture, output_filename):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture, exit_code=7)
    assert execute_synthetic_analysis(fixture) == 7
    exhausted_budget = read_analysis_attempt_budget(fixture)
    output_path = fixture["analysis_directory"] / output_filename
    output_path.write_text("已补写的分析文本。" if output_filename == "summary.md" else output_path.read_text() + "\n")
    assert execute_synthetic_analysis(fixture) == 1
    assert fixture["process_count_path"].read_text() == "3"
    assert read_analysis_attempt_budget(fixture) == exhausted_budget


@pytest.mark.parametrize("frozen_input_filename", [
    "analysis-causality-before-first-agent-execution.json", "analysis-frozen-prompt-before-first-agent-execution.md",
])
def test_frozen_input_readability_is_observed_without_content_based_budget_refresh(
    analysis_recovery_fixture, frozen_input_filename,
):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture, exit_code=7)
    execute_synthetic_analysis(fixture)
    evidence = fixture["wrapper"].automatic_analysis_evidence_generation
    argv = [sys.executable, str(fixture["agent_path"])]
    before = evidence(fixture["run_directory"], argv)
    frozen_input = fixture["run_directory"] / "artifacts" / frozen_input_filename
    frozen_input.chmod(0)
    try:
        assert evidence(fixture["run_directory"], argv) != before
    finally:
        frozen_input.chmod(0o600)
    assert evidence(fixture["run_directory"], argv) == before


def test_failed_completion_count_retries_after_verified_replay_without_new_agent(analysis_recovery_fixture):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture)
    guardian = fixture["guardian"]
    original_write = guardian._atomic_write_json

    def reject_counter_write(*_args, **_kwargs):
        raise PermissionError("自产的暂时计数写入失败")

    fixture["monkeypatch"].setattr(guardian, "_atomic_write_json", reject_counter_write)
    assert execute_synthetic_analysis(fixture) == 0
    failed_status = json.loads(fixture["status_path"].read_text())
    assert failed_status["formal_analysis_completion_status"] == "completed"
    assert failed_status["formal_analysis_completion_counter_status"] == "pending"
    assert failed_status["formal_analysis_completion_counter_failure_history"]
    assert failed_status["formal_analysis_completion_counter_recovery_condition"]
    fixture["monkeypatch"].setattr(guardian, "_atomic_write_json", original_write)
    assert execute_synthetic_analysis(fixture) == 0
    assert execute_synthetic_analysis(fixture) == 0
    recovered_status = json.loads(fixture["status_path"].read_text())
    assert recovered_status["formal_analysis_completion_counter_status"] == "recorded"
    assert recovered_status["formal_analysis_completion_counter_failure_history"] == failed_status["formal_analysis_completion_counter_failure_history"]
    assert fixture["process_count_path"].read_text() == "1"
    assert json.loads(fixture["counter_path"].read_text())["analyze_completion_count"] == 1


def test_exact_run_count_claim_is_atomic_across_concurrent_retries(analysis_recovery_fixture):
    fixture = analysis_recovery_fixture
    guardian = fixture["guardian"]
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: guardian.record_analyze_completion(
            run_dir=str(fixture["run_directory"]), root=fixture["guardian_root"],
        ), range(24)))
    assert sum(result["status"] == "recorded" for result in results) == 1
    assert all(result["status"] in {"recorded", "already_recorded"} for result in results)
    counter = json.loads(fixture["counter_path"].read_text())
    assert counter["analyze_completion_count"] == 1
    assert len(counter["counted_analysis_run_directories"]) == 1
    distinct_run = fixture["run_directory"].parent / "other-parent" / fixture["run_directory"].name
    assert guardian.record_analyze_completion(run_dir=str(distinct_run), root=fixture["guardian_root"])["analyze_completion_count"] == 2


def test_completion_counter_requires_a_real_lock_and_preserves_retry_eligibility(analysis_recovery_fixture):
    fixture = analysis_recovery_fixture
    guardian = fixture["guardian"]
    original_flock = guardian.fcntl.flock
    before = fixture["counter_path"].read_bytes()

    def reject_lock(*_args):
        raise OSError("自产的内核锁暂不可用")

    fixture["monkeypatch"].setattr(guardian.fcntl, "flock", reject_lock)
    failed = guardian.record_analyze_completion(run_dir=str(fixture["run_directory"]), root=fixture["guardian_root"])
    assert failed["status"] == "guardian_record_failed"
    assert fixture["counter_path"].read_bytes() == before
    fixture["monkeypatch"].setattr(guardian.fcntl, "flock", original_flock)
    assert guardian.record_analyze_completion(run_dir=str(fixture["run_directory"]), root=fixture["guardian_root"])["analyze_completion_count"] == 1


def test_malformed_completion_counter_is_preserved_for_recovery(analysis_recovery_fixture):
    fixture = analysis_recovery_fixture
    fixture["counter_path"].write_text("{暂时损坏的计数记录")
    before = fixture["counter_path"].read_bytes()
    result = fixture["guardian"].record_analyze_completion(
        run_dir=str(fixture["run_directory"]), root=fixture["guardian_root"],
    )
    assert result["status"] == "guardian_record_failed"
    assert fixture["counter_path"].read_bytes() == before


def test_counter_commit_survives_native_process_exit_before_acknowledgement(analysis_recovery_fixture):
    fixture = analysis_recovery_fixture
    guardian = fixture["guardian"]
    code = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(Path(guardian.__file__).parent)!r})\n"
        "import agentic_initiation_guardian_dispatch as guardian\n"
        "original_write=guardian._atomic_write_json\n"
        "def commit_then_exit(path,payload):\n"
        "    original_write(path,payload)\n"
        "    os._exit(23)\n"
        "guardian._atomic_write_json=commit_then_exit\n"
        f"guardian.record_analyze_completion(run_dir={str(fixture['run_directory'])!r}, root={str(fixture['guardian_root'])!r})\n"
    )
    interrupted = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
    assert interrupted.returncode == 23
    recovered = guardian.record_analyze_completion(
        run_dir=str(fixture["run_directory"]), root=fixture["guardian_root"],
    )
    assert recovered["status"] == "already_recorded"
    assert recovered["analyze_completion_count"] == 1


def test_local_count_status_write_failure_cannot_duplicate_the_atomic_count(analysis_recovery_fixture):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture)
    original_write = fixture["wrapper"]._atomic_write_json

    def reject_local_count_status(path, payload):
        if "formal_analysis_completion_counter_status" in payload:
            raise PermissionError("自产的本地计数回执暂不可写")
        return original_write(path, payload)

    fixture["monkeypatch"].setattr(fixture["wrapper"], "_atomic_write_json", reject_local_count_status)
    assert execute_synthetic_analysis(fixture) == 0
    fixture["monkeypatch"].setattr(fixture["wrapper"], "_atomic_write_json", original_write)
    assert execute_synthetic_analysis(fixture) == 0
    assert fixture["process_count_path"].read_text() == "1"
    assert json.loads(fixture["counter_path"].read_text())["analyze_completion_count"] == 1


def seed_exhausted_production_analysis_launch_claims(fixture, *, launch_claim_count=3):
    """真实 claim/advisory/索引扫描使用自产仓库身份，只有 host 命令为假入口。"""
    archive = load_script_module("review_run_terminal_archive")
    advisory = load_script_module("rvf_analyze_advisory")
    thread = load_script_module("rvf_analyze_thread")
    repo = fixture["run_directory"].parent / "isolated-business-repository"
    repo.mkdir()
    analysis = {"run_dir": str(fixture["run_directory"]),
                "summary_md_path": str(fixture["analysis_directory"] / "summary.md"),
                "causality_json_path": str(fixture["analysis_directory"] / "causality.json")}
    finalization = {"run_dir": analysis["run_dir"], "analysis": analysis}
    (fixture["run_directory"] / "summary.json").write_text(json.dumps({
        "run_id": fixture["run_directory"].name, "run_dir": analysis["run_dir"], "repo": str(repo),
        "session_id": "synthetic-parent-session", "finalize": finalization,
    }))
    status = json.loads(fixture["status_path"].read_text())
    fixture["wrapper"].freeze_or_load_original_causality(fixture["analysis_directory"], status)
    (fixture["run_directory"] / "artifacts/analysis-frozen-prompt-before-first-agent-execution.md").write_text("自产冻结分析指令")
    build_calls = []

    def build_synthetic_agent_command(*_args, **_kwargs):
        build_calls.append(True)
        return [sys.executable, str(fixture["agent_path"])], "-"

    fixture["monkeypatch"].setattr(thread, "build_analyze_command", build_synthetic_agent_command)
    fixture["monkeypatch"].setattr(thread, "select_host", lambda _event: "codex")
    fixture["monkeypatch"].setattr(advisory, "launch_detached_analyze_thread", lambda **_kwargs: pytest.fail("恢复正式完成或计数时不得启动 Analyze"))
    for variable in ("RVF_SUPPRESS_STOP_HOOK", "RVF_ANALYZE_THREAD"):
        fixture["monkeypatch"].delenv(variable, raising=False)
    event = {"session_id": "synthetic-parent-session", "cwd": str(repo)}
    for _ in range(launch_claim_count):
        latest_launch_claim = archive.claim_review_run_analysis_launch(analysis=analysis, event=event)
        assert latest_launch_claim["status"] == "launch_claimed"
    request_path = fixture["run_directory"] / "artifacts" / archive.ANALYSIS_RECOVERY_FILENAME
    index_directory = archive._pending_archive_directory(repo, event["session_id"])
    return {"archive": archive, "advisory": advisory, "repo": repo, "event": event,
            "analysis": analysis, "finalization": finalization, "request_path": request_path,
            "index_directory": index_directory, "build_calls": build_calls, "latest_launch_claim": latest_launch_claim}


def scan_production_analysis_recovery(fixture, production):
    result = production["archive"].resume_pending_review_run_terminal_archives(
        repo=production["repo"], session_id=production["event"]["session_id"],
    )
    receipt_path = fixture["run_directory"].parent / "production-recovery-scan-observations.json"
    observations = json.loads(receipt_path.read_text()) if receipt_path.exists() else []
    observations.append({"results": result,
        "pending_index_count": len(list(production["index_directory"].glob("analysis-*.json"))),
        "request": json.loads(production["request_path"].read_text()),
        "thread": json.loads(fixture["status_path"].read_text()),
        "completion_count": json.loads(fixture["counter_path"].read_text())["analyze_completion_count"],
        "analysis_process_count": int(fixture["process_count_path"].read_text())})
    receipt_path.write_text(json.dumps(observations, ensure_ascii=False, indent=2))
    return result


def test_pending_scan_retries_completion_count_without_launching_analyze_or_losing_index(
    analysis_recovery_fixture, capsys,
):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture)
    production = seed_exhausted_production_analysis_launch_claims(fixture)
    guardian = fixture["guardian"]
    original_counter_write = guardian._atomic_write_json

    def temporarily_reject_count(*_args, **_kwargs):
        raise PermissionError("自产的生产路径计数存储暂不可写")

    fixture["monkeypatch"].setattr(guardian, "_atomic_write_json", temporarily_reject_count)
    assert execute_synthetic_analysis(fixture) == 0
    first_failure_history = json.loads(fixture["status_path"].read_text())["formal_analysis_completion_counter_failure_history"]
    capsys.readouterr()
    original_record = guardian.record_analyze_completion

    def record_only_outside_metadata_lock(**kwargs):
        with (fixture["run_directory"] / "artifacts/.review-run-metadata.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return original_record(**kwargs)

    fixture["monkeypatch"].setattr(guardian, "record_analyze_completion", record_only_outside_metadata_lock)
    for _ in range(2):
        pending = scan_production_analysis_recovery(fixture, production)
        assert pending[0]["status"] == "waiting_for_evidence", pending
        assert list(production["index_directory"].glob("analysis-*.json"))
        assert json.loads(fixture["status_path"].read_text())["formal_analysis_completion_status"] == "completed"
        request = json.loads(production["request_path"].read_text())
        assert request["formal_analysis_completion_counter_status"] == "pending"
        assert request["recovery_condition"] and request["attempt_count"] == 3
    fixture["monkeypatch"].setattr(guardian, "_atomic_write_json", original_counter_write)
    recovered = scan_production_analysis_recovery(fixture, production)
    assert recovered[0]["status"] == "completed", recovered
    assert not list(production["index_directory"].glob("analysis-*.json"))
    assert fixture["process_count_path"].read_text() == "1"
    assert json.loads(fixture["counter_path"].read_text())["analyze_completion_count"] == 1
    assert json.loads(fixture["status_path"].read_text())["formal_analysis_completion_counter_failure_history"][:1] == first_failure_history
    assert len(production["build_calls"]) == 3
    assert scan_production_analysis_recovery(fixture, production) == []
    class Ledger:
        summary_path = fixture["run_directory"] / "summary.json"
    repeated = production["advisory"].surface_rvf_analyze_advisory(
        event=production["event"], ledger=Ledger(), payload={}, finalize_record=production["finalization"],
    )
    assert repeated["rvf_analyze_status"] == "completed"
    assert json.loads(fixture["counter_path"].read_text())["analyze_completion_count"] == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("output_filename", ["summary.md", "causality.json"])
def test_pending_scan_revalidates_successful_agent_outputs_after_launch_budget_exhaustion(
    analysis_recovery_fixture, output_filename, capsys,
):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture, unreadable_output=output_filename)
    production = seed_exhausted_production_analysis_launch_claims(fixture)
    assert execute_synthetic_analysis(fixture) == 1
    initial_budget = read_analysis_attempt_budget(fixture)
    capsys.readouterr()
    assert scan_production_analysis_recovery(fixture, production)[0]["status"] == "waiting_for_evidence"
    (fixture["analysis_directory"] / output_filename).chmod(0o600)
    result = scan_production_analysis_recovery(fixture, production)
    assert result[0]["status"] == "completed", result
    assert not list(production["index_directory"].glob("analysis-*.json"))
    assert fixture["process_count_path"].read_text() == "1"
    assert read_analysis_attempt_budget(fixture) == initial_budget
    assert json.loads(production["request_path"].read_text())["attempt_count"] == 3
    assert json.loads(fixture["counter_path"].read_text())["analyze_completion_count"] == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("proof_damage", ["missing-digest", "mismatched-digest", "foreign-run"])
def test_completion_only_scan_preserves_binding_gates_until_original_proof_returns(
    analysis_recovery_fixture, proof_damage,
):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture)
    production = seed_exhausted_production_analysis_launch_claims(fixture)
    guardian = fixture["guardian"]
    original_write = guardian._atomic_write_json

    def temporarily_reject_completion_counter_write(*_args):
        raise PermissionError("自产的计数临时故障")

    fixture["monkeypatch"].setattr(guardian, "_atomic_write_json", temporarily_reject_completion_counter_write)
    assert execute_synthetic_analysis(fixture) == 0
    fixture["monkeypatch"].setattr(guardian, "_atomic_write_json", original_write)
    original_status = json.loads(fixture["status_path"].read_text())
    damaged_status = dict(original_status)
    if proof_damage == "missing-digest":
        damaged_status.pop("formal_analysis_causality_snapshot_sha256")
    elif proof_damage == "mismatched-digest":
        damaged_status["formal_analysis_causality_snapshot_sha256"] = "0" * 64
    else:
        damaged_status["run_dir"] = str(fixture["run_directory"].parent / "foreign-run")
    fixture["status_path"].write_text(json.dumps(damaged_status))
    pending = scan_production_analysis_recovery(fixture, production)
    assert pending[0]["status"] == "waiting_for_evidence", pending
    assert json.loads(fixture["counter_path"].read_text())["analyze_completion_count"] == 0
    assert list(production["index_directory"].glob("analysis-*.json"))
    fixture["status_path"].write_text(json.dumps(original_status))
    assert scan_production_analysis_recovery(fixture, production)[0]["status"] == "completed"
    assert json.loads(fixture["counter_path"].read_text())["analyze_completion_count"] == 1
    assert fixture["process_count_path"].read_text() == "1"


def test_pending_scan_does_not_turn_manual_output_edits_into_new_model_budget(analysis_recovery_fixture):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture, exit_code=7)
    production = seed_exhausted_production_analysis_launch_claims(fixture)
    assert execute_synthetic_analysis(fixture) == 7
    exhausted_budget = read_analysis_attempt_budget(fixture)
    (fixture["analysis_directory"] / "summary.md").write_text("手工补写的正式文本。")
    assert scan_production_analysis_recovery(fixture, production)[0]["status"] == "waiting_for_evidence"
    assert read_analysis_attempt_budget(fixture) == exhausted_budget
    assert fixture["process_count_path"].read_text() == "3"
    assert json.loads(fixture["counter_path"].read_text())["analyze_completion_count"] == 0


def test_completion_only_scan_waits_for_the_analysis_owner_without_consuming_launch_budget(analysis_recovery_fixture):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture, unreadable_output="summary.md")
    production = seed_exhausted_production_analysis_launch_claims(fixture)
    assert execute_synthetic_analysis(fixture) == 1
    (fixture["analysis_directory"] / "summary.md").chmod(0o600)
    with (fixture["run_directory"] / "artifacts/.analysis-execution.lock").open("a+b") as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX)
        try:
            pending = scan_production_analysis_recovery(fixture, production)
            assert pending[0]["status"] == "waiting_for_evidence", pending
            assert json.loads(production["request_path"].read_text())["attempt_count"] == 3
        finally:
            fcntl.flock(owner.fileno(), fcntl.LOCK_UN)
    assert scan_production_analysis_recovery(fixture, production)[0]["status"] == "completed"
    assert fixture["process_count_path"].read_text() == "1"


def analysis_recovery_identity_file_observation(path):
    import hashlib
    if not path.exists() and not path.is_symlink():
        return {"exists": False}
    metadata = path.lstat()
    return {"exists": True, "is_symlink": path.is_symlink(),
            "link_target": os.readlink(path) if path.is_symlink() else None,
            "inode": metadata.st_ino, "mtime_ns": metadata.st_mtime_ns,
            "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}


def analysis_recovery_request_identity_observation(fixture, production):
    return {
        "request": analysis_recovery_identity_file_observation(production["request_path"]),
        "outside_request": analysis_recovery_identity_file_observation(fixture["run_directory"].parent / "foreign-owner-request.json"),
        "index": {path.name: analysis_recovery_identity_file_observation(path)
                  for path in production["index_directory"].glob("analysis-*.json")},
        "budget": analysis_recovery_identity_file_observation(fixture["run_directory"] / "artifacts"
                   / fixture["wrapper"].ANALYSIS_ATTEMPT_BUDGET_FILENAME),
    }


def damage_analysis_recovery_request_identity(fixture, production, damage):
    path = production["request_path"]
    if damage == "request-symlink":
        outside = fixture["run_directory"].parent / "foreign-owner-request.json"
        path.replace(outside)
        path.symlink_to(outside)
    elif damage == "request-deleted":
        path.unlink()
    elif damage == "malformed-json":
        path.write_text("{自产的无效请求")
    else:
        request = json.loads(path.read_text())
        if damage == "repo-mismatch":
            request["repo_realpath"] = str(fixture["run_directory"].parent / "foreign-repository")
        elif damage == "run-directory-mismatch":
            request["run_dir"] = str(fixture["run_directory"].parent / "foreign-run")
        elif damage.startswith("missing-"):
            request.pop(damage.removeprefix("missing-"), None)
        elif damage == "empty-object":
            request = {}
        elif damage == "claim-id-changed":
            request["analysis_launch_claim_id"] = "different-synthetic-launch-claim"
        path.write_text(json.dumps(request))


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("damage", [
    "repo-mismatch", "run-directory-mismatch", "request-symlink", "missing-repo_realpath",
    "missing-run_dir", "missing-session_id", "missing-run_id", "empty-object", "malformed-json",
])
def test_analysis_recovery_request_identity_is_checked_before_launch_or_counter_side_effects(
    analysis_recovery_fixture, completed, damage,
):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture)
    production = seed_exhausted_production_analysis_launch_claims(fixture)
    if completed:
        assert execute_synthetic_analysis(fixture) == 0
    guardian_calls = []
    fixture["monkeypatch"].setattr(fixture["guardian"], "record_analyze_completion",
        lambda **_kwargs: guardian_calls.append(True) or {"status": "already_recorded"})
    damage_analysis_recovery_request_identity(fixture, production, damage)
    before = analysis_recovery_request_identity_observation(fixture, production)
    caught = None
    try:
        production["archive"].claim_review_run_analysis_launch(analysis=production["analysis"], event=production["event"])
    except (ValueError, OSError) as exc:
        caught = exc
    after = analysis_recovery_request_identity_observation(fixture, production)
    receipt = {"damage": damage, "completed": completed,
               "error": f"{type(caught).__name__}: {caught}" if caught else None,
               "guardian_calls": len(guardian_calls), "before": before, "after": after}
    (fixture["run_directory"].parent / "request-identity-validation-observation.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    assert caught is not None, receipt
    assert not guardian_calls and len(production["build_calls"]) == 3
    assert after == before


@pytest.mark.parametrize("long_operation", ["build-command", "reconcile-completion"])
@pytest.mark.parametrize("damage", ["repo-mismatch", "run-directory-mismatch", "request-symlink", "request-deleted"])
def test_analysis_recovery_request_identity_is_rechecked_after_long_operations(
    analysis_recovery_fixture, long_operation, damage,
):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture)
    production = seed_exhausted_production_analysis_launch_claims(fixture)
    if long_operation == "reconcile-completion":
        assert execute_synthetic_analysis(fixture) == 0
    after_mutation = []

    def mutate_during_long_operation(*_args, **_kwargs):
        damage_analysis_recovery_request_identity(fixture, production, damage)
        after_mutation.append(analysis_recovery_request_identity_observation(fixture, production))
        if long_operation == "build-command":
            return [sys.executable, str(fixture["agent_path"])], "-"
        return {"status": "completed", "formal_analysis_completion_status": "completed",
                "formal_analysis_completion_counter_status": "recorded", "recovery_condition": None}

    target = fixture["wrapper"] if long_operation == "reconcile-completion" else load_script_module("rvf_analyze_thread")
    symbol = "reconcile_formal_analysis_completion_without_agent_execution" if long_operation == "reconcile-completion" else "build_analyze_command"
    fixture["monkeypatch"].setattr(target, symbol, mutate_during_long_operation)
    with pytest.raises((ValueError, OSError)):
        production["archive"].claim_review_run_analysis_launch(analysis=production["analysis"], event=production["event"])
    assert analysis_recovery_request_identity_observation(fixture, production) == after_mutation[0]


@pytest.mark.parametrize("damage", [
    "repo-mismatch", "run-directory-mismatch", "request-symlink", "request-deleted",
    "missing-repo_realpath", "claim-id-changed",
])
def test_analysis_recovery_request_identity_blocks_foreign_launch_result_writeback(analysis_recovery_fixture, damage):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture)
    production = seed_exhausted_production_analysis_launch_claims(fixture)
    damage_analysis_recovery_request_identity(fixture, production, damage)
    before = analysis_recovery_request_identity_observation(fixture, production)
    with pytest.raises((ValueError, OSError)):
        production["archive"].record_review_run_analysis_launch(
            production["latest_launch_claim"], {"launch_status": "already_running"},
        )
    assert analysis_recovery_request_identity_observation(fixture, production) == before


@pytest.mark.parametrize("missing_field", ["run_id", "run_dir", "repo_realpath", "session_id", "analysis_launch_claim_id"])
def test_analysis_recovery_request_identity_never_guesses_missing_claim_binding(analysis_recovery_fixture, missing_field):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture)
    production = seed_exhausted_production_analysis_launch_claims(fixture)
    incomplete_claim = dict(production["latest_launch_claim"])
    incomplete_claim.pop(missing_field, None)
    before = analysis_recovery_request_identity_observation(fixture, production)
    with pytest.raises((ValueError, OSError)):
        production["archive"].record_review_run_analysis_launch(incomplete_claim, {"launch_status": "launched"})
    assert analysis_recovery_request_identity_observation(fixture, production) == before


def test_analysis_recovery_request_identity_allows_one_current_bound_launch_result(analysis_recovery_fixture):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture)
    production = seed_exhausted_production_analysis_launch_claims(fixture)
    claim = production["latest_launch_claim"]
    assert all(isinstance(claim.get(key), str) and claim[key] for key in (
        "run_id", "run_dir", "repo_realpath", "session_id", "analysis_launch_claim_id",
    ))
    production["archive"].record_review_run_analysis_launch(claim, {"launch_status": "already_running"})
    request = json.loads(production["request_path"].read_text())
    assert request["attempt_count"] == 2 and request["status"] == "pending"
    before = analysis_recovery_request_identity_observation(fixture, production)
    with pytest.raises(ValueError):
        production["archive"].record_review_run_analysis_launch(claim, {"launch_status": "already_running"})
    assert analysis_recovery_request_identity_observation(fixture, production) == before


@pytest.mark.parametrize("output_filename", ["summary.md", "causality.json"])
def test_temporary_formal_output_io_gap_pauses_the_current_batch_and_direct_replays(
    analysis_recovery_fixture, output_filename,
):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture, unreadable_output=output_filename)
    assert execute_synthetic_analysis(fixture) == 1
    assert fixture["process_count_path"].read_text() == "1"
    first_budget = read_analysis_attempt_budget(fixture)
    assert first_budget["attempts_started"] == 1
    for _ in range(2):
        assert execute_synthetic_analysis(fixture) == 1
        assert fixture["process_count_path"].read_text() == "1"
        assert read_analysis_attempt_budget(fixture) == first_budget
    (fixture["analysis_directory"] / output_filename).chmod(0o600)
    assert execute_synthetic_analysis(fixture) == 0
    assert fixture["process_count_path"].read_text() == "1"
    assert read_analysis_attempt_budget(fixture) == first_budget


@pytest.mark.parametrize("output_filename", ["summary.md", "causality.json"])
def test_temporary_formal_output_io_gap_keeps_unspent_launch_budget_until_output_is_readable(
    analysis_recovery_fixture, output_filename,
):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture, unreadable_output=output_filename)
    production = seed_exhausted_production_analysis_launch_claims(fixture, launch_claim_count=1)
    original_finish = fixture["wrapper"]._finish_analysis_attempt

    def persist_first_attempt_then_interrupt(*args):
        original_finish(*args)
        raise SystemExit(23)

    fixture["monkeypatch"].setattr(fixture["wrapper"], "_finish_analysis_attempt", persist_first_attempt_then_interrupt)
    with pytest.raises(SystemExit, match="23"):
        execute_synthetic_analysis(fixture)
    fixture["monkeypatch"].setattr(fixture["wrapper"], "_finish_analysis_attempt", original_finish)
    assert fixture["process_count_path"].read_text() == "1"
    launch_calls = []

    def launch_a_real_synthetic_analysis_process(**_kwargs):
        launch_calls.append(True)
        result = execute_synthetic_analysis(fixture)
        return {"launch_status": "launched", "returncode": result}

    fixture["monkeypatch"].setattr(production["advisory"], "launch_detached_analyze_thread", launch_a_real_synthetic_analysis_process)
    initial_budget = read_analysis_attempt_budget(fixture)
    pending = scan_production_analysis_recovery(fixture, production)
    assert pending[0]["status"] == "waiting_for_evidence", pending
    assert not launch_calls
    assert fixture["process_count_path"].read_text() == "1"
    assert json.loads(production["request_path"].read_text())["attempt_count"] == 1
    assert read_analysis_attempt_budget(fixture) == initial_budget
    (fixture["analysis_directory"] / output_filename).chmod(0o600)
    assert scan_production_analysis_recovery(fixture, production)[0]["status"] == "completed"
    assert not launch_calls and fixture["process_count_path"].read_text() == "1"
    assert json.loads(fixture["counter_path"].read_text())["analyze_completion_count"] == 1


@pytest.mark.parametrize("semantic_failure", ["todo", "malformed-json"])
@pytest.mark.parametrize("eventually_valid", [True, False])
def test_semantic_formal_output_incompleteness_keeps_genuine_retries_within_three_attempts(
    analysis_recovery_fixture, semantic_failure, eventually_valid,
):
    fixture = analysis_recovery_fixture
    write_synthetic_recovery_agent(fixture)
    summary_path = fixture["analysis_directory"] / "summary.md"
    causality_path = fixture["analysis_directory"] / "causality.json"
    original_causality = causality_path.read_text()
    mutation = (f"Path({str(summary_path)!r}).write_text('TODO(rvf-analyze): 语义尚未完成')"
                if semantic_failure == "todo" else f"Path({str(causality_path)!r}).write_text('{{')")
    fixture["agent_path"].write_text(fixture["agent_path"].read_text()
        + f"Path({str(causality_path)!r}).write_text({original_causality!r})\n"
        + f"if counter.read_text() == '1' or {not eventually_valid!r}:\n    {mutation}\n")
    assert execute_synthetic_analysis(fixture) == (0 if eventually_valid else 1)
    assert fixture["process_count_path"].read_text() == ("2" if eventually_valid else "3")
    if not eventually_valid:
        assert execute_synthetic_analysis(fixture) == 1
        assert fixture["process_count_path"].read_text() == "3"


@pytest.mark.parametrize("invalid_run_identity", [None, "", " \t", 123])
def test_guardian_rejects_unknown_run_identity_without_modifying_the_counter(analysis_recovery_fixture, invalid_run_identity):
    fixture = analysis_recovery_fixture
    before = analysis_recovery_identity_file_observation(fixture["counter_path"])
    results = [fixture["guardian"].record_analyze_completion(
        run_dir=invalid_run_identity, root=fixture["guardian_root"],
    ) for _ in range(2)]
    after = analysis_recovery_identity_file_observation(fixture["counter_path"])
    receipt = {"provided_identity": invalid_run_identity, "results": results, "before": before, "after": after}
    (fixture["run_directory"].parent / "guardian-unknown-run-identity-observation.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    assert all(result["status"] == "guardian_record_failed" for result in results), receipt
    assert after == before


def test_guardian_missing_run_identity_cannot_create_new_counter_state(analysis_recovery_fixture):
    fixture = analysis_recovery_fixture
    uncreated_root = fixture["run_directory"].parent / "uncreated-guardian-counter-root"
    dispatches = []
    result = fixture["guardian"].record_analyze_completion(
        root=uncreated_root, guardian_analysis_dispatcher=lambda **_kwargs: dispatches.append(True) or {"launch_status": "faked"},
    )
    assert result["status"] == "guardian_record_failed"
    assert not uncreated_root.exists() and not dispatches


@pytest.mark.parametrize("invalid_cli_arguments", [[], ["--run-dir", ""], ["--run-dir", "   "]])
def test_guardian_cli_requires_exact_run_identity_before_counting(analysis_recovery_fixture, invalid_cli_arguments):
    fixture = analysis_recovery_fixture
    before = analysis_recovery_identity_file_observation(fixture["counter_path"])
    command = [sys.executable, "-B", fixture["guardian"].__file__, "record-analyze-completion", *invalid_cli_arguments]
    result = subprocess.run(command, capture_output=True, text=True)
    after = analysis_recovery_identity_file_observation(fixture["counter_path"])
    receipt = {"argv": command, "exit_code": result.returncode, "stdout": result.stdout,
               "stderr": result.stderr, "before": before, "after": after}
    (fixture["run_directory"].parent / "guardian-cli-run-identity-observation.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    assert result.returncode != 0, receipt
    assert after == before


def test_guardian_cli_replays_the_same_exact_run_without_double_counting(analysis_recovery_fixture):
    fixture = analysis_recovery_fixture
    command = [sys.executable, "-B", fixture["guardian"].__file__, "record-analyze-completion",
               "--run-dir", str(fixture["run_directory"])]
    relative_command = [*command[:-1], fixture["run_directory"].name]
    results = [subprocess.run(argv, cwd=fixture["run_directory"].parent, capture_output=True, text=True)
               for argv in (command, relative_command)]
    assert all(result.returncode == 0 for result in results)
    assert [json.loads(result.stdout)["status"] for result in results] == ["recorded", "already_recorded"]
    assert json.loads(fixture["counter_path"].read_text())["analyze_completion_count"] == 1


def test_guardian_run_identity_normalizes_relative_absolute_and_equivalent_paths(analysis_recovery_fixture):
    fixture = analysis_recovery_fixture
    fixture["monkeypatch"].chdir(fixture["run_directory"].parent)
    identities = [fixture["run_directory"].name, str(fixture["run_directory"]),
                  "./" + fixture["run_directory"].name, fixture["run_directory"].name + "/../" + fixture["run_directory"].name]
    results = [fixture["guardian"].record_analyze_completion(run_dir=identity, root=fixture["guardian_root"])
               for identity in identities]
    assert [result["status"] for result in results] == ["recorded", "already_recorded", "already_recorded", "already_recorded"]
    counter = json.loads(fixture["counter_path"].read_text())
    assert counter["analyze_completion_count"] == 1
    assert list(counter["counted_analysis_run_directories"]) == [str(fixture["run_directory"].resolve())]


def test_guardian_run_identity_resolves_same_relative_name_in_distinct_calling_directories(analysis_recovery_fixture):
    fixture = analysis_recovery_fixture
    roots = [fixture["run_directory"].parent / "first-caller", fixture["run_directory"].parent / "second-caller"]
    for root in roots:
        (root / "same-review-run-name").mkdir(parents=True)
        fixture["monkeypatch"].chdir(root)
        assert fixture["guardian"].record_analyze_completion(
            run_dir="same-review-run-name", root=fixture["guardian_root"],
        )["status"] == "recorded"
    counter = json.loads(fixture["counter_path"].read_text())
    assert counter["analyze_completion_count"] == 2
    assert set(counter["counted_analysis_run_directories"]) == {str(root / "same-review-run-name") for root in roots}
