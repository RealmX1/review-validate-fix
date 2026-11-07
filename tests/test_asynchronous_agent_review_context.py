"""主会话补充独立登记、迟到补验和简短通知的行为验收。"""
import json
import sys
import signal
import subprocess
import time
from pathlib import Path

import pytest

from test_task_snapshot_review_workflow import repository, fake_config
from core.task_snapshot_review.persistent_artifacts import exclusive_lock, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import resume_review
from core.task_snapshot_review.main_agent_context_supplements import register_main_agent_context_supplement
from core.task_snapshot_review.agent_review_summary_artifacts import acknowledge_agent_review_notice


def prepared_review(repository, tmp_path, *, maximum_rounds=3, validation_commands=None):
    (repository / "task.py").write_text("value = 2\n")
    run = tmp_path / "asynchronous-agent-review"
    create_snapshot(repository, base_ref="HEAD", scope_paths=["task.py"], requirements="独立核对事实补充。".encode(),
        run_directory=run, execution_plan={"fix": True, "validation_commands": validation_commands or [[sys.executable, "-c", "pass"]],
                                         "maximum_semantic_repair_rounds": maximum_rounds})
    config = fake_config(tmp_path, findings=[{"path": "task.py", "message": "检查当前值"}])
    # 第一轮反证不修改文件，便于证明迟到补充保留此前成果。
    fixer = tmp_path / "context-aware-fixer.py"
    fixer.write_text("import json,os,pathlib\n"
        "r=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "assert pathlib.Path('task.py').read_text() == 'value = 2\\n'\n"
        "print(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'false_positive','reason':'实际值符合需求'} for f in r['findings']],"
        "'context_supplement_responses':[{'supplement_id':s['supplement_id'],'status':'refuted','reason':'已独立读取文件，事实补充不成立'} for s in r.get('main_agent_context_supplements',[])]}))\n")
    fixer_config = {"command": [sys.executable, str(fixer)], "output_format": "text"}
    return run, config, fixer_config


def submit(run, tmp_path, *, supplement_id="fact-1", **overrides):
    context = tmp_path / "main-agent-facts.md"
    context.write_text("主会话事实主张：值应该是 99，请独立核验。\n")
    snapshot = read_json(run / "reviewer-findings.json")
    return register_main_agent_context_supplement(run, supplement_id=supplement_id,
        candidate_commit=overrides.get("candidate_commit", snapshot["candidate_commit"]),
        findings_sha256=overrides.get("findings_sha256", snapshot["findings_sha256"]),
        context_file=context)


def test_late_context_is_independently_refuted_without_repeating_reviewers(repository, tmp_path):
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    first = resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    assert first["status"] == "review_and_validation_completed"
    first_notice = first["agentContinuationNotice"]
    first_original = (run / "agent-review-summary.md").read_bytes()
    # 登记不取得 controller/workflow 锁，主会话与后台 fixer 不相互等待。
    with exclusive_lock(run / "controller.lock"), exclusive_lock(run / "workflow.lock"):
        registered = submit(run, tmp_path)
        assert submit(run, tmp_path) == registered
    assert read_json(run / "business-result.json")["disposition"] == "machine_evidence_pending"
    assert all(n["noticeId"] != first_notice["noticeId"] for n in read_json(run / "agent-continuation-notices.json")["notices"])
    final = resume_review(run)
    assert final["status"] == "review_and_validation_completed"
    assert final["agentContinuationNotice"]["noticeId"] != first_notice["noticeId"]
    assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == 2
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 2
    request = read_json(run / "fixer/attempt-002/request.json")
    assert [s["supplement_id"] for s in request["main_agent_context_supplements"]] == ["fact-1"]
    summary = (run / "agent-review-summary.md").read_text()
    assert "已独立读取文件" in summary
    assert "fact-1" in summary
    assert first_original in [p.read_bytes() for p in run.glob("agent-review-result-revisions/*/agent-review-summary.md")]
    resume_review(run)
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 2


