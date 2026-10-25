"""Land 成功与拒绝结果必须在同一正常写入 reservation 内持久化。"""
from pathlib import Path
import json
import os
import sys

import pytest

from test_rvf_land_single_step_prepare_and_commit import (
    LAND_TEST_COMMIT_MESSAGE,
    SCRIPT_DIRECTORY,
    git,
    land_environment,
    prepare,
    reviewed_dirty_run,
)

sys.path.insert(0, str(SCRIPT_DIRECTORY))
import independent_backup_route  # noqa: E402
import rvf_land  # noqa: E402


@pytest.mark.parametrize("outcome", ["committed", "scope_refusal", "stale_plan"])
def test_competing_backup_waits_until_land_result_is_persisted(tmp_path: Path, monkeypatch, outcome: str) -> None:
    repo, run_directory, _counter = reviewed_dirty_run(tmp_path)
    assert prepare(tmp_path, repo, run_directory).returncode == 0
    expected_exit_code = 0
    if outcome == "scope_refusal":
        (repo / "unrelated.txt").write_text("其他会话的工作\n", encoding="utf-8")
        expected_exit_code = 12
    elif outcome == "stale_plan":
        git(repo, "commit", "--allow-empty", "-qm", "test: 改变 prepare 后的 HEAD")
        expected_exit_code = 20
    for name in list(os.environ):
        if name.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_")):
            monkeypatch.delenv(name)
    for name, value in land_environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    message_path = tmp_path / "land-commit-message.txt"
    message_path.write_text(LAND_TEST_COMMIT_MESSAGE, encoding="utf-8")
    authorization_path = tmp_path / "synthetic-authorization.md"
    authorization_path.write_text("隔离夹具：仅用于验证接管与正常登记互斥。\n", encoding="utf-8")
    original_persistence = rvf_land.persist_land_commit_result
    observations = []

    def persist_while_competitor_claims(run_dir, repo_root, result):
        status = independent_backup_route.claim_backup_route(
            repo_root, "land-result-competitor", tmp_path / "backup-route-artifacts", authorization_path,
            "隔离验证正常结果登记与接管的交错", run_dir, "land",
        )
        observations.append(status)
        assert status["status"] == "waiting_for_normal_mutations", status
        reservations = [json.loads(Path(path).read_text()) for path in status["normal_mutation_reservations"]]
        assert any(record["original_run_directory"] == str(run_dir) for record in reservations)
        with pytest.raises(independent_backup_route.BackupRouteOwned, match="原写入尚未结束"):
            independent_backup_route.assert_backup_route_ready(repo_root, "land-result-competitor")
        original_persistence(run_dir, repo_root, result)
        assert independent_backup_route.inspect_backup_route(repo_root)["status"] == "waiting_for_normal_mutations"

    monkeypatch.setattr(rvf_land, "persist_land_commit_result", persist_while_competitor_claims)
    exit_code = rvf_land.main([
        "commit", "--repo", str(repo), "--run-dir", str(run_directory), "--message-file", str(message_path),
    ])
    assert exit_code == expected_exit_code
    assert len(observations) == 1
    persisted = json.loads((run_directory / "artifacts/rvf-land-commit-result.json").read_text())
    assert persisted["result"]["exit_code"] == expected_exit_code
    assert persisted["head_oid_after_commit"] == git(repo, "rev-parse", "HEAD")
    assert independent_backup_route.inspect_backup_route(repo)["status"] == "backup_active"
    independent_backup_route.assert_backup_route_ready(repo, "land-result-competitor")
    persisted_bytes = (run_directory / "artifacts/rvf-land-commit-result.json").read_bytes()
    for stage in ("review", "fix_validate", "land"):
        independent_backup_route.record_stage_fact(
            repo, "land-result-competitor", stage, authorization_path,
            git(repo, "rev-parse", "HEAD") if stage == "land" else None, None,
        )
    closed = independent_backup_route.finish_backup_route(repo, "land-result-competitor", authorization_path)
    assert closed["status"] == "backup_closed"
    assert rvf_land.main([
        "commit", "--repo", str(repo), "--run-dir", str(run_directory), "--message-file", str(message_path),
    ]) == 2
    assert (run_directory / "artifacts/rvf-land-commit-result.json").read_bytes() == persisted_bytes


def test_land_persistence_failure_retains_unknown_reservation(tmp_path: Path, monkeypatch) -> None:
    repo, run_directory, _counter = reviewed_dirty_run(tmp_path)
    assert prepare(tmp_path, repo, run_directory).returncode == 0
    for name, value in land_environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    message_path = tmp_path / "land-commit-message.txt"
    message_path.write_text(LAND_TEST_COMMIT_MESSAGE, encoding="utf-8")

    def fail_persistence(*arguments):
        raise OSError("隔离夹具：结果持久化不可写")

    monkeypatch.setattr(rvf_land, "persist_land_commit_result", fail_persistence)
    assert rvf_land.main([
        "commit", "--repo", str(repo), "--run-dir", str(run_directory), "--message-file", str(message_path),
    ]) == 2
    reservations = list((independent_backup_route.control_directory(repo) / "normal-mutations").glob("*.json"))
    assert reservations, "已发生 commit 而登记失败时必须留存结果未知的 reservation"
    assert all(json.loads(path.read_text())["outcome"] == "unknown" for path in reservations)
    assert git(repo, "show", "HEAD:tracked.txt") == "candidate\nreviewed change"
