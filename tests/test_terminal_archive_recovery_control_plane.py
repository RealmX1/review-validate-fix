"""终态归档的等价交付证据、并发提交及完成事实对账。"""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from _rvf_test_support.loader import load_script_module


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def isolated_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "isolated-user-local"))
    for name in ("RVF_SUPPRESS_STOP_HOOK", "RVF_ANALYZE_THREAD"):
        monkeypatch.delenv(name, raising=False)
    archive = load_script_module("review_run_terminal_archive")
    policy = load_script_module("codex_gui_without_kanban_task_stop_hook_policy")
    monkeypatch.setattr(policy, "codex_gui_without_kanban_task_stop_hook_is_disabled", lambda event: False)
    run = tmp_path / "rvf-review-start-0123456789abcdef"
    repo = tmp_path / "isolated-repository"
    repo.mkdir()
    write_json(run / "summary.json", {"run_id": run.name, "run_dir": str(run), "repo": str(repo),
                                     "session_id": "isolated-session"})
    write_json(run / "artifacts/inputs/scope.contract.json", {"run_id": run.name, "scope": "fixed"})
    return archive, repo, run


def request_archive(archive, run):
    return archive.request_review_run_terminal_archive(run_dir=run, decision_kind="interrupted",
        reason="隔离回归中的非成功终态", event={"session_id": "isolated-session"})


def resume_archive(archive, repo):
    return archive.resume_pending_review_run_terminal_archives(repo=repo, session_id="isolated-session")


def stale_attempt_fixture(run, repo):
    attempt_id = "isolated-attempt-rvf-003"
    directory = run / "artifacts/fix-attempts" / attempt_id
    directory.mkdir(parents=True)
    return directory, {"attempt_id": attempt_id, "run_id": run.name, "issue_id": "RVF-003",
                       "worktree_path": str(directory / "worktree"), "status": "started"}


def write_delivery_artifacts(directory, tracker_attempt, repo):
    terminal = {**tracker_attempt, "status": "failed", "fix_patch_path": str(directory / "fix.patch")}
    write_json(directory / "attempt.json", {**terminal, "repo": str(repo)})
    write_json(directory / "result.json", {key: terminal[key] for key in (
        "run_id", "issue_id", "attempt_id", "status", "fix_patch_path")})
    (directory / "fix.patch").write_text("", encoding="utf-8")


@pytest.mark.parametrize("variable", ["RVF_SUPPRESS_STOP_HOOK", "RVF_ANALYZE_THREAD"])
@pytest.mark.parametrize("value", ["0", "false", "off", "no", " FALSE ", ""])
def test_false_flags_do_not_prohibit_automatic_analysis(isolated_archive, monkeypatch, variable, value):
    archive, _, _ = isolated_archive
    monkeypatch.setenv(variable, value)
    assert archive._automatic_analysis_is_prohibited({}) is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "y", "on", " TRUE "])
def test_true_flags_and_gui_policy_still_prohibit_analysis(isolated_archive, monkeypatch, value):
    archive, _, _ = isolated_archive
    monkeypatch.setenv("RVF_ANALYZE_THREAD", value)
    assert archive._automatic_analysis_is_prohibited({}) is True
    monkeypatch.setenv("RVF_ANALYZE_THREAD", "false")
    policy = load_script_module("codex_gui_without_kanban_task_stop_hook_policy")
    monkeypatch.setattr(policy, "codex_gui_without_kanban_task_stop_hook_is_disabled", lambda event: True)
    assert archive._automatic_analysis_is_prohibited({}) is True


