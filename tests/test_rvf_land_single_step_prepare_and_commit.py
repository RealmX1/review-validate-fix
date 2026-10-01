"""``rvf_land.py`` prepare / commit 的端到端测试（隔离 HOME 状态根、临时 run 目录、真 git 仓库）。

两条主路径各一条：
- 无补修正：run 自己的 validation index 已满足 evidence 门槛 ⇒ 零执行复用（计数器证明验证命令没跑）。
- 有补修正：内容摘要变了 ⇒ 按结构化 argv 经缓存重跑（计数器 +1），evidence 仍写入成功。
其余覆盖硬门槛退出码、验证失败回显尾部、旧 run 无 argv 工件、prepare plan 过期、无改动 land。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIRECTORY))
# 复用 evidence 测试的「已完成 run」构造器，而不是手抄一份：两边断言的是同一份 run 形状，
# 手抄会在 evidence 门槛演进时悄悄漂移。
from test_reviewed_task_candidate_evidence import (  # noqa: E402
    SCRIPT_DIRECTORY,
    clean_test_environment,
    create_completed_run,
    git,
    repository,
)

RVF_LAND = SCRIPT_DIRECTORY / "rvf_land.py"
VALIDATION_RESULT_CACHE = SCRIPT_DIRECTORY / "validation_result_cache.py"
LAND_TEST_KANBAN_TASK_ID = "rvf-land-single-step-test-task"


def land_environment(tmp_path: Path) -> dict[str, str]:
    environment = {
        name: value
        for name, value in clean_test_environment().items()
        if not name.startswith(("CODEX_RVF_", "RVF_", "KANBAN_", "CLINE_KANBAN_"))
    }
    environment.update(
        {
            "RVF_USER_LOCAL_DATA_ROOT": str(tmp_path / "user-data"),
            "RVF_HOME": str(tmp_path / "rvf-home"),
            "KANBAN_TASK_ID": LAND_TEST_KANBAN_TASK_ID,
        }
    )
    return environment


def run_land(
    tmp_path: Path, repo: Path, *arguments: str, stdin_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RVF_LAND), *arguments],
        input=stdin_text,
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
        env=land_environment(tmp_path),
    )


def record_validation(
    tmp_path: Path, repo: Path, run_dir: Path, name: str, *command: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATION_RESULT_CACHE),
            "run",
            "--repo",
            str(repo),
            "--name",
            name,
            "--rvf-run-dir",
            str(run_dir),
            "--",
            *command,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=land_environment(tmp_path),
    )


def counting_validation_command(counter_path: Path) -> list[str]:
    """每执行一次就往仓库外的计数文件追加一行：用来证明「零执行复用」确实没执行。"""
    return [
        sys.executable,
        "-c",
        f"open({str(counter_path)!r}, 'a').write('run\\n'); print('validation passed')",
    ]


def counter_value(counter_path: Path) -> int:
    return len(counter_path.read_text(encoding="utf-8").splitlines()) if counter_path.exists() else 0


def reviewed_dirty_run(tmp_path: Path) -> tuple[Path, Path, Path]:
    """仓库里有一段已审、未提交的改动；run 的验证记录就是在这份内容上跑出来的。"""
    repo = repository(tmp_path / "repo")
    (repo / "tracked.txt").write_text("candidate\nreviewed change\n", encoding="utf-8")
    run_dir = create_completed_run(tmp_path, repo)
    counter = tmp_path / "validation-execution-counter.txt"
    completed = record_validation(
        tmp_path, repo, run_dir, "handoff-validation-001", *counting_validation_command(counter)
    )
    assert completed.returncode == 0, completed.stderr
    assert counter_value(counter) == 1
    return repo, run_dir, counter


def prepare(tmp_path: Path, repo: Path, run_dir: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return run_land(
        tmp_path,
        repo,
        "prepare",
        "--handoff",
        str(run_dir / "artifacts" / "handoff.md"),
        "--repo",
        str(repo),
        "--format",
        "json",
        *extra,
    )


def commit(tmp_path: Path, repo: Path, run_dir: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return run_land(
        tmp_path,
        repo,
        "commit",
        "--run-dir",
        str(run_dir),
        "--message-file",
        "-",
        *extra,
        stdin_text="fix(test): land 单步化\n\nsanity-check：自洽\n验证：\n[[rvf-land-validation-summary]]\n",
    )


def test_land_without_patch_reuses_run_validation_index_with_zero_execution(tmp_path: Path) -> None:
    repo, run_dir, counter = reviewed_dirty_run(tmp_path)

    prepared = prepare(tmp_path, repo, run_dir)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    report = json.loads(prepared.stdout)
    assert report["hard_gate_failures"] == []
    assert report["stageable_dirty_paths"] == ["tracked.txt"]
    assert report["validation"]["validation_reusable"] is True
    assert report["validation"]["validation_plan"] == "reuse_run_validation_index_with_zero_execution"
    assert "[[rvf-land-validation-summary]]" in report["commit_message_draft"]
    assert "+reviewed change" in report["scoped_diff"]["diff"]

    committed = commit(tmp_path, repo, run_dir)
    assert committed.returncode == 0, committed.stdout + committed.stderr
    result = json.loads(committed.stdout)
    assert result["status"] == "committed"
    assert result["validation"]["mode"] == "reused_run_validation_index_with_zero_execution"
    assert counter_value(counter) == 1, "零执行复用却执行了验证命令"
    assert result["worktree_clean_after_commit"] is True
    assert result["round_baseline_seal"]["sealed"] is True
    assert result["candidate_evidence"]["status"] == "written", result["candidate_evidence"]
    assert git(repo, "status", "--porcelain") == ""
    message = git(repo, "log", "-1", "--format=%B")
    assert "[[rvf-land-validation-summary]]" not in message
    assert "handoff-validation-001：passed（复用 RVF run 验证记录，零执行）" in message


def test_land_with_minimal_patch_reruns_structured_argv_through_cache(tmp_path: Path) -> None:
    repo, run_dir, counter = reviewed_dirty_run(tmp_path)
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    # agent 在 sanity-check 里做的最小修正：内容摘要变了，run 的记录不再对应当前内容。
    (repo / "tracked.txt").write_text("candidate\nreviewed change, typo fixed\n", encoding="utf-8")

    committed = commit(tmp_path, repo, run_dir)
    assert committed.returncode == 0, committed.stdout + committed.stderr
    result = json.loads(committed.stdout)
    assert result["validation"]["mode"] == "reran_structured_argv_through_validation_result_cache"
    # 按 argv 工件的顺序逐条重放：run 里登记过的每一条都重跑（create_completed_run 自带一条）。
    assert [outcome["name"] for outcome in result["validation"]["outcomes"]] == [
        "targeted-contract-tests",
        "handoff-validation-001",
    ]
    assert all(
        outcome["cache_status"] == "executed" and outcome["status"] == "passed"
        for outcome in result["validation"]["outcomes"]
    )
    assert counter_value(counter) == 2
    assert result["candidate_evidence"]["status"] == "written", result["candidate_evidence"]
    assert "handoff-validation-001：passed（已执行）" in git(repo, "log", "-1", "--format=%B")


def test_rerun_failure_prints_output_tail_and_leaves_worktree_uncommitted(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    (repo / "tracked.txt").write_text("candidate\nreviewed change\n", encoding="utf-8")
    run_dir = create_completed_run(tmp_path, repo)
    content_sensitive_check = [
        sys.executable,
        "-c",
        "import sys\n"
        "print('checking tracked.txt')\n"
        "ok = 'typo' not in open('tracked.txt').read()\n"
        "print('ASSERTION: tracked.txt must not contain typo', file=sys.stderr) if not ok else None\n"
        "sys.exit(0 if ok else 3)",
    ]
    assert record_validation(tmp_path, repo, run_dir, "handoff-validation-001", *content_sensitive_check).returncode == 0
    head_before = git(repo, "rev-parse", "HEAD")
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    (repo / "tracked.txt").write_text("candidate\nreviewed change typo\n", encoding="utf-8")

    committed = commit(tmp_path, repo, run_dir)
    assert committed.returncode == 21, committed.stdout + committed.stderr
    refusal = json.loads(committed.stdout)
    assert refusal["reason_code"] == "validation_failed"
    assert "ASSERTION: tracked.txt must not contain typo" in committed.stderr
    assert "checking tracked.txt" in committed.stderr
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_legacy_run_without_structured_argv_refuses_rerun_with_instructions(tmp_path: Path) -> None:
    repo, run_dir, _counter = reviewed_dirty_run(tmp_path)
    for suffix in ("", ".sha256"):
        (run_dir / "artifacts" / f"handoff-validation-commands.json{suffix}").unlink()
    (repo / "tracked.txt").write_text("candidate\npatched\n", encoding="utf-8")
    report = json.loads(prepare(tmp_path, repo, run_dir).stdout)
    assert report["validation"]["validation_plan"] == "blocked_no_structured_argv_to_replay"
    assert report["validation"]["index_record_names_without_structured_argv"] == [
        "handoff-validation-001",
        "targeted-contract-tests",
    ]

    committed = commit(tmp_path, repo, run_dir)
    assert committed.returncode == 22, committed.stdout + committed.stderr
    assert json.loads(committed.stdout)["reason_code"] == "validation_not_replayable"


def test_prepare_hard_gate_exit_codes(tmp_path: Path) -> None:
    repo, run_dir, _counter = reviewed_dirty_run(tmp_path)

    (repo / "unrelated-notes.txt").write_text("someone else's work\n", encoding="utf-8")
    unrelated = prepare(tmp_path, repo, run_dir)
    assert unrelated.returncode == 12, unrelated.stdout
    failure = json.loads(unrelated.stdout)["hard_gate_failures"][0]
    assert failure["reason_code"] == "unrelated_dirty_paths_would_remain_after_commit"
    assert failure["paths"] == ["unrelated-notes.txt"]
    # agent 判断它属于本次工作 ⇒ --extra-path 显式纳入后门槛通过。
    assert prepare(tmp_path, repo, run_dir, "--extra-path", "unrelated-notes.txt").returncode == 0
    (repo / "unrelated-notes.txt").unlink()

    other_worktree = tmp_path / "other-worktree"
    git(repo, "worktree", "add", "-q", "--detach", str(other_worktree))
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary_path.write_text(json.dumps({**summary, "repo": str(other_worktree)}) + "\n", encoding="utf-8")
    cross = prepare(tmp_path, repo, run_dir)
    assert cross.returncode == 10, cross.stdout
    assert json.loads(cross.stdout)["hard_gate_failures"][0]["reason_code"] == "handoff_targets_another_worktree"
    cross_mode = prepare(tmp_path, repo, run_dir, "--cross-worktree")
    assert cross_mode.returncode == 0, cross_mode.stdout
    # 跨 worktree 模式要把 RVF worktree 那边的 scoped diff 也给出来——修复在那边，agent 要照着移植。
    assert json.loads(cross_mode.stdout)["target_worktree_scoped_diff"]["target_worktree"] == str(
        other_worktree.resolve()
    )
    summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")

    bare_handoff = run_land(
        tmp_path,
        repo,
        "prepare",
        "--stdin",
        "--repo",
        str(repo),
        "--format",
        "json",
        stdin_text="# handoff\n\n- handoff_status: COMPLETED\n",
    )
    assert bare_handoff.returncode == 13, bare_handoff.stdout

    scopeless_run = tmp_path / "scopeless-run"
    (scopeless_run / "artifacts").mkdir(parents=True)
    (scopeless_run / "summary.json").write_text(json.dumps({"repo": str(repo)}) + "\n", encoding="utf-8")
    (scopeless_run / "artifacts" / "handoff.md").write_text("# handoff\n", encoding="utf-8")
    scopeless = prepare(tmp_path, repo, scopeless_run)
    assert scopeless.returncode == 11, scopeless.stdout
    assert (
        json.loads(scopeless.stdout)["hard_gate_failures"][0]["reason_code"]
        == "reviewed_scope_unknown_with_dirty_worktree"
    )


def test_commit_refuses_stale_prepare_plan_and_unfilled_draft(tmp_path: Path) -> None:
    repo, run_dir, _counter = reviewed_dirty_run(tmp_path)
    missing_plan = commit(tmp_path, repo, run_dir)
    assert missing_plan.returncode == 20, missing_plan.stdout

    prepared = json.loads(prepare(tmp_path, repo, run_dir).stdout)
    unfilled = run_land(
        tmp_path,
        repo,
        "commit",
        "--run-dir",
        str(run_dir),
        "--message-file",
        "-",
        stdin_text=prepared["commit_message_draft"],
    )
    assert unfilled.returncode == 2
    assert "草稿占位" in unfilled.stderr

    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    git(repo, "add", "side.txt")
    git(repo, "commit", "-q", "-m", "moved HEAD after prepare")
    stale = commit(tmp_path, repo, run_dir)
    assert stale.returncode == 20, stale.stdout
    assert json.loads(stale.stdout)["reason_code"] == "prepare_plan_stale"


def test_land_with_nothing_to_commit_still_seals_and_writes_evidence(tmp_path: Path) -> None:
    """已提交工作的 committed-round review 无 finding：land 没有东西可提交，但仍须封窗，
    否则下一次 Stop 会把这段已审的已提交工作当成未审（backlog d3f64 的无改动形态）。"""
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    report = json.loads(prepare(tmp_path, repo, run_dir).stdout)
    assert report["nothing_to_commit"] is True
    assert report["validation"]["validation_reusable"] is True

    committed = commit(tmp_path, repo, run_dir)
    assert committed.returncode == 0, committed.stdout + committed.stderr
    result = json.loads(committed.stdout)
    assert result["status"] == "nothing_to_commit"
    assert result["commit_oid"] is None
    assert result["round_baseline_seal"]["sealed"] is True
    assert result["candidate_evidence"]["status"] == "written", result["candidate_evidence"]


def run_fix_ledger_script(tmp_path: Path, script_name: str, *arguments: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_DIRECTORY / script_name), *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=land_environment(tmp_path),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_prepare_reads_fix_ledger_written_by_real_fix_attempt_script(tmp_path: Path) -> None:
    """ledger 由真实 ``rvf_fix_issue.py`` / ``rvf_fix_attempt.py`` 写出（不手抄字段形状）：
    stop 写 ``changed_paths`` 为 ``{path, op}`` 列表、apply 把 attempt.json 的 status 改成 applied。
    已合回的修复必须进「已采纳」，其补丁路径（含 scope expansion 新增文件）必须并入可 stage 全集。"""
    repo, run_dir, _counter = reviewed_dirty_run(tmp_path)
    log_root = tmp_path / "fix-ledger-tracker-state"
    issue_file = tmp_path / "rvf-land-ledger-issue.json"
    issue_file.write_text(
        json.dumps(
            {
                "run_id": "rvf-candidate-evidence-test",
                "issue_id": "RVF-L1",
                "kind": "REAL",
                "severity": "medium",
                "path": "tracked.txt",
                "line": 2,
                "title": "tracked.txt 的已审改动缺少补充说明",
                "summary": "tracked.txt 的已审改动缺少补充说明",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # 两腿 no_issues、这条 fix-issue 来自主 agent 自审：先在合并处置里登记为非 reviewer 来源，upsert 才放行。
    merge_decisions_file = tmp_path / "rvf-land-ledger-merge-decisions.json"
    merge_decisions_file.write_text(
        json.dumps({"non_reviewer_fix_issue_sources": [{"issue_id": "RVF-L1", "origin": "main_agent_self_review"}]})
        + "\n",
        encoding="utf-8",
    )
    run_fix_ledger_script(
        tmp_path,
        "rvf_merge_dispositions.py",
        "write",
        "--run-dir",
        str(run_dir),
        "--decisions-file",
        str(merge_decisions_file),
        "--json",
    )
    common = ["--run-dir", str(run_dir), "--log-root", str(log_root)]
    run_fix_ledger_script(
        tmp_path, "rvf_fix_issue.py", "upsert", "--repo", str(repo), "--issue-file", str(issue_file), *common
    )
    prepared_attempt = run_fix_ledger_script(
        tmp_path, "rvf_fix_attempt.py", "prepare", "--repo", str(repo), "--issue-id", "RVF-L1", *common
    )
    attempt_id = prepared_attempt["attempt_id"]
    attempt_worktree = Path(prepared_attempt["worktree_path"])
    run_fix_ledger_script(tmp_path, "rvf_fix_attempt.py", "start", "--attempt-id", attempt_id, *common)
    (attempt_worktree / "tracked.txt").write_text(
        "candidate\nreviewed change\nfix note\n", encoding="utf-8"
    )
    (attempt_worktree / "fix-helper-outside-reviewed-scope.txt").write_text("helper\n", encoding="utf-8")
    stopped = run_fix_ledger_script(
        tmp_path,
        "rvf_fix_attempt.py",
        "stop",
        "--attempt-id",
        attempt_id,
        "--status",
        "fixed",
        "--scope-expansion-path",
        "fix-helper-outside-reviewed-scope.txt",
        "--scope-expansion-reason",
        "最小修复需要一个新文件",
        *common,
    )
    assert all(isinstance(entry, dict) for entry in stopped["changed_paths"])
    applied = run_fix_ledger_script(
        tmp_path, "rvf_fix_attempt.py", "apply", "--attempt-id", attempt_id, "--target-repo", str(repo), *common
    )
    assert applied["status"] == "applied"

    prepared = prepare(tmp_path, repo, run_dir)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    report = json.loads(prepared.stdout)
    disposition = next(
        item for item in report["issue_dispositions_from_fix_ledger"] if item["issue_id"] == "RVF-L1"
    )
    assert disposition["fix_status"] == "fixed"
    assert disposition["fix_applied_to_worktree"] is True
    assert sorted(disposition["changed_paths"]) == ["fix-helper-outside-reviewed-scope.txt", "tracked.txt"]
    assert "fix-helper-outside-reviewed-scope.txt" in report["stageable_dirty_paths"]
    assert report["unrelated_dirty_paths"] == []
    draft = report["commit_message_draft"]
    accepted_section = draft.split("已采纳（fixed 且已合回工作树）：", 1)[1].split("未采纳：", 1)[0]
    assert "- RVF-L1" in accepted_section
    assert "RVF-L1" not in draft.split("未采纳：", 1)[1]



def isolated_state_root_files_mentioning(tmp_path: Path, needle: str) -> list[str]:
    """隔离状态根（RVF_HOME / user-local-data）里提到 ``needle`` 的文件：用来独立核实封窗是否写过 marker。"""
    matches: list[str] = []
    for state_root in (tmp_path / "rvf-home", tmp_path / "user-data"):
        for candidate in state_root.rglob("*") if state_root.exists() else []:
            if candidate.is_file() and needle in candidate.read_text(encoding="utf-8", errors="replace"):
                matches.append(str(candidate))
    return matches


def test_pre_commit_hook_rewriting_content_refuses_seal_and_evidence(tmp_path: Path) -> None:
    """pre-commit hook 改写文件并纳入本次提交：提交进去的字节不是验证过的字节。
    commit 已发生（如实报告、不回滚），但不得封窗、不得写 evidence——否则下一次 Stop 把这段未验证内容当成已审。"""
    repo, run_dir, counter = reviewed_dirty_run(tmp_path)
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    pre_commit_hook = repo / ".git" / "hooks" / "pre-commit"
    pre_commit_hook.parent.mkdir(parents=True, exist_ok=True)
    pre_commit_hook.write_text(
        "#!/bin/sh\nprintf 'candidate\\nreviewed change, reformatted by hook\\n' > tracked.txt\ngit add tracked.txt\n",
        encoding="utf-8",
    )
    pre_commit_hook.chmod(0o755)

    committed = commit(tmp_path, repo, run_dir)
    assert committed.returncode == 26, committed.stdout + committed.stderr
    result = json.loads(committed.stdout)
    assert result["status"] == "committed"
    assert result["reason_code"] == "candidate_content_changed_during_commit"
    assert result["commit_oid"] == git(repo, "rev-parse", "HEAD").strip()
    assert "reformatted by hook" in git(repo, "show", "HEAD:tracked.txt"), "hook 改写应已进入提交（前提校验）"
    assert result["candidate_content_manifest_sha256_after_commit"] != (
        result["validation"]["candidate_content_manifest_sha256"]
    )
    assert result["round_baseline_seal"]["sealed"] is False
    assert result["candidate_evidence"]["status"] == "skipped_content_changed_during_commit"
    assert isolated_state_root_files_mentioning(tmp_path, result["commit_oid"]) == [], "封窗 marker 不应推进到新 HEAD"
    assert counter_value(counter) == 1


def test_rename_with_one_end_outside_stageable_scope_is_not_dragged_into_commit(tmp_path: Path) -> None:
    """RVF-002(a)：rename 的新旧两端会被一起 ``git add``；只有一端在 reviewed scope 内时，另一端不得被顺带
    拖进提交（落地 delta 用 ``--no-renames``，越界那一端会让 evidence 在提交后才拒绝）。必须 exit 12 并点名越界端，
    agent 用 --extra-path 显式确认后才放行，且 prepare 提前摆出 evidence 会拒绝的路径。"""
    repo, run_dir, _counter = reviewed_dirty_run(tmp_path)
    git(repo, "mv", "tracked.txt", "renamed-outside-reviewed-scope.txt")

    refused = prepare(tmp_path, repo, run_dir)
    assert refused.returncode == 12, refused.stdout + refused.stderr
    report = json.loads(refused.stdout)
    failure = report["hard_gate_failures"][0]
    assert failure["reason_code"] == "unrelated_dirty_paths_would_remain_after_commit"
    assert failure["paths"] == ["renamed-outside-reviewed-scope.txt"]
    assert report["stageable_dirty_paths"] == []

    confirmed = prepare(tmp_path, repo, run_dir, "--extra-path", "renamed-outside-reviewed-scope.txt")
    assert confirmed.returncode == 0, confirmed.stdout + confirmed.stderr
    confirmed_report = json.loads(confirmed.stdout)
    assert confirmed_report["stageable_dirty_paths"] == ["renamed-outside-reviewed-scope.txt"]
    assert confirmed_report["stageable_dirty_paths_outside_candidate_evidence_landing_universe"] == [
        "renamed-outside-reviewed-scope.txt"
    ]


def test_handoff_reviewed_scope_cannot_widen_stage_set_beyond_scope_contract(tmp_path: Path) -> None:
    """RVF-002(b)：handoff 正文里的 reviewed scope 不能把可 stage 上界撑到 scope contract 全集之外——
    contract 全集才是 evidence 落地 delta 的收敛判据；否则提交会越过判据、evidence 在提交后才拒绝。"""
    repo, run_dir, _counter = reviewed_dirty_run(tmp_path)
    (repo / "handoff-only-scope-path.txt").write_text("not in the scope contract\n", encoding="utf-8")
    widened_handoff = run_dir / "artifacts" / "handoff-with-widened-review-scope.md"
    widened_handoff.write_text(
        (run_dir / "artifacts" / "handoff.md").read_text(encoding="utf-8")
        + "\n## Review scope\n\n- `tracked.txt`\n- `handoff-only-scope-path.txt`\n",
        encoding="utf-8",
    )
    arguments = ("prepare", "--handoff", str(widened_handoff), "--repo", str(repo), "--format", "json")

    refused = run_land(tmp_path, repo, *arguments)
    assert refused.returncode == 12, refused.stdout + refused.stderr
    report = json.loads(refused.stdout)
    assert report["hard_gate_failures"][0]["paths"] == ["handoff-only-scope-path.txt"]
    assert report["stageable_dirty_paths"] == ["tracked.txt"]
    plan = json.loads((run_dir / "artifacts" / "rvf-land-prepare-plan.json").read_text(encoding="utf-8"))
    assert "handoff-only-scope-path.txt" not in plan["stageable_path_universe"]


if __name__ == "__main__":
    _cases = sorted(
        (name, value)
        for name, value in list(globals().items())
        if name.startswith("test_") and callable(value) and value.__module__ == __name__
    )
    for _name, _case in _cases:
        _case(Path(tempfile.mkdtemp(prefix="rvf-land-single-step-test-")))
    print(f"rvf land single step tests OK ({len(_cases)} cases)")
