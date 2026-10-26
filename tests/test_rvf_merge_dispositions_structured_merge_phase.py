#!/usr/bin/env python3
"""合并阶段结构化处置（RVF 降本 T4）测试簇。

覆盖：``rvf_merge_dispositions.py write|check`` 的 schema / 完整性 / 哈希 / 过期判定；
``rvf_fix_issue.upsert_canonical_issue`` 强制点（缺处置拒绝、未覆盖 issue_id 拒绝、来源只从处置派生、
镜像去来源字段、来源标签泄漏拒绝、主 agent 自审的非 reviewer 来源）；tracker 只同步状态时保留
``source_refs``；evidence writer 在「全部 dropped」时允许 N=0、计数与处置不一致时拒绝；``rvf_land``
端到端可 land 全部 dropped 的 run；``causality.json`` 的按 reviewer 汇总（backlog 440b5 数据来源）。

隔离：每个用例自建 tmp 下的 run 目录 / 仓库 / tracker log root，子进程环境剥掉 ``RVF_*`` /
``CODEX_RVF_*``；进程内调用只经显式参数，不读宿主会话的 ``RVF_RUN_DIR``。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIRECTORY))
# 复用 evidence / land 测试的 run 构造器：三边断言的是同一份 run 形状，手抄会随门槛演进悄悄漂移。
from test_reviewed_task_candidate_evidence import (  # noqa: E402
    SCRIPT_DIRECTORY,
    WRITE_CANDIDATE_EVIDENCE,
    clean_test_environment,
    create_completed_run,
    record_terminal_fix_attempt,
    repository,
)
from test_rvf_land_single_step_prepare_and_commit import (  # noqa: E402
    commit as rvf_land_commit,
    prepare as rvf_land_prepare,
)

sys.path.insert(0, str(SCRIPT_DIRECTORY))
import rvf_merge_dispositions  # noqa: E402

MERGE_DISPOSITIONS_SCRIPT = SCRIPT_DIRECTORY / "rvf_merge_dispositions.py"
FIX_ISSUE_SCRIPT = SCRIPT_DIRECTORY / "rvf_fix_issue.py"
REVIEWER_A_LABEL = "alternative-reviewer:cursor-cli"
REVIEWER_B_LABEL = "alternative-reviewer:codex-cli"


def _reviewer_issue(message: str, line: int = 1) -> dict:
    return {"path": "tracked.txt", "line": line, "kind": "REAL", "severity": "medium", "message": message}


def _set_reviewer_results(run_dir: Path, results: dict[str, list[dict]]) -> None:
    """改写 reviewer 腿结果，并给 plan 补上 label（生产 plan 里每腿都有）。"""
    reviewer_root = run_dir / "artifacts" / "reviewers"
    for reviewer_id, issues in results.items():
        payload = (
            {"schema_version": 1, "kind": "issues", "issues": issues, "requests": []}
            if issues
            else {"schema_version": 1, "kind": "no_issues", "issues": [], "requests": [], "audit_summary": "clean"}
        )
        (reviewer_root / reviewer_id / "review-result.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    plan_path = reviewer_root / "reviewer-plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    labels = {"reviewer-a": REVIEWER_A_LABEL, "reviewer-b": REVIEWER_B_LABEL}
    for reviewer in plan["reviewers"]:
        reviewer["label"] = labels[reviewer["reviewer_id"]]
    plan_path.write_text(json.dumps(plan) + "\n", encoding="utf-8")


def _run_with_reviewer_issues(tmp_path: Path) -> tuple[Path, Path]:
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    _set_reviewer_results(
        run_dir,
        {
            "reviewer-a": [_reviewer_issue("off-by-one in loop"), _reviewer_issue("prefer f-strings", 2)],
            "reviewer-b": [_reviewer_issue("loop bound is wrong")],
        },
    )
    return repo, run_dir


def _write_dispositions(run_dir: Path, decisions: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MERGE_DISPOSITIONS_SCRIPT), "write", "--run-dir", str(run_dir), "--json"],
        input=json.dumps(decisions),
        capture_output=True,
        text=True,
        check=False,
        env=clean_test_environment(),
    )


def _adopt_merge_drop_decisions() -> dict:
    return {
        "dispositions": [
            {"reviewer_id": "reviewer-a", "index": 1, "disposition": "adopted", "issue_id": "RVF-001"},
            {
                "reviewer_id": "reviewer-a",
                "index": 2,
                "disposition": "dropped",
                "reason_code": "style_or_preference_only",
            },
            {"reviewer_id": "reviewer-b", "index": 1, "disposition": "merged_into", "issue_id": "RVF-001"},
        ]
    }


def _canonical_issue_file(tmp_path: Path, issue_id: str, **extra: object) -> Path:
    path = tmp_path / f"{issue_id}.json"
    path.write_text(
        json.dumps({"issue_id": issue_id, "path": "tracked.txt", "line": 1, "message": "loop bound is off by one", **extra})
        + "\n",
        encoding="utf-8",
    )
    return path


def _upsert(tmp_path: Path, repo: Path, run_dir: Path, issue_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(FIX_ISSUE_SCRIPT),
            "upsert",
            "--repo",
            str(repo),
            "--run-dir",
            str(run_dir),
            "--issue-file",
            str(issue_file),
            "--log-root",
            str(tmp_path / "tracker-log-root"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=clean_test_environment(),
    )


def _tracker_issue(tmp_path: Path, repo: Path, issue_id: str) -> dict:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))
    import _rvf_pyroot  # noqa: F401
    from core.session_scope_allocation import reviewable_unit_diff_tracker

    ledger = reviewable_unit_diff_tracker.rvf_causality_for_run(
        repo=repo, run_id="rvf-candidate-evidence-test", log_root_override=tmp_path / "tracker-log-root"
    )
    return next(issue for issue in ledger["issues"] if issue["issue_id"] == issue_id)


def _handoff(run_dir: Path, summary_line: str, outcomes: str = "") -> None:
    (run_dir / "artifacts" / "handoff.md").write_text(
        "- handoff_status: COMPLETED\n- review_status: COMPLETED\n\n"
        f"## 本次 review\n\n{summary_line}\n\n## Issue 处理结果\n\n{outcomes}",
        encoding="utf-8",
    )


def _run_evidence_writer(repo: Path, run_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WRITE_CANDIDATE_EVIDENCE), "--repo", str(repo), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=False,
        env=clean_test_environment(),
    )


# --------------------------------------------------------------------------- #
# write / check
# --------------------------------------------------------------------------- #


def test_write_fills_hashes_with_one_based_index_and_check_requires_upserted_targets(tmp_path: Path) -> None:
    repo, run_dir = _run_with_reviewer_issues(tmp_path)
    written = _write_dispositions(run_dir, _adopt_merge_drop_decisions())
    assert written.returncode == 0, written.stderr
    payload = json.loads(written.stdout)["payload"]
    assert payload["schema"] == "rvf-merge-dispositions"
    assert payload["reviewer_issue_index_base"] == 1
    assert payload["review_result_coverage"] == "canonical_review_result_only_superseded_generations_excluded"
    assert [(item["reviewer_id"], item["issue_count"]) for item in payload["reviewer_results"]] == [
        ("reviewer-a", 2),
        ("reviewer-b", 1),
    ]
    first_issue = json.loads((run_dir / "artifacts/reviewers/reviewer-a/review-result.json").read_text())["issues"][0]
    assert payload["dispositions"][0]["issue_sha256"] == rvf_merge_dispositions.reviewer_issue_sha256(first_issue)
    path = run_dir / "artifacts/merge/merge_dispositions.json"
    assert Path(f"{path}.sha256").is_file()

    structure_only = rvf_merge_dispositions.check_merge_dispositions(run_dir, require_fix_issue_cross_reference=False)
    assert structure_only.passed, structure_only.errors
    full = rvf_merge_dispositions.check_merge_dispositions(run_dir, require_fix_issue_cross_reference=True)
    assert any("RVF-001 is a disposition target but was never upserted" in error for error in full.errors), full.errors

    assert _upsert(tmp_path, repo, run_dir, _canonical_issue_file(tmp_path, "RVF-001")).returncode == 0
    full = rvf_merge_dispositions.check_merge_dispositions(run_dir, require_fix_issue_cross_reference=True)
    assert full.passed, full.errors
    assert full.accounted_fix_issue_ids == ("RVF-001",)


def test_write_rejects_incomplete_or_malformed_dispositions(tmp_path: Path) -> None:
    _repo, run_dir = _run_with_reviewer_issues(tmp_path)
    base = _adopt_merge_drop_decisions()["dispositions"]
    cases = {
        "has no disposition": base[:2],
        "more than one disposition": [*base, base[0]],
        "dropped requires reason_code": [base[0], {**base[1], "reason_code": None}, base[2]],
        "requires a non-empty reason": [base[0], {**base[1], "reason_code": "other_with_reason"}, base[2]],
        "has no adopted reviewer issue": [
            {**base[0], "disposition": "merged_into", "issue_id": "RVF-009"},
            base[1],
            base[2],
        ],
        "index out of range": [{**base[0], "index": 0}, base[1], base[2]],
    }
    for expected_fragment, dispositions in cases.items():
        completed = _write_dispositions(run_dir, {"dispositions": dispositions})
        assert completed.returncode == 3, (expected_fragment, completed.stdout, completed.stderr)
        assert expected_fragment in completed.stderr, (expected_fragment, completed.stderr)
    collision = _write_dispositions(
        run_dir,
        {
            **_adopt_merge_drop_decisions(),
            "non_reviewer_fix_issue_sources": [{"issue_id": "RVF-001", "origin": "main_agent_self_review"}],
        },
    )
    assert collision.returncode == 3 and "both reviewer-sourced and non-reviewer-sourced" in collision.stderr
    assert not (run_dir / "artifacts/merge/merge_dispositions.json").exists()


def test_write_refuses_request_leg_and_check_detects_redispatched_canonical_result(tmp_path: Path) -> None:
    _repo, run_dir = _run_with_reviewer_issues(tmp_path)
    assert _write_dispositions(run_dir, _adopt_merge_drop_decisions()).returncode == 0
    # 模拟 --redispatch-review-request-legs-only：旧 canonical 轮转进 superseded、新一代结果落在原位。
    leg_directory = run_dir / "artifacts/reviewers/reviewer-b"
    superseded = leg_directory / "superseded-review-result-artifacts"
    superseded.mkdir()
    (leg_directory / "review-result.json").rename(superseded / "review-result.json")
    _set_reviewer_results(run_dir, {"reviewer-b": [_reviewer_issue("loop bound is wrong, second generation")]})
    stale = rvf_merge_dispositions.check_merge_dispositions(run_dir, require_fix_issue_cross_reference=False)
    assert any("changed since dispositions were written" in error for error in stale.errors), stale.errors
    assert any("issue_sha256 does not match" in error for error in stale.errors), stale.errors

    (leg_directory / "review-result.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "request",
                "issues": [],
                "requests": [{"type": "context_request", "need": "file", "reason": "need more", "paths": ["tracked.txt"]}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    refused = _write_dispositions(run_dir, _adopt_merge_drop_decisions())
    assert refused.returncode == 3
    assert "not a completed issues/no_issues result" in refused.stderr


# --------------------------------------------------------------------------- #
# upsert 强制点 + source-agnostic
# --------------------------------------------------------------------------- #


def test_upsert_requires_dispositions_and_derives_source_refs_without_leaking_to_mirror(tmp_path: Path) -> None:
    repo, run_dir = _run_with_reviewer_issues(tmp_path)
    issue_file = _canonical_issue_file(
        tmp_path,
        "RVF-001",
        source_refs=[{"reviewer_id": "forged"}],
        reviewer_id="reviewer-a",
        source_reviewers=[REVIEWER_A_LABEL],
    )
    missing = _upsert(tmp_path, repo, run_dir, issue_file)
    assert missing.returncode == 2 and "merge dispositions are missing" in missing.stderr

    assert _write_dispositions(run_dir, _adopt_merge_drop_decisions()).returncode == 0
    not_covered = _upsert(tmp_path, repo, run_dir, _canonical_issue_file(tmp_path, "RVF-002"))
    assert not_covered.returncode == 2 and "RVF-002 is not a disposition target" in not_covered.stderr

    upserted = _upsert(tmp_path, repo, run_dir, issue_file)
    assert upserted.returncode == 0, upserted.stderr
    mirror = json.loads((run_dir / "artifacts/fix-issues/RVF-001.json").read_text(encoding="utf-8"))
    for key in ("source_refs", "reviewer_id", "source_reviewers"):
        assert key not in mirror
    assert REVIEWER_A_LABEL not in json.dumps(mirror)
    tracker_issue = _tracker_issue(tmp_path, repo, "RVF-001")
    assert [(ref["reviewer_id"], ref["index"], ref["disposition"]) for ref in tracker_issue["source_refs"]] == [
        ("reviewer-a", 1, "adopted"),
        ("reviewer-b", 1, "merged_into"),
    ]
    assert all(ref["source_origin"] == "reviewer_result_issue" for ref in tracker_issue["source_refs"])

    leaking = _canonical_issue_file(tmp_path, "RVF-001", message=f"{REVIEWER_B_LABEL} also flagged this")
    refused = _upsert(tmp_path, repo, run_dir, leaking)
    assert refused.returncode == 2 and "mentions reviewer source labels" in refused.stderr


def test_nested_source_attribution_fields_are_stripped_from_mirror_and_flagged_by_full_check(tmp_path: Path) -> None:
    """来源字段藏在任意深度（dict 套 list 套 dict）也不得进 fixer 镜像；绕过 upsert 手写的嵌套来源要被全量 check 抓到。"""
    repo, run_dir = _run_with_reviewer_issues(tmp_path)
    assert _write_dispositions(run_dir, _adopt_merge_drop_decisions()).returncode == 0
    issue_file = _canonical_issue_file(
        tmp_path,
        "RVF-001",
        context={"source_refs": [{"reviewer_id": "x", "index": 1}], "hint": "check the loop"},
        notes=[{"reviewer_label": "someone", "text": "bound off by one"}],
    )
    upserted = _upsert(tmp_path, repo, run_dir, issue_file)
    assert upserted.returncode == 0, upserted.stderr
    mirror_path = run_dir / "artifacts/fix-issues/RVF-001.json"
    mirror = json.loads(mirror_path.read_text(encoding="utf-8"))
    assert mirror["context"] == {"hint": "check the loop"}
    assert mirror["notes"] == [{"text": "bound off by one"}]
    assert rvf_merge_dispositions.check_merge_dispositions(run_dir, require_fix_issue_cross_reference=True).passed

    mirror_path.write_text(
        json.dumps({**mirror, "context": {"hint": "x", "source_refs": [{"reviewer_id": "x", "index": 1}]}}) + "\n",
        encoding="utf-8",
    )
    full = rvf_merge_dispositions.check_merge_dispositions(run_dir, require_fix_issue_cross_reference=True)
    assert not full.passed
    assert any("carries source attribution fields" in error and "context.source_refs" in error for error in full.errors), full.errors
    nested_list_errors = rvf_merge_dispositions.fix_issue_payload_source_attribution_errors(
        {"notes": [{"inner": {"source_reviewer": "y"}}]}, source_markers=(), label="probe"
    )
    assert nested_list_errors and "notes[0].inner.source_reviewer" in nested_list_errors[0]


def test_state_only_tracker_upsert_preserves_disposition_derived_source_refs(tmp_path: Path) -> None:
    """fix attempt stop/apply 只同步状态、读的是去了来源的镜像：不能把 tracker 里的 source_refs 清空。"""
    repo, run_dir = _run_with_reviewer_issues(tmp_path)
    assert _write_dispositions(run_dir, _adopt_merge_drop_decisions()).returncode == 0
    assert _upsert(tmp_path, repo, run_dir, _canonical_issue_file(tmp_path, "RVF-001")).returncode == 0
    import _rvf_pyroot  # noqa: F401
    from core.session_scope_allocation import reviewable_unit_diff_tracker

    mirror_path = run_dir / "artifacts/fix-issues/RVF-001.json"
    reviewable_unit_diff_tracker.rvf_issue_upsert(
        repo=repo,
        run_id="rvf-candidate-evidence-test",
        issue_id="RVF-001",
        payload=json.loads(mirror_path.read_text(encoding="utf-8")),
        artifact_path=mirror_path,
        source_refs=None,
        state="fixed",
        log_root_override=tmp_path / "tracker-log-root",
    )
    tracker_issue = _tracker_issue(tmp_path, repo, "RVF-001")
    assert tracker_issue["state"] == "fixed"
    assert len(tracker_issue["source_refs"]) == 2


def test_full_check_detects_tracker_source_refs_stale_after_dispositions_rewritten_post_upsert(tmp_path: Path) -> None:
    """upsert 之后再 write 把 merged_into 改成 dropped：tracker 仍记着旧来源，镜像按设计不含来源看不出来——
    全量 check 必须拿 upsert 时的记录比对当前处置并报过期；重新 upsert 后 tracker 来源跟上、check 通过。"""
    repo, run_dir = _run_with_reviewer_issues(tmp_path)
    assert _write_dispositions(run_dir, _adopt_merge_drop_decisions()).returncode == 0
    issue_file = _canonical_issue_file(tmp_path, "RVF-001")
    assert _upsert(tmp_path, repo, run_dir, issue_file).returncode == 0
    recorded = run_dir / "artifacts/merge/fix-issue-source-refs-recorded-at-upsert/RVF-001.json"
    assert recorded.is_file()
    initial = rvf_merge_dispositions.check_merge_dispositions(run_dir, require_fix_issue_cross_reference=True)
    assert initial.passed, initial.errors

    rewritten = _adopt_merge_drop_decisions()
    rewritten["dispositions"][2] = {
        "reviewer_id": "reviewer-b",
        "index": 1,
        "disposition": "dropped",
        "reason_code": "not_traceable_to_current_diff",
    }
    assert _write_dispositions(run_dir, rewritten).returncode == 0
    stale = rvf_merge_dispositions.check_merge_dispositions(run_dir, require_fix_issue_cross_reference=True)
    assert any(
        "RVF-001" in error and "no longer match the current merge dispositions" in error for error in stale.errors
    ), stale.errors
    assert len(_tracker_issue(tmp_path, repo, "RVF-001")["source_refs"]) == 2

    assert _upsert(tmp_path, repo, run_dir, issue_file).returncode == 0
    refreshed = rvf_merge_dispositions.check_merge_dispositions(run_dir, require_fix_issue_cross_reference=True)
    assert refreshed.passed, refreshed.errors
    assert [(ref["reviewer_id"], ref["disposition"]) for ref in _tracker_issue(tmp_path, repo, "RVF-001")["source_refs"]] == [
        ("reviewer-a", "adopted")
    ]

    recorded.unlink()
    unrecorded = rvf_merge_dispositions.check_merge_dispositions(run_dir, require_fix_issue_cross_reference=True)
    assert any("RVF-001 has no source_refs recorded at upsert" in error for error in unrecorded.errors), unrecorded.errors


def test_main_agent_self_review_finding_is_a_non_reviewer_source(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    _set_reviewer_results(run_dir, {"reviewer-a": [], "reviewer-b": []})
    written = _write_dispositions(
        run_dir,
        {"non_reviewer_fix_issue_sources": [{"issue_id": "RVF-SELF-1", "origin": "main_agent_self_review"}]},
    )
    assert written.returncode == 0, written.stderr
    assert _upsert(tmp_path, repo, run_dir, _canonical_issue_file(tmp_path, "RVF-SELF-1")).returncode == 0
    assert _tracker_issue(tmp_path, repo, "RVF-SELF-1")["source_refs"] == [{"source_origin": "main_agent_self_review"}]
    full = rvf_merge_dispositions.check_merge_dispositions(run_dir, require_fix_issue_cross_reference=True)
    assert full.passed, full.errors
    assert full.non_reviewer_sourced_fix_issue_ids == ("RVF-SELF-1",)

    # 一条既不是处置目标、也没登记为非 reviewer 来源的 fix-issue 镜像（例如绕过 upsert 手写的）必须被 check 抓到。
    (run_dir / "artifacts/fix-issues/RVF-ORPHAN.json").write_text(
        json.dumps({"issue_id": "RVF-ORPHAN", "path": "tracked.txt", "line": 1}) + "\n", encoding="utf-8"
    )
    orphan = rvf_merge_dispositions.check_merge_dispositions(run_dir, require_fix_issue_cross_reference=True)
    assert any("RVF-ORPHAN has no adopted reviewer source" in error for error in orphan.errors), orphan.errors


def test_excluded_path_reviewer_issue_can_only_be_dropped(tmp_path: Path) -> None:
    """Contract Gate 把排除区 issue 从「整腿作废」放宽成 warning 后，合并是唯一收口点：只能 dropped。"""
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    contract_path = run_dir / "artifacts" / "inputs" / "scope.contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["excluded_path_prefixes"] = ["vendor"]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    vendor_issue = {"path": "vendor/lib.py", "line": 3, "kind": "REAL", "severity": "low", "message": "vendored bug"}
    _set_reviewer_results(run_dir, {"reviewer-a": [vendor_issue], "reviewer-b": []})
    adopted = _write_dispositions(
        run_dir,
        {"dispositions": [{"reviewer_id": "reviewer-a", "index": 1, "disposition": "adopted", "issue_id": "RVF-001"}]},
    )
    assert adopted.returncode != 0
    assert "excluded path prefix" in (adopted.stderr + adopted.stdout)
    dropped = _write_dispositions(
        run_dir,
        {"dispositions": [{"reviewer_id": "reviewer-a", "index": 1, "disposition": "dropped",
                           "reason_code": "outside_review_scope_protected_background_or_excluded"}]},
    )
    assert dropped.returncode == 0, dropped.stderr


def test_outside_scope_reviewer_issue_prefills_validate_only_handling_mode_and_override_needs_reason(
    tmp_path: Path,
) -> None:
    """范围外问题照常被采纳；upsert 按来源 reviewer 的 scope_relation 预填只验证模式，主会话改写须写理由。"""
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    outside = {**_reviewer_issue("pre-existing bug", 2), "scope_relation": "outside_current_round_scope"}
    _set_reviewer_results(run_dir, {"reviewer-a": [_reviewer_issue("in-round bug"), outside], "reviewer-b": []})
    written = _write_dispositions(
        run_dir,
        {"dispositions": [
            {"reviewer_id": "reviewer-a", "index": 1, "disposition": "adopted", "issue_id": "RVF-001"},
            {"reviewer_id": "reviewer-a", "index": 2, "disposition": "adopted", "issue_id": "RVF-002"},
        ]},
    )
    assert written.returncode == 0, written.stderr
    assert _upsert(tmp_path, repo, run_dir, _canonical_issue_file(tmp_path, "RVF-001")).returncode == 0
    assert _upsert(tmp_path, repo, run_dir, _canonical_issue_file(tmp_path, "RVF-002")).returncode == 0
    mirrors = run_dir / "artifacts" / "fix-issues"
    assert json.loads((mirrors / "RVF-001.json").read_text())["fix_issue_handling_mode"] == "validate_then_fix"
    assert (
        json.loads((mirrors / "RVF-002.json").read_text())["fix_issue_handling_mode"]
        == "validate_only_then_submit_to_kanban_backlog"
    )
    overridden = _upsert(
        tmp_path, repo, run_dir,
        _canonical_issue_file(tmp_path, "RVF-002", fix_issue_handling_mode="validate_then_fix"),
    )
    assert overridden.returncode != 0
    assert "handling_mode_override_reason" in overridden.stderr
    justified = _upsert(
        tmp_path, repo, run_dir,
        _canonical_issue_file(
            tmp_path, "RVF-002", fix_issue_handling_mode="validate_then_fix",
            handling_mode_override_reason="其实是本轮改名暴露的回归",
        ),
    )
    assert justified.returncode == 0, justified.stderr
    assert json.loads((mirrors / "RVF-002.json").read_text())["fix_issue_handling_mode"] == "validate_then_fix"


def test_upsert_refuses_run_id_that_differs_from_the_run_directory_whose_dispositions_were_checked(tmp_path: Path) -> None:
    """处置按 run_dir 校验放行：payload 的 run_id 或 --run-id 覆盖指向别的 run 时必须拒绝，
    否则记录落进另一个 run 的 ledger、当前 run 的因果与最终状态缺失。"""
    repo, run_dir = _run_with_reviewer_issues(tmp_path)
    assert _write_dispositions(run_dir, _adopt_merge_drop_decisions()).returncode == 0
    foreign_run_id = "rvf-some-other-run"

    payload_points_elsewhere = _upsert(
        tmp_path, repo, run_dir, _canonical_issue_file(tmp_path, "RVF-001", run_id=foreign_run_id)
    )
    assert payload_points_elsewhere.returncode == 2, payload_points_elsewhere.stderr
    assert "does not match the run directory's own run_id" in payload_points_elsewhere.stderr

    override_points_elsewhere = subprocess.run(
        [
            sys.executable,
            str(FIX_ISSUE_SCRIPT),
            "upsert",
            "--repo",
            str(repo),
            "--run-dir",
            str(run_dir),
            "--run-id",
            foreign_run_id,
            "--issue-file",
            str(_canonical_issue_file(tmp_path, "RVF-001")),
            "--log-root",
            str(tmp_path / "tracker-log-root"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=clean_test_environment(),
    )
    assert override_points_elsewhere.returncode == 2, override_points_elsewhere.stderr
    assert "does not match the run directory's own run_id" in override_points_elsewhere.stderr
    assert not (run_dir / "artifacts/fix-issues/RVF-001.json").exists()

    # 与 run 目录一致的 run_id（显式写在 payload 里）照常放行，记录落在当前 run。
    matching = _upsert(
        tmp_path, repo, run_dir, _canonical_issue_file(tmp_path, "RVF-001", run_id="rvf-candidate-evidence-test")
    )
    assert matching.returncode == 0, matching.stderr
    assert _tracker_issue(tmp_path, repo, "RVF-001")["run_id"] == "rvf-candidate-evidence-test"
    import _rvf_pyroot  # noqa: F401
    from core.session_scope_allocation import reviewable_unit_diff_tracker

    foreign_ledger = reviewable_unit_diff_tracker.rvf_causality_for_run(
        repo=repo, run_id=foreign_run_id, log_root_override=tmp_path / "tracker-log-root"
    )
    assert foreign_ledger["issues"] == []


def test_last_resort_mimic_legs_are_discovered_and_their_identity_never_reaches_the_fixer(tmp_path: Path) -> None:
    """R3：plan 没有腿、两路 in-harness mimic reviewer 按同一目录布局落结果——处置照常可写，
    mimic 身份同样算来源标签，不得出现在给 fixer 的 issue 正文里。"""
    repo = repository(tmp_path / "repo")
    run_dir = create_completed_run(tmp_path, repo)
    reviewer_root = run_dir / "artifacts" / "reviewers"
    for old_leg in ("reviewer-a", "reviewer-b"):
        (reviewer_root / old_leg / "review-result.json").unlink()
        (reviewer_root / old_leg).rmdir()
    for mimic_id, issues in (("codex-mimic-reviewer-a", [_reviewer_issue("loop bound")]), ("codex-mimic-reviewer-b", [])):
        (reviewer_root / mimic_id).mkdir()
        payload = (
            {"schema_version": 1, "kind": "issues", "issues": issues, "requests": []}
            if issues
            else {"schema_version": 1, "kind": "no_issues", "issues": [], "requests": [], "audit_summary": "clean"}
        )
        (reviewer_root / mimic_id / "review-result.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    (reviewer_root / "reviewer-plan.json").write_text(
        json.dumps({"routing_rule": "R3", "needs_last_resort_fallback": True, "status": "planned", "reviewers": []}) + "\n",
        encoding="utf-8",
    )
    written = _write_dispositions(
        run_dir,
        {"dispositions": [{"reviewer_id": "codex-mimic-reviewer-a", "index": 1, "disposition": "adopted", "issue_id": "RVF-001"}]},
    )
    assert written.returncode == 0, written.stderr
    assert [item["reviewer_id"] for item in json.loads(written.stdout)["payload"]["reviewer_results"]] == [
        "codex-mimic-reviewer-a",
        "codex-mimic-reviewer-b",
    ]
    leaking = _canonical_issue_file(tmp_path, "RVF-001", message="codex-mimic-reviewer-a says the loop bound is wrong")
    refused = _upsert(tmp_path, repo, run_dir, leaking)
    assert refused.returncode == 2 and "mentions reviewer source labels" in refused.stderr
    assert _upsert(tmp_path, repo, run_dir, _canonical_issue_file(tmp_path, "RVF-001")).returncode == 0


# --------------------------------------------------------------------------- #
# evidence / land
# --------------------------------------------------------------------------- #


def test_evidence_allows_zero_canonical_issues_when_every_reviewer_issue_is_dropped(tmp_path: Path) -> None:
    repo, run_dir = _run_with_reviewer_issues(tmp_path)
    _handoff(run_dir, "共 0 条，0 真实修复 / 0 误报 / 0 升级")
    without_dispositions = _run_evidence_writer(repo, run_dir)
    assert without_dispositions.returncode == 2
    assert "merge dispositions are missing" in without_dispositions.stderr

    dropped_everything = {
        "dispositions": [
            {"reviewer_id": "reviewer-a", "index": 1, "disposition": "dropped", "reason_code": "not_traceable_to_current_diff"},
            {"reviewer_id": "reviewer-a", "index": 2, "disposition": "dropped", "reason_code": "style_or_preference_only"},
            {
                "reviewer_id": "reviewer-b",
                "index": 1,
                "disposition": "dropped",
                "reason_code": "other_with_reason",
                "reason": "描述的循环在本轮 diff 之外",
            },
        ]
    }
    assert _write_dispositions(run_dir, dropped_everything).returncode == 0
    written = _run_evidence_writer(repo, run_dir)
    assert written.returncode == 0, written.stderr
    evidence = json.loads(Path(json.loads(written.stdout)["candidate_evidence_path"]).read_text(encoding="utf-8"))
    assert evidence["review"]["canonical_issue_count"] == 0
    assert evidence["review"]["conclusion"] == "no_issues"


def test_evidence_refuses_handoff_count_that_disagrees_with_dispositions(tmp_path: Path) -> None:
    repo, run_dir = _run_with_reviewer_issues(tmp_path)
    assert _write_dispositions(run_dir, _adopt_merge_drop_decisions()).returncode == 0
    assert _upsert(tmp_path, repo, run_dir, _canonical_issue_file(tmp_path, "RVF-001")).returncode == 0
    record_terminal_fix_attempt(run_dir, "RVF-001")
    _handoff(run_dir, "共 2 条，2 真实修复 / 0 误报 / 0 升级", "- **[fixed]** a\n- **[fixed]** b\n")
    refused = _run_evidence_writer(repo, run_dir)
    assert refused.returncode == 2
    assert "does not match completed outcomes" in refused.stderr
    _handoff(run_dir, "共 1 条，1 真实修复 / 0 误报 / 0 升级", "- **[fixed]** `tracked.txt:1` - RVF-001\n")
    written = _run_evidence_writer(repo, run_dir)
    assert written.returncode == 0, written.stderr


def test_rvf_land_commits_and_writes_evidence_for_run_whose_reviewer_issues_were_all_dropped(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    (repo / "tracked.txt").write_text("candidate\nreviewed change\n", encoding="utf-8")
    run_dir = create_completed_run(tmp_path, repo)
    _set_reviewer_results(run_dir, {"reviewer-a": [_reviewer_issue("prefer f-strings")], "reviewer-b": []})
    assert (
        _write_dispositions(
            run_dir,
            {
                "dispositions": [
                    {"reviewer_id": "reviewer-a", "index": 1, "disposition": "dropped", "reason_code": "style_or_preference_only"}
                ]
            },
        ).returncode
        == 0
    )
    prepared = rvf_land_prepare(tmp_path, repo, run_dir)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    committed = rvf_land_commit(tmp_path, repo, run_dir)
    assert committed.returncode == 0, committed.stdout + committed.stderr
    result = json.loads(committed.stdout)
    assert result["status"] == "committed"
    assert result["candidate_evidence"]["status"] == "written", result["candidate_evidence"]


def test_rvf_land_refuses_before_git_commit_when_reviewer_issues_lack_merge_dispositions(tmp_path: Path) -> None:
    """处置强校验以前只在 evidence writer 里：land 先 git commit、再得到 refused_fail_closed（提交不回滚）。
    缺失或过期的处置必须在提交**之前**拦下——HEAD 不动、改动不被 stage。"""
    repo = repository(tmp_path / "repo")
    (repo / "tracked.txt").write_text("candidate\nreviewed change\n", encoding="utf-8")
    run_dir = create_completed_run(tmp_path, repo)
    _set_reviewer_results(run_dir, {"reviewer-a": [_reviewer_issue("prefer f-strings")], "reviewer-b": []})
    head_before_land = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    prepared = rvf_land_prepare(tmp_path, repo, run_dir)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr

    def assert_refused_before_commit(expected_error_fragment: str) -> None:
        refused = rvf_land_commit(tmp_path, repo, run_dir)
        assert refused.returncode == 27, refused.stdout + refused.stderr
        payload = json.loads(refused.stdout)
        assert payload["status"] == "refused"
        assert payload["reason_code"] == "merge_dispositions_check_failed"
        assert any(expected_error_fragment in error for error in payload["merge_dispositions_errors"]), payload
        assert (
            subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
            == head_before_land
        )
        assert subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo, check=False).returncode == 0

    assert_refused_before_commit("merge dispositions are missing")

    dropped = {
        "dispositions": [
            {"reviewer_id": "reviewer-a", "index": 1, "disposition": "dropped", "reason_code": "style_or_preference_only"}
        ]
    }
    assert _write_dispositions(run_dir, dropped).returncode == 0
    # canonical 结果事后变了（重派 / 手改）⇒ 处置按哈希判过期，同样在提交前拦下。
    _set_reviewer_results(run_dir, {"reviewer-a": [_reviewer_issue("prefer f-strings", 3)], "reviewer-b": []})
    assert_refused_before_commit("changed since dispositions were written")

    _set_reviewer_results(run_dir, {"reviewer-a": [_reviewer_issue("prefer f-strings")], "reviewer-b": []})
    committed = rvf_land_commit(tmp_path, repo, run_dir)
    assert committed.returncode == 0, committed.stdout + committed.stderr
    assert json.loads(committed.stdout)["candidate_evidence"]["status"] == "written"


# --------------------------------------------------------------------------- #
# causality.json（440b5）
# --------------------------------------------------------------------------- #


def test_causality_json_carries_dispositions_and_per_reviewer_outcomes(tmp_path: Path, monkeypatch) -> None:
    repo, run_dir = _run_with_reviewer_issues(tmp_path)
    assert _write_dispositions(run_dir, _adopt_merge_drop_decisions()).returncode == 0
    assert _upsert(tmp_path, repo, run_dir, _canonical_issue_file(tmp_path, "RVF-001")).returncode == 0
    import _rvf_pyroot  # noqa: F401
    from core.session_scope_allocation import reviewable_unit_diff_tracker

    reviewable_unit_diff_tracker.rvf_issue_upsert(
        repo=repo,
        run_id="rvf-candidate-evidence-test",
        issue_id="RVF-001",
        payload={"issue_id": "RVF-001"},
        source_refs=None,
        state="fixed",
        log_root_override=tmp_path / "tracker-log-root",
    )
    for name in ("RVF_RUN_DIR", "RVF_REPO", "CODEX_RVF_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(tmp_path / "tracker-log-root"))
    import analysis_artifacts

    result = analysis_artifacts.scaffold_run(run_dir)
    causality = json.loads(Path(result["causality_json_path"]).read_text(encoding="utf-8"))
    section = causality["merge_dispositions"]
    assert section["check_errors"] == []
    per_reviewer = section["per_reviewer_disposition_and_final_fix_issue_outcome_summary"]["per_reviewer"]
    assert per_reviewer["reviewer-a"]["disposition_counts"] == {"adopted": 1, "merged_into": 0, "dropped": 1}
    assert per_reviewer["reviewer-a"]["dropped_reason_code_counts"] == {"style_or_preference_only": 1}
    assert per_reviewer["reviewer-a"]["final_fix_issue_state_counts_for_adopted_or_merged_reviewer_issues"] == {"fixed": 1}
    assert per_reviewer["reviewer-b"]["disposition_counts"] == {"adopted": 0, "merged_into": 1, "dropped": 0}
    assert per_reviewer["reviewer-b"]["final_fix_issue_state_counts_for_adopted_or_merged_reviewer_issues"] == {"fixed": 1}
    ledger_issue = next(issue for issue in causality["issues"] if issue["issue_id"] == "RVF-001")
    assert {ref["reviewer_id"] for ref in ledger_issue["source_refs"]} == {"reviewer-a", "reviewer-b"}