def test_stale_tracker_recovers_automatically_when_exact_delivery_arrives(isolated_archive, monkeypatch):
    archive, repo, run = isolated_archive
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker
    directory, attempt = stale_attempt_fixture(run, repo)
    monkeypatch.setattr(tracker, "rvf_causality_for_run", lambda **kwargs: {"fix_attempts": [attempt]})
    calls = []
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: calls.append(kwargs) or {})
    waiting = request_archive(archive, run)
    assert waiting["status"] == "waiting_for_evidence"
    assert waiting["attempt_count"] == 0
    assert waiting["writer_evidence"][0]["status"] == "unknown"
    assert waiting["writer_evidence"][0]["recovery_condition"]
    # 启动者信息不构成 fixer 退出证据。
    attempt.update({"pid": 99999999, "preparation_boot_session_id": "old-boot"})
    assert resume_archive(archive, repo)[0]["status"] == "waiting_for_evidence"
    assert not calls
    write_delivery_artifacts(directory, attempt, repo)
    recovered = resume_archive(archive, repo)[0]
    assert recovered["status"] == "archived"
    assert recovered["writer_evidence"][0]["evidence_kind"] == "fixer_delivery_artifacts"
    assert recovered["writer_evidence"][0]["process_exit_status"] == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize("invalid", ["run", "issue", "repo", "worktree", "patch", "result-symlink"])
def test_conflicting_delivery_artifacts_never_release_writer_gate(isolated_archive, monkeypatch, invalid):
    archive, repo, run = isolated_archive
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker
    directory, attempt = stale_attempt_fixture(run, repo)
    write_delivery_artifacts(directory, attempt, repo)
    path = directory / ("result.json" if invalid in ("run", "issue", "patch") else "attempt.json")
    value = json.loads(path.read_text())
    key = {"run": "run_id", "issue": "issue_id", "repo": "repo", "worktree": "worktree_path",
           "patch": "fix_patch_path"}.get(invalid)
    if key:
        value[key] = str(directory / "foreign") if invalid in ("repo", "worktree", "patch") else "foreign-identity"
        write_json(path, value)
    else:
        result = directory / "result.json"
        target = directory / "foreign-result.json"
        result.rename(target)
        result.symlink_to(target)
    monkeypatch.setattr(tracker, "rvf_causality_for_run", lambda **kwargs: {"fix_attempts": [attempt]})
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: pytest.fail("不可信工件不能放行 finalize"))
    assert request_archive(archive, run)["status"] == "waiting_for_evidence"


@pytest.mark.parametrize("already_archived", [False, True])
def test_both_archive_branches_preserve_concurrent_successor(isolated_archive, monkeypatch, already_archived):
    archive, repo, run = isolated_archive
    entered, release = Event(), Event()
    successor = run.parent / "rvf-review-start-fedcba9876543210"
    write_json(successor / "summary.json", {"run_id": successor.name, "repo": str(repo)})
    if already_archived:
        monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: {"analysis": {}})
        assert request_archive(archive, run)["status"] == "archived"
    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        if args:
            args[0].pop("analysis_recovery_condition", None)
        return {}
    monkeypatch.setattr(archive, "_surface_terminal_archive_analysis" if already_archived else "finalize_run", pause)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(resume_archive, archive, repo) if already_archived else executor.submit(request_archive, archive, run)
        assert entered.wait(5)
        archive.record_review_run_successor(source_run_dir=run, successor_run_dir=successor, session_id="isolated-session")
        path = run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME
        value = json.loads(path.read_text())
        value["independent_persisted_fact"] = "另一个写入者已持久保存的事实"
        write_json(path, value)
        release.set()
        future.result(5)
    final = json.loads(path.read_text())
    assert final["successor_review_runs"][0]["run_id"] == successor.name
    assert final["independent_persisted_fact"] == "另一个写入者已持久保存的事实"