def test_context_identity_conflicts_and_stop_cannot_be_overridden(repository, tmp_path):
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    with pytest.raises(ValueError, match="候选"):
        submit(run, tmp_path, candidate_commit="0" * 40)
    with pytest.raises(ValueError, match="findings"):
        submit(run, tmp_path, findings_sha256="0" * 64)
    submit(run, tmp_path)
    context = tmp_path / "conflicting-facts.md"
    context.write_text("不同事实")
    snapshot = read_json(run / "reviewer-findings.json")
    with pytest.raises(ValueError, match="补充 ID"):
        register_main_agent_context_supplement(run, supplement_id="fact-1", candidate_commit=snapshot["candidate_commit"],
            findings_sha256=snapshot["findings_sha256"], context_file=context)
    write_json(run / "execution-revocation.json", {"reason": "明确 Stop", "revoked_at": 1})
    with pytest.raises(InterruptedError):
        submit(run, tmp_path, supplement_id="fact-after-stop")
    assert resume_review(run)["disposition"] == "cancelled"
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 1


def test_summary_and_notice_omit_complete_validation_argv(repository, tmp_path):
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    result = resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    notice = result["agentContinuationNotice"]
    assert len(notice["text"]) < 800
    assert "后台事件" in notice["text"]
    assert "最终审阅" in notice["text"]
    assert sys.executable not in notice["text"]
    assert sys.executable not in (run / "agent-review-summary.md").read_text()
    assert sys.executable in (run / "validation.json").read_text()
    entry = Path(__file__).resolve().parents[1] / "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py"
    completed = subprocess.run([sys.executable, str(entry), "resume-task-review", "--run-dir", str(run)], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert '"closeout"' not in completed.stdout
    assert sys.executable not in completed.stdout


@pytest.mark.parametrize("interrupt_signal", [signal.SIGINT, signal.SIGTERM])
def test_frontend_interrupt_retains_roles_and_resume_reuses_their_results(repository, tmp_path, interrupt_signal):
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    script = Path(reviewers["command"][1])
    script.write_text("import time\ntime.sleep(3)\n" + script.read_text())
    config = tmp_path / "reviewer-configuration.json"
    write_json(config, reviewers)
    entry = Path(__file__).resolve().parents[1] / "plugins/review-validate-fix/skills/review-validate-fix/scripts/rvf_command_line_shortcut_entrypoint.py"
    process = subprocess.Popen([sys.executable, str(entry), "resume-task-review", "--run-dir", str(run),
        "--reviewer-config", str(config), "--reviewer-config", str(config)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 20
        while len(list(run.glob("reviewer-*/attempt-*/model-invocation.json"))) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.03)
        process.send_signal(interrupt_signal)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 130, (stdout, stderr)
        assert not (run / "execution-revocation.json").exists()
        while len(list(run.glob("reviewer-*/attempt-*/outcome.json"))) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.03)
        assert resume_review(run, fixer_config=fixer)["status"] == "review_and_validation_completed"
        assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == 2
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_context_arriving_during_validation_invalidates_final_eligibility(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review import workflow
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    validate = workflow._validate
    submitted = False
    def with_late_context(*arguments, **kwargs):
        nonlocal submitted
        result = validate(*arguments, **kwargs)
        if not submitted:
            submitted = True
            submit(run, tmp_path)
        return result
    monkeypatch.setattr(workflow, "_validate", with_late_context)
    result = resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    assert result["status"] == "context_supplements_pending"
    assert result["disposition"] == "machine_evidence_pending"
    assert not any(n["stage"] == "final_agent_review" for n in read_json(run / "agent-continuation-notices.json")["notices"])
    assert resume_review(run)["status"] == "review_and_validation_completed"


def test_context_fixer_cannot_claim_completion_without_independent_response(repository, tmp_path):
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    submit(run, tmp_path)
    script = Path(fixer["command"][1])
    script.write_text("import json,os,pathlib\nr=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "print(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'false_positive','reason':'只回应 finding'} for f in r['findings']]}))\n")
    result = resume_review(run)
    assert result["disposition"] != "agent_review_ready"
    outcomes = [read_json(path) for path in run.glob("fixer/attempt-*/outcome.json")]
    assert any("逐份回应" in str(item.get("error")) for item in outcomes)


def test_consumed_phase_is_not_republished_and_stale_final_cannot_be_accepted(repository, tmp_path):
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    result = resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    notices = read_json(run / "agent-continuation-notices.json")["notices"]
    for notice in notices:
        receipt = acknowledge_agent_review_notice(run, notice["noticeId"])
        assert acknowledge_agent_review_notice(run, notice["noticeId"]) == receipt
    resume_review(run)
    assert read_json(run / "agent-continuation-notices.json")["notices"] == []
    assert len(list(run.glob("agent-review-notice-consumption-receipts/*.json"))) == 2
    submit(run, tmp_path)
    with pytest.raises(ValueError, match="失效"):
        acknowledge_agent_review_notice(run, result["agentContinuationNotice"]["noticeId"])
    revised = resume_review(run)
    assert revised["agentContinuationNotice"] != result["agentContinuationNotice"]
    submit(run, tmp_path, supplement_id="another-fact")
    with pytest.raises(ValueError, match="失效"):
        acknowledge_agent_review_notice(run, revised["agentContinuationNotice"]["noticeId"])


