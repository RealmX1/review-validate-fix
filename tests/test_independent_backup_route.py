"""独立备用路线的真实 Git、在途写入和重启恢复行为。"""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "plugins/review-validate-fix/skills/review-validate-fix/scripts/independent_backup_route.py"


@pytest.fixture
def route(tmp_path):
    spec = importlib.util.spec_from_file_location("independent_backup_route", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "repository"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q", "-b", "main")
    git("config", "user.name", "备用路线测试")
    git("config", "user.email", "backup-route@example.test")
    (repo / "source.txt").write_text("original\n")
    git("add", "source.txt")
    git("commit", "-qm", "初始化")
    authorization = tmp_path / "authorization.md"
    authorization.write_text("用户已授权评审与本地提交；测试夹具。\n")
    artifacts = tmp_path / "persistent-handoff"
    return module, repo, authorization, artifacts, git


def claim(route, operation="operation-a"):
    module, repo, authorization, artifacts, _ = route
    return module.claim_backup_route(repo, operation, artifacts, authorization, "测试正常工具不可用", None, "land")


def test_claim_blocks_new_normal_writes_and_preserves_original_failure(route):
    module, repo, _, artifacts, _ = route
    original = repo / "original-refusal.md"
    original.write_text("refused_fail_closed\n")
    assert claim(route)["status"] == "backup_active"
    with pytest.raises(module.BackupRouteOwned):
        with module.normal_worktree_mutation(repo, "land"):
            original.unlink()
    assert original.read_text() == "refused_fail_closed\n"
    assert (artifacts / "operation-a" / "operation.md").is_file()


def test_takeover_waits_for_a_writer_already_inside_its_mutation(route):
    module, repo, *_ = route
    with module.normal_worktree_mutation(repo, "apply"):
        assert claim(route)["status"] == "waiting_for_normal_mutations"
        with pytest.raises(module.BackupRouteOwned):
            module.assert_backup_route_ready(repo, "operation-a")
    assert module.assert_backup_route_ready(repo, "operation-a")["operation_id"] == "operation-a"


def test_restart_reuses_exact_operation_and_another_owner_cannot_take_over(route):
    module, repo, *_ = route
    claim(route)
    assert claim(route)["operation_id"] == "operation-a"
    with pytest.raises(module.BackupRouteOwned):
        claim(route, "operation-b")
    assert module.inspect_backup_route(repo)["operation_id"] == "operation-a"


def test_corrupt_ownership_fails_closed_without_requiring_result_parser(route):
    module, repo, *_ = route
    claim(route)
    (module.control_directory(repo) / "active-owner" / "identity.json").write_text("broken")
    with pytest.raises(module.BackupRouteOwned):
        with module.normal_worktree_mutation(repo, "commit"):
            pytest.fail("不得执行")


def test_completed_candidate_is_suppressed_only_until_content_changes(route):
    module, repo, _, artifacts, git = route
    claim(route)
    report = artifacts / "report.md"
    report.write_text("双评审、问题处置和当前内容验证已人工核实。\n")
    for stage in ["review", "fix_validate", "land"]:
        module.record_stage_fact(repo, "operation-a", stage, report, git("rev-parse", "HEAD") if stage == "land" else None, None)
    module.finish_backup_route(repo, "operation-a", report)
    assert module.should_suppress_normal_stop(repo)
    (repo / "source.txt").write_text("new implementation\n")
    assert not module.should_suppress_normal_stop(repo)
    with module.normal_worktree_mutation(repo, "new-run"):
        pass


def record_candidate_coverage_beyond_first_recovery_page(route):
    """真实完成候选排在第二页；前页为其他候选的完整历史记录。"""
    module, repo, *_ = route
    report = record_complete_delivery_fixture(route, operation_id="operation-zzz-target")
    result = module.finish_backup_route(repo, "operation-zzz-target", report)
    for index in range(module.STOP_COMPLETION_RECOVERY_OPERATION_LIMIT):
        operation_id = f"operation-{index:03}"
        module.durable_json(module.control_directory(repo) / "completed-operations" / (operation_id + ".json"),
                            {**result, "operation_id": operation_id, "content_fingerprint": "0" * 64})
    return result


def test_paginated_candidate_coverage_waits_then_reuses_verified_target_across_processes(route, monkeypatch):
    module, repo, *_ = route
    record_candidate_coverage_beyond_first_recovery_page(route)
    reads, read_completion = [], module.read_backup_operation_completion_evidence
    def record_completion_read(*args, **kwargs):
        reads.append(args[1])
        return read_completion(*args, **kwargs)
    monkeypatch.setattr(module, "read_backup_operation_completion_evidence", record_completion_read)
    assert module.should_suppress_normal_stop(repo) is True
    observation_path = module.control_directory(repo) / "stop-content-verification-observation.json"
    first = json.loads(observation_path.read_text())
    assert first["status"] == "unknown" and first["recovery_after_operation"]
    assert len(reads) == module.STOP_COMPLETION_RECOVERY_OPERATION_LIMIT
    restarted_query = (
        "import importlib.util,json,sys; from pathlib import Path; "
        "spec=importlib.util.spec_from_file_location('restarted_backup_route',sys.argv[1]); "
        "helper=importlib.util.module_from_spec(spec); spec.loader.exec_module(helper); "
        "print(json.dumps({'suppress':helper.should_suppress_normal_stop(Path(sys.argv[2]))}))"
    )
    restarted = json.loads(subprocess.check_output(
        [__import__("sys").executable, "-c", restarted_query, str(SCRIPT), str(repo)], text=True))
    assert restarted["suppress"] is True
    assert json.loads(observation_path.read_text())["status"] == "covered"
    reads.clear()
    for _ in range(3):
        assert module.should_suppress_normal_stop(repo) is True
        assert json.loads(observation_path.read_text())["status"] == "covered"
    assert reads == ["operation-zzz-target"] * 3


@pytest.mark.parametrize("previous_state", ["pending", "covered"])
@pytest.mark.parametrize("change", ["head", "content", "index"])
def test_candidate_changes_invalidate_coverage_cursor_and_verified_target(route, monkeypatch, previous_state, change):
    module, repo, _, _, git = route
    monkeypatch.setattr(module, "STOP_COMPLETION_RECOVERY_OPERATION_LIMIT", 2)
    record_candidate_coverage_beyond_first_recovery_page(route)
    assert module.should_suppress_normal_stop(repo) is True
    if previous_state == "covered":
        assert module.should_suppress_normal_stop(repo) is True
    observation_path = module.control_directory(repo) / "stop-content-verification-observation.json"
    previous = json.loads(observation_path.read_text())
    if change == "head":
        git("commit", "--allow-empty", "-qm", "fix: 新提交应重新核验覆盖")
    elif change == "content":
        (repo / "source.txt").write_text("尚未评审的新候选\n")
    else:
        blob = subprocess.check_output(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=b"new staged bytes\n").decode().strip()
        git("update-index", "--cacheinfo", "100644", blob, "source.txt")
    assert module.should_suppress_normal_stop(repo) is True
    current = json.loads(observation_path.read_text())
    assert current["status"] == "unknown" and current["recovery_after_operation"]
    assert current["candidate_content_fingerprint"] != previous["candidate_content_fingerprint"]
    assert module.should_suppress_normal_stop(repo) is False
    assert json.loads(observation_path.read_text())["status"] == "not_covered"


def test_cached_candidate_coverage_rechecks_source_and_exhausted_gaps_allow_new_review(route, monkeypatch):
    module, repo, _, artifacts, _ = route
    monkeypatch.setattr(module, "STOP_COMPLETION_RECOVERY_OPERATION_LIMIT", 2)
    record_candidate_coverage_beyond_first_recovery_page(route)
    assert module.should_suppress_normal_stop(repo) is True
    assert module.should_suppress_normal_stop(repo) is True
    completed = module.control_directory(repo) / "completed-operations/operation-zzz-target.json"
    delivery = artifacts / "operation-zzz-target/backup-route-delivery-result.json"
    saved_delivery = delivery.read_bytes()
    completed.write_text("损坏的缓存来源索引\n")
    delivery.write_text("暂不可读的缓存来源原件\n")
    assert module.should_suppress_normal_stop(repo) is False
    observation_path = module.control_directory(repo) / "stop-content-verification-observation.json"
    observation = json.loads(observation_path.read_text())
    assert observation["status"] == "unknown"
    assert not observation["recovery_after_operation"]
    delivery.write_bytes(saved_delivery)
    assert module.should_suppress_normal_stop(repo) is True
    assert module.should_suppress_normal_stop(repo) is True
    assert json.loads(observation_path.read_text())["status"] == "covered"


def test_finish_requires_actual_commit_and_all_required_stages(route):
    module, repo, _, artifacts, _ = route
    claim(route)
    report = artifacts / "report.md"
    report.write_text("证据\n")
    with pytest.raises(ValueError):
        module.record_stage_fact(repo, "operation-a", "land", report, "0" * 40, None)
    with pytest.raises(ValueError):
        module.finish_backup_route(repo, "operation-a", report)


def test_shell_created_marker_also_blocks_normal_writes(route):
    module, repo, *_ = route
    (module.control_directory(repo) / "active-owner").mkdir(parents=True)
    with pytest.raises(module.BackupRouteOwned):
        with module.normal_worktree_mutation(repo, "apply"):
            pytest.fail("不得执行")


def test_crashed_mutation_is_not_silently_discarded(route):
    module, repo, _, artifacts, _ = route
    reservations = module.control_directory(repo) / "normal-mutations"
    reservations.mkdir(parents=True)
    (reservations / "crashed.json").write_text(json.dumps({"pid": 99999999, "operation": "commit"}))
    assert claim(route)["status"] == "waiting_for_normal_mutations"
    proof = artifacts / "reconciliation.md"
    proof.write_text("已读取原提交结果和 Git 对象，提交实际没有发生。\n")
    module.reconcile_normal_mutation(repo, "operation-a", "crashed.json", proof)
    assert module.assert_backup_route_ready(repo, "operation-a")["operation_id"] == "operation-a"


def test_candidate_snapshot_preserves_index_and_binds_exact_selected_paths(route):
    module, repo, _, _, git = route
    claim(route)
    (repo / "source.txt").write_text("candidate\n")
    (repo / "background.txt").write_text("other session\n")
    before = (Path(git("rev-parse", "--absolute-git-dir")) / "index").read_bytes()
    snapshot = module.freeze_candidate(repo, "operation-a", ["source.txt"], git("rev-parse", "HEAD"))
    assert git("show", snapshot["candidate_commit"] + ":source.txt") == "candidate"
    assert "background.txt" not in git("ls-tree", "-r", "--name-only", snapshot["candidate_commit"])
    assert (Path(git("rev-parse", "--absolute-git-dir")) / "index").read_bytes() == before


def test_cli_options_after_action_are_not_swallowed_as_command(route):
    _, repo, *_ = route
    result = subprocess.run([__import__('sys').executable, str(SCRIPT), "status", "--repo", str(repo)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "normal"


def test_claim_never_publishes_an_incomplete_identity(route, monkeypatch):
    module, repo, *_ = route
    write = module.durable_json
    def fail_identity(path, value):
        if path.name == "identity.json":
            raise OSError("模拟身份文件刷盘失败")
        write(path, value)
    monkeypatch.setattr(module, "durable_json", fail_identity)
    with pytest.raises(OSError):
        claim(route)
    assert not module.should_suppress_normal_stop(repo)
    monkeypatch.setattr(module, "durable_json", write)
    assert claim(route)["status"] == "backup_active"


def test_dangling_ownership_marker_still_blocks_normal_writes(route):
    module, repo, *_ = route
    root = module.control_directory(repo)
    root.mkdir(parents=True)
    (root / "active-owner").symlink_to(root / "missing-generation")
    with pytest.raises(module.BackupRouteOwned):
        with module.normal_worktree_mutation(repo, "land"):
            pytest.fail("断开的接管链接不能被视为未接管")


def test_dead_launcher_does_not_authorize_stealing_its_live_child(route):
    import os
    import signal
    import sys
    module, repo, _, artifacts, _ = route
    launcher = subprocess.Popen([sys.executable, str(SCRIPT), "run-normal-mutation", "--repo", str(repo),
                                 "--", sys.executable, "-c", "import time; print('child-ready',flush=True); time.sleep(120)"],
                                stdout=subprocess.PIPE, text=True)
    group = None
    try:
        assert launcher.stdout.readline().strip() == "child-ready"
        reservation = next((module.control_directory(repo) / "normal-mutations").glob("*.json"))
        group = json.loads(reservation.read_text())["child_process_group_id"]
        launcher.kill()
        launcher.wait(timeout=10)
        claim(route)
        proof = artifacts / "reconciliation.md"
        proof.write_text("已查询父进程退出；子命令仍存活。\n")
        with pytest.raises(module.BackupRouteOwned, match="进程组"):
            module.reconcile_normal_mutation(repo, "operation-a", reservation.name, proof)
    finally:
        if group:
            os.killpg(group, signal.SIGKILL)
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=10)
        if launcher.stdout:
            launcher.stdout.close()


def test_manual_markdown_route_can_be_adopted_without_releasing_its_guard(route):
    module, repo, authorization, artifacts, _ = route
    marker = module.control_directory(repo) / "active-owner"
    marker.mkdir(parents=True)
    (marker / "manual-operation-id").write_text("operation-a\n")
    journal = artifacts / "manual-journal.md"
    journal.parent.mkdir(parents=True)
    journal.write_text("合成夹具：已核对原始 mkdir 命令、目录归属和现有写入全部结束。\n")
    result = module.adopt_manual_ownership(repo, "operation-a", artifacts, authorization,
                                         "辅助器恢复，读取原持久日志", None, "land", journal)
    assert marker.exists()
    assert result["status"] == "backup_active"
    with pytest.raises(module.BackupRouteOwned):
        with module.normal_worktree_mutation(repo, "land"):
            pytest.fail("补录不能开放竞写窗口")
    with pytest.raises(module.BackupRouteOwned):
        module.adopt_manual_ownership(repo, "another-operation", artifacts, authorization,
                                      "不能覆盖原 ID", None, "land", journal)


def test_snapshot_freezes_raw_bytes_without_git_clean_filters(route):
    module, repo, _, _, git = route
    claim(route)
    (repo / ".gitattributes").write_text("source.txt filter=rewrite\n")
    git("config", "filter.rewrite.clean", "sed s/candidate/rewritten/g")
    (repo / "source.txt").write_text("candidate\n")
    snapshot = module.freeze_candidate(repo, "operation-a", ["source.txt"], git("rev-parse", "HEAD"))
    assert git("show", snapshot["candidate_commit"] + ":source.txt") == "candidate"


def test_content_fingerprint_cannot_hide_new_bytes_behind_clean_filter(route):
    module, repo, _, _, git = route
    (repo / ".gitattributes").write_text("source.txt filter=rewrite\n")
    git("config", "filter.rewrite.clean", "sed s/new/old/g")
    (repo / "source.txt").write_text("old\n")
    before = module.worktree_content_fingerprint(repo)
    (repo / "source.txt").write_text("new\n")
    assert module.worktree_content_fingerprint(repo) != before


def test_real_land_orphan_git_still_blocks_reconciliation(route, tmp_path):
    """公开 command_commit 与既有完成 run 夹具验证真实 Git／hook 的存活区间。"""
    import os
    import signal
    import sys
    import time
    from test_rvf_land_single_step_prepare_and_commit import (
        LAND_TEST_COMMIT_MESSAGE,
        git as land_fixture_git,
        land_environment,
        prepare,
        reviewed_dirty_run,
    )
    module, _, authorization, artifacts, _ = route
    repo, original_run_directory, _ = reviewed_dirty_run(tmp_path)

    def git(*arguments):
        return land_fixture_git(repo, *arguments)

    route = (module, repo, authorization, artifacts, git)
    prepared = prepare(tmp_path, repo, original_run_directory)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    message_file = tmp_path / "orphan-git-land-commit-message.txt"
    message_file.write_text(LAND_TEST_COMMIT_MESSAGE, encoding="utf-8")
    before = git("rev-parse", "HEAD")
    ready, release = tmp_path / "hook-ready", tmp_path / "hook-release"
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text(f"#!{sys.executable}\nimport time\nfrom pathlib import Path\n"
                    f"Path({str(ready)!r}).touch()\n"
                    f"while not Path({str(release)!r}).exists(): time.sleep(.02)\n")
    hook.chmod(0o755)
    code = f'''import sys
sys.path.insert(0,{str(SCRIPT.parent)!r})
import rvf_land as land
arguments = land.build_argument_parser().parse_args([
 'commit', '--repo', {str(repo)!r}, '--run-dir', {str(original_run_directory)!r},
 '--message-file', {str(message_file)!r}])
raise SystemExit(land.command_commit(arguments))
'''
    parent = subprocess.Popen([sys.executable, "-c", code], cwd=repo, env=land_environment(tmp_path),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    groups = []
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists(), parent.communicate(timeout=5)
        reservation = next((module.control_directory(repo) / "normal-mutations").glob("*.json"))
        reservation_record = json.loads(reservation.read_text())
        assert reservation_record["operation"] == "rvf-land-commit"
        assert reservation_record["original_run_directory"] == str(original_run_directory)
        groups = reservation_record["child_process_group_ids"]
        assert groups
        parent.kill()
        parent.wait(timeout=5)
        assert claim(route)["status"] == "waiting_for_normal_mutations"
        proof = artifacts / "reconciliation.md"
        proof.write_text("合成对账：原父进程退出，Git 仍在阻塞 hook 内，尚不可释放。\n")
        with pytest.raises(module.BackupRouteOwned, match="进程组"):
            module.reconcile_normal_mutation(repo, "operation-a", reservation.name, proof)
        assert git("rev-parse", "HEAD") == before
        release.touch()
        deadline = time.monotonic() + 10
        while git("rev-parse", "HEAD") == before and time.monotonic() < deadline:
            time.sleep(.02)
        actual_commit = git("rev-parse", "HEAD")
        assert actual_commit != before
        assert git("show", "HEAD:tracked.txt") == "candidate\nreviewed change"
        while time.monotonic() < deadline:
            try:
                module.assert_child_process_groups_stopped(json.loads(reservation.read_text()))
                break
            except module.BackupRouteOwned:
                time.sleep(.02)
        proof.write_text("已核对孤儿 Git 已退出及真实提交 SHA；复用既有提交，不能重做。\n")
        module.reconcile_normal_mutation(repo, "operation-a", reservation.name, proof)
        assert module.assert_backup_route_ready(repo, "operation-a")["operation_id"] == "operation-a"
        assert git("rev-parse", "HEAD") == actual_commit
        assert git("rev-list", "--count", f"{before}..HEAD") == "1"
    finally:
        release.touch()
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        parent.communicate(timeout=5)


def test_inflight_base_landing_reserves_source_worktree_too(route, tmp_path):
    module, repo, authorization, artifacts, git = route
    task = tmp_path / "source-task"
    git("worktree", "add", "-qb", "source-task", str(task), "HEAD")
    tip = git("rev-parse", "HEAD")
    with module.normal_worktree_mutation(repo, "base-landing", source_task_tip=tip, source_task_worktree=task):
        state = module.claim_backup_route(task, "operation-a", artifacts, authorization, "在途来源", None, "base_sync")
        assert state["status"] == "waiting_for_normal_mutations"
        with pytest.raises(module.BackupRouteOwned):
            module.assert_backup_route_ready(task, "operation-a")
    assert module.assert_backup_route_ready(task, "operation-a")["operation_id"] == "operation-a"


def test_dispatch_claim_waits_for_full_dispatch_and_tracks_both_parallel_legs(route, tmp_path, monkeypatch):
    import sys
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    import dispatch_reviewers as dispatch
    import independent_backup_route as shared
    _, repo, authorization, artifacts, _ = route
    run = tmp_path / "original-run"
    run.mkdir()
    observed = {}
    import threading
    lock = threading.Lock()
    def claim_before_first_dispatch(*_, **kwargs):
        with lock:
            if not observed:
                observed.update(shared.claim_backup_route(repo, "operation-a", artifacts, authorization, "派发交错", run, "review"))
        return [sys.executable, "-c", "pass"]
    monkeypatch.setattr(dispatch, "require_prepared_run_review_start_boundary", lambda **kwargs: None)
    monkeypatch.setattr(dispatch, "_build_reviewer_command", claim_before_first_dispatch)
    monkeypatch.setattr(dispatch, "_leg_usage_limit", lambda *a: (False, None))
    monkeypatch.setattr(dispatch, "_leg_review_request_pending", lambda *a: False)
    def classify_leg(*_):
        paths = list((shared.control_directory(repo) / "normal-mutations").glob("*.json"))
        assert len(paths) == 1
        assert len(json.loads(paths[0].read_text())["child_process_group_ids"]) == 2
        with pytest.raises(shared.BackupRouteOwned):
            shared.assert_backup_route_ready(repo, "operation-a")
        return True
    monkeypatch.setattr(dispatch, "_leg_is_effective", classify_leg)
    plan = {"reviewers": [{"reviewer_id": "leg-a", "harness_id": "synthetic-a"},
                          {"reviewer_id": "leg-b", "harness_id": "synthetic-b"}], "warnings": []}
    dispatch.execute_plan(plan, repo=str(repo), review_packet=None, session_context=None, scope_contract=None,
                          run_id="synthetic-run", run_dir=str(run), artifacts_dir=run / "artifacts", max_fallback_rounds=0)
    assert observed["status"] == "waiting_for_normal_mutations"
    assert shared.assert_backup_route_ready(repo, "operation-a")["operation_id"] == "operation-a"


def test_completion_metadata_api_keeps_real_git_ownership_guard(route, tmp_path, monkeypatch):
    import sys
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    import independent_backup_route as shared
    import rvf_automatic_closeout_bridge as bridge
    _, repo, *_ = route
    claim(route)
    for operation in [bridge.freeze_completion_notification, bridge.publish_completion]:
        with pytest.raises(shared.BackupRouteOwned):
            operation(tmp_path / "run", {"repo": str(repo)})
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("path_form", ["absolute", "outside_symlink", "relative"])
def test_metadata_only_mode_cannot_hide_a_broken_git_identity(route, tmp_path, monkeypatch, path_form):
    module, repo, *_ = route
    assert module.repository_for_normal_metadata_mutation(tmp_path / "metadata-without-repository") is None
    nested = repo / "nested-source"
    nested.mkdir()
    selected = nested
    if path_form == "outside_symlink":
        selected = tmp_path / "outside-repository-alias"
        selected.symlink_to(nested, target_is_directory=True)
    elif path_form == "relative":
        monkeypatch.chdir(nested)
        selected = Path(".")
    (repo / ".git/config").write_text("[invalid config\n")
    with pytest.raises(ValueError):
        module.repository_for_normal_metadata_mutation(selected)


def record_complete_delivery_fixture(route, delivery_stage="land", original_run=None, base_ref=None, operation_id="operation-a"):
    """用真实 Git 事实完成指定阶段，供恢复与内容隔离回归复用。"""
    module, repo, authorization, artifacts, git = route
    module.claim_backup_route(repo, operation_id, artifacts, authorization, "隔离回归", original_run, delivery_stage)
    report = artifacts / "report.md"
    report.write_text("合成夹具：原始评审、验证与实际提交已经核对。\n")
    for stage in module.STAGES[:module.STAGES.index(delivery_stage) + 1]:
        module.record_stage_fact(repo, operation_id, stage, report,
                                 git("rev-parse", "HEAD") if stage in {"land", "base_sync"} else None,
                                 base_ref if stage == "base_sync" else None)
    return report


def test_consumed_original_run_rejects_a_new_backup_operation(route, tmp_path):
    module, repo, authorization, artifacts, _ = route
    original_run = tmp_path / "original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    assert module.consumed_run_record(repo, original_run).is_file()
    with pytest.raises(module.BackupRouteOwned, match="消费"):
        module.claim_backup_route(repo, "operation-b", artifacts, authorization, "不得重复交付", original_run, "land")
    assert not module.marker_present(module.control_directory(repo) / "active-owner")


@pytest.mark.parametrize("entry", ["root", "subdirectory", "symlink"])
def test_candidate_identity_and_snapshot_use_actual_worktree_root(route, tmp_path, entry):
    module, repo, authorization, artifacts, git = route
    nested = repo / "nested-source"
    nested.mkdir()
    alias = tmp_path / "repository-entry-alias"
    alias.symlink_to(nested, target_is_directory=True)
    selected = {"root": repo, "subdirectory": nested, "symlink": alias}[entry]
    (repo / "source.txt").write_text("仓库根目录的候选内容\n")
    assert module.worktree_content_fingerprint(selected) == module.worktree_content_fingerprint(repo)
    module.claim_backup_route(selected, "operation-a", artifacts, authorization, "仓库入口归一", None, "review")
    identity = module.operation_identity(selected, "operation-a")
    assert identity["worktree"] == str(repo.resolve())
    snapshot = module.freeze_candidate(selected, "operation-a", ["source.txt"], git("rev-parse", "HEAD"))
    assert git("show", snapshot["candidate_commit"] + ":source.txt") == "仓库根目录的候选内容"


def test_readonly_consumption_survives_commit_without_claiming_new_candidate_coverage(route, tmp_path):
    module, repo, _, _, git = route
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    result = module.finish_backup_route(repo, "operation-a", report)
    assert module.read_current_candidate_backup_coverage_evidence(repo)["status"] == "covered"
    git("commit", "--allow-empty", "-qm", "fix: 完成后提交改变 HEAD")
    root = module.control_directory(repo)
    before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    consumption = module.read_original_run_backup_consumption_evidence(repo, original_run)
    assert consumption["status"] == "consumed"
    assert consumption["result"] == result
    assert module.read_current_candidate_backup_coverage_evidence(repo)["status"] == "not_covered"
    assert {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("failure", ["missing", "corrupt", "unreadable", "completed_index_corrupt"])
def test_original_run_consumption_recovers_from_bound_completion_records(route, tmp_path, monkeypatch, failure):
    module, repo, *_ = route
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    consumed = module.consumed_run_record(repo, original_run)
    if failure == "missing":
        consumed.unlink()
    elif failure == "corrupt":
        consumed.write_text("损坏的消费索引\n")
    elif failure == "completed_index_corrupt":
        (module.control_directory(repo) / "completed-operations/operation-a.json").write_text("broken")
    else:
        read_text = Path.read_text
        def deny_consumption_read(path, *args, **kwargs):
            if path == consumed:
                raise PermissionError("模拟消费索引读取受限")
            return read_text(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", deny_consumption_read)
    evidence = module.reconcile_original_run_backup_consumption(repo, original_run)
    assert evidence["status"] == "consumed"
    assert evidence["result"]["operation_id"] == "operation-a"
    if failure in {"missing", "corrupt"}:
        assert json.loads(consumed.read_text())["status"] == "backup_closed"
    if failure == "corrupt":
        originals = list((module.control_directory(repo) / "recovered-consumption-record-originals").glob("*"))
        assert [path.read_text() for path in originals] == ["损坏的消费索引\n"]


def test_consumption_is_bound_to_exact_run_and_independent_worktree(route, tmp_path):
    module, repo, _, _, git = route
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    other_worktree = tmp_path / "other-worktree"
    git("worktree", "add", "--detach", str(other_worktree), "HEAD")
    assert module.worktree_content_fingerprint(other_worktree) == module.worktree_content_fingerprint(repo)
    assert module.control_directory(other_worktree) != module.control_directory(repo)
    assert module.read_original_run_backup_consumption_evidence(other_worktree, original_run)["status"] == "not_consumed"
    assert module.read_original_run_backup_consumption_evidence(repo, tmp_path / "another-run")["status"] == "not_consumed"
    borrowed = module.consumed_run_record(other_worktree, original_run)
    module.durable_json(borrowed, json.loads(module.consumed_run_record(repo, original_run).read_text()))
    assert module.read_original_run_backup_consumption_evidence(other_worktree, original_run)["status"] == "unknown"
    assert module.should_suppress_normal_stop(other_worktree) is False


@pytest.mark.parametrize("interruption", ["completion_index", "consumption_index", "owner_archive"])
def test_stop_recovers_interrupted_finish_after_head_and_content_change(route, tmp_path, monkeypatch, interruption):
    module, repo, _, artifacts, git = route
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    write_json, rename = module.durable_json, module.os.rename
    def interrupted_write(path, value):
        if ((interruption == "completion_index" and path.parent.name == "completed-operations")
                or (interruption == "consumption_index" and path.parent.name == "consumed-original-runs")):
            raise OSError("模拟完成写入中断")
        return write_json(path, value)
    def interrupted_archive(source, destination):
        if interruption == "owner_archive" and Path(source).name == "active-owner":
            raise OSError("模拟归档中断")
        return rename(source, destination)
    monkeypatch.setattr(module, "durable_json", interrupted_write)
    monkeypatch.setattr(module.os, "rename", interrupted_archive)
    with pytest.raises(OSError):
        module.finish_backup_route(repo, "operation-a", report)
    original_result = (artifacts / "operation-a/backup-route-delivery-result.json").read_bytes()
    git("commit", "--allow-empty", "-qm", "fix: 中断后 HEAD 改变")
    (repo / "source.txt").write_text("中断后新增未审内容\n")
    monkeypatch.setattr(module, "durable_json", write_json)
    monkeypatch.setattr(module.os, "rename", rename)
    assert module.should_suppress_normal_stop(repo) is False
    assert module.reconcile_original_run_backup_consumption(repo, original_run)["status"] == "consumed"
    assert not module.marker_present(module.control_directory(repo) / "active-owner")
    assert (artifacts / "operation-a/backup-route-delivery-result.json").read_bytes() == original_result


def test_missing_completion_evidence_stays_unknown_then_recovers_on_later_stop(route, tmp_path):
    module, repo, _, artifacts, _ = route
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    completed = module.control_directory(repo) / "completed-operations/operation-a.json"
    delivery = artifacts / "operation-a/backup-route-delivery-result.json"
    historical_bytes = delivery.read_bytes()
    completed.write_text("broken")
    delivery.write_text("broken")
    assert module.reconcile_original_run_backup_consumption(repo, original_run)["status"] == "unknown"
    delivery.write_bytes(historical_bytes)
    assert module.reconcile_original_run_backup_consumption(repo, original_run)["status"] == "consumed"
    observations = list(module.control_directory(repo).glob("stop-original-run-verification-*-history/*.json"))
    assert {json.loads(path.read_text())["status"] for path in observations} == {"unknown", "consumed"}


@pytest.mark.parametrize("failure", ["missing", "corrupt", "unreadable"])
def test_unrelated_run_does_not_inherit_another_operations_completion_gap(route, tmp_path, monkeypatch, failure):
    module, repo, _, artifacts, _ = route
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    completed = module.control_directory(repo) / "completed-operations/operation-a.json"
    delivery = artifacts / "operation-a/backup-route-delivery-result.json"
    completed.write_text("损坏的完成索引\n")
    if failure == "missing":
        delivery.unlink()
    elif failure == "corrupt":
        delivery.write_text("损坏的持久交付原件\n")
    else:
        read_text = Path.read_text
        def deny_delivery_read(path, *args, **kwargs):
            if path == delivery:
                raise PermissionError("模拟其他 run 的原件读取受限")
            return read_text(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", deny_delivery_read)
    evidence = module.reconcile_original_run_backup_consumption(repo, tmp_path / "another-run")
    assert evidence["status"] == "not_consumed"
    assert evidence["verification_gaps"] == []


@pytest.mark.parametrize("failure", ["missing", "corrupt", "unreadable"])
def test_bound_archived_run_missing_completion_stays_unknown_and_recovers(route, tmp_path, monkeypatch, failure):
    module, repo, _, artifacts, git = route
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    result = module.finish_backup_route(repo, "operation-a", report)
    module.consumed_run_record(repo, original_run).unlink()
    (module.control_directory(repo) / "completed-operations/operation-a.json").unlink()
    delivery = artifacts / "operation-a/backup-route-delivery-result.json"
    saved_delivery = delivery.read_bytes()
    if failure == "missing":
        delivery.unlink()
    elif failure == "corrupt":
        delivery.write_text("损坏的持久交付原件\n")
    else:
        read_text = Path.read_text
        def deny_delivery_read(path, *args, **kwargs):
            if path == delivery:
                raise PermissionError("模拟目标 run 的原件读取受限")
            return read_text(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", deny_delivery_read)
    git("commit", "--allow-empty", "-qm", "fix: 历史完成后新增候选")
    (repo / "source.txt").write_text("尚未评审的新内容\n")
    evidence = module.reconcile_original_run_backup_consumption(repo, original_run)
    assert evidence["status"] == "unknown"
    assert evidence["verification_gaps"]
    assert not module.consumed_run_record(repo, original_run).exists()
    monkeypatch.undo()
    delivery.write_bytes(saved_delivery)
    recovered = module.reconcile_original_run_backup_consumption(repo, original_run)
    assert recovered["status"] == "consumed"
    assert recovered["result"] == result
    assert module.read_current_candidate_backup_coverage_evidence(repo)["status"] == "not_covered"


@pytest.mark.parametrize("identity_field", ["operation_id", "worktree_git_directory", "original_run_directory", "artifacts_directory"])
def test_unrelated_run_exclusion_requires_exact_archived_operation_identity(route, tmp_path, identity_field):
    module, repo, _, artifacts, _ = route
    report = record_complete_delivery_fixture(route, original_run=tmp_path / "rvf-original-run")
    module.finish_backup_route(repo, "operation-a", report)
    (module.control_directory(repo) / "completed-operations/operation-a.json").unlink()
    (artifacts / "operation-a/backup-route-delivery-result.json").unlink()
    archive_identity = module.control_directory(repo) / "closed-ownership-markers/operation-a/identity.json"
    identity = json.loads(archive_identity.read_text())
    identity[identity_field] = "不能用于排除目标 run 的身份"
    module.durable_json(archive_identity, identity)
    assert module.reconcile_original_run_backup_consumption(repo, tmp_path / "another-run")["status"] == "unknown"


def test_completion_recovery_is_bounded_and_resumes_persisted_cursor(route, tmp_path, monkeypatch):
    module, repo, *_ = route
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run, operation_id="operation-zzz-target")
    result = module.finish_backup_route(repo, "operation-zzz-target", report)
    module.consumed_run_record(repo, original_run).unlink()
    for index in range(5):
        operation = f"operation-{index}"
        module.durable_json(module.control_directory(repo) / "completed-operations" / (operation + ".json"),
                            {**result, "operation_id": operation, "original_run_directory": str(tmp_path / f"other-run-{index}")})
    monkeypatch.setattr(module, "STOP_COMPLETION_RECOVERY_OPERATION_LIMIT", 2)
    first = module.reconcile_original_run_backup_consumption(repo, original_run)
    assert first["status"] == "unknown"
    assert first["recovery_after_operation"] == "operation-1"
    restarted_query = (
        "import importlib.util,json,sys; from pathlib import Path; "
        "spec=importlib.util.spec_from_file_location('restarted_backup_route',sys.argv[1]); "
        "helper=importlib.util.module_from_spec(spec); spec.loader.exec_module(helper); "
        "helper.STOP_COMPLETION_RECOVERY_OPERATION_LIMIT=2; "
        "print(json.dumps(helper.reconcile_original_run_backup_consumption(Path(sys.argv[2]),Path(sys.argv[3]))))"
    )
    # 新解释器只凭持久游标接续，不共享当前进程内的对象或状态。
    second = json.loads(subprocess.check_output(
        [__import__("sys").executable, "-c", restarted_query, str(SCRIPT), str(repo), str(original_run)], text=True))
    assert second["status"] == "unknown"
    assert second["recovery_after_operation"] == "operation-3"
    assert module.reconcile_original_run_backup_consumption(repo, original_run)["status"] == "consumed"


def test_completion_recovery_keeps_earlier_page_unknown_until_full_recheck(route, tmp_path, monkeypatch):
    module, repo, _, artifacts, _ = route
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    result = module.finish_backup_route(repo, "operation-a", report)
    module.consumed_run_record(repo, original_run).unlink()
    completed = module.control_directory(repo) / "completed-operations/operation-a.json"
    completed.write_text("broken")
    delivery = artifacts / "operation-a/backup-route-delivery-result.json"
    saved_delivery = delivery.read_bytes()
    delivery.write_text("broken")
    module.durable_json(module.control_directory(repo) / "completed-operations/operation-z.json",
                        {**result, "operation_id": "operation-z", "original_run_directory": str(tmp_path / "other-run")})
    monkeypatch.setattr(module, "STOP_COMPLETION_RECOVERY_OPERATION_LIMIT", 1)
    first = module.reconcile_original_run_backup_consumption(repo, original_run)
    assert first["status"] == "unknown" and first["recovery_after_operation"] == "operation-a"
    second = module.reconcile_original_run_backup_consumption(repo, original_run)
    assert second["status"] == "unknown" and second["recovery_after_operation"] is None
    assert second["verification_gaps"] == first["verification_gaps"]
    delivery.write_bytes(saved_delivery)
    assert module.reconcile_original_run_backup_consumption(repo, original_run)["status"] == "consumed"


def test_completed_artifact_cannot_recover_a_different_operation_identity(route, tmp_path):
    module, repo, _, artifacts, _ = route
    original_run = tmp_path / "rvf-original-run"
    report = record_complete_delivery_fixture(route, original_run=original_run)
    module.finish_backup_route(repo, "operation-a", report)
    (module.control_directory(repo) / "completed-operations/operation-a.json").unlink()
    delivery = artifacts / "operation-a/backup-route-delivery-result.json"
    result = json.loads(delivery.read_text())
    result["operation_id"] = "another-operation"
    module.durable_json(delivery, result)
    assert module.read_original_run_backup_consumption_evidence(repo, original_run)["status"] == "unknown"


def test_base_sync_rejects_unabsorbed_base_changes(route, tmp_path):
    module, repo, authorization, artifacts, git = route
    base = tmp_path / "base-worktree"
    git("worktree", "add", "-qb", "local-base", str(base), "HEAD")
    (base / "base-only.txt").write_text("未吸收的 base 内容\n")
    subprocess.run(["git", "-C", str(base), "add", "."], check=True)
    subprocess.run(["git", "-C", str(base), "commit", "-qm", "base 前进"], check=True)
    module.claim_backup_route(repo, "operation-a", artifacts, authorization, "隔离同步回归", None, "base_sync")
    report = artifacts / "report.md"
    report.write_text("合成同步核验材料\n")
    with pytest.raises(ValueError):
        module.record_stage_fact(repo, "operation-a", "base_sync", report, git("rev-parse", "HEAD"), "refs/heads/local-base")


def test_base_sync_first_finish_accepts_later_monotonic_base_changes(route, tmp_path):
    module, repo, _, _, git = route
    base = tmp_path / "base-worktree"
    git("worktree", "add", "-qb", "local-base", str(base), "HEAD")
    report = record_complete_delivery_fixture(route, delivery_stage="base_sync", base_ref="refs/heads/local-base")
    snapshot = (route[3] / "operation-a/stage-facts/base_sync.json").read_bytes()
    (base / "later-base.txt").write_text("登记后另一次 base 前进\n")
    subprocess.run(["git", "-C", str(base), "add", "."], check=True)
    subprocess.run(["git", "-C", str(base), "commit", "-qm", "新的 base 工作"], check=True)
    result = module.finish_backup_route(repo, "operation-a", report)
    assert result["status"] == "backup_closed"
    assert (route[3] / "operation-a/stage-facts/base_sync.json").read_bytes() == snapshot
    # 完成观察单独报告当前 base，不重写原阶段成功快照。
    observation = result["base_sync_completion_observation"]
    assert observation["base_advanced_after_stage"] is True
    assert observation["recorded_base_tip"] != observation["current_base_tip"]
    assert observation["recorded_base_tree"] != observation["current_base_tree"]


def test_base_sync_first_finish_rejects_nonmonotonic_snapshot_loss(route, tmp_path):
    module, repo, _, artifacts, git = route
    original = git("rev-parse", "HEAD")
    (repo / "source.txt").write_text("已同步候选\n")
    git("add", ".")
    git("commit", "-qm", "已授权候选")
    git("branch", "local-base", "HEAD")
    report = record_complete_delivery_fixture(route, delivery_stage="base_sync", base_ref="refs/heads/local-base")
    snapshot = (artifacts / "operation-a/stage-facts/base_sync.json").read_bytes()
    git("update-ref", "refs/heads/local-base", original)
    with pytest.raises(ValueError, match="base"):
        module.finish_backup_route(repo, "operation-a", report)
    assert (artifacts / "operation-a/stage-facts/base_sync.json").read_bytes() == snapshot
    assert not (module.control_directory(repo) / "completed-operations/operation-a.json").exists()


def test_normal_completed_process_group_does_not_require_ps_permission(route, monkeypatch):
    module, repo, _, _, git = route
    check_output = module.subprocess.check_output
    def deny_process_listing(command, *args, **kwargs):
        if command[0] == "ps":
            raise PermissionError("合成沙箱拒绝 ps")
        return check_output(command, *args, **kwargs)
    monkeypatch.setattr(module.subprocess, "check_output", deny_process_listing)
    before = git("rev-parse", "HEAD")
    (repo / "source.txt").write_text("ps 被拒时仍能正常提交的真实隔离候选\n")
    with module.normal_worktree_mutation(repo, "completed-child-without-process-listing"):
        for arguments in [("add", "source.txt"), ("commit", "-qm", "fix: 隔离进程权限回归")]:
            assert module.tracked_subprocess_run(["git", "-C", str(repo), *arguments], capture_output=True).returncode == 0
    assert git("rev-parse", "HEAD") != before
    assert not list((module.control_directory(repo) / "normal-mutations").glob("*.json"))


def test_completed_marker_recovery_survives_new_candidate_bytes(route, monkeypatch):
    module, repo, _, _, _ = route
    report = record_complete_delivery_fixture(route)
    rename = module.os.rename
    def fail_final_marker_archive(source, destination):
        if Path(source).name == "active-owner":
            raise OSError("合成完成记录后崩溃")
        return rename(source, destination)
    monkeypatch.setattr(module.os, "rename", fail_final_marker_archive)
    with pytest.raises(OSError):
        module.finish_backup_route(repo, "operation-a", report)
    root = module.control_directory(repo)
    historical_result = (root / "completed-operations/operation-a.json").read_bytes()
    (repo / "source.txt").write_text("完成后出现新候选\n")
    monkeypatch.setattr(module.os, "rename", rename)
    with pytest.raises(ValueError):
        module.finish_backup_route(repo, "operation-a", report)
    assert not module.marker_present(root / "active-owner")
    assert (root / "completed-operations/operation-a.json").read_bytes() == historical_result
    assert not module.should_suppress_normal_stop(repo)
    assert claim(route, "operation-b")["status"] == "backup_active"


def test_content_fingerprint_has_unambiguous_binary_file_boundaries(route):
    module, repo, *_ = route
    (repo / "source.txt").write_bytes(b"A")
    (repo / "z.bin").write_bytes(b"z.bin\x000\x00B")
    before = module.worktree_content_fingerprint(repo)
    (repo / "source.txt").write_bytes(b"Az.bin\x000\x00")
    (repo / "z.bin").write_bytes(b"B")
    assert module.worktree_content_fingerprint(repo) != before


@pytest.mark.parametrize("changed_generation", ["boot_session_id", "process_start_id"])
def test_stale_reservation_does_not_wait_for_a_reused_live_pid(route, changed_generation):
    import os
    import sys
    import uuid
    module, repo, _, artifacts, _ = route
    try:
        with module.normal_worktree_mutation(repo, "crashed-operation") as reservation:
            raise RuntimeError("合成作用域异常")
    except RuntimeError:
        pass
    value = json.loads(reservation.read_text())
    value["outcome"] = "in_flight"
    unrelated_process = None
    if changed_generation == "boot_session_id":
        original_boot = uuid.UUID(value["process_identity"]["boot_session_id"])
        value["process_identity"]["boot_session_id"] = str(uuid.UUID(int=original_boot.int ^ 1))
    else:
        old_process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            old_identity = module.read_process_identity(old_process.pid)
        finally:
            old_process.terminate()
            old_process.wait(timeout=5)
        unrelated_process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        # 使用真实旧／新进程开始时间，仅把数值 PID 重用情景注入持久记录。
        value["pid"] = unrelated_process.pid
        value["process_identity"] = {**old_identity, "pid": unrelated_process.pid}
    module.durable_json(reservation, value)
    claim(route)
    proof = artifacts / "reconciliation.md"
    proof.write_text("合成对账：旧代际已结束；当前同号 PID 不属于原 operation。\n")
    try:
        module.reconcile_normal_mutation(repo, "operation-a", reservation.name, proof)
        assert module.assert_backup_route_ready(repo, "operation-a")["operation_id"] == "operation-a"
        if unrelated_process:
            assert unrelated_process.poll() is None
    finally:
        if unrelated_process:
            unrelated_process.terminate()
            unrelated_process.wait(timeout=5)


@pytest.mark.parametrize("failure", ["invalid_json", "git_enumeration_failed", "file_read_failed"])
def test_stop_content_verification_failure_preserves_unknown_without_hiding_new_candidate(route, monkeypatch, failure):
    module, repo, *_ = route
    report = record_complete_delivery_fixture(route)
    module.finish_backup_route(repo, "operation-a", report)
    (repo / "source.txt").write_text("未审查的新候选\n")
    if failure == "invalid_json":
        (module.control_directory(repo) / "completed-operations/operation-a.json").write_text("broken")
        # 同 operation 的持久原件也不可读时才仍然未知；单份索引损坏应自动补证。
        (route[3] / "operation-a/backup-route-delivery-result.json").write_text("broken")
    elif failure == "git_enumeration_failed":
        check_output = module.subprocess.check_output
        def reject_git_file_enumeration(command, *args, **kwargs):
            if command[0] == "git" and "ls-files" in command:
                raise subprocess.CalledProcessError(128, command, stderr="合成 Git 核验失败".encode())
            return check_output(command, *args, **kwargs)
        monkeypatch.setattr(module.subprocess, "check_output", reject_git_file_enumeration)
    else:
        read_bytes = Path.read_bytes
        def reject_source_read(path):
            if path == repo / "source.txt":
                raise OSError("合成内容读取失败")
            return read_bytes(path)
        monkeypatch.setattr(Path, "read_bytes", reject_source_read)
    assert module.should_suppress_normal_stop(repo) is False
    observation = json.loads((module.control_directory(repo) / "stop-content-verification-observation.json").read_text())
    assert observation["status"] == "unknown"


def test_stage_commit_ancestry_is_not_redirected_by_git_environment(route, tmp_path, monkeypatch):
    module, repo, _, artifacts, git = route
    outside = tmp_path / "unrelated-head-worktree"
    git("worktree", "add", "-qb", "unrelated-head", str(outside), "HEAD")
    (outside / "unrelated.txt").write_text("不属于当前 task HEAD\n")
    subprocess.run(["git", "-C", str(outside), "add", "."], check=True)
    subprocess.run(["git", "-C", str(outside), "commit", "-qm", "其他 worktree 提交"], check=True)
    outside_commit = subprocess.check_output(["git", "-C", str(outside), "rev-parse", "HEAD"], text=True).strip()
    outside_git_directory = subprocess.check_output(["git", "-C", str(outside), "rev-parse", "--absolute-git-dir"], text=True).strip()
    claim(route)
    proof = artifacts / "land.md"
    proof.write_text("合成归属核验\n")
    monkeypatch.setenv("GIT_DIR", outside_git_directory)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        module.record_stage_fact(repo, "operation-a", "land", proof, outside_commit, None)


def test_base_sync_accepts_integrated_no_ff_landing_and_preserves_completed_history(route, tmp_path):
    module, repo, _, _, git = route
    base = tmp_path / "base-worktree"
    git("worktree", "add", "-qb", "local-base", str(base), "HEAD")
    (base / "base.txt").write_text("base 原有工作\n")
    subprocess.run(["git", "-C", str(base), "add", "."], check=True)
    subprocess.run(["git", "-C", str(base), "commit", "-qm", "base 工作"], check=True)
    (repo / "task.txt").write_text("task 新工作\n")
    git("add", ".")
    git("commit", "-qm", "task 工作")
    git("merge", "--no-ff", "-m", "已吸收本地 base", "refs/heads/local-base")
    task_tip = git("rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(base), "merge", "--no-ff", "-m", "已落地 task", task_tip], check=True, capture_output=True)
    report = record_complete_delivery_fixture(route, delivery_stage="base_sync", base_ref="refs/heads/local-base")
    result = module.finish_backup_route(repo, "operation-a", report)
    completed = module.control_directory(repo) / "completed-operations/operation-a.json"
    historical_bytes = completed.read_bytes()
    (base / "later.txt").write_text("完成后另一次合法工作\n")
    subprocess.run(["git", "-C", str(base), "add", "."], check=True)
    subprocess.run(["git", "-C", str(base), "commit", "-qm", "后续独立工作"], check=True)
    assert module.finish_backup_route(repo, "operation-a", report) == result
    assert completed.read_bytes() == historical_bytes


def test_base_sync_rejects_equal_tree_when_original_base_parent_was_not_absorbed(route, tmp_path):
    module, repo, authorization, artifacts, git = route
    base = tmp_path / "base-worktree"
    git("worktree", "add", "-qb", "local-base", str(base), "HEAD")
    subprocess.run(["git", "-C", str(base), "commit", "--allow-empty", "-qm", "尚未吸收的 base 祖先"], check=True)
    (repo / "source.txt").write_text("task 候选\n")
    git("add", ".")
    git("commit", "-qm", "task 工作")
    subprocess.run(["git", "-C", str(base), "merge", "--no-ff", "-m", "未经过 task 吸收的落地", git("rev-parse", "HEAD")], check=True, capture_output=True)
    assert git("rev-parse", "HEAD^{tree}") == git("rev-parse", "refs/heads/local-base^{tree}")
    module.claim_backup_route(repo, "operation-a", artifacts, authorization, "核对 first parent", None, "base_sync")
    report = artifacts / "report.md"
    report.write_text("等 tree 不能替代 base 吸收事实\n")
    with pytest.raises(ValueError):
        module.record_stage_fact(repo, "operation-a", "base_sync", report, git("rev-parse", "HEAD"), "refs/heads/local-base")


def test_ps_permission_denial_cannot_release_a_live_descendant_group(route, monkeypatch):
    import os
    import signal
    import sys
    module, repo, _, artifacts, _ = route
    check_output = module.subprocess.check_output
    def deny_process_listing(command, *args, **kwargs):
        if command[0] == "ps":
            raise PermissionError("合成沙箱拒绝 ps")
        return check_output(command, *args, **kwargs)
    monkeypatch.setattr(module.subprocess, "check_output", deny_process_listing)
    group = None
    try:
        with pytest.raises(module.BackupRouteOwned, match="进程组"):
            with module.normal_worktree_mutation(repo, "live-descendant-without-process-listing") as reservation:
                module.tracked_subprocess_run([sys.executable, "-c",
                    "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],"
                    "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"], capture_output=True)
                group = json.loads(reservation.read_text())["child_process_group_id"]
        assert json.loads(reservation.read_text())["outcome"] == "unknown"
        claim(route)
        proof = artifacts / "reconciliation.md"
        proof.write_text("没有得到子组结束事实，不能释放。\n")
        with pytest.raises(module.BackupRouteOwned, match="进程组"):
            module.reconcile_normal_mutation(repo, "operation-a", reservation.name, proof)
        assert reservation.is_file()
    finally:
        if group:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_native_process_identity_reads_exact_generation_without_ps(route, monkeypatch):
    import os
    module, *_ = route
    check_output = module.subprocess.check_output
    def deny_process_listing(command, *args, **kwargs):
        if command[0] == "ps":
            raise PermissionError("合成沙箱拒绝 ps")
        return check_output(command, *args, **kwargs)
    monkeypatch.setattr(module.subprocess, "check_output", deny_process_listing)
    identity = module.read_process_identity(os.getpid())
    assert identity["boot_session_id"] == module.current_boot_session_id()
    assert identity["process_start_id"] == module.read_process_identity(os.getpid())["process_start_id"]
    assert identity["parent_process_id"] == os.getppid()


def test_internal_launcher_authenticates_its_real_parent_with_native_generation(route):
    import sys
    module, repo, *_ = route
    validator = [sys.executable, str(SCRIPT), "validate-normal-reservation", "--repo", str(repo)]
    with module.normal_worktree_mutation(repo, "native-launcher-parent-check"):
        result = module.tracked_subprocess_run([sys.executable, "-c",
            "import subprocess,sys; sys.exit(subprocess.run(" + repr(validator) + ").returncode)"],
            capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["status"] == "normal_reservation_verified"


@pytest.mark.parametrize("native_proof_matches_operation", [True, False])
def test_live_node_service_requires_exact_local_operation_quiescence(route, native_proof_matches_operation):
    import os
    module, repo, _, artifacts, _ = route
    claim(route)
    operation_id = "node-reservation"
    context = {"workspace_id": "workspace", "task_id": "fixture-task", "decision_token": "fixture-token",
               "run_id": "fixture-run", "run_directory": "/persistent/fixture-run"}
    proof = {**context, "schema": "kanban-normal-closeout-quiescence/v1", "operation_id": operation_id,
             "observed_at": "2026-10-25T00:00:00Z", "conversation_session_id": "conversation",
             "native_session_id": "native-session", "runtime_session_incarnation_id": "incarnation",
             "delivery_idempotency_key": "exact-delivery", "queued_delivery_absent": True,
             "terminal_delivery_pending": False, "native_control_verified_idle": True,
             "native_turns_sha256": "a" * 64, "delivery_ledger_sha256": "b" * 64}
    if not native_proof_matches_operation:
        proof["decision_token"] = "another-token"
    reservation = module.control_directory(repo) / "normal-mutations" / (operation_id + ".json")
    module.durable_json(reservation, {"pid": os.getpid(), "process_identity": module.capture_process_identity(os.getpid()),
        "operation_id": operation_id, "outcome": "unknown", "operation_state": "settled",
        "reconciliation_context": context, "native_outcome_evidence": proof})
    report = artifacts / "reconciliation.md"
    report.write_text("合成夹具：同一局部操作的原生控制面和投递账本对账。\n")
    if native_proof_matches_operation:
        module.reconcile_normal_mutation(repo, "operation-a", reservation.name, report)
        assert not reservation.exists()
    else:
        with pytest.raises(module.BackupRouteOwned, match="原生操作"):
            module.reconcile_normal_mutation(repo, "operation-a", reservation.name, report)
        assert reservation.exists()