def crash_after_third_real_finalize(archive, repo, run, monkeypatch):
    finalize = load_script_module("rvf_run_finalize")
    counters = {"capture": 0, "release": 0, "finalize": 0}
    def capture(**kwargs):
        counters["capture"] += 1
        return {}
    def release(*args, **kwargs):
        counters["release"] += 1
        return {}
    monkeypatch.setattr(finalize, "capture_run", capture)
    monkeypatch.setattr(finalize, "_release_tracker_lease", release)
    monkeypatch.setattr(finalize, "_write_usage_summary", lambda *args: {})
    monkeypatch.setattr(finalize, "_scaffold_analysis", lambda *args: {})
    class SimulatedCrash(BaseException):
        pass
    def finish_then_crash(**kwargs):
        counters["finalize"] += 1
        if counters["finalize"] < 3:
            raise OSError("隔离的前两次失败")
        record = finalize.finalize_run(**kwargs)
        assert record["errors"] == []
        raise SimulatedCrash()
    monkeypatch.setattr(archive, "finalize_run", finish_then_crash)
    request_archive(archive, run)
    resume_archive(archive, repo)
    with pytest.raises(SimulatedCrash):
        resume_archive(archive, repo)
    path = run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME
    assert json.loads(path.read_text())["attempt_count"] == 3
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: pytest.fail("完成事实对账不得再调用 finalize"))
    return counters


def test_third_attempt_completion_reconciles_without_duplicate_side_effects(isolated_archive, monkeypatch):
    archive, repo, run = isolated_archive
    counters = crash_after_third_real_finalize(archive, repo, run, monkeypatch)
    result = resume_archive(archive, repo)[0]
    assert result["status"] == "archived"
    assert result["attempt_count"] == 3
    assert result["finalize_completion_evidence"]["sha256"]
    assert result["reconciled_at"]
    resume_archive(archive, repo)
    assert counters == {"capture": 1, "release": 1, "finalize": 3}