def test_missing_context_original_is_machine_waiting_and_recovers_without_reviewer_repeat(repository, tmp_path):
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    registered = submit(run, tmp_path)
    original = Path(registered["context_file"])
    content = original.read_bytes()
    original.write_text("损坏的原件")
    pending = resume_review(run)
    assert pending["disposition"] == "machine_evidence_pending"
    assert pending["recovery"]["trigger"] == "context_supplement_original_evidence_changed"
    original.write_bytes(content)
    assert resume_review(run)["status"] == "review_and_validation_completed"
    assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == 2


def test_late_context_preserves_fixed_tree_patch_and_has_bounded_additional_budget(repository, tmp_path):
    run, reviewers, fixer = prepared_review(repository, tmp_path, maximum_rounds=1)
    script = Path(fixer["command"][1])
    script.write_text("import json,os,pathlib\nr=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "if not r.get('main_agent_context_supplements'): pathlib.Path('task.py').write_text('value = 3\\n')\n"
        "assert pathlib.Path('task.py').read_text() == 'value = 3\\n'\n"
        "print(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'fixed','reason':'原修复保持有效'} for f in r['findings']],"
        "'context_supplement_responses':[{'supplement_id':s['supplement_id'],'status':'refuted','reason':'独立核对当前完整修复树'} for s in r.get('main_agent_context_supplements',[])]}))\n")
    assert resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)["status"] == "review_and_validation_completed"
    patch = (run / "fixer.patch").read_bytes()
    assert patch
    submit(run, tmp_path)
    assert resume_review(run)["status"] == "review_and_validation_completed"
    assert (repository / "task.py").read_text() == "value = 3\n"
    assert (run / "fixer.patch").read_bytes() == patch
    record = read_json(run / "run.json")
    assert record["semantic_repair_rounds_used"] == 1
    assert list(record["context_supplement_repair_rounds_used"].values()) == [1]


def test_stop_at_publication_boundary_cannot_publish_a_ready_status_or_rewrite_history(repository, tmp_path):
    from core.task_snapshot_review.execution_lifecycle import publish_business_result
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    result = resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    originals = {p: p.read_bytes() for p in run.glob("agent-review-result-revisions/*/*")}
    write_json(run / "execution-revocation.json", {"reason": "最终发布边界明确 Stop", "revoked_at": 1})
    cancelled = publish_business_result(run, result)
    assert cancelled["status"] == "cancelled"
    assert cancelled["disposition"] == "cancelled"
    assert "agentContinuationNotice" not in cancelled
    assert read_json(run / "run.json")["status"] == "cancelled"
    assert all(p.read_bytes() == content for p, content in originals.items())


def test_summary_does_not_reinject_validation_script_copied_into_fixer_reason(repository, tmp_path):
    script = "# " + "VALIDATION_SCRIPT_MUST_STAY_IN_ORIGINALS" * 100 + "\npass"
    run, reviewers, fixer = prepared_review(repository, tmp_path, validation_commands=[[sys.executable, "-c", script]])
    fixer_script = Path(fixer["command"][1])
    original = fixer_script.read_text()
    original = original.replace("'实际值符合需求'", repr("已独立核对，机器验证原件：" + script))
    fixer_script.write_text(original)
    assert resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)["status"] == "review_and_validation_completed"
    summary = (run / "agent-review-summary.md").read_text()
    assert "VALIDATION_SCRIPT_MUST_STAY_IN_ORIGINALS" not in summary
    assert script in read_json(run / "validation.json")["commands"][0]


