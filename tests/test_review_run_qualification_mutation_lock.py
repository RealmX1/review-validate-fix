"""真实 canonical writer 与消费者的同 inode flock 协议验收。"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest

from test_user_delegated_elevated_issue_disposition import delegated_issue_fixture, run_delegation
from test_reviewed_task_candidate_evidence import SCRIPT_DIRECTORY, run_writer
from test_rvf_land_single_step_prepare_and_commit import land_environment

sys.path.insert(0, str(SCRIPT_DIRECTORY))
import rvf_elevated_issue_delegation as delegation
import write_reviewed_task_candidate_evidence as evidence
from core.review_run_qualification_mutation_lock import (
    QUALIFICATION_MUTATION_LOCK_RELATIVE_PATH,
    ReviewRunQualificationMutationLockBusy,
    review_run_qualification_mutation_lock,
)


WRITERS = (
    "prepare", "start", "stop", "apply", "awaiting_marker", "upsert", "merge",
    "review_result", "review_result_issue", "review_result_request", "review_result_rotation",
    "reviewer_plan", "scope", "scope_finalization", "validation", "handoff", "summary", "lifecycle_summary", "finalize_summary", "evidence",
)


@pytest.mark.parametrize("writer", WRITERS)
def test_real_canonical_writer_waits_for_consumer_lock_then_resumes(tmp_path: Path, writer: str) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    assert run_delegation(tmp_path, repo, run_dir, "record", request_file).returncode == 0
    assert run_writer(repo, run_dir).returncode == 0
    environment = {**land_environment(tmp_path), "HOME": str(tmp_path / "isolated-home")}
    log_root = tmp_path / "fix-issue-tracker-log-root"
    attempt_script = [sys.executable, str(SCRIPT_DIRECTORY / "rvf_fix_attempt.py")]
    common = ["--run-dir", str(run_dir), "--log-root", str(log_root)]

    def successful(arguments: list[str]) -> dict:
        completed = subprocess.run(arguments, env=environment, capture_output=True, text=True)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return json.loads(completed.stdout) if completed.stdout.strip() else {}

    prepared = None
    if writer in {"start", "stop", "apply", "awaiting_marker"}:
        prepared = successful([*attempt_script, "prepare", "--repo", str(repo), "--issue-id", "RVF-002", *common])
        if writer == "apply":
            (Path(prepared["worktree_path"]) / "tracked.txt").write_text("fixed under the real writer protocol\n")
            successful([*attempt_script, "stop", "--attempt-id", prepared["attempt_id"], "--status", "fixed", *common])

    stdin_path = tmp_path / "writer-standard-input.json"
    stdin_path.write_text("{}")
    if writer == "prepare":
        arguments = [*attempt_script, "prepare", "--repo", str(repo), "--issue-id", "RVF-002", *common]
    elif writer in {"start", "stop", "apply"}:
        arguments = [*attempt_script, writer, "--attempt-id", prepared["attempt_id"], *common]
        if writer == "stop":
            arguments.extend(["--status", "false_positive"])
        if writer == "apply":
            arguments.extend(["--target-repo", str(repo)])
    elif writer == "upsert":
        arguments = [sys.executable, str(SCRIPT_DIRECTORY / "rvf_fix_issue.py"), "upsert", "--repo", str(repo),
                     "--issue-file", str(run_dir / "artifacts/fix-issues/RVF-002.json"), *common]
    elif writer == "merge":
        payload = json.loads((run_dir / "artifacts/merge/merge_dispositions.json").read_bytes())
        decisions = {"dispositions": [{name: entry[name] for name in ("reviewer_id", "index", "disposition", "issue_id")} for entry in payload["dispositions"]]}
        stdin_path.write_text(json.dumps(decisions))
        arguments = [sys.executable, str(SCRIPT_DIRECTORY / "rvf_merge_dispositions.py"), "write", "--run-dir", str(run_dir)]
    elif writer in {"review_result", "review_result_issue", "review_result_request"}:
        command = {"review_result": "no-issues", "review_result_issue": "issue", "review_result_request": "lock-request"}[writer]
        arguments = [sys.executable, str(SCRIPT_DIRECTORY / "write_review_result.py"), command, "--out",
                     str(run_dir / "artifacts/reviewers/reviewer-b/review-result.json")]
        arguments.extend({
            "review_result": ["--audit-summary", "资格锁下更新实际 reviewer 结果"],
            "review_result_issue": ["--path", "tracked.txt", "--line", "1", "--message", "资格锁下追加真实 finding", "--kind", "REAL", "--severity", "medium"],
            "review_result_request": ["--name", "qualification-lock", "--command", "python --version", "--reason", "资格锁下更换 request 结果"],
        }[writer])
    elif writer == "validation":
        arguments = [sys.executable, str(SCRIPT_DIRECTORY / "validation_result_cache.py"), "run", "--repo", str(repo),
                     "--rvf-run-dir", str(run_dir), "--name", "qualification-lock-protocol", "--", sys.executable, "-c", "print('真实验证完成')"]
    elif writer == "evidence":
        arguments = [sys.executable, str(SCRIPT_DIRECTORY / "write_reviewed_task_candidate_evidence.py"), "--repo", str(repo), "--run-dir", str(run_dir)]
    else:
        bootstrap = "import sys; from pathlib import Path; sys.path.insert(0, " + repr(str(SCRIPT_DIRECTORY)) + "); import _rvf_pyroot; "
        if writer == "awaiting_marker":
            body = ("from rvf_fix_attempt import arm_fix_attempt_subagent_awaiting_marker; "
                    + f"print(arm_fix_attempt_subagent_awaiting_marker(run_dir=Path({str(run_dir)!r}), attempt_id={prepared['attempt_id']!r}, main_agent_session_id='isolated-lock-protocol'))")
        elif writer == "review_result_rotation":
            successful([sys.executable, str(SCRIPT_DIRECTORY / "write_review_result.py"), "lock-request", "--out",
                        str(run_dir / "artifacts/reviewers/reviewer-b/review-result.json"), "--name", "qualification-lock",
                        "--command", "python --version", "--reason", "准备正式 request 轮转验收"])
            body = ("from dispatch_reviewers import _rotate_review_request_leg_artifacts; "
                    + f"print(_rotate_review_request_leg_artifacts({{'reviewers': [{{'reviewer_id': 'reviewer-b'}}]}}, [0], Path({str(run_dir / 'artifacts')!r})))")
        elif writer == "reviewer_plan":
            body = ("from dispatch_reviewers import write_canonical_reviewer_plan; import json; "
                    + f"path=Path({str(run_dir / 'artifacts/reviewers/reviewer-plan.json')!r}); "
                    + "payload=json.loads(path.read_text()); payload['qualification_lock_protocol']='completed'; print(write_canonical_reviewer_plan(path, payload))")
        elif writer in {"scope_finalization", "finalize_summary"}:
            module = "rvf_agent_decision_cli" if writer == "scope_finalization" else "rvf_run_finalize"
            relative_path = "artifacts/inputs/scope.contract.json" if writer == "scope_finalization" else "summary.json"
            body = (f"from {module} import _atomic_write_json; import json; "
                    + f"path=Path({str(run_dir / relative_path)!r}); payload=json.loads(path.read_text()); "
                    + "payload['qualification_lock_protocol']='completed'; print(_atomic_write_json(path, payload))")
        elif writer == "lifecycle_summary":
            body = ("from core.run_ledger.review_run_lifecycle import merge_review_run_summary; "
                    + f"print(merge_review_run_summary(Path({str(run_dir)!r}), "
                    + "{'run_id':'rvf-candidate-evidence-test','qualification_lock_protocol':'completed'}))")
        elif writer == "scope":
            body = ("from prepare_review_run import write_scope_contract; "
                    + f"run=Path({str(run_dir)!r}); "
                    + "print(write_scope_contract(path=run/'artifacts/inputs/scope.contract.json', run_id='rvf-candidate-evidence-test', "
                    + f"repo=Path({str(repo)!r}), created_at='2026-10-25T00:00:00+00:00', scope_mode='custom', "
                    + "primary_files=['tracked.txt'], background_files=[], protected_files=[], canonical_issues=[], fix_allowlist=['tracked.txt'], excluded_path_prefixes=[], "
                    + "start_snapshot_path=run/'artifacts/before-workspace-snapshot.json', review_packet_path=run/'artifacts/review-packet.md', "
                    + "session_manifest_path=None, scope_of_work_path=None, review_packet_metadata_path=run/'artifacts/review-packet.metadata.json'))")
        else:
            body = ("from core.run_ledger.run_ledger import RunLedger; "
                    + f"ledger=RunLedger(component='lock-protocol', run_id='rvf-candidate-evidence-test', repo={str(repo)!r}, run_dir={str(run_dir)!r}); ")
            body += ("print(ledger.artifact('handoff.md', (ledger.run_dir/'artifacts/handoff.md').read_text()+'\\n资格锁下发布\\n'))"
                     if writer == "handoff" else "print(ledger.summary(status='completed', reason_code='qualification_lock_protocol'))")
        arguments = [sys.executable, "-c", bootstrap + body]

    lock_path = run_dir / QUALIFICATION_MUTATION_LOCK_RELATIVE_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    inputs_before = evidence.candidate_qualification_artifact_hashes(run_dir)
    with lock_path.open("a") as consumer_lock, stdin_path.open() as stdin_handle:
        fcntl.flock(consumer_lock, fcntl.LOCK_EX)
        process = subprocess.Popen(arguments, env=environment, stdin=stdin_handle, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        observed_stderr = []
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                ready, _, _ = select.select([process.stderr], [], [], 0.1)
                if ready:
                    line = process.stderr.readline()
                    observed_stderr.append(line)
                    if "等待 RVF run 资格锁" in line:
                        break
                if process.poll() is not None:
                    break
            assert any("等待 RVF run 资格锁" in line and str(lock_path) in line for line in observed_stderr), "真实 writer 未进入同锁等待：" + "".join(observed_stderr)
            assert process.poll() is None, "消费者锁持有期间 writer 已越过门禁"
            assert evidence.candidate_qualification_artifact_hashes(run_dir) == inputs_before
        finally:
            fcntl.flock(consumer_lock, fcntl.LOCK_UN)
            stdout, stderr = process.communicate(timeout=40)
    result = {"writer": writer, "argv": arguments, "blocked_before_canonical_mutation": True,
              "resumed_after_consumer_unlock": process.returncode == 0, "returncode": process.returncode,
              "stdout": stdout, "stderr": "".join(observed_stderr) + stderr}
    (tmp_path / "writer-lock-protocol-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    assert process.returncode == 0, stdout + stderr
    if writer == "prepare":
        with pytest.raises(ValueError):
            evidence.validated_reviewed_task_candidate_evidence(repo)


def test_qualification_lock_reentry_does_not_release_the_outer_flock(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    with review_run_qualification_mutation_lock(run_dir):
        with review_run_qualification_mutation_lock(run_dir):
            pass
        script = "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
        blocked = subprocess.run([sys.executable, "-c", script, str(run_dir / QUALIFICATION_MUTATION_LOCK_RELATIVE_PATH)], capture_output=True)
        assert blocked.returncode != 0
    assert subprocess.run([sys.executable, "-c", script, str(run_dir / QUALIFICATION_MUTATION_LOCK_RELATIVE_PATH)], capture_output=True).returncode == 0


def test_qualification_lock_timeout_preserves_the_existing_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "artifacts/elevated-issue-delegation/mutation.lock"
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("a") as consumer_lock:
        fcntl.flock(consumer_lock, fcntl.LOCK_EX)
        with pytest.raises(ReviewRunQualificationMutationLockBusy):
            with review_run_qualification_mutation_lock(tmp_path, timeout_seconds=0.05):
                pytest.fail("不得因等待超时夺锁")


def test_recovery_budget_reopens_automatically_when_the_real_owner_releases_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    original_lock = review_run_qualification_mutation_lock
    monkeypatch.setattr(delegation, "review_run_qualification_mutation_lock", lambda directory: original_lock(directory, timeout_seconds=0.03))
    lock_path = run_dir / QUALIFICATION_MUTATION_LOCK_RELATIVE_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as consumer_lock:
        fcntl.flock(consumer_lock, fcntl.LOCK_EX)
        for _ in range(3):
            assert delegation.resume_delegation(repo, run_dir, request_file)["status"] == "waiting_for_machine_evidence"
        assert delegation.resume_delegation(repo, run_dir, request_file)["automatic_retry_budget_exhausted"] is True
    recovered = delegation.resume_delegation(repo, run_dir, request_file)
    assert recovered["status"] == "accepted"
    assert len(delegation.accepted_delegations(repo, run_dir)) == 1