def test_completed_third_attempt_survives_actual_process_crash_and_restart(isolated_archive):
    archive, repo, run = isolated_archive
    scripts = Path(archive.__file__).parent
    program = '''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import review_run_terminal_archive as archive
import rvf_run_finalize as finalizer
run, repo, mode = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
counter_path = run / "isolated-side-effect-counts.jsonl"
def side_effect(kind):
    with counter_path.open("a") as handle:
        handle.write(json.dumps({"kind": kind}) + "\\n")
    return {}
finalizer.capture_run = lambda **kwargs: side_effect("capture")
finalizer._release_tracker_lease = lambda *args, **kwargs: side_effect("release")
finalizer._write_usage_summary = lambda *args: {}
finalizer._scaffold_analysis = lambda *args: {}
if mode == "crash":
    archive.request_review_run_terminal_archive(run_dir=run, decision_kind="interrupted", reason="实际隔离进程中断",
        event={"session_id": "isolated-session"}, defer_resume=True)
    path = run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME
    request = json.loads(path.read_text())
    request.update({"attempt_count": 2, "evidence_generation_sha256": archive._archive_evidence_generation(request)})
    path.write_text(json.dumps(request))
    def finalize_then_exit(**kwargs):
        record = finalizer.finalize_run(**kwargs)
        assert record["errors"] == []
        os._exit(73)
    archive.finalize_run = finalize_then_exit
    archive._resume_archive(path, repo=repo, session_id="isolated-session")
else:
    def forbidden_finalize(**kwargs):
        raise AssertionError("实际新进程不得重复 finalize")
    archive.finalize_run = forbidden_finalize
    print(json.dumps(archive.resume_pending_review_run_terminal_archives(repo=repo, session_id="isolated-session")))
'''
    receipts = []
    for mode in ("crash", "resume", "resume"):
        argv = [sys.executable, "-B", "-c", program, str(scripts), str(run), str(repo), mode]
        result = subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=10)
        receipts.append({"argv": argv, "cwd": str(repo), "exit_code": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
        write_json(run / "native-restart-process-receipts.json", receipts)
        if mode == "crash":
            assert result.returncode == 73
        else:
            assert result.returncode == 0, result.stderr
            recovered = json.loads(result.stdout)[0]
            assert recovered["status"] == "archived"
            assert recovered["attempt_count"] == 3
    counts = [json.loads(line)["kind"] for line in (run / "isolated-side-effect-counts.jsonl").read_text().splitlines()]
    assert counts == ["capture", "release"]


@pytest.mark.parametrize("invalid", ["run_id", "repo", "decision_kind", "review_run_terminal_status", "completed_at", "errors", "symlink"])
def test_completion_reconciliation_requires_exact_native_record(isolated_archive, monkeypatch, invalid):
    archive, repo, run = isolated_archive
    crash_after_third_real_finalize(archive, repo, run, monkeypatch)
    path = run / "artifacts/.finalize.lock"
    record = json.loads(path.read_text())
    if invalid == "symlink":
        target = path.with_name("foreign-finalize.json")
        path.rename(target)
        path.symlink_to(target)
    else:
        record[invalid] = None if invalid == "completed_at" else ([{"stage": "incomplete"}] if invalid == "errors" else "foreign")
        write_json(path, record)
    result = resume_archive(archive, repo)[0]
    assert result["status"] == "waiting_for_evidence"
    assert result["recovery_condition"]


def test_exhausted_budget_records_wait_and_resumes_after_stable_evidence_changes(isolated_archive, monkeypatch):
    archive, repo, run = isolated_archive
    calls = []
    def fail(**kwargs):
        calls.append(kwargs)
        raise OSError("归档输入暂时不可读")
    monkeypatch.setattr(archive, "finalize_run", fail)
    request_archive(archive, run)
    for _ in range(7):
        result = resume_archive(archive, repo)[0]
    assert len(calls) == 3
    assert result["status"] == "waiting_for_evidence"
    assert result["recovery_condition"]
    write_json(run / "artifacts/before-workspace-snapshot.json", {})
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: {})
    assert resume_archive(archive, repo)[0]["status"] == "archived"


def test_waiting_cancel_preserves_real_handoff_and_does_not_start_cancel_analysis(isolated_archive, monkeypatch):
    archive, repo, run = isolated_archive
    finalize = load_script_module("rvf_run_finalize")
    monkeypatch.setattr(archive, "_writers_still_active", lambda *args: True)
    assert request_archive(archive, run)["status"] == "waiting_for_evidence"
    monkeypatch.setattr(finalize, "capture_run", lambda **kwargs: {})
    monkeypatch.setattr(finalize, "_release_tracker_lease", lambda *args, **kwargs: {})
    monkeypatch.setattr(finalize, "_advance_review_highwater", lambda *args, **kwargs: {})
    monkeypatch.setattr(finalize, "_write_usage_summary", lambda *args: {})
    monkeypatch.setattr(finalize, "_scaffold_analysis", lambda *args: {"real_isolated_handoff": True})
    monkeypatch.setattr(finalize, "freeze_completion_notification", lambda *args: None)
    monkeypatch.setattr(finalize, "publish_completion", lambda *args: None)
    completed = finalize.finalize_run(run_dir=run, decision_kind="handoff")
    assert completed["errors"] == []
    original = (run / "artifacts/.finalize.lock").read_bytes()
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: pytest.fail("先成立的 handoff 不能被取消 finalize 覆盖"))
    monkeypatch.setattr(archive, "_surface_terminal_archive_analysis", lambda *args: pytest.fail("旧取消请求不能启动分析"))
    result = resume_archive(archive, repo)[0]
    assert result["status"] == "already_terminal"
    assert result["preserved_terminal_status"] == "handoff"
    assert result["review_completed"] is True
    assert result["decision_kind"] == "interrupted"
    assert "archived_at" not in result
    assert (run / "artifacts/.finalize.lock").read_bytes() == original
    assert json.loads((run / "summary.json").read_text())["review_run_terminal_status"] == "handoff"
    assert resume_archive(archive, repo) == []


def test_first_handoff_without_completed_cache_waits_for_original_completion(isolated_archive, monkeypatch):
    archive, repo, run = isolated_archive
    monkeypatch.setattr(archive, "_writers_still_active", lambda *args: True)
    request_archive(archive, run)
    from core.run_ledger.review_run_lifecycle import record_review_run_terminal
    record_review_run_terminal(run, run_id=run.name, terminal_status="handoff", terminal_at="2026-10-25T12:00:00Z")
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: pytest.fail("成功终态尚缺完成回执时也不能执行取消归档"))
    result = resume_archive(archive, repo)[0]
    assert result["status"] == "waiting_for_evidence"
    assert result["preserved_terminal_status"] == "handoff"
    assert "review_completed" not in result
    assert result["attempt_count"] == 0