def test_short_inline_validation_script_is_not_copied_into_the_summary(repository, tmp_path):
    script = "import pathlib; assert pathlib.Path('task.py').read_text()=='value = 2\\n'"
    assert 16 < len(script) <= 160
    run, reviewers, fixer = prepared_review(repository, tmp_path, validation_commands=[[sys.executable, "-c", script]])
    fixer_script = Path(fixer["command"][1])
    fixer_script.write_text(fixer_script.read_text().replace("'实际值符合需求'", repr("已独立核对，机器验证原件：" + script)))
    assert resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)["status"] == "review_and_validation_completed"
    summary = (run / "agent-review-summary.md").read_text()
    assert script not in summary
    assert "完整验证脚本见机器原件" in summary
    assert "passed" in summary
    assert script in read_json(run / "validation.json")["commands"][0]


def test_covered_supplement_budget_does_not_replace_the_main_repair_budget(repository, tmp_path):
    command = [sys.executable, "-c", "from pathlib import Path; assert Path('task.py').read_text()=='value = 3\\n'"]
    run, reviewers, fixer = prepared_review(repository, tmp_path, maximum_rounds=2, validation_commands=[command])
    Path(fixer["command"][1]).write_text(
        "import json,os,pathlib\n"
        "r=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "responses=[{'supplement_id':s['supplement_id'],'status':'refuted','reason':'补充已独立覆盖'} for s in r.get('main_agent_context_supplements',[])]\n"
        "if r.get('main_agent_context_supplements'):\n"
        "    status='false_positive'; reason='补充覆盖后不再改写文件'\n"
        "elif r.get('validation_failure'):\n"
        "    pathlib.Path('task.py').write_text('value = 3\\n'); status='fixed'; reason='验证失败后的主预算修复'\n"
        "else:\n"
        "    status='false_positive'; reason='首轮保留失败以消耗主预算'\n"
        "print(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':status,'reason':reason} for f in r['findings']],'context_supplement_responses':responses}))\n")
    assert resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)["status"] == "review_and_validation_completed"
    submit(run, tmp_path)
    assert resume_review(run)["status"] == "review_and_validation_completed"
    record = read_json(run / "run.json")
    assert record["semantic_repair_rounds_used"] == 2
    assert list(record["context_supplement_repair_rounds_used"].values()) == [1]
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 3
    validation = read_json(run / "validation.json")
    validation["status"] = "failed"
    write_json(run / "validation.json", validation)
    blocked = resume_review(run)
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 3
    assert blocked["disposition"] == "agent_judgment_required"
    assert blocked["task_snapshot_review_result_facts"]["validation"]["status"] == "failed"
    assert read_json(run / "run.json")["repair_budget_exhausted"] is True


def test_main_budget_remains_available_after_supplement_coverage(repository, tmp_path):
    command = [sys.executable, "-c", "from pathlib import Path; assert Path('task.py').read_text()=='value = 3\\n'"]
    run, reviewers, fixer = prepared_review(repository, tmp_path, maximum_rounds=3, validation_commands=[command])
    Path(fixer["command"][1]).write_text(
        "import json,os,pathlib\n"
        "r=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "counter=pathlib.Path(r['run_directory'])/'supplement-rounds.txt'\n"
        "if r.get('main_agent_context_supplements'):\n"
        "    seen=int(counter.read_text()) if counter.exists() else 0\n"
        "    counter.write_text(str(seen+1))\n"
        "    response='unresolved' if seen<2 else 'refuted'\n"
        "    responses=[{'supplement_id':s['supplement_id'],'status':response,'reason':'逐轮独立核对补充'} for s in r['main_agent_context_supplements']]\n"
        "else:\n"
        "    pathlib.Path('task.py').write_text('value = 3\\n')\n"
        "    responses=[]\n"
        "print(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'fixed','reason':'主修复保持有效'} for f in r['findings']],'context_supplement_responses':responses}))\n")
    assert resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)["status"] == "review_and_validation_completed"
    submit(run, tmp_path)
    assert resume_review(run)["status"] == "review_and_validation_completed"
    record = read_json(run / "run.json")
    assert record["semantic_repair_rounds_used"] == 1
    assert list(record["context_supplement_repair_rounds_used"].values()) == [3]
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 4
    validation = read_json(run / "validation.json")
    validation["status"] = "failed"
    write_json(run / "validation.json", validation)
    resumed = resume_review(run)
    assert resumed["status"] == "review_and_validation_completed"
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 5
    assert read_json(run / "run.json")["semantic_repair_rounds_used"] == 2


