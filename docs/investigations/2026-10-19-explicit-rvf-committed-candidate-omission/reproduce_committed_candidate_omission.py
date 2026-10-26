#!/usr/bin/env python3
"""在持久隔离目录复现候选遗漏；不运行 reviewer、不连接 Kanban、不改来源仓库。

历史缺陷断言必须配合固定历史产品目录；修复验收使用正常 Kanban 证据发现路径。
只允许新建输出目录；所有 Git 写操作及 RVF 状态写操作均限定在该目录。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIRECTORY = REPOSITORY_ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
ASSERTION_MODE = "historical-defects"
sys.path.insert(0, str(SCRIPT_DIRECTORY))
sys.path.insert(0, str(REPOSITORY_ROOT))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_git(repository: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", *arguments], cwd=repository, text=True, stderr=subprocess.PIPE
    ).strip()


@contextlib.contextmanager
def isolated_runtime_environment(case_directory: Path):
    original_environment = os.environ.copy()
    for key in list(os.environ):
        if key.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_", "GIT_")):
            del os.environ[key]
    os.environ.update({
        "RVF_HOME": str(case_directory / "isolated-rvf-home"),
        "RVF_USER_LOCAL_DATA_ROOT": str(case_directory / "isolated-user-local-data"),
        "CODEX_RVF_LOG_ROOT": str(case_directory / "isolated-run-logs"),
        "CODEX_RVF_STATE_DIR": str(case_directory / "isolated-runtime-state"),
        "CODEX_RVF_SESSION_HOOK_STATE_DIR": str(case_directory / "isolated-session-hook-state"),
        "CODEX_RVF_PREP_ROOT": str(case_directory / "isolated-prep-files"),
        "CODEX_RVF_CLINE_KANBAN_STATE_DIR": str(case_directory / "isolated-kanban-state"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original_environment)


def initialize_repository(case_directory: Path) -> tuple[Path, str]:
    repository = case_directory / "isolated-git-repository"
    repository.mkdir()
    run_git(repository, "init", "-q", "-b", "main")
    run_git(repository, "config", "user.name", "隔离调查探针")
    run_git(repository, "config", "user.email", "isolated-investigation@example.invalid")
    run_git(repository, "config", "core.hooksPath", os.devnull)
    run_git(repository, "config", "maintenance.auto", "false")
    run_git(repository, "config", "gc.auto", "0")
    commit_file(repository, "initial.txt", "初始内容\n")
    return repository, run_git(repository, "rev-parse", "HEAD")


def commit_file(repository: Path, path: str, content: str) -> str:
    (repository / path).write_text(content, encoding="utf-8")
    run_git(repository, "add", "--", path)
    run_git(repository, "commit", "-q", "-m", "test: 写入隔离调查内容")
    commit = run_git(repository, "rev-parse", "HEAD")
    if ASSERTION_MODE == "fix-acceptance" and run_git(repository, "branch", "--show-current") == "investigation-task":
        update_isolated_kanban_provenance(repository, additional_commit=commit)
    return commit


def hook_event(repository: Path, *, task: bool = True, session: str = "investigation-session") -> dict:
    event = {"cwd": str(repository), "session_id": session, "hook_event_name": "UserPromptSubmit"}
    if task:
        event["kanban_task_id"] = "isolated-investigation-task"
        if ASSERTION_MODE == "fix-acceptance":
            update_isolated_kanban_provenance(repository, initial=run_git(repository, "rev-parse", "HEAD"))
    return event


def update_isolated_kanban_provenance(repository: Path, *, initial: str | None = None,
                                     additional_commit: str | None = None, captured_entry: dict | None = None) -> None:
    """只重建隔离身份；来源事故使用捕获的原始条目，不读取后来的真实 tracker。"""
    task_id = "isolated-investigation-task"
    directory = Path(os.environ["CODEX_RVF_CLINE_KANBAN_STATE_DIR"]) / "workspaces/isolated-project"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "task-commit-integration-provenance.json"
    if captured_entry:
        entry = json.loads(json.dumps(captured_entry))
        entry["taskId"] = task_id
        entry["lastWorkspaceGitSnapshot"]["path"] = str(repository)
    elif path.exists() and not initial:
        entry = json.loads(path.read_text())["tasks"][task_id]
    else:
        entry = {"taskId": task_id, "baseRef": "main", "incarnations": [{
            "incarnationId": f"{initial}:1", "initialBaseCommit": initial,
            "captureSource": "worktree_creation", "startedAt": 1, "observedTaskCommits": [],
        }], "lastWorkspaceGitSnapshot": {"path": str(repository)}}
    if additional_commit:
        from adapters.cline_kanban.committed_candidate_provenance import _stable_patch_id
        entry["incarnations"][-1]["observedTaskCommits"].append({
            "commitSha": additional_commit, "stablePatchId": _stable_patch_id(repository, additional_commit),
        })
    write_json(path, {"schemaVersion": 1, "tasks": {task_id: entry}})
    write_json(directory / "sessions.json", {task_id: {"taskId": task_id, "workspacePath": str(repository)}})
    write_json(directory / "board.json", {"columns": [{"cards": [{"id": task_id, "baseRef": "main"}]}]})


def capture_baseline(event: dict) -> dict:
    import rvf_user_prompt_submit as submit
    result = submit._capture_round_baseline(event, "隔离调查 prompt")
    assert result is not None
    return result


def candidate_summary(event: dict, *, baseline: str | None = None, exclude_base: bool = True) -> dict:
    import stop_decision_candidate_summary as summary
    options = {}
    if baseline:
        options["committed_baseline_resolver"] = lambda *_: baseline
    if not exclude_base:
        options["committed_base_ref_resolver"] = lambda *_: None
    return summary.build_candidate_summary(
        event=event, cwd=event["cwd"], repo=event["cwd"],
        workspace_guard=lambda **_: None, **options,
    )


def summarize_case(case_directory: Path, label: str, event: dict, expected_count: int,
                   *, known_defect: bool = False, **options) -> dict:
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker
    result = candidate_summary(event, **options)
    repository = Path(event["cwd"])
    result["investigation_case"] = label
    result["known_defect"] = known_defect
    result["assertion_mode"] = ASSERTION_MODE
    if ASSERTION_MODE == "fix-acceptance" and known_defect and event.get("kanban_task_id"):
        expected_count = 10 if label.startswith("source-") else 1
    result["git_head"] = run_git(repository, "rev-parse", "HEAD")
    result["git_main"] = run_git(repository, "rev-parse", "main")
    result["raw_window_without_base_exclusion"] = tracker._list_committed_round_changed_paths(
        repository, result["committed_round_baseline"], None
    ) if result["committed_round_baseline"] else []
    write_json(case_directory / f"{label}.json", result)
    assert result["candidate_path_count"] == expected_count, (label, result)
    return {"case": label, "candidate_path_count": result["candidate_path_count"],
            "review_state_filtered": result["review_state_filtered_committed_path_count"],
            "other_owner_filtered": result["filtered_out_other_session_path_count"],
            "known_defect": known_defect, "assertion_mode": ASSERTION_MODE}


def register_committed_work(repository: Path, baseline: str, *, owner: str = "investigation-session"):
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker
    result = tracker.register_claims(
        repo=repository, session_id=owner, run_id="isolated-investigation-run",
        worktree=repository, branch=None, owned_paths=["own.txt"],
        apply_patch_paths={"own.txt"}, exec_only_paths=set(),
        committed_paths={"own.txt"}, committed_baseline=baseline,
    )
    assert result.status == "ok", result
    return result


def run_synthetic_matrix(output: Path) -> list[dict]:
    import rvf_user_prompt_submit as submit
    from core.session_scope_allocation import reviewable_unit_diff_tracker as tracker
    from core.session_scope_allocation import review_highwater_marker as highwater
    results = []

    for topology in ["task-handback", "base-absorption", "merge-absorption", "rebase-absorption",
                     "main-direct", "tracker-reviewed", "other-session", "rewritten-highwater",
                     "merge-handback", "squash-handback"]:
        case_directory = output / topology
        case_directory.mkdir()
        with isolated_runtime_environment(case_directory):
            repository, initial = initialize_repository(case_directory)
            event = hook_event(repository, task=topology != "main-direct")
            capture_baseline(event)
            if topology != "main-direct":
                run_git(repository, "checkout", "-q", "-b", "investigation-task")

            if topology == "base-absorption":
                run_git(repository, "checkout", "-q", "main")
                commit_file(repository, "base-only.txt", "来自其他 base 工作\n")
                run_git(repository, "checkout", "-q", "investigation-task")
                run_git(repository, "merge", "-q", "--ff-only", "main")
                results.append(summarize_case(case_directory, "base-ff-absorption", event, 0,
                                              baseline=initial))
                run_git(repository, "checkout", "-q", "--detach", "HEAD")
                results.append(summarize_case(case_directory, "detached-base-ff-absorption", event, 0,
                                              baseline=initial))
                continue

            own = commit_file(repository, "own.txt", "本会话未审工作\n")
            if topology == "task-handback":
                results.append(summarize_case(case_directory, "first-review-before-prompt", event, 1))
                capture_baseline(event)
                results.append(summarize_case(case_directory, "ordinary-prompt-before-handback", event, 1))
                # 只调用 UPS 的本地路径；不发起 rvf-agent start 或任何 reviewer。
                payload = submit.inspect_user_prompt_submit({**event, "prompt": "$review-validate-fix:review-validate-fix"})
                assert payload["status"] == "manual_decision_injected", payload
                write_json(case_directory / "manual-before-handback.json", {
                    "status": payload["status"], "candidate_path_count": payload["candidate_path_count"],
                    "has_additional_context": "hookSpecificOutput" in payload,
                })
                results.append({"case": "manual-before-handback", "candidate_path_count": 1,
                                "known_defect": False})
                run_git(repository, "checkout", "-q", "main")
                run_git(repository, "merge", "-q", "--ff-only", "investigation-task")
                run_git(repository, "checkout", "-q", "investigation-task")
                results.append(summarize_case(case_directory, "handback-current-baseline", event, 0,
                                              known_defect=True))
                results.append(summarize_case(case_directory, "handback-frozen-initial-only", event, 0,
                                              baseline=initial, known_defect=True))
                results.append(summarize_case(case_directory, "handback-both-overrides", event, 1,
                                              baseline=initial, exclude_base=False))
                register_committed_work(repository, initial)
                results.append(summarize_case(case_directory, "handback-with-available-owned-units", event, 0,
                                              baseline=initial, known_defect=True))
                allocation = tracker.allocate_review_scope(
                    repo=repository, session_id=event["session_id"], run_id="isolated-after-handback",
                    reviewer_id="isolated-fixture-reviewer", auto_claim_observed=False,
                    committed_baseline=initial, committed_base_ref="main",
                    **({"committed_scope_evidence": candidate_summary(event)["committed_scope_evidence"]}
                       if ASSERTION_MODE == "fix-acceptance" else {}),
                )
                assert allocation["status"] == ("allocated" if ASSERTION_MODE == "fix-acceptance" else "empty"), allocation
                write_json(case_directory / "allocation-after-handback.json", allocation)
                results.append({"case": "allocation-after-handback", "candidate_unit_count": allocation["candidate_unit_count"],
                                "known_defect": True})
                highwater.write_review_highwater(task_id=event["kanban_task_id"],
                                                session_id=event["session_id"], reviewed_head=initial,
                                                repo=str(repository))
                results.append(summarize_case(case_directory, "handback-older-highwater", event, 0,
                                              known_defect=True))
                payload = submit.inspect_user_prompt_submit({**event, "prompt": "/review-validate-fix"})
                accepted_statuses = {"manual_decision_injected", "manual_decision_deduplicated"} if ASSERTION_MODE == "fix-acceptance" else {"manual_decision_no_candidates"}
                assert payload["status"] in accepted_statuses, payload
                if ASSERTION_MODE != "fix-acceptance":
                    assert "hookSpecificOutput" not in payload and "token" not in payload
                write_json(case_directory / "manual-after-handback.json", submit._render_hook_payload(payload))
                results.append({"case": "manual-after-handback", "candidate_path_count": payload["candidate_path_count"],
                                "known_defect": True})
            elif topology in ("merge-absorption", "rebase-absorption"):
                run_git(repository, "checkout", "-q", "main")
                commit_file(repository, "base-only.txt", "来自其他 base 工作\n")
                run_git(repository, "checkout", "-q", "investigation-task")
                if topology == "merge-absorption":
                    run_git(repository, "merge", "-q", "--no-ff", "main", "-m", "test: 吸收 base")
                else:
                    run_git(repository, "rebase", "main")
                capture_baseline(event)
                results.append(summarize_case(case_directory, topology, event, 1))
            elif topology == "main-direct":
                results.append(summarize_case(case_directory, "on-main-before-next-prompt", event, 1))
                capture_baseline(event)
                results.append(summarize_case(case_directory, "on-main-after-next-prompt", event, 0,
                                              known_defect=True))
                payload = submit.inspect_user_prompt_submit({**event, "prompt": "/review-validate-fix"})
                assert payload["status"] == "manual_decision_no_candidates", payload
                results.append({"case": "on-main-manual-before-first-review", "candidate_path_count": 0,
                                "known_defect": True})
            elif topology == "tracker-reviewed":
                register_committed_work(repository, initial)
                results.append(summarize_case(case_directory, "available-owned", event, 1))
                allocation = tracker.allocate_review_scope(
                    repo=repository, session_id=event["session_id"], run_id="isolated-review-run",
                    reviewer_id="isolated-fixture-reviewer", auto_claim_observed=False,
                    committed_baseline=initial, committed_base_ref="main",
                )
                assert allocation["status"] == "allocated", allocation
                tracker.complete_review_scope(repo=repository, lease_id=allocation["lease_id"],
                                              scope_hash=allocation["scope_hash"], run_id="isolated-review-run")
                results.append(summarize_case(case_directory, "tracker-reviewed-owned", event, 0))
                highwater.write_review_highwater(task_id=event["kanban_task_id"],
                                                session_id=event["session_id"], reviewed_head=own,
                                                repo=str(repository))
                results.append(summarize_case(case_directory, "reviewed-highwater-at-head", event, 0))
            elif topology == "other-session":
                register_committed_work(repository, initial, owner="another-session")
                results.append(summarize_case(case_directory, "strong-other-owner", event, 0))
                database = tracker.tracker_dir(Path(os.environ["CODEX_RVF_LOG_ROOT"]),
                                              tracker.repo_key(tracker.git_common_dir(repository))) / tracker.SQLITE_FILENAME
                # 仅修改本用例生成的隔离 SQLite，模拟弱时间窗证据；不触及真实 tracker。
                # 弱证据标签以产品常量为准，避免探针自创标签。
                import stop_decision_candidate_summary as summary
                with sqlite3.connect(database) as connection:
                    connection.execute("UPDATE session_units SET evidence=?",
                                       (next(iter(summary.WEAK_SESSION_WRITE_ATTRIBUTION_EVIDENCE_LABELS)),))
                results.append(summarize_case(case_directory, "weak-other-owner", event, 1))
            elif topology == "rewritten-highwater":
                highwater.write_review_highwater(task_id=event["kanban_task_id"],
                                                session_id=event["session_id"], reviewed_head=own,
                                                repo=str(repository))
                run_git(repository, "reset", "--hard", initial)
                commit_file(repository, "own.txt", "重写后的未审工作\n")
                capture_baseline(event)
                results.append(summarize_case(case_directory, "nonancestor-highwater-fallback", event, 1))
            else:
                run_git(repository, "checkout", "-q", "main")
                if topology == "merge-handback":
                    commit_file(repository, "base-only.txt", "其他 base 工作\n")
                    run_git(repository, "merge", "-q", "--no-ff", "investigation-task", "-m", "test: 合入 task")
                else:
                    run_git(repository, "merge", "-q", "--squash", "investigation-task")
                    run_git(repository, "commit", "-q", "-m", "test: squash 合入 task")
                run_git(repository, "checkout", "-q", "investigation-task")
                results.append(summarize_case(case_directory, topology, event,
                                              0 if topology == "merge-handback" else 1,
                                              baseline=initial, known_defect=topology == "merge-handback"))
    return results


def replay_source_commits(output: Path, source_repository: Path, initial: str, head: str,
                         source_provenance_snapshot: Path | None = None) -> list[dict]:
    """只 fetch 指定本地仓库对象到全新仓库；不用真实 marker、tracker 或来源仓库 index。"""
    import rvf_user_prompt_submit as submit
    from core.session_scope_allocation import round_baseline_marker
    case_directory = output / "source-commit-replay"
    case_directory.mkdir()
    with isolated_runtime_environment(case_directory):
        repository, _ = initialize_repository(case_directory)
        run_git(repository, "fetch", "--quiet", "--no-tags", str(source_repository), head)
        run_git(repository, "cat-file", "-e", initial + "^{commit}")
        run_git(repository, "checkout", "-q", "-B", "main", head)
        run_git(repository, "checkout", "-q", "-b", "investigation-task")
        event = hook_event(repository)
        if ASSERTION_MODE == "fix-acceptance":
            if source_provenance_snapshot is None:
                raise ValueError("来源修复验收必须提供事故现场捕获的 provenance，不能读取后来的 tracker")
            captured = json.loads(source_provenance_snapshot.read_text())
            assert captured["incarnations"][-1]["initialBaseCommit"] == initial
            update_isolated_kanban_provenance(repository, captured_entry=captured)
        capture_baseline(event)
        results = [summarize_case(case_directory, "source-default", event, 0, known_defect=True),
                   summarize_case(case_directory, "source-fixed-baseline-only", event, 0,
                                  baseline=initial, known_defect=True),
                   summarize_case(case_directory, "source-fixed-baseline-no-base-exclusion", event, 10,
                                  baseline=initial, exclude_base=False)]
        # 完整 UPS 主入口的真实 stdout 子进程；只传隔离身份，不读取来源 transcript。
        event["prompt"] = "$review-validate-fix:review-validate-fix"
        write_json(case_directory / "reconstructed-hook-input.json", event)
        completed = subprocess.run([sys.executable, str(SCRIPT_DIRECTORY / "rvf_user_prompt_submit.py")],
                                   cwd=repository, input=json.dumps(event), capture_output=True,
                                   text=True, check=True)
        stdout = json.loads(completed.stdout)
        if ASSERTION_MODE == "fix-acceptance":
            assert "hookSpecificOutput" in stdout, stdout
        else:
            assert "无可审候选" in stdout["systemMessage"] and "hookSpecificOutput" not in stdout
        write_json(case_directory / "hook-mode-stdout.json", stdout)
        round_baseline_marker.write_round_baseline_marker(
            task_id=event["kanban_task_id"], session_id=event["session_id"],
            repo=str(repository), baseline_head=initial,
        )
        results.append(summarize_case(case_directory, "source-persisted-initial-still-excluded", event, 0,
                                      known_defect=True))
        if ASSERTION_MODE == "fix-acceptance":
            return results
        # 反事实只注入 resolver；不修改生产代码，不把恢复候选冒充修复完成。
        import stop_decision_candidate_summary as summary
        with patch.object(summary, "_default_committed_baseline_resolver", lambda *_: initial), \
             patch.object(summary, "_default_committed_base_ref_resolver", lambda *_: None):
            payload = submit._handle_manual_decision_point(event, event["prompt"], base_payload={}, payload={})
        assert payload["status"] == "manual_decision_injected" and payload["candidate_path_count"] == 10
        write_json(case_directory / "counterfactual-manual-output-metadata.json", {
            "status": payload["status"], "candidate_path_count": payload["candidate_path_count"],
            "has_additional_context": "hookSpecificOutput" in payload,
        })
        return results


def main() -> None:
    global ASSERTION_MODE, REPOSITORY_ROOT, SCRIPT_DIRECTORY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--source-repository", type=Path)
    parser.add_argument("--source-provenance-snapshot", type=Path)
    parser.add_argument("--assertion-mode", choices=["historical-defects", "fix-acceptance"], default="fix-acceptance")
    parser.add_argument("--product-root", type=Path, help="历史缺陷断言使用的固定产品源码目录。")
    parser.add_argument("--source-initial-commit", default="fc25ab76176ad34865cbbacfe421b83577a75606")
    parser.add_argument("--source-head-commit", default="55ea63d9b218449378dd2b1f0a9167fc04287ac7")
    arguments = parser.parse_args()
    ASSERTION_MODE = arguments.assertion_mode
    if arguments.product_root:
        REPOSITORY_ROOT = arguments.product_root.resolve()
        SCRIPT_DIRECTORY = REPOSITORY_ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
        sys.path.insert(0, str(SCRIPT_DIRECTORY))
        sys.path.insert(0, str(REPOSITORY_ROOT))
    if ASSERTION_MODE == "historical-defects" and not arguments.product_root:
        raise ValueError("历史缺陷断言须显式提供固定历史 --product-root")
    output = arguments.output_directory.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    # 在任何产品 import 前封住其可变状态路径；内层每个用例再细分。
    with isolated_runtime_environment(output):
        results = run_synthetic_matrix(output)
        if arguments.source_repository:
            results += replay_source_commits(output, arguments.source_repository.resolve(),
                                             arguments.source_initial_commit, arguments.source_head_commit,
                                             arguments.source_provenance_snapshot)
    for result in results:
        result["assertion_mode"] = ASSERTION_MODE
    write_json(output / "regression-matrix-results.json", results)
    print(json.dumps({"asserted_case_count": len(results), "output_directory": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