def test_final_commit_rechecks_latest_request_identity(isolated_archive, monkeypatch):
    archive, _, run = isolated_archive
    path = run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME
    def change_identity(**kwargs):
        value = json.loads(path.read_text())
        value["session_id"] = "foreign-session"
        write_json(path, value)
        return {}
    monkeypatch.setattr(archive, "finalize_run", change_identity)
    with pytest.raises(ValueError, match="身份|归属"):
        request_archive(archive, run)
    value = json.loads(path.read_text())
    assert value["session_id"] == "foreign-session"
    assert value["status"] == "archiving"


@pytest.mark.parametrize("kind", ["aborted_before_review", "interrupted"])
def test_explicit_terminal_request_cli_is_exact_and_can_defer_work(isolated_archive, monkeypatch, capsys, kind):
    archive, repo, run = isolated_archive
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: pytest.fail("defer-resume 只能登记请求"))
    monkeypatch.setattr(sys, "argv", ["review_run_terminal_archive.py", "request", "--repo", str(repo),
        "--session-id", "isolated-session", "--run-dir", str(run), "--decision-kind", kind,
        "--reason", "已观测的显式终止事件", "--defer-resume"])
    assert archive.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["decision_kind"] == kind
    assert result["initial_resume_deferred"] is True
    assert result["attempt_count"] == 0
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: {})
    assert resume_archive(archive, repo)[0]["status"] == "archived"


@pytest.mark.parametrize("invalid", ["repo", "session", "run-dir", "reason", "decision-kind"])
def test_explicit_terminal_request_cli_rejects_missing_or_foreign_identity(isolated_archive, monkeypatch, invalid):
    archive, repo, run = isolated_archive
    fields = {"repo": str(repo), "session-id": "isolated-session", "run-dir": str(run),
              "decision-kind": "aborted_before_review", "reason": "明确终止"}
    if invalid == "repo":
        fields["repo"] = str(repo.parent / "foreign-repository")
    elif invalid == "session":
        fields["session-id"] = "foreign-session"
    else:
        fields.pop(invalid)
    argv = ["review_run_terminal_archive.py", "request"]
    for key, value in fields.items():
        argv.extend(["--" + key, value])
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as error:
        archive.main()
    assert error.value.code == 2
    assert not (run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME).exists()


@pytest.mark.parametrize("operation", [[], ["resume"]])
def test_resume_without_request_never_invents_abort(isolated_archive, monkeypatch, capsys, operation):
    archive, repo, run = isolated_archive
    monkeypatch.setattr(sys, "argv", ["review_run_terminal_archive.py", *operation,
                                     "--repo", str(repo), "--session-id", "isolated-session"])
    assert archive.main() == 0
    assert json.loads(capsys.readouterr().out) == []
    assert not (run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME).exists()


def test_explicit_analysis_prohibition_survives_deferred_recovery(isolated_archive, monkeypatch):
    archive, repo, run = isolated_archive
    transcript = run.parent / "isolated-transcript.jsonl"
    transcript.write_text('{}\n')
    archive.request_review_run_terminal_archive(run_dir=run, decision_kind="interrupted", reason="显式停止",
        event={"session_id": "isolated-session", "transcript_path": str(transcript),
               "suppress_review_validate_fix": True}, defer_resume=True)
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: {"analysis": {}})
    monkeypatch.setattr(archive, "surface_rvf_analyze_advisory", lambda **kwargs: pytest.fail("普通恢复不能清掉显式禁止"))
    for _ in range(2):
        result = resume_archive(archive, repo)[0]
        assert result["event"]["suppress_review_validate_fix"] is True
        assert result["analysis_recovery_condition"]


