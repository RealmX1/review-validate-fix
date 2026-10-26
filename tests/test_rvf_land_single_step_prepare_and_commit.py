"""``rvf_land.py`` prepare / commit 的端到端测试（隔离 HOME 状态根、临时 run 目录、真 git 仓库）。

两条主路径各一条：
- 无补修正：run 自己的 validation index 已满足 evidence 门槛 ⇒ 零执行复用（计数器证明验证命令没跑）。
- 有补修正：内容摘要变了 ⇒ 按结构化 argv 经缓存重跑（计数器 +1），evidence 仍写入成功。
其余覆盖硬门槛退出码、验证失败回显尾部、旧 run 无 argv 工件、prepare plan 过期、无改动 land。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

TESTS_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIRECTORY))
# 复用 evidence 测试的「已完成 run」构造器，而不是手抄一份：两边断言的是同一份 run 形状，
# 手抄会在 evidence 门槛演进时悄悄漂移。
from test_reviewed_task_candidate_evidence import (  # noqa: E402
    ROOT,
    SCRIPT_DIRECTORY,
    canonical_bytes,
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
    tmp_path: Path,
    repo: Path,
    *arguments: str,
    stdin_text: str | None = None,
    environment_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RVF_LAND), *arguments],
        input=stdin_text,
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
        env={**land_environment(tmp_path), **(environment_overrides or {})},
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


def prepare(
    tmp_path: Path,
    repo: Path,
    run_dir: Path,
    *extra: str,
    environment_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
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
        environment_overrides=environment_overrides,
    )


def commit(
    tmp_path: Path,
    repo: Path,
    run_dir: Path,
    *extra: str,
    environment_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_land(
        tmp_path,
        repo,
        "commit",
        "--run-dir",
        str(run_dir),
        "--message-file",
        "-",
        *extra,
        stdin_text=LAND_TEST_COMMIT_MESSAGE,
        environment_overrides=environment_overrides,
    )


LAND_TEST_COMMIT_MESSAGE = "fix(test): land 单步化\n\nsanity-check：自洽\n验证：\n[[rvf-land-validation-summary]]\n"
LEAVE_UNRELATED_DIRTY_PATHS_FLAG = "--commit-staged-scope-only-and-leave-unrelated-dirty-paths"


def rewrite_scope_contract_fields(run_dir: Path, **fields: object) -> None:
    """同时改顶层与 ``canonical_scope`` 并重算 ``scope_hash``：evidence writer 校验这三者一致。"""
    contract_path = run_dir / "artifacts" / "inputs" / "scope.contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    canonical_scope = {**contract["canonical_scope"], **fields}
    contract.update(fields)
    contract["canonical_scope"] = canonical_scope
    contract["scope_hash"] = hashlib.sha256(canonical_bytes(canonical_scope)).hexdigest()
    contract_path.write_bytes(canonical_bytes(contract))


def point_run_summary_at_another_worktree(tmp_path: Path, repo: Path, run_dir: Path) -> Path:
    """让 run 的目标仓库变成同仓的另一个 worktree：当前 worktree 就成了跨 worktree 模式里的主会话 worktree。"""
    other_worktree = tmp_path / "rvf-fork-worktree"
    git(repo, "worktree", "add", "-q", "--detach", str(other_worktree))
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary_path.write_text(json.dumps({**summary, "repo": str(other_worktree)}) + "\n", encoding="utf-8")
    return other_worktree


def paths_in_head_commit(repo: Path) -> list[str]:
    return sorted(git(repo, "show", "--name-only", "--format=", "HEAD").split())


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
    assert not (scopeless_run / "artifacts/rvf-land-prepare-plan.json").exists()


def test_prepare_refuses_explicit_run_directory_conflicting_with_handoff_before_writing_plan(tmp_path: Path) -> None:
    repo, handoff_run, _counter = reviewed_dirty_run(tmp_path)
    selected_run = create_completed_run(tmp_path, repo, "another-run")
    completed = prepare(tmp_path, repo, handoff_run, "--run-dir", str(selected_run))
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "run" in completed.stderr
    assert not (selected_run / "artifacts/rvf-land-prepare-plan.json").exists()
    assert not (handoff_run / "artifacts/rvf-land-prepare-plan.json").exists()


def test_prepare_can_bind_pasted_handoff_to_exact_explicit_run_directory(tmp_path: Path) -> None:
    repo, run_dir, _counter = reviewed_dirty_run(tmp_path)
    completed = run_land(
        tmp_path, repo, "prepare", "--stdin", "--repo", str(repo), "--run-dir", str(run_dir),
        "--format", "json", stdin_text="# 本轮交接\n- handoff_status: COMPLETED\n",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["run_id"] == json.loads((run_dir / "summary.json").read_text())["run_id"]
    assert report["run_dir"] == str(run_dir)
    assert report["prepare_plan_path"]


def test_prepare_refuses_pasted_run_id_conflicting_with_selected_run(tmp_path: Path) -> None:
    repo, run_dir, _counter = reviewed_dirty_run(tmp_path)
    completed = run_land(
        tmp_path, repo, "prepare", "--stdin", "--repo", str(repo), "--run-dir", str(run_dir),
        "--format", "json", stdin_text="- run id: `rvf-foreign-run`\n- handoff_status: COMPLETED\n",
    )
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert not (run_dir / "artifacts/rvf-land-prepare-plan.json").exists()


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


def applied_expansion_run_from_real_attempt_lifecycle(
    tmp_path: Path, *, acknowledge_guarded_helper: bool = False
) -> tuple[Path, Path, str, list[str]]:
    """经真实 prepare/start/stop/apply 生成已应用扩展，同时保留其主会话确认事实。"""
    repo, run_dir, _counter = reviewed_dirty_run(tmp_path)
    if acknowledge_guarded_helper:
        (repo / "fix-helper-outside-reviewed-scope.txt").write_text("原有背景改动\n", encoding="utf-8")
        rewrite_scope_contract_fields(
            run_dir,
            background_files=["fix-helper-outside-reviewed-scope.txt"],
            protected_files=["fix-helper-outside-reviewed-scope.txt"],
        )
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
        ("--acknowledged-protected-or-background-path-modification" if acknowledge_guarded_helper
         else "--scope-expansion-path"),
        "fix-helper-outside-reviewed-scope.txt",
        "--scope-expansion-reason",
        "最小修复需要一个新文件",
        *common,
    )
    assert all(isinstance(entry, dict) for entry in stopped["changed_paths"])
    applied = run_fix_ledger_script(
        tmp_path, "rvf_fix_attempt.py", "apply", "--attempt-id", attempt_id, "--target-repo", str(repo),
        *(["--confirm-acknowledged-protected-or-background-path-modification"] if acknowledge_guarded_helper else []),
        *common,
    )
    assert applied["status"] == "applied"
    return repo, run_dir, attempt_id, common


def test_prepare_reads_fix_ledger_written_by_real_fix_attempt_script(tmp_path: Path) -> None:
    """ledger 由真实 stop/apply 写出；已合回修复的补丁路径（含扩展）必须并入可 stage 全集。"""
    repo, run_dir, attempt_id, _common = applied_expansion_run_from_real_attempt_lifecycle(tmp_path)

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
    # 正式 land 必须消费真实 stop/apply 写出的扩展事实，而非只在 prepare 中展示它。
    handoff = run_dir / "artifacts/handoff.md"
    handoff.write_text(handoff.read_text().replace("共 0 条，0 真实修复", "共 1 条，1 真实修复") +
                       "\n- **[fixed]** `tracked.txt:2` - RVF-L1\n")
    for name in ("targeted-contract-tests", "handoff-validation-001"):
        validated = record_validation(tmp_path, repo, run_dir, name, sys.executable, "-c", "print('passed')")
        assert validated.returncode == 0, validated.stderr
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    landed = commit(tmp_path, repo, run_dir)
    assert landed.returncode == 0, landed.stdout + landed.stderr
    result = json.loads(landed.stdout)
    assert result["candidate_evidence"]["status"] == "written"
    candidate = json.loads(Path(result["candidate_evidence"]["candidate_evidence_path"]).read_text())
    assert candidate["schema_version"] == 2
    expansion = json.loads(Path(candidate["scope"]["applied_scope_expansion_receipt_path"]).read_text())
    assert expansion["expansions"][0]["attempt_id"] == attempt_id
    assert expansion["expansions"][0]["paths"] == ["fix-helper-outside-reviewed-scope.txt"]


@pytest.mark.parametrize("acknowledge_guarded_helper", [False, True])
def test_land_keeps_historical_applied_expansion_when_latest_attempt_is_false_positive(
    tmp_path: Path, acknowledge_guarded_helper: bool
) -> None:
    """最新误报只更新完成结论，不能抹掉历史已应用补丁或主会话对背景路径的确认。"""
    repo, run_dir, applied_attempt_id, common = applied_expansion_run_from_real_attempt_lifecycle(
        tmp_path, acknowledge_guarded_helper=acknowledge_guarded_helper
    )
    next_attempt = run_fix_ledger_script(
        tmp_path, "rvf_fix_attempt.py", "prepare", "--repo", str(repo), "--issue-id", "RVF-L1", *common
    )
    next_attempt_id = next_attempt["attempt_id"]
    run_fix_ledger_script(tmp_path, "rvf_fix_attempt.py", "start", "--attempt-id", next_attempt_id, *common)
    # 最新 attempt 的外部改动并未 apply；即使同名 WIP 出现在主工作树，也不能借历史 applied 放行。
    unrelated_wip_path = "latest-unapplied-other-session-wip.txt"
    (Path(next_attempt["worktree_path"]) / unrelated_wip_path).write_text("未应用的 attempt 改动\n", encoding="utf-8")
    stopped = run_fix_ledger_script(
        tmp_path, "rvf_fix_attempt.py", "stop", "--attempt-id", next_attempt_id, "--status", "false_positive",
        "--scope-expansion-path", unrelated_wip_path, "--scope-expansion-reason", "误报 attempt 的未应用改动", *common,
    )
    assert unrelated_wip_path in {entry["path"] for entry in stopped["all_changed_paths"]}
    assert unrelated_wip_path in {entry["path"] for entry in stopped["changed_paths"]}
    handoff = run_dir / "artifacts/handoff.md"
    handoff.write_text(handoff.read_text().replace("共 0 条，0 真实修复 / 0 误报", "共 1 条，0 真实修复 / 1 误报") +
                       "\n- **[false_positive]** `tracked.txt:2` - RVF-L1\n", encoding="utf-8")
    for name in ("targeted-contract-tests", "handoff-validation-001"):
        validated = record_validation(tmp_path, repo, run_dir, name, sys.executable, "-c", "print('passed')")
        assert validated.returncode == 0, validated.stderr

    (repo / unrelated_wip_path).write_text("主工作树里其他会话的 WIP\n", encoding="utf-8")
    index_before = git(repo, "diff", "--cached", "--binary")
    refused_wip = prepare(tmp_path, repo, run_dir)
    refused_report = json.loads(refused_wip.stdout)
    assert sorted(refused_report["stageable_dirty_paths"]) == ["fix-helper-outside-reviewed-scope.txt", "tracked.txt"]
    assert refused_wip.returncode == 12, refused_wip.stdout + refused_wip.stderr
    assert refused_report["unrelated_dirty_paths"] == [unrelated_wip_path]
    assert (repo / unrelated_wip_path).read_text(encoding="utf-8") == "主工作树里其他会话的 WIP\n"
    assert git(repo, "diff", "--cached", "--binary") == index_before
    # 移除本测试特意加入的 WIP 后，完整 prepare/commit 仍应绑定历史 applied 扩展并写出 v2 evidence。
    (repo / unrelated_wip_path).unlink()
    prepared = prepare(tmp_path, repo, run_dir)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    report = json.loads(prepared.stdout)
    assert report["candidate_evidence_qualification"]["status"] == "eligible"
    disposition = report["issue_dispositions_from_fix_ledger"][0]
    assert disposition["fix_status"] == "false_positive"
    assert disposition["fix_applied_to_worktree"] is False
    assert disposition["changed_paths"] == ["fix-helper-outside-reviewed-scope.txt", "tracked.txt"]
    assert disposition["confirmed_acknowledged_paths"] == (
        ["fix-helper-outside-reviewed-scope.txt"] if acknowledge_guarded_helper else []
    )
    landed = commit(tmp_path, repo, run_dir)
    assert landed.returncode == 0, landed.stdout + landed.stderr
    result = json.loads(landed.stdout)
    assert paths_in_head_commit(repo) == ["fix-helper-outside-reviewed-scope.txt", "tracked.txt"]
    assert result["candidate_evidence"]["status"] == "written"
    candidate = json.loads(Path(result["candidate_evidence"]["candidate_evidence_path"]).read_text())
    assert candidate["schema_version"] == 2
    expansion = json.loads(Path(candidate["scope"]["applied_scope_expansion_receipt_path"]).read_text())
    assert expansion["expansions"][0]["attempt_id"] == applied_attempt_id
    assert expansion["expansions"][0]["paths"] == ["fix-helper-outside-reviewed-scope.txt"]
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("latest_attempt_status", ["failed", "started"])
def test_land_refuses_latest_unresolved_attempt_despite_historical_applied_expansion(
    tmp_path: Path, latest_attempt_status: str
) -> None:
    """历史 applied 只证明路径归属，最新失败或未完成的 issue 仍须在 stage/commit 前拒绝。"""
    repo, run_dir, _applied_attempt_id, common = applied_expansion_run_from_real_attempt_lifecycle(tmp_path)
    next_attempt = run_fix_ledger_script(
        tmp_path, "rvf_fix_attempt.py", "prepare", "--repo", str(repo), "--issue-id", "RVF-L1", *common
    )
    run_fix_ledger_script(tmp_path, "rvf_fix_attempt.py", "start", "--attempt-id", next_attempt["attempt_id"], *common)
    if latest_attempt_status == "failed":
        run_fix_ledger_script(
            tmp_path, "rvf_fix_attempt.py", "stop", "--attempt-id", next_attempt["attempt_id"], "--status", "failed", *common
        )
    for name in ("targeted-contract-tests", "handoff-validation-001"):
        validated = record_validation(tmp_path, repo, run_dir, name, sys.executable, "-c", "print('passed')")
        assert validated.returncode == 0, validated.stderr
    head_before = git(repo, "rev-parse", "HEAD")
    index_before = git(repo, "diff", "--cached", "--binary")
    prepared = prepare(tmp_path, repo, run_dir)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    report = json.loads(prepared.stdout)
    assert sorted(report["stageable_dirty_paths"]) == ["fix-helper-outside-reviewed-scope.txt", "tracked.txt"]
    assert report["candidate_evidence_qualification"]["reason_code"] == "canonical_fix_completion_invalid"
    refused = commit(tmp_path, repo, run_dir)
    assert refused.returncode == 28, refused.stdout + refused.stderr
    assert json.loads(refused.stdout)["reason_code"] == "canonical_fix_completion_invalid"
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert git(repo, "diff", "--cached", "--binary") == index_before



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



def reviewed_dirty_run_with_background_dirty_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    """run 在 prepare 时已看到别的会话的 WIP（进了 contract 的 background / protected），验证也是在这份内容上跑的
    ——零执行复用成立，于是这份 WIP 一旦被 stage 就会连同封窗、evidence 一路成功（审计 H1 的隔离实验形态）。"""
    repo = repository(tmp_path / "repo")
    (repo / "tracked.txt").write_text("candidate\nreviewed change\n", encoding="utf-8")
    (repo / "other-session-wip.txt").write_text("another session's unfinished work\n", encoding="utf-8")
    (repo / "included-by-rvf-scope-include.txt").write_text("claimed into primary scope\n", encoding="utf-8")
    run_dir = create_completed_run(tmp_path, repo)
    rewrite_scope_contract_fields(
        run_dir,
        # ``rvf scope --include`` 把 background 路径并入 primary_files，但不会把它从 background / protected 删掉。
        primary_files=["included-by-rvf-scope-include.txt", "tracked.txt"],
        fix_allowlist=["included-by-rvf-scope-include.txt", "tracked.txt"],
        background_files=["included-by-rvf-scope-include.txt", "other-session-wip.txt"],
        protected_files=["included-by-rvf-scope-include.txt", "other-session-wip.txt"],
    )
    counter = tmp_path / "validation-execution-counter.txt"
    completed = record_validation(
        tmp_path, repo, run_dir, "handoff-validation-001", *counting_validation_command(counter)
    )
    assert completed.returncode == 0, completed.stderr
    return repo, run_dir, counter


def test_background_dirty_path_of_another_session_is_never_automatically_staged(tmp_path: Path) -> None:
    """审计 H1：可 stage 集合曾等于 contract 路径全集（含 background_files），别人的 WIP 被 stage、提交、封窗，
    evidence 还写成功。evidence 判据的路径全集含 background 是既有口径（不动）；这里钉住的是 stage 集合。"""
    repo, run_dir, counter = reviewed_dirty_run_with_background_dirty_paths(tmp_path)
    head_before = git(repo, "rev-parse", "HEAD")

    refused = prepare(tmp_path, repo, run_dir)
    assert refused.returncode == 12, refused.stdout + refused.stderr
    report = json.loads(refused.stdout)
    assert report["unrelated_dirty_paths"] == ["other-session-wip.txt"]
    assert report["hard_gate_failures"][0]["paths"] == ["other-session-wip.txt"]
    # 显式出现在 contract 主审边界里的路径照常可 stage，即便它还挂在过期的 background / protected 列表上。
    assert sorted(report["stageable_dirty_paths"]) == ["included-by-rvf-scope-include.txt", "tracked.txt"]
    plan = json.loads((run_dir / "artifacts" / "rvf-land-prepare-plan.json").read_text(encoding="utf-8"))
    assert "other-session-wip.txt" not in plan["stageable_path_universe"]

    committed = commit(tmp_path, repo, run_dir)
    assert committed.returncode == 12, committed.stdout + committed.stderr
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert "?? other-session-wip.txt" in git(repo, "status", "--porcelain=v1", "-uall")

    # 确属本次工作时，--extra-path 仍是唯一的显式纳入方式。
    assert prepare(tmp_path, repo, run_dir, "--extra-path", "other-session-wip.txt").returncode == 0
    assert counter_value(counter) == 1


def test_leave_unrelated_flag_commits_only_stage_set_and_keeps_other_session_wip(tmp_path: Path) -> None:
    """同一个 background WIP，加显式开关：只提交 stage 集合，WIP 原样留在工作树；evidence 跳过（工作树不 clean）。"""
    repo, run_dir, counter = reviewed_dirty_run_with_background_dirty_paths(tmp_path)
    prepared = prepare(tmp_path, repo, run_dir, LEAVE_UNRELATED_DIRTY_PATHS_FLAG)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    assert json.loads(prepared.stdout)["leave_unrelated_dirty_paths_uncommitted"] is True

    committed = commit(tmp_path, repo, run_dir)  # 开关写进了 plan，commit 自动沿用
    assert committed.returncode == 0, committed.stdout + committed.stderr
    result = json.loads(committed.stdout)
    assert result["staged_paths"] == ["included-by-rvf-scope-include.txt", "tracked.txt"]
    assert paths_in_head_commit(repo) == ["included-by-rvf-scope-include.txt", "tracked.txt"]
    assert result["unrelated_dirty_paths_left_uncommitted"] == ["other-session-wip.txt"]
    assert result["worktree_clean_after_commit"] is False
    assert result["dirty_paths_remaining_beyond_declared_unrelated"] == []
    assert result["candidate_evidence"]["status"] == "skipped_unrelated_dirty_paths_left_uncommitted_by_explicit_flag"
    assert result["round_baseline_seal"]["sealed"] is True, "同 worktree 模式：提交进去的就是 RVF 审过的内容"
    assert git(repo, "status", "--porcelain=v1", "-uall") == "?? other-session-wip.txt"
    assert counter_value(counter) == 1


def test_pre_commit_hook_adding_unchanged_path_outside_stage_set_refuses_seal_and_evidence(tmp_path: Path) -> None:
    """RVF-001：``commit --only`` 让 hook 跑在临时 index 上，pre-commit ``git add`` 了 stage 集合外、内容字节未变的
    路径（这里是保留开关放行的别的会话 WIP）时，该路径照样进入本次提交，真实 index 却被留成 D+??。内容摘要不变
    （exit 26 的摘要判据漏掉），该路径又仍落在声明的无关集合里（exit 25 也漏掉）——必须直接比对提交实际包含的路径：
    commit 已发生（如实报告、不回滚），但不得封窗、不得写 evidence。"""
    repo, run_dir, counter = reviewed_dirty_run_with_background_dirty_paths(tmp_path)
    assert prepare(tmp_path, repo, run_dir, LEAVE_UNRELATED_DIRTY_PATHS_FLAG).returncode == 0
    pre_commit_hook = repo / ".git" / "hooks" / "pre-commit"
    pre_commit_hook.parent.mkdir(parents=True, exist_ok=True)
    pre_commit_hook.write_text("#!/bin/sh\ngit add other-session-wip.txt\n", encoding="utf-8")
    pre_commit_hook.chmod(0o755)

    committed = commit(tmp_path, repo, run_dir)
    assert committed.returncode == 26, committed.stdout + committed.stderr
    result = json.loads(committed.stdout)
    assert result["status"] == "committed"
    assert result["reason_code"] == "committed_paths_outside_stage_set"
    assert result["staged_paths"] == ["included-by-rvf-scope-include.txt", "tracked.txt"]
    assert "other-session-wip.txt" in paths_in_head_commit(repo), "hook add 的 stage 集合外路径应已进入提交（前提校验）"
    assert result["committed_paths_outside_stage_set"] == ["other-session-wip.txt"]
    assert result["candidate_content_manifest_sha256_after_commit"] == (
        result["validation"]["candidate_content_manifest_sha256"]
    ), "前提校验：内容字节未变，摘要判据单独抓不到"
    assert result["round_baseline_seal"]["sealed"] is False
    assert result["candidate_evidence"]["status"] == "skipped_committed_paths_outside_stage_set"
    assert isolated_state_root_files_mentioning(tmp_path, result["commit_oid"]) == [], "封窗 marker 不应推进到新 HEAD"
    assert counter_value(counter) == 1


def test_commit_requires_explicit_run_dir_and_ignores_rvf_run_dir_environment_variable(tmp_path: Path) -> None:
    """审计 H2a：commit 缺 --run-dir 时曾回退读 RVF_RUN_DIR；reviewer 子进程、Codex task shell 都带着它（指向活 run）。"""
    repo, run_dir, _counter = reviewed_dirty_run(tmp_path)
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    head_before = git(repo, "rev-parse", "HEAD")
    without_run_dir = run_land(
        tmp_path,
        repo,
        "commit",
        "--message-file",
        "-",
        stdin_text=LAND_TEST_COMMIT_MESSAGE,
        environment_overrides={"RVF_RUN_DIR": str(run_dir)},
    )
    assert without_run_dir.returncode == 2, without_run_dir.stdout + without_run_dir.stderr
    assert "--run-dir" in without_run_dir.stderr
    assert git(repo, "rev-parse", "HEAD") == head_before


def test_cross_worktree_flag_leaves_parallel_wip_including_pre_staged_paths_and_skips_unproven_seal(
    tmp_path: Path,
) -> None:
    """审计 H4 + 跨 worktree 封窗待证项。

    H4：主会话 worktree 有并行 WIP 是常态；旧 intake 流程保留无关改动照常提交，T2 之后一律 exit 12。显式开关
    只提交 stage 集合——别人预先 ``git add`` 过的路径也不能被 ``git commit`` 顺带带走。

    封窗：tracker 的已审单元按 worktree 记账，fork 里审过的内容在主会话 worktree 不算已审；当前内容与 RVF
    验证过的内容不一致（这里是并行 WIP 让内容摘要变了、验证经缓存重跑）时，封窗会把未经审查的字节一并宣称已审。
    """
    repo, run_dir, counter = reviewed_dirty_run(tmp_path)
    point_run_summary_at_another_worktree(tmp_path, repo, run_dir)
    (repo / "main-session-parallel-wip.txt").write_text("main session keeps working\n", encoding="utf-8")
    (repo / "main-session-pre-staged-wip.txt").write_text("staged by the main session\n", encoding="utf-8")
    git(repo, "add", "main-session-pre-staged-wip.txt")

    without_flag = prepare(tmp_path, repo, run_dir, "--cross-worktree")
    assert without_flag.returncode == 12, without_flag.stdout + without_flag.stderr

    with_flag = prepare(tmp_path, repo, run_dir, "--cross-worktree", LEAVE_UNRELATED_DIRTY_PATHS_FLAG)
    assert with_flag.returncode == 0, with_flag.stdout + with_flag.stderr
    assert json.loads(with_flag.stdout)["unrelated_dirty_paths"] == [
        "main-session-parallel-wip.txt",
        "main-session-pre-staged-wip.txt",
    ]

    committed = commit(tmp_path, repo, run_dir)
    assert committed.returncode == 0, committed.stdout + committed.stderr
    result = json.loads(committed.stdout)
    assert result["cross_worktree_mode"] is True
    assert paths_in_head_commit(repo) == ["tracked.txt"], "别人预先 stage 的路径不得被带进提交"
    assert sorted(git(repo, "status", "--porcelain=v1", "-uall").splitlines()) == [
        "?? main-session-parallel-wip.txt",
        "A  main-session-pre-staged-wip.txt",
    ]
    assert result["unrelated_dirty_paths_left_uncommitted"] == [
        "main-session-parallel-wip.txt",
        "main-session-pre-staged-wip.txt",
    ]
    assert result["candidate_evidence"]["status"] == "skipped_unrelated_dirty_paths_left_uncommitted_by_explicit_flag"
    assert result["validation"]["mode"] == "reran_structured_argv_through_validation_result_cache"
    assert result["round_baseline_seal"] == {
        "sealed": False,
        "reason": "skipped_cross_worktree_landed_content_not_proven_identical_to_rvf_validated_content",
    }
    assert isolated_state_root_files_mentioning(tmp_path, result["commit_oid"]) == [], "封窗 marker 不应推进到新 HEAD"
    assert counter_value(counter) == 2


def test_cross_worktree_land_seals_when_landed_content_is_identical_to_rvf_validated_content(tmp_path: Path) -> None:
    """封窗待证项的另一半：当前内容与 RVF 验证过的内容字节一致（零执行复用成立）时，信任级别与同 worktree 模式
    相同，照常封窗——否则下一次 Stop 会对刚 land 的已审工作再问一次。"""
    repo, run_dir, counter = reviewed_dirty_run(tmp_path)
    point_run_summary_at_another_worktree(tmp_path, repo, run_dir)
    assert prepare(tmp_path, repo, run_dir, "--cross-worktree").returncode == 0
    committed = commit(tmp_path, repo, run_dir)
    assert committed.returncode == 0, committed.stdout + committed.stderr
    result = json.loads(committed.stdout)
    assert result["validation"]["mode"] == "reused_run_validation_index_with_zero_execution"
    assert result["round_baseline_seal"]["sealed"] is True
    assert counter_value(counter) == 1


def test_user_global_excludes_file_is_respected_when_listing_dirty_paths(tmp_path: Path) -> None:
    """审计 M5：列脏文件曾用 core.excludesFile 被置空的环境，commit 后的 clean 检查却按用户环境。用户全局 ignore
    的文件会触发假 exit 12；agent 照提示加 --extra-path 就把本地权限配置提交进去。两处统一尊重用户 excludesFile。"""
    repo, run_dir, _counter = reviewed_dirty_run(tmp_path)
    global_ignore = tmp_path / "user-global-gitignore"
    global_ignore.write_text("**/.claude/settings.local.json\n", encoding="utf-8")
    global_git_configuration = tmp_path / "user-global-gitconfig"
    global_git_configuration.write_text(f"[core]\n\texcludesFile = {global_ignore}\n", encoding="utf-8")
    user_global_git_configuration = {"GIT_CONFIG_GLOBAL": str(global_git_configuration)}
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text('{"permissions": {"allow": ["Bash(*)"]}}\n', encoding="utf-8")

    prepared = prepare(tmp_path, repo, run_dir, environment_overrides=user_global_git_configuration)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    report = json.loads(prepared.stdout)
    assert report["unrelated_dirty_paths"] == []
    assert report["stageable_dirty_paths"] == ["tracked.txt"]

    committed = commit(tmp_path, repo, run_dir, environment_overrides=user_global_git_configuration)
    assert committed.returncode == 0, committed.stdout + committed.stderr
    assert paths_in_head_commit(repo) == ["tracked.txt"]
    assert json.loads(committed.stdout)["worktree_clean_after_commit"] is True


def test_commit_lists_dirty_paths_changed_since_prepare(tmp_path: Path) -> None:
    """审计 L-F1a：prepare 与 commit 之间的改动只会触发验证重跑，agent 看不出变的是哪些路径——分不清是自己的
    最小修正，还是并发改动 / 别人的 git add。commit 按 prepare plan 里的逐路径指纹列出来（成功与拒绝都列）。"""
    repo, run_dir, _counter = reviewed_dirty_run(tmp_path)
    assert prepare(tmp_path, repo, run_dir).returncode == 0
    (repo / "concurrently-created.txt").write_text("appeared after prepare\n", encoding="utf-8")
    refused = commit(tmp_path, repo, run_dir)
    assert refused.returncode == 12, refused.stdout + refused.stderr
    assert json.loads(refused.stdout)["dirty_paths_changed_since_prepare"] == ["concurrently-created.txt"]
    (repo / "concurrently-created.txt").unlink()

    (repo / "tracked.txt").write_text("candidate\nreviewed change\nminimal land patch\n", encoding="utf-8")
    committed = commit(tmp_path, repo, run_dir)
    assert committed.returncode == 0, committed.stdout + committed.stderr
    assert json.loads(committed.stdout)["dirty_paths_changed_since_prepare"] == ["tracked.txt"]


RVF_LAND_ENTRYPOINT_DOCUMENTS = (
    ROOT / "plugins/review-validate-fix/skills/rvf-land/SKILL.md",
    ROOT / "plugins/review-validate-fix/skills/rvf-handoff-intake/SKILL.md",
    ROOT / "plugins/review-validate-fix/commands/rvf-handoff-commit.md",
)


def test_every_rvf_land_exit_code_has_a_disposition_row_in_all_three_land_entrypoint_documents() -> None:
    """审计 M6：exit 24 / 2 三份文档都没写处置，intake / commit 两个入口还缺 20/23/25/26（26 = 已提交但不得手动
    封窗）。三份入口文档各自内联一张退出码表（跨文档引用会让 agent 多发请求），由这里逐码核对不漂移。"""
    exit_codes = sorted(
        {
            int(match.group(1))
            for match in re.finditer(r"^RVF_LAND_EXIT_\w+ = (\d+)$", RVF_LAND.read_text(encoding="utf-8"), re.MULTILINE)
        }
    )
    assert {0, 2, 12, 24, 26} <= set(exit_codes), exit_codes
    missing = {
        document.name if document.name != "SKILL.md" else document.parent.name: [
            code
            for code in exit_codes
            if not re.search(rf"^\|\s*{code}\s*\|", document.read_text(encoding="utf-8"), re.MULTILINE)
        ]
        for document in RVF_LAND_ENTRYPOINT_DOCUMENTS
    }
    assert not any(missing.values()), missing
    for document in RVF_LAND_ENTRYPOINT_DOCUMENTS[1:]:
        assert LEAVE_UNRELATED_DIRTY_PATHS_FLAG in document.read_text(encoding="utf-8"), document


if __name__ == "__main__":
    _cases = sorted(
        (name, value)
        for name, value in list(globals().items())
        if name.startswith("test_") and callable(value) and value.__module__ == __name__
    )
    for _name, _case in _cases:
        if _case.__code__.co_argcount:
            _case(Path(tempfile.mkdtemp(prefix="rvf-land-single-step-test-")))
        else:
            _case()
    print(f"rvf land single step tests OK ({len(_cases)} cases)")