def test_budget_exhausted_judgment_with_pending_supplement_can_be_acknowledged(repository, tmp_path):
    run, reviewers, fixer = prepared_review(repository, tmp_path, maximum_rounds=1)
    Path(fixer["command"][1]).write_text(
        "import json,os,pathlib\n"
        "r=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "print(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'false_positive','reason':'finding 已核对'} for f in r['findings']],"
        "'context_supplement_responses':[{'supplement_id':s['supplement_id'],'status':'unresolved','reason':'补充仍缺独立证据'} for s in r.get('main_agent_context_supplements',[])]}))\n")
    assert resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)["status"] == "review_and_validation_completed"
    submit(run, tmp_path)
    result = resume_review(run)
    assert result["status"] == "fix_incomplete"
    assert result["disposition"] == "agent_judgment_required"
    notice = result["agentContinuationNotice"]
    assert "未解决 1 项" in notice["text"]
    receipt = acknowledge_agent_review_notice(run, notice["noticeId"])
    assert acknowledge_agent_review_notice(run, notice["noticeId"]) == receipt
    assert all(item["noticeId"] != notice["noticeId"] for item in read_json(run / "agent-continuation-notices.json")["notices"])


def test_explicit_cancel_withdraws_current_delivery_without_rewriting_history(repository, tmp_path):
    from core.task_snapshot_review.execution_lifecycle import revoke_execution
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    result = resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    originals = {path: path.read_bytes() for path in run.glob("agent-review-result-revisions/*/*")}
    evidence = revoke_execution(run, "明确 cancel-task-review", source="rvf_cancel_task_review_command", request_basis="显式取消")
    assert evidence["status"] == "cancelled"
    business = read_json(run / "business-result.json")
    assert business["status"] == "cancelled"
    assert business["disposition"] == "cancelled"
    assert "agentContinuationNotice" not in business
    assert "closeout" not in business
    assert business["task_snapshot_review_result_facts"]["candidate_commit"] == result["task_snapshot_review_result_facts"]["candidate_commit"]
    assert read_json(run / "agent-continuation-notices.json")["notices"] == []
    assert read_json(run / "run.json")["status"] == "cancelled"
    assert (run / "execution-revocation.json").exists()
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_revocation_during_final_publication_cannot_leave_a_ready_notice(repository, tmp_path):
    import core.task_snapshot_review.agent_review_summary_artifacts as artifacts
    from core.task_snapshot_review.execution_lifecycle import publish_business_result
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    result = resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    originals = {path: path.read_bytes() for path in run.glob("agent-review-result-revisions/*/*")}
    original = artifacts.registered_main_agent_context_supplements
    def revoke_after_summary_check(run_directory):
        supplements = original(run_directory)
        write_json(run_directory / "execution-revocation.json", {"reason": "发布中途明确 Stop", "revoked_at": 1,
            "source": "test", "scope": "exact_rvf_run", "request_basis": "发布边界"})
        return supplements
    artifacts.registered_main_agent_context_supplements = revoke_after_summary_check
    try:
        cancelled = publish_business_result(run, result)
    finally:
        artifacts.registered_main_agent_context_supplements = original
    assert cancelled["disposition"] == "cancelled"
    assert "closeout" not in cancelled
    assert cancelled["status"] == "cancelled"
    assert "验真修复及最终验证已完成" not in json.dumps(read_json(run / "agent-continuation-notices.json"), ensure_ascii=False)
    assert all(path.read_bytes() == content for path, content in originals.items())


@pytest.mark.parametrize("quality_original", ["validation.json", "fixer/attempt-001/result.json"])
def test_cancel_and_cancelled_resume_do_not_require_readable_quality_originals(repository, tmp_path, monkeypatch, quality_original):
    from core.task_snapshot_review.execution_lifecycle import revoke_execution
    import core.task_snapshot_review.retained_command_supervisor as supervision
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    damaged_original = run / quality_original
    damaged_original.write_text("{原始损坏材料")
    observed_command_directories = []
    def observe_cancellation(directory):
        observed_command_directories.append(directory)
        return []
    monkeypatch.setattr(supervision, "terminate_supervised_command_descendants", observe_cancellation)
    assert revoke_execution(run, "明确停止")['status'] == 'cancelled'
    assert observed_command_directories
    assert read_json(run / "business-result.json")["disposition"] == "cancelled"
    assert read_json(run / "agent-continuation-notices.json")["notices"] == []
    assert resume_review(run)["disposition"] == "cancelled"
    assert "task_snapshot_review_result_facts" not in read_json(run / "business-result.json")
    assert damaged_original.read_text() == "{原始损坏材料"