def test_native_synchronous_prepare_interrupt_only_records_and_preserves_exception(isolated_archive, monkeypatch):
    archive, repo, run = isolated_archive
    prepare = load_script_module("prepare_review_run")
    context = run.parent / "scope-of-work.md"
    context.write_text("原生中断连接的隔离上下文\n")
    native = KeyboardInterrupt("实际 preparation 已中断")
    def interrupt(**kwargs):
        kwargs["preparation_execution_identity_observer"]({"repo": repo, "run_id": run.name, "run_dir": run,
                                                          "verified_session_id": "isolated-session"})
        raise native
    monkeypatch.setattr(prepare, "prepare_run", interrupt)
    monkeypatch.setattr(prepare, "scope_contract_v3_extensions", lambda **kwargs: {})
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: pytest.fail("异常清理不能执行 finalize"))
    monkeypatch.setattr(sys, "argv", ["prepare_review_run.py", "--repo", str(repo), "--session-context", str(context),
        "--rvf-run-id", run.name, "--rvf-run-dir", str(run)])
    with pytest.raises(KeyboardInterrupt) as error:
        prepare.main()
    assert error.value is native
    request = json.loads((run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME).read_text())
    assert request["decision_kind"] == "interrupted"
    assert request["attempt_count"] == 0
    assert request["initial_resume_deferred"] is True
    evidence = json.loads((run / "artifacts/preparation-native-interruption-observation.json").read_text())
    assert evidence["native_exception_kind"] == "KeyboardInterrupt"
    assert evidence["status"] == "terminal_archive_request_recorded"


def test_default_prepare_cli_records_actual_new_run_without_run_arguments(isolated_archive, monkeypatch):
    archive, repo, old_run = isolated_archive
    prepare = load_script_module("prepare_review_run")
    context = old_run.parent / "default-preparation-context.md"
    context.write_text("省略两个 run 参数时，验证实际创建的本执行身份\n")
    created_run = old_run.parent / "rvf-review-start-234567890abcdef1"
    old_summary = (old_run / "summary.json").read_bytes()
    native = KeyboardInterrupt("实际新 run 创建后 preparation 被中断")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=隔离回归", "-c", "user.email=rvf-test@example.invalid",
                    "commit", "--allow-empty", "-qm", "test: 创建隔离准备中断回归基线"],
                   cwd=repo, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    monkeypatch.setattr(prepare, "resolve_local_review_base_branch_ref", lambda *args, **kwargs: None)
    monkeypatch.setattr(prepare, "require_review_start_boundary_for_prepare", lambda **kwargs: {
        "session_id": "isolated-session", "worktree_head_commit_sha": head, "automatic_closeout_eligibility": {}})
    monkeypatch.setattr(prepare, "scope_contract_v3_extensions", lambda **kwargs: {})
    monkeypatch.setattr(prepare, "bind_review_start_boundary_to_one_run", lambda **kwargs: (created_run.name, created_run))
    original_start_run = prepare.start_run
    def create_actual_ledger_then_interrupt(*args, **kwargs):
        ledger = original_start_run(*args, **kwargs)
        def native_interrupt(**event):
            raise native
        monkeypatch.setattr(ledger, "event", native_interrupt)
        return ledger
    monkeypatch.setattr(prepare, "start_run", create_actual_ledger_then_interrupt)
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: pytest.fail("默认入口的异常清理也不能执行 finalize"))
    monkeypatch.setattr(sys, "argv", ["prepare_review_run.py", "--repo", str(repo), "--session-context", str(context)])
    with pytest.raises(KeyboardInterrupt) as error:
        prepare.main()
    assert error.value is native
    result = json.loads((created_run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME).read_text())
    assert result["run_id"] == created_run.name
    assert result["run_dir"] == str(created_run)
    assert result["initial_resume_deferred"] is True
    assert result["attempt_count"] == 0
    assert (old_run / "summary.json").read_bytes() == old_summary
    assert not (old_run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME).exists()


