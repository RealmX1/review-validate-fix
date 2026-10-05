#!/usr/bin/env python3
"""范围外问题提交 Kanban backlog 的契约（2026-10-04 开发者修订：不放跑、不当场修，作为新 task 提交）。

护的不变量：

* **BACKLOG-1（卡片可识别）** —— 标题前缀 ``[RVF 发现]``，prompt 首行 ``KANBAN_TASK_ORIGIN=…``；看板
  support agent 尚未上线时必带 ``KANBAN_SUPPORT_AGENT_RELATION_TRIAGE_STATUS=not_triaged``。
* **BACKLOG-2（卡片自包含）** —— 只建不 start、plan mode、**不传** ``--parent-session-id``（传了会 fork
  父会话上下文）、``--project-path`` 指向主仓而不是 task worktree。
* **BACKLOG-3（只拦完全重复）** —— 同仓所有 worktree 共用一份指纹 ledger；同一问题第二次提交不建新卡。
* **BACKLOG-4（不阻断）** —— Kanban 不可用 ⇒ 持久 pending，下次提交先重试；输入不合法才报错。
* **BACKLOG-5（不替别人提卡）** —— 落在其他会话未提交背景改动上的问题不提交。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import rvf_out_of_scope_findings_kanban_backlog_submission as backlog_submission  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "rvf-test@example.com")
    _git(path, "config", "user.name", "RVF Tester")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _fake_kanban(tmp_path: Path, *, fail: bool = False) -> tuple[str, Path]:
    calls = tmp_path / ("fake-kanban-failing-calls.jsonl" if fail else "fake-kanban-calls.jsonl")
    fake = tmp_path / ("fake_kanban_failing.py" if fail else "fake_kanban.py")
    body = (
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    if fail:
        body += "print('Kanban server is unavailable', file=sys.stderr)\nsys.exit(1)\n"
    else:
        body += "n = sum(1 for _ in open(%r))\nprint(json.dumps({'ok': True, 'task': {'id': 'card%%d' %% n}}))\n" % str(calls)
    fake.write_text(body, encoding="utf-8")
    return f"{sys.executable} {fake}", calls


def _finding(**overrides) -> dict:
    finding = {
        "title": "缓存键漏了平台维度",
        "path": "src/cache.py",
        "line": 12,
        "severity": "medium",
        "problem_statement": "缓存键只含版本号，跨平台会读到错误产物。",
        "evidence": "grep -n cache_key src/cache.py",
    }
    finding.update(overrides)
    return finding


@pytest.fixture()
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "rvf-home"))
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_BASE_REF", "main")
    monkeypatch.delenv("CODEX_RVF_CLINE_KANBAN_AGENT_ID", raising=False)
    return tmp_path


def test_created_card_is_marked_self_contained_and_targets_the_main_repository(
    isolated_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path = isolated_environment
    repo = _init_repo(tmp_path / "repo")
    task_worktree = tmp_path / "task-worktree"
    _git(repo, "worktree", "add", "-q", "--detach", str(task_worktree))
    task_cmd, calls = _fake_kanban(tmp_path)
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", task_cmd)
    findings = backlog_submission.normalize_out_of_scope_findings([_finding()], label="test")
    receipt = backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=task_worktree,
        findings=findings,
        finding_source=backlog_submission.FINDING_SOURCE_VALIDATE_ONLY_ATTEMPT_VALIDATOR,
        run_dir=None,
        run_id="rvf-test-run",
    )
    group = receipt["groups"][0]
    assert group["outcome"] == backlog_submission.SUBMISSION_OUTCOME_CREATED
    argv = json.loads(calls.read_text(encoding="utf-8").splitlines()[0])
    assert "create" in argv and "--start-in-plan-mode" in argv
    assert "--parent-session-id" not in argv
    assert Path(argv[argv.index("--project-path") + 1]).resolve() == repo.resolve()
    assert argv[argv.index("--base-ref") + 1] == "main"
    assert argv[argv.index("--title") + 1].startswith("[RVF 发现] ")
    prompt = argv[argv.index("--prompt") + 1]
    first_lines = prompt.splitlines()[:2]
    assert first_lines == [
        "KANBAN_TASK_ORIGIN=rvf_validated_out_of_scope_finding",
        "KANBAN_SUPPORT_AGENT_RELATION_TRIAGE_STATUS=not_triaged",
    ]
    assert "validator 已在原工作区核验属实" in prompt
    assert "cline-kanban-survey-sibling-task-work-in-same-workspace" in prompt
    assert any("已建卡 `card1`" in line for line in receipt["handoff_markdown_lines"])


def test_identical_finding_is_not_submitted_twice_across_worktrees_of_the_same_repository(
    isolated_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path = isolated_environment
    repo = _init_repo(tmp_path / "repo")
    other_worktree = tmp_path / "other-worktree"
    _git(repo, "worktree", "add", "-q", "--detach", str(other_worktree))
    task_cmd, calls = _fake_kanban(tmp_path)
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", task_cmd)
    findings = backlog_submission.normalize_out_of_scope_findings([_finding()], label="test")
    backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=repo, findings=findings,
        finding_source=backlog_submission.FINDING_SOURCE_VALIDATE_ONLY_ATTEMPT_VALIDATOR, run_dir=None,
    )
    # 同一问题换了空白与大小写，从另一个 worktree 再提交一次 ⇒ 不建新卡。
    again = backlog_submission.normalize_out_of_scope_findings(
        [_finding(problem_statement="缓存键只含版本号，  跨平台会读到错误产物。")], label="test"
    )
    receipt = backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=other_worktree, findings=again,
        finding_source=backlog_submission.FINDING_SOURCE_VALIDATE_THEN_FIX_ATTEMPT_FIXER, run_dir=None,
    )
    assert receipt["groups"][0]["outcome"] == "nothing_new_to_submit"
    assert receipt["groups"][0]["findings"][0]["previous_kanban_task_id"] == "card1"
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1


def test_unavailable_kanban_records_a_durable_pending_card_and_the_next_submission_retries_it(
    isolated_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path = isolated_environment
    repo = _init_repo(tmp_path / "repo")
    failing_cmd, _ = _fake_kanban(tmp_path, fail=True)
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", failing_cmd)
    findings = backlog_submission.normalize_out_of_scope_findings([_finding()], label="test")
    pending = backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=repo, findings=findings,
        finding_source=backlog_submission.FINDING_SOURCE_VALIDATE_ONLY_ATTEMPT_VALIDATOR, run_dir=None,
    )
    group = pending["groups"][0]
    assert group["outcome"] == backlog_submission.SUBMISSION_OUTCOME_PENDING
    pending_path = Path(group["pending_prompt_path"])
    assert pending_path.is_file()
    assert str(pending_path).startswith(str(tmp_path / "rvf-home"))

    working_cmd, calls = _fake_kanban(tmp_path)
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", working_cmd)
    other = backlog_submission.normalize_out_of_scope_findings(
        [_finding(title="另一处问题", path="src/other.py", problem_statement="完全不同的问题。")], label="test"
    )
    receipt = backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=repo, findings=other,
        finding_source=backlog_submission.FINDING_SOURCE_VALIDATE_ONLY_ATTEMPT_VALIDATOR, run_dir=None,
    )
    assert receipt["retried_pending"][0]["retried"] is True
    assert not pending_path.exists()
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 2


def test_finding_on_other_session_uncommitted_background_path_is_not_submitted(
    isolated_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path = isolated_environment
    repo = _init_repo(tmp_path / "repo")
    run_dir = tmp_path / "run"
    (run_dir / "artifacts" / "inputs").mkdir(parents=True)
    (run_dir / "artifacts" / "inputs" / "scope.contract.json").write_text(
        json.dumps({"background_files": ["src/cache.py"], "protected_files": ["src/cache.py"]}), encoding="utf-8"
    )
    task_cmd, calls = _fake_kanban(tmp_path)
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", task_cmd)
    findings = backlog_submission.normalize_out_of_scope_findings([_finding()], label="test")
    receipt = backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=repo, findings=findings,
        finding_source=backlog_submission.FINDING_SOURCE_VALIDATE_ONLY_ATTEMPT_VALIDATOR, run_dir=run_dir,
    )
    assert receipt["groups"][0]["findings"][0]["outcome"] == backlog_submission.SUBMISSION_OUTCOME_SKIPPED_BACKGROUND_WIP
    assert not calls.exists()
    recorded = json.loads((run_dir / backlog_submission.RUN_ARTIFACT_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert recorded["submissions"][0]["groups"][0]["outcome"] == "nothing_new_to_submit"


def test_same_group_findings_become_one_card_and_invalid_input_is_rejected(
    isolated_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path = isolated_environment
    repo = _init_repo(tmp_path / "repo")
    task_cmd, calls = _fake_kanban(tmp_path)
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", task_cmd)
    findings = backlog_submission.normalize_out_of_scope_findings(
        [_finding(group_id="cache"), _finding(path="src/cache_two.py", group_id="cache", problem_statement="第二处。")],
        label="test",
    )
    receipt = backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=repo, findings=findings,
        finding_source=backlog_submission.FINDING_SOURCE_MAIN_SESSION_WITHOUT_VALIDATOR, run_dir=None,
    )
    assert len(receipt["groups"]) == 1
    argv = json.loads(calls.read_text(encoding="utf-8").splitlines()[0])
    assert "（及另 1 条同根因问题）" in argv[argv.index("--title") + 1]
    assert "未经 validator 验证" in argv[argv.index("--prompt") + 1]
    with pytest.raises(backlog_submission.OutOfScopeFindingInputInvalid):
        backlog_submission.normalize_out_of_scope_findings([_finding(path="../escape.py")], label="test")
    with pytest.raises(backlog_submission.OutOfScopeFindingInputInvalid):
        backlog_submission.normalize_out_of_scope_findings([{"title": "缺字段"}], label="test")


# --------------------------------------------------------------------------- #
# BACKLOG-6（恰好一条持久事实）：每条通过校验的 finding 返回前恰好留下 created / pending / duplicate /
# skipped 之一；建卡前先落 pending intent，建成后升级为 created；只读预演什么都不留。
# --------------------------------------------------------------------------- #


def _pending_record_paths(tmp_path: Path) -> list[Path]:
    root = (
        tmp_path
        / "rvf-home"
        / backlog_submission.OUT_OF_SCOPE_FINDINGS_KANBAN_BACKLOG_LEDGER_DIRECTORY_NAME
        / backlog_submission.PENDING_KANBAN_TASK_PROMPTS_DIRECTORY_NAME
    )
    return sorted(root.glob("*/*.json"))


def _ledger_entries(tmp_path: Path) -> list[dict]:
    root = tmp_path / "rvf-home" / backlog_submission.OUT_OF_SCOPE_FINDINGS_KANBAN_BACKLOG_LEDGER_DIRECTORY_NAME
    return [json.loads(line) for path in sorted(root.glob("*.jsonl")) for line in path.read_text(encoding="utf-8").splitlines()]


def _recorded_kanban_calls(calls: Path) -> list[list[str]]:
    return [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()] if calls.exists() else []


def test_card_created_but_local_ledger_append_failed_is_recovered_on_retry_without_a_second_card(
    isolated_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """历史缺陷：远端建卡成功之后才追加 ledger；ledger 写失败时已建的卡没有任何去重事实，再提交建出第二张卡。"""
    tmp_path = isolated_environment
    repo = _init_repo(tmp_path / "repo")
    task_cmd, calls = _fake_kanban(tmp_path)
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", task_cmd)
    findings = backlog_submission.normalize_out_of_scope_findings([_finding()], label="test")
    real_append = backlog_submission._append_ledger_entries

    def append_failing_for_created_entries(ledger_path: Path, entries) -> None:
        if any(entry.get("outcome") == backlog_submission.SUBMISSION_OUTCOME_CREATED for entry in entries):
            raise OSError("disk full while recording the created card")
        real_append(ledger_path, entries)

    monkeypatch.setattr(backlog_submission, "_append_ledger_entries", append_failing_for_created_entries)
    with pytest.raises(OSError, match="disk full"):
        backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
            repo=repo, findings=findings,
            finding_source=backlog_submission.FINDING_SOURCE_VALIDATE_ONLY_ATTEMPT_VALIDATOR, run_dir=None,
        )
    assert len(_recorded_kanban_calls(calls)) == 1
    # intent 先于建卡落盘：pending 记录 + ledger PENDING；建成后 ledger 写不进去 ⇒ task id 写回 pending 记录。
    (pending_path,) = _pending_record_paths(tmp_path)
    pending_record = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending_record[backlog_submission.PENDING_RECORD_FIELD_KANBAN_TASK_ID_CREATED_BUT_LEDGER_APPEND_FAILED] == "card1"
    assert [entry["outcome"] for entry in _ledger_entries(tmp_path)] == [backlog_submission.SUBMISSION_OUTCOME_PENDING]

    monkeypatch.setattr(backlog_submission, "_append_ledger_entries", real_append)
    receipt = backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=repo, findings=findings,
        finding_source=backlog_submission.FINDING_SOURCE_VALIDATE_ONLY_ATTEMPT_VALIDATOR, run_dir=None,
    )
    assert receipt["retried_pending"][0]["retried"] is True
    assert receipt["retried_pending"][0]["kanban_task_id"] == "card1"
    assert receipt["groups"][0]["outcome"] == "nothing_new_to_submit"
    assert receipt["groups"][0]["findings"][0]["previous_kanban_task_id"] == "card1"
    assert len(_recorded_kanban_calls(calls)) == 1, "同一 finding 不得建出第二张卡"
    assert not pending_path.exists()


def test_process_death_right_after_kanban_create_is_flagged_as_possible_duplicate_when_retried(
    isolated_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """残留窗口如实标注：远端没有幂等键，建卡返回后、记账前进程死掉 ⇒ 重试只能重建，但必须标「可能重复」。"""
    tmp_path = isolated_environment
    repo = _init_repo(tmp_path / "repo")
    task_cmd, calls = _fake_kanban(tmp_path)
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", task_cmd)
    findings = backlog_submission.normalize_out_of_scope_findings([_finding()], label="test")
    real_create = backlog_submission._create_kanban_task

    def create_then_die(**kwargs):
        real_create(**kwargs)
        raise SystemExit("process killed right after kanban task create returned")

    monkeypatch.setattr(backlog_submission, "_create_kanban_task", create_then_die)
    with pytest.raises(SystemExit):
        backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
            repo=repo, findings=findings,
            finding_source=backlog_submission.FINDING_SOURCE_VALIDATE_ONLY_ATTEMPT_VALIDATOR, run_dir=None,
        )
    (pending_path,) = _pending_record_paths(tmp_path)
    pending_record = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending_record[backlog_submission.PENDING_RECORD_FIELD_KANBAN_TASK_CREATE_INVOCATION_STARTED_AT_OUTCOME_NOT_OBSERVED]
    assert [entry["outcome"] for entry in _ledger_entries(tmp_path)] == [backlog_submission.SUBMISSION_OUTCOME_PENDING]

    monkeypatch.setattr(backlog_submission, "_create_kanban_task", real_create)
    receipt = backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=repo, findings=findings,
        finding_source=backlog_submission.FINDING_SOURCE_VALIDATE_ONLY_ATTEMPT_VALIDATOR, run_dir=None,
    )
    retried = receipt["retried_pending"][0]
    assert retried["retried"] is True
    assert retried[backlog_submission.PENDING_RECORD_FIELD_KANBAN_TASK_POSSIBLY_ALREADY_CREATED_BY_UNOBSERVED_INVOCATION]
    second_call = _recorded_kanban_calls(calls)[1]
    assert "可能重复" in second_call[second_call.index("--prompt") + 1]
    assert receipt["groups"][0]["outcome"] == "nothing_new_to_submit"
    backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=repo, findings=findings,
        finding_source=backlog_submission.FINDING_SOURCE_VALIDATE_ONLY_ATTEMPT_VALIDATOR, run_dir=None,
    )
    assert len(_recorded_kanban_calls(calls)) == 2
    assert not pending_path.exists()


def test_unresolved_base_ref_is_stored_as_none_and_re_resolved_when_retrying_pending(
    isolated_environment: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """历史缺陷：base_ref 解析失败时 pending 被强写成 'main'，retry-pending 直接拿它建卡、不再重新解析。"""
    tmp_path = isolated_environment
    repo = _init_repo(tmp_path / "repo")
    task_cmd, calls = _fake_kanban(tmp_path)
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", task_cmd)
    resolved_base_ref: dict[str, str | None] = {"value": None}
    monkeypatch.setattr(backlog_submission, "_resolve_kanban_base_ref", lambda run_dir, repo: resolved_base_ref["value"])
    findings = backlog_submission.normalize_out_of_scope_findings([_finding()], label="test")
    receipt = backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=repo, findings=findings,
        finding_source=backlog_submission.FINDING_SOURCE_VALIDATE_ONLY_ATTEMPT_VALIDATOR, run_dir=None,
    )
    assert receipt["groups"][0]["outcome"] == backlog_submission.SUBMISSION_OUTCOME_PENDING
    (pending_path,) = _pending_record_paths(tmp_path)
    assert json.loads(pending_path.read_text(encoding="utf-8"))["base_ref"] is None
    assert not calls.exists()

    # 仍解析不出 ⇒ 继续 pending，不拿占位基线硬建。
    assert backlog_submission.main(["retry-pending", "--repo", str(repo)]) == 0
    assert not calls.exists() and pending_path.exists()

    resolved_base_ref["value"] = "release/2026-10"
    assert backlog_submission.main(["retry-pending", "--repo", str(repo)]) == 0
    capsys.readouterr()
    (argv,) = _recorded_kanban_calls(calls)
    assert argv[argv.index("--base-ref") + 1] == "release/2026-10"
    assert not pending_path.exists()


def test_preview_failure_leaves_no_pending_ledger_or_run_artifact_and_is_never_replayed_as_a_card(
    isolated_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """历史缺陷：预演里 base_ref 解析失败或 ``kanban --preview`` 报错时落进 pending 分支，下次提交把预演真建成卡。"""
    tmp_path = isolated_environment
    repo = _init_repo(tmp_path / "repo")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    failing_cmd, _ = _fake_kanban(tmp_path, fail=True)
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", failing_cmd)
    findings = backlog_submission.normalize_out_of_scope_findings([_finding()], label="test")
    previewed = backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=repo, findings=findings,
        finding_source=backlog_submission.FINDING_SOURCE_MAIN_SESSION_WITHOUT_VALIDATOR, run_dir=run_dir,
        preview_without_creating=True,
    )
    assert previewed["groups"][0]["outcome"] == backlog_submission.SUBMISSION_OUTCOME_PREVIEW_FAILED
    assert "unavailable" in previewed["groups"][0]["error"]

    real_resolver = backlog_submission._resolve_kanban_base_ref
    monkeypatch.setattr(backlog_submission, "_resolve_kanban_base_ref", lambda run_dir, repo: None)
    unresolved = backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=repo, findings=findings,
        finding_source=backlog_submission.FINDING_SOURCE_MAIN_SESSION_WITHOUT_VALIDATOR, run_dir=run_dir,
        preview_without_creating=True,
    )
    assert unresolved["groups"][0]["outcome"] == backlog_submission.SUBMISSION_OUTCOME_PREVIEW_FAILED
    assert not (tmp_path / "rvf-home").exists(), "只读预演不得留下 pending / ledger"
    assert not (run_dir / backlog_submission.RUN_ARTIFACT_RELATIVE_PATH).exists()

    monkeypatch.setattr(backlog_submission, "_resolve_kanban_base_ref", real_resolver)
    working_cmd, calls = _fake_kanban(tmp_path)
    monkeypatch.setenv("CODEX_RVF_CLINE_KANBAN_TASK_CMD", working_cmd)
    created = backlog_submission.submit_out_of_scope_findings_to_kanban_backlog(
        repo=repo, findings=findings,
        finding_source=backlog_submission.FINDING_SOURCE_MAIN_SESSION_WITHOUT_VALIDATOR, run_dir=run_dir,
    )
    assert created["retried_pending"] == []
    assert created["groups"][0]["outcome"] == backlog_submission.SUBMISSION_OUTCOME_CREATED
    assert len(_recorded_kanban_calls(calls)) == 1


def test_parallel_receipt_recording_into_the_run_artifact_loses_no_submission(tmp_path: Path) -> None:
    """历史缺陷：run 回执无锁 read-modify-write，多个并行 attempt 同时 stop 互相覆盖，handoff 漏掉已建卡的问题。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    worker_script = (
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(SCRIPT_DIRECTORY)!r})\n"
        "import rvf_out_of_scope_findings_kanban_backlog_submission as backlog_submission\n"
        "worker = sys.argv[1]\n"
        "for index in range(8):\n"
        "    backlog_submission.record_submission_receipt_in_run_artifact(\n"
        f"        Path({str(run_dir)!r}), {{'attempt_id': f'{{worker}}-{{index}}', 'groups': []}}\n"
        "    )\n"
    )
    workers = [
        subprocess.Popen([sys.executable, "-c", worker_script, f"worker{number}"], stderr=subprocess.PIPE, text=True)
        for number in range(6)
    ]
    for worker in workers:
        _, stderr = worker.communicate(timeout=180)
        assert worker.returncode == 0, stderr
    recorded = sorted(item["attempt_id"] for item in backlog_submission.read_submission_receipts_for_run(run_dir))
    assert recorded == sorted(f"worker{number}-{index}" for number in range(6) for index in range(8))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