def test_frontend_interrupt_keeps_the_published_final_notice(repository, tmp_path, monkeypatch):
    from core.task_snapshot_review import workflow
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    result = resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    notice_id = result["agentContinuationNotice"]["noticeId"]
    def interrupt(*args, **kwargs):
        record = read_json(run / "run.json")
        record["status"] = "interrupted"
        write_json(run / "run.json", record)
        raise KeyboardInterrupt
    monkeypatch.setattr(workflow, "_validate", interrupt)
    with pytest.raises(KeyboardInterrupt):
        resume_review(run)
    business = read_json(run / "business-result.json")
    assert business["disposition"] == "agent_review_ready"
    assert business["status"] == "review_and_validation_completed"
    assert business["agentContinuationNotice"]["noticeId"] == notice_id
    assert read_json(run / "run.json")["status"] == "ready_to_commit"
    assert any(item["noticeId"] == notice_id for item in read_json(run / "agent-continuation-notices.json")["notices"])
    assert not (run / "execution-revocation.json").exists()


def test_interrupt_after_new_validation_starts_preserves_history_without_current_ready_qualification(repository, tmp_path, monkeypatch):
    import core.task_snapshot_review.execution_lifecycle as lifecycle
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    result = resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    notice = result["agentContinuationNotice"]
    originals = {path: path.read_bytes() for path in run.glob("agent-review-result-revisions/*/*")}

    def interrupt_wait(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as interrupted_wait:
        interrupted_wait.setattr(lifecycle, "wait_for_owned_process", interrupt_wait)
        with pytest.raises(KeyboardInterrupt):
            resume_review(run)
    assert read_json(run / "validation.json")["status"] == "interrupted"
    current = read_json(run / "business-result.json")
    assert current["disposition"] == "machine_evidence_pending"
    assert current["task_snapshot_review_result_facts"]["validation"]["status"] == "interrupted"
    assert notice not in read_json(run / "agent-continuation-notices.json")["notices"]
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert not (run / "execution-revocation.json").exists()
    # 监督命令继续存活；同 run 恢复等待，不撤销，也不重复 reviewer 或 fixer。
    deadline = time.monotonic() + 10
    supervision = run / "final-validation" / "attempt-002" / "command-001" / "command-supervision.json"
    while not supervision.exists() or read_json(supervision).get("status") == "running":
        assert time.monotonic() < deadline
        time.sleep(0.03)
    assert resume_review(run)["status"] == "review_and_validation_completed"
    assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == 2
    assert len(list(run.glob("fixer/attempt-*/request.json"))) == 1


def test_registration_crash_before_manifest_withdrawal_invalidates_final_via_current_business_result(repository, tmp_path, monkeypatch):
    import core.task_snapshot_review.main_agent_context_supplements as supplements
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    result = resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    notice = result["agentContinuationNotice"]

    def crash_before_manifest_withdrawal(*args):
        raise OSError("业务状态已持久化，通知清单尚未撤回")

    with monkeypatch.context() as interrupted_registration:
        interrupted_registration.setattr(supplements, "_withdraw_final_continuation_notices", crash_before_manifest_withdrawal)
        with pytest.raises(OSError, match="通知清单"):
            submit(run, tmp_path)
    assert notice in read_json(run / "agent-continuation-notices.json")["notices"]
    current = read_json(run / "business-result.json")
    assert current["disposition"] == "machine_evidence_pending"
    assert "agentContinuationNotice" not in current
    assert notice.get("requiresCurrentResultNoticeMatch") is True
    assert current["pending_context_registration"]["supplement_id"] == "fact-1"
    assert resume_review(run)["status"] == "review_and_validation_completed"
    assert len(list(run.glob("reviewer-*/attempt-*/request.json"))) == 2


def test_interrupted_registration_is_replayed_and_old_final_notice_withdrawn(repository, tmp_path, monkeypatch):
    import core.task_snapshot_review.main_agent_context_supplements as supplements
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    assert resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)["status"] == "review_and_validation_completed"
    original = supplements.write_json
    def crash_before_registration(path, content):
        if Path(path).parent.name == "main-agent-context-supplements":
            raise OSError("登记名录写出前中断")
        return original(path, content)
    monkeypatch.setattr(supplements, "write_json", crash_before_registration)
    with pytest.raises(OSError, match="登记名录"):
        submit(run, tmp_path)
    monkeypatch.setattr(supplements, "write_json", original)
    assert not (run / "main-agent-context-supplements" / "fact-1.json").exists()
    staged = read_json(run / "business-result.json")
    assert staged["disposition"] == "machine_evidence_pending"
    assert staged["pending_context_registration"]["supplement_id"] == "fact-1"
    assert not any(item["stage"] == "final_agent_review" for item in read_json(run / "agent-continuation-notices.json")["notices"])
    assert resume_review(run)["status"] == "review_and_validation_completed"
    assert (run / "main-agent-context-supplements" / "fact-1.json").exists()
    assert read_json(run / "main-agent-context-continuation-request.json")["supplement_ids"] == ["fact-1"]


