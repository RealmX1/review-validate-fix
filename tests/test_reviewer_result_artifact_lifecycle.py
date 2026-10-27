"""结果生命周期：发现与补证并存、请求显式解决、续跑保留 canonical 及完成腿。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
WRITE_REVIEW_RESULT = SCRIPT_DIRECTORY / "write_review_result.py"
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import write_review_result  # noqa: E402


def _write(run_dir: Path, out: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(WRITE_REVIEW_RESULT), *arguments, "--out", str(out)],
        capture_output=True,
        text=True,
        env={"RVF_RUN_DIR": str(run_dir), "PATH": "/usr/bin:/bin"},
    )


def _issue_arguments() -> list[str]:
    return [
        "issue",
        "--path", "a.py",
        "--line", "1",
        "--message", "真的有问题",
        "--kind", "REAL",
        "--severity", "high",
    ]


def _no_issues_arguments() -> list[str]:
    return ["no-issues", "--audit-summary", "审完了，没发现问题。"]


def _request_arguments() -> list[str]:
    return [
        "context-request",
        "--need", "file",
        "--reason", "看不到这个文件",
    ]


def _archive_dir(out: Path) -> Path:
    return out.parent / write_review_result.SUPERSEDED_RESULT_ARTIFACTS_DIRNAME


@pytest.mark.parametrize(
    "first_arguments, first_kind, second_arguments, second_kind",
    [
        (_no_issues_arguments(), "no_issues", _issue_arguments(), "issues"),
        (_no_issues_arguments(), "no_issues", _request_arguments(), "request"),
    ],
)
def test_non_finding_bearing_kind_transitions_are_reachable_and_archive_the_previous_artifact(
    tmp_path: Path,
    first_arguments: list[str],
    first_kind: str,
    second_arguments: list[str],
    second_kind: str,
) -> None:
    """LIFE-1 + LIFE-2：不会抹掉 finding 的四个方向全通，且每次换 kind 都留下可追溯的归档。"""
    run_dir = tmp_path / "run"
    out = run_dir / "artifacts" / "reviewers" / "alpha" / "review-result.json"

    first = _write(run_dir, out, *first_arguments)
    assert first.returncode == 0, first.stderr
    original = out.read_text(encoding="utf-8")
    assert json.loads(original)["kind"] == first_kind

    second = _write(run_dir, out, *second_arguments)
    assert second.returncode == 0, (
        f"{first_kind} → {second_kind} 应当可达（历史上 request→issues 是死结）：{second.stderr}"
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["kind"] == second_kind

    lineage = payload.get("superseded")
    assert isinstance(lineage, list) and len(lineage) == 1, payload
    assert lineage[0]["kind"] == first_kind
    archived = Path(lineage[0]["path"])
    assert archived.is_file(), "旧工件必须留档，不能被静默覆写"
    assert archived.read_text(encoding="utf-8") == original
    assert archived.parent == _archive_dir(out)


def test_same_kind_still_appends_in_place_without_archiving(tmp_path: Path) -> None:
    """反面：同 kind 是原地追加（不是每写一条 issue 就归档一次），否则归档目录会被刷爆。"""
    run_dir = tmp_path / "run"
    out = run_dir / "artifacts" / "reviewers" / "alpha" / "review-result.json"
    assert _write(run_dir, out, *_issue_arguments()).returncode == 0
    second = _write(
        run_dir, out,
        "issue", "--path", "b.py", "--line", "2", "--message", "第二条",
        "--kind", "NIT", "--severity", "low",
    )
    assert second.returncode == 0, second.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["issues"]) == 2
    assert not _archive_dir(out).exists()


def test_supersession_lineage_accumulates_across_multiple_flips(tmp_path: Path) -> None:
    """LIFE-2：连续换 kind 时，溯源链逐条累积而不是只留最后一次。"""
    run_dir = tmp_path / "run"
    out = run_dir / "artifacts" / "reviewers" / "alpha" / "review-result.json"
    assert _write(run_dir, out, *_no_issues_arguments()).returncode == 0
    assert _write(run_dir, out, *_request_arguments()).returncode == 0
    assert _write(run_dir, out, *_issue_arguments()).returncode == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["kind"] == "request" and len(payload["issues"]) == 1
    request_id = payload["requests"][0]["request_id"]
    assert _write(run_dir, out, "respond-request", "--request-id", request_id, "--response", "已取得证据").returncode == 0
    assert _write(run_dir, out, "resolve-request", "--request-id", request_id, "--resolution", "已核验").returncode == 0
    assert _write(run_dir, out, "complete", "--audit-summary", "完整评审完成").returncode == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert [entry["kind"] for entry in payload["superseded"]] == ["no_issues", "request"]
    assert len(list(_archive_dir(out).glob("review-result.*.json"))) == 2


@pytest.mark.parametrize(
    "second_arguments, second_kind",
    [
        (_no_issues_arguments(), "no_issues"),
    ],
)
def test_recorded_issues_are_a_terminal_kind_and_cannot_be_flipped_away(
    tmp_path: Path, second_arguments: list[str], second_kind: str
) -> None:
    """LIFE-4：已记录 issues 的工件不可换 kind——canonical 读者不合并 superseded 链。

    归档保住的是取证能力，不是判定能力：放行这一步，``check_review_result`` 报 ``no_issues``、
    dispatcher 认它是有效完成腿、candidate evidence 判 protocol-valid、merge 按 clean 合并，
    已找到的 REAL issue 只剩归档里一份没人读的副本。
    """
    run_dir = tmp_path / "run"
    out = run_dir / "artifacts" / "reviewers" / "alpha" / "review-result.json"
    assert _write(run_dir, out, *_issue_arguments()).returncode == 0
    before = out.read_text(encoding="utf-8")

    refused = _write(run_dir, out, *second_arguments)

    assert refused.returncode != 0, f"issues → {second_kind} 必须被拒绝"
    assert "已记录 issues" in refused.stderr, refused.stderr
    # 闸在 os.replace 之前：被拒时磁盘一个字节都没动。
    assert out.read_text(encoding="utf-8") == before
    assert not _archive_dir(out).exists()


def test_the_terminal_guard_still_allows_appending_more_issues(tmp_path: Path) -> None:
    """LIFE-4 的反面：终态闸只拦**换 kind**，同 kind 追加照常（否则第二条 issue 就写不进去）。"""
    run_dir = tmp_path / "run"
    out = run_dir / "artifacts" / "reviewers" / "alpha" / "review-result.json"
    assert _write(run_dir, out, *_issue_arguments()).returncode == 0
    second = _write(
        run_dir, out,
        "issue", "--path", "b.py", "--line", "9", "--message", "又一条",
        "--kind", "REAL", "--severity", "medium",
    )
    assert second.returncode == 0, second.stderr
    assert len(json.loads(out.read_text(encoding="utf-8"))["issues"]) == 2


def _seed_run_with_two_legs(tmp_path: Path, *, alpha_kind: str, beta_kind: str) -> Path:
    """造一个「上一轮已跑完」的 run：alpha / beta 两腿各有自己的 review-result.json + plan。"""
    artifacts = tmp_path / "run" / "artifacts"
    reviewers = artifacts / "reviewers"
    for reviewer_id, kind in (("alpha", alpha_kind), ("beta", beta_kind)):
        leg = reviewers / reviewer_id
        leg.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "kind": kind, "issues": [], "requests": []}
        if kind == "issues":
            payload["issues"] = [
                {"path": "a.py", "line": 1, "message": "旧一轮找到的", "kind": "REAL", "severity": "high"}
            ]
        elif kind == "request":
            payload["requests"] = [{"type": "context_request", "need": "file", "target": "a.py", "reason": "所需材料缺失"}]
        elif kind == "no_issues":
            payload["audit_summary"] = "clean"
        (leg / "review-result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (leg / "reviewer.summary.json").write_text(
            json.dumps({"review_request_pending": kind == "request"}) + "\n", encoding="utf-8"
        )
    (reviewers / "reviewer-plan.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "blocked_on_reviewer_request",
                "reason": "reviewer_review_request_pending",
                # routing_rule / slot / fallbacks 是 dispatch_reviewers.main() 复用 plan 时
                # 会读到的字段，端到端用例要求 plan 形状与真实产物一致。
                "routing_rule": "R1",
                "reviewers": [
                    {"reviewer_id": "alpha", "harness_id": "codex", "slot": 1, "returncode": 0},
                    {"reviewer_id": "beta", "harness_id": "cursor", "slot": 2, "returncode": 0},
                ],
                "warnings": [],
                "fallbacks": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return artifacts


def test_resume_selects_only_the_request_legs_without_touching_any_artifact(tmp_path: Path) -> None:
    """LIFE-3 + LIFE-5：挑腿这一步是**只读**的——两条腿的工件一个字节都不动。

    轮转曾经就写在这里，而这里跑在 ``--dry-run`` 短路之前，于是一次纯预演就把当前结果搬走了。
    """
    import dispatch_reviewers  # noqa: PLC0415

    artifacts = _seed_run_with_two_legs(tmp_path, alpha_kind="request", beta_kind="issues")
    reviewers_dir = artifacts / "reviewers"
    before = {
        reviewer_id: (reviewers_dir / reviewer_id / "review-result.json").read_text(encoding="utf-8")
        for reviewer_id in ("alpha", "beta")
    }

    plan, indices = dispatch_reviewers.load_reviewer_plan_and_select_incomplete_legs(reviewers_dir, artifacts)

    assert indices == [0], "只有 alpha 停在 request"
    assert plan["reviewers"][0]["reviewer_id"] == "alpha"
    assert plan["status"] == "planned", "上一轮的终态标记必须清掉，否则本轮永远挂着 blocked"
    assert "reason" not in plan
    assert any(
        warning["code"] == "resuming_incomplete_reviewer_legs" for warning in plan["warnings"]
    ), plan["warnings"]

    for reviewer_id, text in before.items():
        path = reviewers_dir / reviewer_id / "review-result.json"
        assert path.is_file() and path.read_text(encoding="utf-8") == text
        assert not (
            reviewers_dir / reviewer_id / write_review_result.SUPERSEDED_RESULT_ARTIFACTS_DIRNAME
        ).exists()


def test_rotation_is_a_separate_execute_only_step(tmp_path: Path) -> None:
    """LIFE-3：轮转本身仍然要做——只是被挪到真正 execute 之前，且只碰 request 腿。"""
    import dispatch_reviewers  # noqa: PLC0415

    artifacts = _seed_run_with_two_legs(tmp_path, alpha_kind="request", beta_kind="issues")
    reviewers_dir = artifacts / "reviewers"
    healthy_before = (reviewers_dir / "beta" / "review-result.json").read_text(encoding="utf-8")
    request_before = (reviewers_dir / "alpha" / "review-result.json").read_text(encoding="utf-8")
    plan, indices = dispatch_reviewers.load_reviewer_plan_and_select_incomplete_legs(reviewers_dir, artifacts)

    rotated = dispatch_reviewers.checkpoint_incomplete_reviewer_leg_artifacts(plan, indices, artifacts)

    assert len(rotated) == 1
    # 健康腿：一个字节都不动。
    assert (reviewers_dir / "beta" / "review-result.json").read_text(encoding="utf-8") == healthy_before
    # request 腿：旧工件被轮转走，重跑从干净起点开始（否则新 request 会追加在旧的后面）。
    assert (reviewers_dir / "alpha" / "review-result.json").read_text(encoding="utf-8") == request_before
    archived = list(
        (reviewers_dir / "alpha" / write_review_result.SUPERSEDED_RESULT_ARTIFACTS_DIRNAME).glob(
            "review-result.*.json"
        )
    )
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == request_before


def test_dry_run_redispatch_prints_the_plan_without_moving_any_result_artifact(tmp_path: Path) -> None:
    """LIFE-5（端到端）：普通恢复入口的 ``--dry-run`` 只打印 plan。

    契约原文是 “Print plan JSON to stdout; do not write or execute”；一次预演不得把当前
    canonical ``review-result.json`` 搬进归档目录。
    """
    import os  # noqa: PLC0415

    artifacts = _seed_run_with_two_legs(tmp_path, alpha_kind="request", beta_kind="issues")
    reviewers_dir = artifacts / "reviewers"
    before = {
        reviewer_id: (reviewers_dir / reviewer_id / "review-result.json").read_text(encoding="utf-8")
        for reviewer_id in ("alpha", "beta")
    }

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("RVF_", "CODEX_RVF_"))
    }
    # ledger 落进 tmp，绝不写真实 ~/.rvf。
    environment["CODEX_RVF_LOG_ROOT"] = str(tmp_path / "logs")
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIRECTORY / "dispatch_reviewers.py"),
            "--dry-run",
            "--rvf-run-id", "rvf-lifecycle-test",
            "--rvf-run-dir", str(artifacts.parent),
            "--assume-available", "codex,cursor",
            "--repo", str(ROOT),
        ],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    assert plan["status"] == "planned"
    for reviewer_id, text in before.items():
        path = reviewers_dir / reviewer_id / "review-result.json"
        assert path.is_file(), f"--dry-run 搬走了 {reviewer_id} 的 canonical 结果"
        assert path.read_text(encoding="utf-8") == text
        assert not (
            reviewers_dir / reviewer_id / write_review_result.SUPERSEDED_RESULT_ARTIFACTS_DIRNAME
        ).exists()


def test_resume_fails_closed_without_a_previous_plan(tmp_path: Path) -> None:
    """fail-closed：没有上一轮 plan 就抛，绝不静默退回整盘重派（那正是要修的行为）。"""
    import dispatch_reviewers  # noqa: PLC0415

    artifacts = tmp_path / "run" / "artifacts"
    (artifacts / "reviewers").mkdir(parents=True)
    with pytest.raises(ValueError, match="reviewer-plan.json"):
        dispatch_reviewers.load_reviewer_plan_and_select_incomplete_legs(artifacts / "reviewers", artifacts)


def test_resume_reports_nothing_to_do_when_no_leg_is_pending(tmp_path: Path) -> None:
    """两腿都已完成 → 返回空 indices，调用方据此报「无需重派」而不是空跑一轮。"""
    import dispatch_reviewers  # noqa: PLC0415

    artifacts = _seed_run_with_two_legs(tmp_path, alpha_kind="issues", beta_kind="no_issues")
    _plan, indices = dispatch_reviewers.load_reviewer_plan_and_select_incomplete_legs(
        artifacts / "reviewers", artifacts
    )
    assert indices == []


def test_blocked_on_reviewer_request_guidance_uses_ordinary_resume_entrypoint() -> None:
    import rvf_agent_decision_cli
    import inspect
    source = inspect.getsource(rvf_agent_decision_cli.run_dispatch)
    assert "redispatch-reviewers" in source
    assert "--review-request-legs-only" not in source
    skill_directory = ROOT / "plugins/review-validate-fix/skills/review-validate-fix"
    for document in (skill_directory / "SKILL.md", skill_directory / "references/review-merge-policy.md"):
        text = document.read_text(encoding="utf-8")
        assert "自动" in text and "未完成" in text
        assert "--redispatch-review-request-legs-only" not in text


def test_detached_argv_uses_ordinary_resume_entrypoint() -> None:
    import argparse
    import dispatch_reviewers
    args = argparse.Namespace(registry="reg.json", probe_mode="preflight", probe_timeout=1.0,
        main_harness="codex", transcript=None, main_harness_file=None, assume_available=None,
        require_external=False, selection_seed=None)
    argv = dispatch_reviewers._build_detached_child_argv(args, run_id="r", run_dir="/persistent/r",
        repo=None, review_packet=None, session_context=None, scope_contract=None)
    assert "--execute" in argv and "--rvf-run-dir" in argv
    assert "--redispatch-review-request-legs-only" not in argv


CHECK_REVIEW_RESULT = SCRIPT_DIRECTORY / "check_review_result.py"


def _check(out: Path, *extra: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(CHECK_REVIEW_RESULT), str(out), "--json", *extra],
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_issue_scope_relation_defaults_to_in_current_round_scope(tmp_path: Path) -> None:
    """SCOPE-1：没写 --scope-relation 的 issue 维持「本轮范围内」语义，且字段落盘为下划线键名。"""
    run_dir = tmp_path / "run"
    out = run_dir / "artifacts" / "reviewers" / "alpha" / "review-result.json"
    assert _write(run_dir, out, *_issue_arguments()).returncode == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["issues"][0]["scope_relation"] == "in_current_round_scope"
    assert "scope-relation" not in payload["issues"][0]
    checked = _check(out)
    assert checked["valid"] is True
    assert checked["outside_current_round_scope_issue_count"] == 0


def test_outside_current_round_scope_issue_is_valid_and_counted(tmp_path: Path) -> None:
    """SCOPE-2：范围外问题照常通过 review（合法 issues 腿），并被单独计数。"""
    run_dir = tmp_path / "run"
    out = run_dir / "artifacts" / "reviewers" / "alpha" / "review-result.json"
    written = _write(
        run_dir, out, *_issue_arguments(), "--scope-relation", "outside_current_round_scope",
        "--current-round-scope-audit-summary", "本轮改动的两处分支与边界都核对过，范围内没有问题。",
    )
    assert written.returncode == 0, written.stderr
    checked = _check(out)
    assert checked["valid"] is True and checked["kind"] == "issues"
    assert checked["outside_current_round_scope_issue_count"] == 1
    assert checked["audit_summary"].startswith("本轮改动")
    assert checked["warnings"] == []


def test_invalid_scope_relation_is_rejected_by_writer_and_checker(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    out = run_dir / "artifacts" / "reviewers" / "alpha" / "review-result.json"
    rejected = _write(run_dir, out, *_issue_arguments(), "--scope-relation", "somewhere_else")
    assert rejected.returncode != 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "schema_version": 1, "kind": "issues", "requests": [],
        "issues": [{"path": "a.py", "line": 1, "message": "m", "kind": "REAL", "severity": "low",
                    "scope_relation": "somewhere_else"}],
    }), encoding="utf-8")
    checked = _check(out)
    assert checked["valid"] is False
    assert any("scope_relation" in error for error in checked["errors"])


def test_all_outside_issues_without_current_round_audit_summary_only_warns(tmp_path: Path) -> None:
    """SCOPE-3：全部是范围外问题却没写本轮审计摘要 ⇒ 非致命 warning，腿仍然有效（不触发重派）。"""
    run_dir = tmp_path / "run"
    out = run_dir / "artifacts" / "reviewers" / "alpha" / "review-result.json"
    assert _write(run_dir, out, *_issue_arguments(), "--scope-relation", "outside_current_round_scope").returncode == 0
    checked = _check(out)
    assert checked["valid"] is True
    assert any("audit_summary" in warning for warning in checked["warnings"])


def test_no_issues_to_issues_carries_the_audit_summary_forward(tmp_path: Path) -> None:
    """SCOPE-4：先下 clean 再补出问题时，原审计摘要仍是「本轮范围内审了什么」的依据。"""
    run_dir = tmp_path / "run"
    out = run_dir / "artifacts" / "reviewers" / "alpha" / "review-result.json"
    assert _write(run_dir, out, *_no_issues_arguments()).returncode == 0
    assert _write(run_dir, out, *_issue_arguments(), "--scope-relation", "outside_current_round_scope").returncode == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["kind"] == "issues"
    assert payload["audit_summary"] == "审完了，没发现问题。"
    assert _check(out)["warnings"] == []


def test_issue_in_excluded_path_warns_instead_of_invalidating_the_leg(tmp_path: Path) -> None:
    """SCOPE-5：护栏而非闸门——排除区里的 issue 不再让整条腿作废，只记 warning 与 1 起的编号。"""
    run_dir = tmp_path / "run"
    out = run_dir / "artifacts" / "reviewers" / "alpha" / "review-result.json"
    contract = tmp_path / "scope.contract.json"
    contract.write_text(json.dumps({"excluded_path_prefixes": ["vendor"]}), encoding="utf-8")
    assert _write(run_dir, out, *_issue_arguments()).returncode == 0
    assert _write(
        run_dir, out, "issue", "--path", "vendor/generated.py", "--line", "1",
        "--message", "排除区里的问题", "--kind", "REAL", "--severity", "low",
    ).returncode == 0
    checked = _check(out, "--scope-contract", str(contract))
    assert checked["valid"] is True and checked["kind"] == "issues"
    assert checked["issue_indices_in_excluded_paths_one_based"] == [2]
    assert any("excluded" in warning for warning in checked["warnings"])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
