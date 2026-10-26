#!/usr/bin/env python3
"""实际运行指定消费者；缺少实现或任一场景不通过均失败，不使用 skip。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tests"))

from test_user_delegated_elevated_issue_disposition import delegated_issue_fixture, run_delegation
from test_reviewed_task_candidate_evidence import SCRIPT_DIRECTORY, canonical_bytes, create_completed_run, git, repository, run_writer
from test_reviewed_candidate_completion_qualification import completed_machine_issue_run
from test_rvf_land_single_step_prepare_and_commit import commit, land_environment, prepare


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consumer-script", type=Path, required=True)
    parser.add_argument("--artifacts-directory", type=Path, required=True)
    args = parser.parse_args()
    consumer_script = args.consumer_script.resolve(strict=True)
    artifact_directory = args.artifacts_directory.resolve()
    artifact_directory.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    def record_actual_expansion_patch(run_dir: Path, repo: Path) -> None:
        patch = subprocess.run(["git", "diff", "--no-index", "--", os.devnull, "scope-expansion-test.txt"],
                               cwd=repo, capture_output=True)
        if patch.returncode != 1:
            raise AssertionError("隔离扩展 fixture 未产生真实 Git patch")
        (run_dir / "artifacts/fix-attempts/attempt-RVF-001-1/fix.patch").write_bytes(patch.stdout)

    def inspect(label: str, fixture_directory: Path, repo: Path, *, should_accept: bool) -> None:
        environment = {**land_environment(fixture_directory), "HOME": str(fixture_directory / "isolated-home")}
        completed = subprocess.run([sys.executable, str(consumer_script), "inspect-reviewed-candidate-evidence", "--task-worktree", str(repo)],
                                   capture_output=True, text=True, env=environment)
        (artifact_directory / f"{label}-consumer.stdout.json").write_text(completed.stdout)
        (artifact_directory / f"{label}-consumer.stderr.log").write_text(completed.stderr)
        payload = (json.loads(completed.stdout) if completed.stdout.strip().startswith("{") else
                   dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line))
        accepted = completed.returncode == 0 and payload.get("REVIEWED_CANDIDATE_EVIDENCE_STATUS") == "VERIFIED"
        # 消费者的键采用现有公开 inspector 协议。
        results.append({"scenario": label, "consumer_exit_code": completed.returncode, "expected_acceptance": should_accept,
                        "actual_acceptance": accepted, "consumer_result": payload, "passed": accepted == should_accept})
        if accepted != should_accept:
            raise AssertionError(f"{label}: exit={completed.returncode}, stdout={completed.stdout}, stderr={completed.stderr}")

    try:
        for version in (1, 2, 3):
            fixture = artifact_directory / f"v{version}-isolated-fixture"
            fixture.mkdir()
            if version == 1:
                repo = repository(fixture / "repo")
                run_dir = create_completed_run(fixture, repo)
                written = run_writer(repo, run_dir)
                assert written.returncode == 0, written.stderr
            elif version == 2:
                repo, run_dir = completed_machine_issue_run(fixture, expansion_paths=["scope-expansion-test.txt"])
                record_actual_expansion_patch(run_dir, repo)
                assert prepare(fixture, repo, run_dir).returncode == 0
                landed = commit(fixture, repo, run_dir)
                assert landed.returncode == 0, landed.stdout
            else:
                repo, run_dir, request_file = delegated_issue_fixture(fixture)
                recorded = run_delegation(fixture, repo, run_dir, "record", request_file)
                assert recorded.returncode == 0, recorded.stdout
                written = run_writer(repo, run_dir)
                assert written.returncode == 0, written.stderr
            inspect(f"v{version}-valid", fixture, repo, should_accept=True)
            if version == 3:
                evidence_path = Path(git(repo, "rev-parse", "--absolute-git-dir")) / "rvf/evidence/reviewed-task-candidate-evidence.json"
                original = evidence_path.read_bytes()
                evidence_payload = json.loads(original)
                for mutation in ("counts", "target", "downgrade"):
                    modified = json.loads(original)
                    if mutation == "counts":
                        modified["review"]["outcome_counts"]["fixed"] += 1
                    elif mutation == "target":
                        modified["review"]["user_delegated_elevated_issues"][0]["follow_up"]["task_id"] = "other-task"
                    else:
                        modified["schema_version"] = 1
                        modified["review"]["conclusion"] = "fixes_applied_and_validated"
                    encoded = canonical_bytes(modified)
                    evidence_path.write_bytes(encoded)
                    Path(f"{evidence_path}.sha256").write_text(hashlib.sha256(encoded).hexdigest() + "\n")
                    inspect(f"v3-{mutation}-rejected", fixture, repo, should_accept=False)
                evidence_path.write_bytes(original)
                Path(f"{evidence_path}.sha256").write_text(hashlib.sha256(original).hexdigest() + "\n")
                receipt_path = Path(evidence_payload["review"]["user_delegated_elevated_issues"][0]["receipt_path"])
                receipt_path.write_bytes(receipt_path.read_bytes() + b" ")
                inspect("v3-receipt-tamper-rejected", fixture, repo, should_accept=False)

        fixture = artifact_directory / "v3-expansion-isolated-fixture"
        fixture.mkdir()
        repo, run_dir, request_file = delegated_issue_fixture(fixture, expansion_paths=["scope-expansion-test.txt"])
        record_actual_expansion_patch(run_dir, repo)
        recorded = run_delegation(fixture, repo, run_dir, "record", request_file)
        assert recorded.returncode == 0, recorded.stdout
        assert prepare(fixture, repo, run_dir).returncode == 0
        landed = commit(fixture, repo, run_dir)
        assert landed.returncode == 0, landed.stdout
        inspect("v3-expansion-valid", fixture, repo, should_accept=True)
        (run_dir / "artifacts/fix-attempts/attempt-RVF-001-1/fix.patch").write_text("tampered expansion patch\n")
        inspect("v3-expansion-tamper-rejected", fixture, repo, should_accept=False)
    except Exception as exc:
        results.append({"scenario": "execution_failure", "passed": False, "reason": str(exc)})
    report = {"schema": "rvf-user-delegation-cross-repository-contract-verification", "schema_version": 1,
              "producer_commit": git(REPOSITORY_ROOT, "rev-parse", "HEAD"), "producer_worktree": str(REPOSITORY_ROOT),
              "consumer_script": str(consumer_script), "consumer_script_sha256": hashlib.sha256(consumer_script.read_bytes()).hexdigest(),
              "python_executable": sys.executable, "command": sys.argv, "results": results,
              "passed": bool(results) and all(result["passed"] for result in results)}
    report_path = artifact_directory / "cross-repository-verification-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "report_path": str(report_path), "scenario_count": len(results)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