def test_registered_supplement_without_withdrawal_is_reconciled(repository, tmp_path):
    from core.task_snapshot_review.main_agent_context_supplements import reconcile_interrupted_context_registration
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    ready = resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    ready_business = read_json(run / "business-result.json")
    ready_notices = read_json(run / "agent-continuation-notices.json")
    submit(run, tmp_path)
    write_json(run / "business-result.json", ready_business)
    write_json(run / "agent-continuation-notices.json", ready_notices)
    (run / "main-agent-context-continuation-request.json").unlink()
    reconcile_interrupted_context_registration(run)
    assert read_json(run / "business-result.json")["disposition"] == "machine_evidence_pending"
    assert not any(item["stage"] == "final_agent_review" for item in read_json(run / "agent-continuation-notices.json")["notices"])
    assert read_json(run / "main-agent-context-continuation-request.json")["supplement_ids"] == ["fact-1"]
    assert ready["agentContinuationNotice"]["noticeId"] not in json.dumps(read_json(run / "agent-continuation-notices.json"), ensure_ascii=False)


def test_consumed_reviewer_notice_is_removed_when_manifest_write_is_retried(repository, tmp_path, monkeypatch):
    import core.task_snapshot_review.agent_review_summary_artifacts as artifacts
    run, reviewers, fixer = prepared_review(repository, tmp_path)
    resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    notice = next(item for item in read_json(run / "agent-continuation-notices.json")["notices"] if item["stage"] == "reviewer_findings_available")
    original = artifacts.write_json
    def crash_manifest(path, content):
        if Path(path).name == "agent-continuation-notices.json":
            raise OSError("清单写出中断")
        return original(path, content)
    monkeypatch.setattr(artifacts, "write_json", crash_manifest)
    with pytest.raises(OSError, match="清单写出"):
        acknowledge_agent_review_notice(run, notice["noticeId"])
    monkeypatch.setattr(artifacts, "write_json", original)
    assert any(item["noticeId"] == notice["noticeId"] for item in read_json(run / "agent-continuation-notices.json")["notices"])
    artifacts.publish_reviewer_findings_for_parallel_review(run, read_json(run / "run.json"), read_json(run / "reviewer-findings.json")["findings"])
    assert all(item["noticeId"] != notice["noticeId"] for item in read_json(run / "agent-continuation-notices.json")["notices"])
    assert acknowledge_agent_review_notice(run, notice["noticeId"])["notice"] == notice


def test_unassigned_new_finding_is_counted_when_repair_budget_is_exhausted(repository, tmp_path):
    run, reviewers, fixer = prepared_review(repository, tmp_path, maximum_rounds=1)
    Path(fixer["command"][1]).write_text(
        "import json,os,pathlib\n"
        "r=json.loads(pathlib.Path(os.environ['RVF_ROLE_INPUT']).read_text())\n"
        "print(json.dumps({'status':'completed','dispositions':[{'finding_id':f['finding_id'],'status':'false_positive','reason':'原发现不成立'} for f in r['findings']],"
        "'new_findings':[{'problem_key':'still-open','path':'task.py','message':'预算耗尽后仍须交给主会话的新问题','severity':'high','scope_relation':'in_task_scope'}]}))\n")
    result = resume_review(run, reviewer_configs=[reviewers, reviewers], fixer_config=fixer)
    assert result["status"] == "fix_incomplete"
    assert result["disposition"] == "agent_judgment_required"
    assert len(read_json(run / "findings.json")["findings"]) == 2
    summary = (run / "agent-review-summary.md").read_text()
    assert "预算耗尽后仍须交给主会话的新问题" in summary
    assert "待处理" in summary
    assert "未解决 1 项" in result["agentContinuationNotice"]["text"]
    assert "未解决 0 项" not in result["agentContinuationNotice"]["text"]