def test_prepare_interrupt_before_this_execution_binds_never_reuses_old_explicit_run(isolated_archive, monkeypatch):
    archive, repo, run = isolated_archive
    prepare = load_script_module("prepare_review_run")
    context = run.parent / "context-before-binding.md"
    context.write_text("本执行尚未绑定任何 ledger\n")
    def interrupt_before_binding(**kwargs):
        raise KeyboardInterrupt("尚未创建本执行的正式 run")
    monkeypatch.setattr(prepare, "prepare_run", interrupt_before_binding)
    monkeypatch.setattr(prepare, "scope_contract_v3_extensions", lambda **kwargs: {})
    monkeypatch.setattr(sys, "argv", ["prepare_review_run.py", "--repo", str(repo), "--session-context", str(context),
                                     "--rvf-run-id", run.name, "--rvf-run-dir", str(run)])
    with pytest.raises(KeyboardInterrupt):
        prepare.main()
    assert not (run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME).exists()
    assert not (run / "artifacts/preparation-native-interruption-observation.json").exists()


def shared_preparation_fixture(prepare, repo, run, monkeypatch):
    monkeypatch.setattr(prepare, "_resolve_prep_target_repo", lambda payload: repo)
    monkeypatch.setattr(prepare, "require_review_start_boundary_for_prepare", lambda **kwargs: {
        "session_id": "isolated-session", "automatic_closeout_eligibility": {}})
    monkeypatch.setattr(prepare, "bind_review_start_boundary_to_one_run", lambda **kwargs: (run.name, run))
    monkeypatch.setattr(prepare, "scope_contract_v3_extensions_from_prep_payload", lambda *args: {})
    monkeypatch.setattr(prepare, "dispatch_scope_of_work_text", lambda **kwargs: "隔离准备上下文\n")
    return prepare.rvf_prep_file.write_prep_file({"origin_session_id": "isolated-session", "target_flow": "flow-manual",
        "rvf_run": {"run_id": run.name, "run_dir": str(run)}}, root=run.parent / "isolated-prepared-inputs")


def test_native_preparation_worker_interrupt_records_only_after_worker_unwinds(isolated_archive, monkeypatch):
    archive, repo, run = isolated_archive
    prepare = load_script_module("prepare_review_run")
    record = shared_preparation_fixture(prepare, repo, run, monkeypatch)
    native = KeyboardInterrupt("来自实际 preparation worker 的原生中断")
    def interrupt(**kwargs):
        kwargs["preparation_execution_identity_observer"]({"repo": repo, "run_id": run.name, "run_dir": run,
                                                          "verified_session_id": "isolated-session"})
        raise native
    monkeypatch.setattr(prepare, "prepare_run", interrupt)
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: pytest.fail("worker 清理不能执行 finalize"))
    with pytest.raises(KeyboardInterrupt) as error:
        prepare.prepare_run_from_prep_file(record, timeout_seconds=5, base_dir=run.parent / "isolated-prepare-output")
    assert error.value is native
    assert json.loads((run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME).read_text())["initial_resume_deferred"] is True


def test_native_interruption_intent_recovers_automatically_after_scope_readability_returns(isolated_archive, monkeypatch):
    archive, repo, run = isolated_archive
    prepare = load_script_module("prepare_review_run")
    scope = run / "artifacts/inputs/scope.contract.json"
    original = scope.read_bytes()
    scope.write_text("{尚未完整可读")
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: pytest.fail("异常清理不能执行 finalize"))
    prepare._record_native_preparation_interruption(repo=repo, run_id=run.name, run_dir=run,
        verified_session_id="isolated-session", policy_event={"suppress_review_validate_fix": True})
    assert not (run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME).exists()
    observation = json.loads((run / "artifacts/preparation-native-interruption-observation.json").read_text())
    assert observation["status"] == "waiting_for_evidence"
    assert list(archive._pending_archive_directory(repo, "isolated-session").glob("native-preparation-interruption-*.json"))
    assert resume_archive(archive, repo)[0]["status"] == "waiting_for_evidence"
    scope.write_bytes(original)
    calls = []
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: calls.append(kwargs) or {})
    recovered = resume_archive(archive, repo)[0]
    assert recovered["status"] == "archived"
    assert recovered["event"]["suppress_review_validate_fix"] is True
    assert len(calls) == 1


