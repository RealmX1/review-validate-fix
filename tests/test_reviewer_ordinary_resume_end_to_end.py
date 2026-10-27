"""隔离 CLI 验证普通入口的跨进程恢复、重复调用和正常退出门禁。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_review_start_independent_user_turn_boundary import valid_boundary, isolate_runtime_state


@pytest.fixture
def isolated_dispatch(valid_boundary, tmp_path):
    import prepare_review_run
    import dispatch_reviewers
    import textwrap

    context = tmp_path / "scope-of-work.md"
    context.write_text("审查 a.py 中 value 从 1 改为 2 的实现。")
    prepared = prepare_review_run.prepare_run(repo=valid_boundary["repo"], session_context=context,
        session_manifest=None, transcript=None, base_dir=tmp_path / "runs", max_file_bytes=10000,
        max_packet_bytes=0, primary_files=["a.py"], background_files=[], exclude_path_prefixes=[],
        decision_token=valid_boundary["token"])
    run_directory = Path(prepared["run_dir"])
    scripts = Path(dispatch_reviewers.__file__).parent
    reviewer_script = tmp_path / "isolated-stateful-reviewer.py"
    reviewer_script.write_text(f"sys_path = {str(scripts)!r}\n" + textwrap.dedent('''
        import json,os,sys,subprocess,time
        from pathlib import Path
        label,run,scenario=sys.argv[1:]
        run=Path(run)
        sys.stdin.read()
        count=run/(label+'-execution-count.txt')
        with count.open('a') as output: output.write('execution\\n')
        attempt=len(count.read_text().splitlines())
        result=Path(os.environ['RVF_REVIEW_RESULT'])
        writer=str(Path(sys_path)/'write_review_result.py')
        def write(command,*args):
            subprocess.run([sys.executable,writer,command,'--out',str(result),*args],check=True)
        if label=='beta' or scenario=='complete':
            write('no-issues','--audit-summary','完整范围已独立查证')
        elif scenario=='request':
            if attempt==1:
                write('issue','--path','a.py','--line','1','--kind','REAL','--severity','medium','--message','请求前的发现')
                write('context-request','--need','file','--target',str(run/'required-evidence.txt'),'--reason','需要补证')
            else:
                payload=json.loads(result.read_text())
                assert payload['requests'][0]['status']=='responded'
                write('issue','--path','a.py','--line','1','--kind','REAL','--severity','medium','--message','续跑后的发现')
                write('resolve-request','--request-id',payload['requests'][0]['request_id'],'--resolution','补证已查证')
                write('complete','--audit-summary','完整 Scope 已查证')
        elif scenario=='interruption':
            write('issue','--path','a.py','--line','1','--kind','REAL','--severity','medium','--message','中断前已记录的发现')
            if attempt==1: raise SystemExit(9)
        elif scenario in ('context-limit','permanent-context-limit'):
            if attempt==1 or scenario=='permanent-context-limit':
                print('context_length_exceeded',file=sys.stderr)
                raise SystemExit(1)
            write('no-issues','--audit-summary','引用读取完成完整 Scope')
        elif scenario=='dispatcher-interruption':
            (run/'alpha-native-process.json').write_text(json.dumps({'pid':os.getpid(),'supervisor_pid':os.getppid()}))
            deadline=time.monotonic()+20
            while not (run/'allow-alpha-completion.txt').exists():
                if time.monotonic()>deadline: raise SystemExit(8)
                time.sleep(.05)
            write('no-issues','--audit-summary','中断父进程后仍正常完成原尝试')
        elif scenario=='command-lock':
            if attempt==1:
                write('lock-request','--name','isolated-build-verification','--command','python3 -c pass','--reason','验证资源暂被占用')
            else:
                payload=json.loads(result.read_text())
                assert payload['requests'][0]['status']=='responded'
                write('resolve-request','--request-id',payload['requests'][0]['request_id'],'--resolution','使用原有命令锁完成验证')
                write('complete','--audit-summary','锁释放后完成完整 Scope')
    '''))

    def dispatch(scenario="request", restore_evidence_during_wait=False, start_without_wait=False):
        harnesses = {}
        for harness_id, label in (("codex", "alpha"), ("cursor", "beta")):
            config = tmp_path / (label + ".json")
            config.write_text(json.dumps({"enabled": True, "label": "alternative-reviewer:" + label,
                "command": [sys.executable, str(reviewer_script), label, str(run_directory), scenario],
                "pre_run_health": False, "allow_repo_cwd": True}))
            harnesses[harness_id] = {"enabled": True, "config_path": str(config), "dispatch_mode": "external_cli",
                                    "label_prefix": "alternative-reviewer:" + label}
        registry = tmp_path / "isolated-reviewer-registry.json"
        registry.write_text(json.dumps({"harnesses": harnesses}))
        command = [sys.executable, str(scripts / "dispatch_reviewers.py"), "--execute", "--registry", str(registry),
            "--repo", str(valid_boundary["repo"]), "--review-packet", prepared["review_packet"],
            "--scope-contract", prepared["scope_contract"], "--session-context", prepared["scope_of_work_file"],
            "--rvf-run-id", prepared["run_id"], "--rvf-run-dir", prepared["run_dir"],
            "--assume-available", "codex,cursor"]
        environment = {**os.environ, "RVF_REVIEWER_REQUEST_CONDITION_WAIT_SECONDS": "5" if restore_evidence_during_wait else "0"}
        if start_without_wait:
            return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment), None
        restoration = None
        if restore_evidence_during_wait:
            import threading
            import time
            def restore_once_condition_is_observable():
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    path = run_directory / "artifacts/reviewers/reviewer-plan.json"
                    plan = json.loads(path.read_text()) if path.is_file() else {}
                    if any(reviewer.get("recovery", {}).get("conditions") for reviewer in plan.get("reviewers", [])):
                        (run_directory / "required-evidence.txt").write_text("自动出现的补证")
                        return
                    time.sleep(.05)
            restoration = threading.Thread(target=restore_once_condition_is_observable)
            restoration.start()
        completed = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=30)
        if restoration is not None:
            restoration.join(timeout=16)
        plan = json.loads((run_directory / "artifacts/reviewers/reviewer-plan.json").read_text())
        return completed, plan
    return dispatch, run_directory, valid_boundary["repo"]


@pytest.mark.parametrize("interrupted_before_summary", [True, False])
def test_completed_exit_receipts_rebuild_summary_without_reexecuting_reviewers(
    isolated_dispatch, interrupted_before_summary,
):
    """真实退出回执已落盘时，恢复中断汇总及旧计数都不得再次执行已完成腿。"""
    from reviewed_candidate_completion_facts import completed_reviewer_legs
    import dispatch_reviewers

    dispatch, run, _ = isolated_dispatch
    initial, plan = dispatch("complete")
    assert initial.returncode == 0, initial.stderr
    plan_path = run / "artifacts/reviewers/reviewer-plan.json"
    receipt_bytes_by_path = {}
    for reviewer in plan["reviewers"]:
        receipt_path = (run / "artifacts/reviewers" / reviewer["reviewer_id"]
                        / "execution-attempt-results" / (reviewer["execution_attempt_id"] + ".json"))
        receipt = json.loads(receipt_path.read_text())
        assert receipt["returncode"] == receipt["reviewer_subprocess_returncode"] == 0
        receipt_bytes_by_path[receipt_path] = receipt_path.read_bytes()
        if interrupted_before_summary:
            reviewer.update(returncode=None, execution_state="running")
    plan.update(status="running" if interrupted_before_summary else "completed", effective_reviewer_count=0)
    plan_path.write_text(json.dumps(plan))

    for _ in range(2):
        resumed, plan = dispatch("complete")
        assert resumed.returncode == 0, resumed.stderr
        assert plan["status"] == "completed"
        assert len(completed_reviewer_legs(run)) == 2
        displayed = subprocess.run(
            [sys.executable, str(Path(dispatch_reviewers.__file__).with_name("rvf_command_line_shortcut_entrypoint.py")),
             "results", "--run-dir", str(run), "--json"],
            capture_output=True, text=True, timeout=10,
        )
        assert displayed.returncode == 0, displayed.stderr
        summary = json.loads(displayed.stdout)["summary"]
        assert plan["effective_reviewer_count"] == summary["effective_reviewer_count"] == 2, summary
        assert summary["dual_review_established_from_canonical_legs"] is True, summary
        assert plan["review_request_pending_reviewer_ids"] == []
        assert plan["probe_evidence_pending_reviewer_ids"] == []
        for label in ("alpha", "beta"):
            assert len((run / (label + "-execution-count.txt")).read_text().splitlines()) == 1
        for path, receipt_bytes in receipt_bytes_by_path.items():
            assert path.read_bytes() == receipt_bytes


@pytest.mark.parametrize("incomplete_evidence", [
    "abnormal_exit", "pending_request", "changed_input", "changed_result", "failed_subprocess_receipt",
])
def test_recovered_summary_excludes_abnormal_pending_or_invalid_exit_receipts(
    isolated_dispatch, incomplete_evidence,
):
    """真实退出后的异常、待补证及回执身份失效都不能贡献有效腿。"""
    import dispatch_reviewers
    from rvf_command_line_shortcut_entrypoint import summarize_reviewer_legs_from_canonical_results

    dispatch, run, _ = isolated_dispatch
    scenario = {"abnormal_exit": "interruption", "pending_request": "request"}.get(incomplete_evidence, "complete")
    initial, plan = dispatch(scenario)
    assert (initial.returncode == 0) == (scenario == "complete"), initial.stderr
    alpha = next(reviewer for reviewer in plan["reviewers"] if reviewer["reviewer_id"] == "alpha")
    result_path = run / "artifacts/reviewers/alpha/review-result.json"
    receipt_path = result_path.parent / "execution-attempt-results" / (alpha["execution_attempt_id"] + ".json")
    receipt = json.loads(receipt_path.read_text())
    if incomplete_evidence == "changed_input":
        receipt["execution_input_binding"]["scope_sha256"] = "不属于本次候选的冻结范围"
        receipt_path.write_text(json.dumps(receipt))
    elif incomplete_evidence == "changed_result":
        result_path.write_bytes(result_path.read_bytes() + b"\n")
    elif incomplete_evidence == "failed_subprocess_receipt":
        receipt["reviewer_subprocess_returncode"] = 9
        receipt_path.write_text(json.dumps(receipt))
    for reviewer in plan["reviewers"]:
        reviewer.update(returncode=None, execution_state="running")
    plan.update(status="running", effective_reviewer_count=0)
    reviewers_directory = run / "artifacts/reviewers"
    plan_path = reviewers_directory / "reviewer-plan.json"
    plan_path.write_text(json.dumps(plan))

    recovered_plan, incomplete_slots = dispatch_reviewers.load_reviewer_plan_and_select_incomplete_legs(
        reviewers_directory, run / "artifacts"
    )
    assert [recovered_plan["reviewers"][index]["reviewer_id"] for index in incomplete_slots] == ["alpha"]
    assert recovered_plan["effective_reviewer_count"] == 1
    dispatch_reviewers.write_canonical_reviewer_plan(plan_path, recovered_plan)
    summary = summarize_reviewer_legs_from_canonical_results(
        reviewers_directory=reviewers_directory, scope_contract=run / "artifacts/inputs/scope.contract.json"
    )
    assert summary["effective_reviewer_count"] == 1
    assert summary["dual_review_established_from_canonical_legs"] is False
    if incomplete_evidence == "pending_request":
        assert recovered_plan["review_request_pending_reviewer_ids"] == ["alpha"]
    for label in ("alpha", "beta"):
        assert len((run / (label + "-execution-count.txt")).read_text().splitlines()) == 1


def test_request_restart_preserves_findings_completed_leg_and_duplicate_entry(isolated_dispatch):
    dispatch, run, _ = isolated_dispatch
    first, plan = dispatch()
    assert first.returncode == 3, first.stderr
    assert plan["status"] == "blocked_on_reviewer_request"
    beta = run / "artifacts/reviewers/beta/review-result.json"
    beta_before = beta.read_bytes()
    from reviewed_candidate_completion_facts import completed_reviewer_legs
    with pytest.raises(ValueError):
        completed_reviewer_legs(run)
    (run / "required-evidence.txt").write_text("补证内容")
    second, plan = dispatch()
    assert second.returncode == 0, second.stderr
    assert plan["status"] == "completed"
    assert beta.read_bytes() == beta_before
    result = json.loads((run / "artifacts/reviewers/alpha/review-result.json").read_text())
    assert len(result["issues"]) == 2
    assert result["requests"][0]["status"] == "resolved"
    third, _ = dispatch()
    assert third.returncode == 0, third.stderr
    assert len((run / "alpha-execution-count.txt").read_text().splitlines()) == 2
    assert len((run / "beta-execution-count.txt").read_text().splitlines()) == 1


def test_abnormal_exit_with_findings_is_incomplete_and_resumes_same_leg(isolated_dispatch):
    dispatch, run, _ = isolated_dispatch
    first, plan = dispatch("interruption")
    assert first.returncode != 0
    assert plan["status"] == "failed"
    second, plan = dispatch("interruption")
    assert second.returncode == 0, second.stderr
    assert plan["status"] == "completed"
    result = json.loads((run / "artifacts/reviewers/alpha/review-result.json").read_text())
    assert len(result["issues"]) == 1
    assert len((run / "beta-execution-count.txt").read_text().splitlines()) == 1


def test_context_limit_automatically_retries_once_with_same_reviewer(isolated_dispatch):
    dispatch, run, _ = isolated_dispatch
    completed, plan = dispatch("context-limit")
    assert completed.returncode == 0, completed.stderr
    assert plan["status"] == "completed"
    assert len((run / "alpha-execution-count.txt").read_text().splitlines()) == 2
    assert len((run / "beta-execution-count.txt").read_text().splitlines()) == 1


def test_repeated_context_failure_does_not_relaunch_unchanged_attempt(isolated_dispatch):
    dispatch, run, _ = isolated_dispatch
    first, _ = dispatch("permanent-context-limit")
    assert first.returncode != 0
    assert len((run / "alpha-execution-count.txt").read_text().splitlines()) == 2
    second, _ = dispatch("permanent-context-limit")
    assert second.returncode != 0
    assert len((run / "alpha-execution-count.txt").read_text().splitlines()) == 2


def test_readable_condition_automatically_resumes_within_existing_runner(isolated_dispatch):
    dispatch, run, _ = isolated_dispatch
    completed, plan = dispatch(restore_evidence_during_wait=True)
    assert completed.returncode == 0, completed.stderr
    assert plan["status"] == "completed"
    assert len((run / "alpha-execution-count.txt").read_text().splitlines()) == 2
    assert len((run / "beta-execution-count.txt").read_text().splitlines()) == 1


def test_cancelled_run_is_not_resumed_after_evidence_appears(isolated_dispatch):
    dispatch, run, _ = isolated_dispatch
    first, _ = dispatch()
    assert first.returncode == 3
    from core.run_ledger.review_run_lifecycle import record_review_run_terminal
    record_review_run_terminal(run, run_id=run.name, terminal_status="cancelled", terminal_at="2026-10-26T23:00:00Z")
    (run / "required-evidence.txt").write_text("Stop 后出现的材料不能撤销 Stop")
    second, _ = dispatch()
    assert second.returncode != 0
    assert len((run / "alpha-execution-count.txt").read_text().splitlines()) == 1


def test_waiting_request_cannot_advance_highwater_or_finalize(isolated_dispatch):
    dispatch, run, repo = isolated_dispatch
    first, plan = dispatch()
    assert first.returncode == 3
    import rvf_run_finalize
    result = rvf_run_finalize._advance_review_highwater(run, repo, {}, {}, {"tracker_lease_release": {"did_review": True}})
    assert result["status"] == "skipped" and result["reason"] == "reviewer_completion_unverified"
    with pytest.raises(ValueError):
        rvf_run_finalize.finalize_run(run_dir=run, decision_kind="handoff")


@pytest.mark.parametrize("kill_reviewer_supervisor", [False, True])
def test_native_reviewer_inherits_execution_lock_after_parent_kill(isolated_dispatch, kill_reviewer_supervisor):
    """真杀父进程后，仍在途的 reviewer 持锁；已有正常退出证据的腿不重跑。"""
    import signal
    import time

    dispatch, run, _ = isolated_dispatch
    process, _ = dispatch("dispatcher-interruption", start_without_wait=True)
    native_identity = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            native_path = run / "alpha-native-process.json"
            plan_path = run / "artifacts/reviewers/reviewer-plan.json"
            plan = json.loads(plan_path.read_text()) if plan_path.is_file() else {}
            beta_complete = any(reviewer["reviewer_id"] == "beta" and reviewer.get("returncode") == 0
                                for reviewer in plan.get("reviewers", []))
            if native_path.is_file() and beta_complete:
                native_identity = json.loads(native_path.read_text())
                break
            time.sleep(.05)
        assert native_identity is not None
        process.kill()
        process.wait(timeout=5)
        if kill_reviewer_supervisor:
            os.kill(native_identity["supervisor_pid"], signal.SIGKILL)
        waiting, _ = dispatch("dispatcher-interruption")
        assert waiting.returncode == 3, waiting.stderr
        condition = json.loads((run / "artifacts/reviewers/.reviewer-dispatch-execution-wait.json").read_text())
        assert condition["recovery_condition"] == "dispatch_execution_lock_available"
        assert len((run / "alpha-execution-count.txt").read_text().splitlines()) == 1
        (run / "allow-alpha-completion.txt").write_text("原执行可以完成")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            completed, final_plan = dispatch("dispatcher-interruption")
            if completed.returncode != 3:
                break
            time.sleep(.1)
        assert completed.returncode == 0, completed.stderr
        assert final_plan["status"] == "completed"
        alpha = next(reviewer for reviewer in final_plan["reviewers"] if reviewer["reviewer_id"] == "alpha")
        if not kill_reviewer_supervisor:
            assert alpha["execution_receipt_recovery"]["receipt_path"]
        expected_alpha_count = 2 if kill_reviewer_supervisor else 1
        assert len((run / "alpha-execution-count.txt").read_text().splitlines()) == expected_alpha_count
        assert len((run / "beta-execution-count.txt").read_text().splitlines()) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        (run / "allow-alpha-completion.txt").write_text("测试清理：让原执行结束")


def test_command_lock_condition_resumes_same_leg_after_release(isolated_dispatch, tmp_path, monkeypatch):
    import command_lock
    import fcntl

    dispatch, run, repo = isolated_dispatch
    monkeypatch.setenv("RVF_LOCK_DIR", str(tmp_path / "isolated-command-locks"))
    lock_path, _ = command_lock.lock_paths(repo, command_lock.default_lock_root(), "isolated-build-verification")
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        first, plan = dispatch("command-lock")
        assert first.returncode == 3, first.stderr
        alpha = next(reviewer for reviewer in plan["reviewers"] if reviewer["reviewer_id"] == "alpha")
        assert alpha["recovery"]["conditions"][0]["kind"] == "command_lock_available"
        assert len((run / "alpha-execution-count.txt").read_text().splitlines()) == 1
    second, plan = dispatch("command-lock")
    assert second.returncode == 0, second.stderr
    assert plan["status"] == "completed"
    assert len((run / "alpha-execution-count.txt").read_text().splitlines()) == 2
    assert len((run / "beta-execution-count.txt").read_text().splitlines()) == 1


@pytest.mark.parametrize("changed_evidence", ["candidate_binding", "canonical_result"])
def test_exited_attempt_receipt_rejects_changed_input_or_result(tmp_path, monkeypatch, changed_evidence):
    from reviewer_execution_attempt_evidence import (
        REVIEWER_ATTEMPT_ID_ENVIRONMENT_VARIABLE, persist_reviewer_execution_attempt_receipt,
        recover_exited_reviewer_execution_attempts,
    )

    run = tmp_path / "exact-run"
    result = run / "artifacts/reviewers/alpha/review-result.json"
    result.parent.mkdir(parents=True)
    result.write_text("原始 canonical 结果")
    attempt_id = "a" * 32
    binding = {"run_id": "exact-run", "repo": str(tmp_path), "packet_sha256": "packet", "scope_sha256": "scope"}
    monkeypatch.setenv(REVIEWER_ATTEMPT_ID_ENVIRONMENT_VARIABLE, attempt_id)
    persist_reviewer_execution_attempt_receipt(run_directory=run, reviewer_id="alpha", execution_input_binding=binding,
        result_path=result, returncode=0, reviewer_subprocess_returncode=0, review_complete=True)
    plan = {"execution_input_binding": dict(binding), "reviewers": [{"reviewer_id": "alpha", "execution_attempt_id": attempt_id,
        "returncode": None, "execution_state": "running"}]}
    if changed_evidence == "candidate_binding":
        plan["execution_input_binding"]["scope_sha256"] = "another-candidate-scope"
    else:
        result.write_text("原尝试退出后 canonical 已变化")
    assert recover_exited_reviewer_execution_attempts(plan, run / "artifacts") == []
    assert plan["reviewers"][0]["returncode"] is None