def test_native_interruption_recovery_rejects_changed_observation_owner(isolated_archive, monkeypatch):
    archive, repo, run = isolated_archive
    observation_path = run / "artifacts/preparation-native-interruption-observation.json"
    observation = {"run_id": run.name, "run_dir": str(run), "repo": str(repo), "session_id": "isolated-session",
                   "native_exception_kind": "KeyboardInterrupt"}
    write_json(observation_path, observation)
    archive.record_native_preparation_interruption_recovery_index(observation_path)
    observation["session_id"] = "foreign-session"
    write_json(observation_path, observation)
    result = resume_archive(archive, repo)[0]
    assert result["status"] == "waiting_for_evidence"
    assert "归属" in result["error"]
    assert not (run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME).exists()


@pytest.mark.parametrize("invalid", ["run", "run-dir", "repo", "session", "snapshot-session", "native-session", "scope-symlink", "summary-symlink"])
def test_request_api_rejects_explicit_contract_identity_conflicts_without_writing_index(isolated_archive, invalid):
    archive, repo, run = isolated_archive
    scope = run / "artifacts/inputs/scope.contract.json"
    contract = json.loads(scope.read_text())
    if invalid in ("run", "run-dir", "repo", "session"):
        field = {"run": "run_id", "run-dir": "run_dir", "repo": "repo", "session": "session_id"}[invalid]
        contract[field] = str(repo.parent / "foreign-owner") if invalid in ("repo", "run-dir") else "foreign-identity"
        write_json(scope, contract)
    elif invalid in ("snapshot-session", "native-session"):
        field = "review_candidate_snapshot" if invalid == "snapshot-session" else "review_start_boundary_receipt"
        contract[field] = {"session_id": "foreign-session"}
        write_json(scope, contract)
    else:
        path = scope if invalid == "scope-symlink" else run / "summary.json"
        target = path.with_name("foreign-" + path.name)
        path.rename(target)
        path.symlink_to(target)
    with pytest.raises(ValueError, match="身份|归属|矛盾|符号链接"):
        request_archive(archive, run)
    assert not (run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME).exists()
    assert not archive._pending_archive_directory(repo, "isolated-session").exists()


@pytest.mark.parametrize("temporary_failure", ["timeout", "waiting-thread-interrupt", "oserror"])
def test_temporary_or_waiter_failures_do_not_invent_preparation_termination(isolated_archive, monkeypatch, temporary_failure):
    archive, repo, run = isolated_archive
    prepare = load_script_module("prepare_review_run")
    record = shared_preparation_fixture(prepare, repo, run, monkeypatch)
    release, finished, entered = Event(), Event(), Event()
    def work(**kwargs):
        entered.set()
        if temporary_failure == "oserror":
            raise OSError("临时输入不可读")
        try:
            assert release.wait(5)
            return {"run_id": run.name, "run_dir": str(run)}
        finally:
            finished.set()
    monkeypatch.setattr(prepare, "prepare_run", work)
    if temporary_failure == "waiting-thread-interrupt":
        def interrupt_waiter(*args, **kwargs):
            assert entered.wait(5)
            raise KeyboardInterrupt("仅等待者被中断，worker 仍活跃")
        monkeypatch.setattr(prepare.concurrent.futures.Future, "result", interrupt_waiter)
    try:
        if temporary_failure == "waiting-thread-interrupt":
            with pytest.raises(KeyboardInterrupt):
                prepare.prepare_run_from_prep_file(record, timeout_seconds=5)
        else:
            state = prepare.prepare_run_from_prep_file(record, timeout_seconds=0.01)
            assert state["status"] == ("timeout" if temporary_failure == "timeout" else "failed")
        assert not (run / "artifacts" / archive.ARCHIVE_REQUEST_FILENAME).exists()
        assert not (run / "artifacts/preparation-native-interruption-observation.json").exists()
    finally:
        release.set()
        if temporary_failure != "oserror":
            assert finished.wait(5)
