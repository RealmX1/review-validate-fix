#!/usr/bin/env python3
"""reviewer 结果工件的生命周期契约（2026-08-15 run 暴露的死结与不对称）。

护的不变量：

* **LIFE-1（死结已解，且方向不对称是有意的）** —— 除了「已写成 ``issues`` 再换 kind」这一条，
  三态之间其余方向都可达，且换 kind 一律先归档旧工件。历史上是：``request → issues`` 直接报错
  （死结，reviewer 契约又禁止它删改工件 ⇒ 同一 run 内永远交不出 issues，实测废掉一整轮 codex
  评审），而 ``request → no_issues`` 却**无条件原地覆写**（合法 request 无归档地凭空消失）。
* **LIFE-2（不丢结果）** —— 归档链可追溯：新工件的 ``superseded`` 指向每一份被取代的旧工件。
* **LIFE-3（只重派 request 腿）** —— ``--redispatch-review-request-legs-only`` 复用上一轮 plan、
  只重跑停在 ``kind: request`` 的腿；健康且已完成的腿保留结果、不重跑。整盘重派会把它重跑一遍，
  新一轮结果覆写掉旧的（实测一条 cursor 腿跑了三轮 2/4/3 条 issue，只留最后一轮）。
* **LIFE-4（issues 是终态）** —— 归档保住的是**取证**能力，不是**判定**能力：canonical ``kind``
  的读者（``check_review_result`` / dispatcher 有效腿判定 / merge policy / candidate evidence）
  没有一个会合并 ``superseded`` 链。所以 ``issues → no_issues`` / ``issues → request`` 必须被
  拒绝，否则后一次 writer 调用就能把已找到的 REAL issue 洗成 clean。反方向（补结论、退回待处理）
  不封——封了就是同一类假绿的镜像。
* **LIFE-5（预演不破坏）** —— ``--dry-run`` 的契约是 “Print plan JSON to stdout; do not write or
  execute”；request-only 重派的工件轮转只能发生在真正 execute 之前，预演一次不得搬走当前结果。
* **LIFE-6（指引与 flag 不许脱节）** —— ``blocked_on_reviewer_request`` 的 next_steps 与
  SKILL.md / references/review-merge-policy.md 必须都指向 ``--redispatch-review-request-legs-only``。
  只在 flag 上实现、指引仍写「整盘重跑」，等于修复在真实入口上没生效。
"""

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
        (_request_arguments(), "request", _issue_arguments(), "issues"),
        (_request_arguments(), "request", _no_issues_arguments(), "no_issues"),
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
    assert [entry["kind"] for entry in payload["superseded"]] == ["no_issues", "request"]
    assert len(list(_archive_dir(out).glob("review-result.*.json"))) == 2


@pytest.mark.parametrize(
    "second_arguments, second_kind",
    [
        (_no_issues_arguments(), "no_issues"),
        (_request_arguments(), "request"),
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
    assert "终态" in refused.stderr, refused.stderr
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
            payload["requests"] = [{"type": "context_request", "need": "file", "target": "a.py"}]
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

    plan, indices = dispatch_reviewers._resume_plan_for_review_request_legs(reviewers_dir, artifacts)

    assert indices == [0], "只有 alpha 停在 request"
    assert plan["reviewers"][0]["reviewer_id"] == "alpha"
    assert plan["status"] == "planned", "上一轮的终态标记必须清掉，否则本轮永远挂着 blocked"
    assert "reason" not in plan
    assert any(
        warning["code"] == "redispatching_review_request_legs_only" for warning in plan["warnings"]
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
    plan, indices = dispatch_reviewers._resume_plan_for_review_request_legs(reviewers_dir, artifacts)

    rotated = dispatch_reviewers._rotate_review_request_leg_artifacts(plan, indices, artifacts)

    assert len(rotated) == 1
    # 健康腿：一个字节都不动。
    assert (reviewers_dir / "beta" / "review-result.json").read_text(encoding="utf-8") == healthy_before
    # request 腿：旧工件被轮转走，重跑从干净起点开始（否则新 request 会追加在旧的后面）。
    assert not (reviewers_dir / "alpha" / "review-result.json").exists()
    archived = list(
        (reviewers_dir / "alpha" / write_review_result.SUPERSEDED_RESULT_ARTIFACTS_DIRNAME).glob(
            "review-result.*.json"
        )
    )
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == request_before


def test_dry_run_redispatch_prints_the_plan_without_moving_any_result_artifact(tmp_path: Path) -> None:
    """LIFE-5（端到端）：``--dry-run --redispatch-review-request-legs-only`` 只打印 plan。

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
            "--redispatch-review-request-legs-only",
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
        dispatch_reviewers._resume_plan_for_review_request_legs(artifacts / "reviewers", artifacts)


def test_resume_reports_nothing_to_do_when_no_leg_is_pending(tmp_path: Path) -> None:
    """两腿都已完成 → 返回空 indices，调用方据此报「无需重派」而不是空跑一轮。"""
    import dispatch_reviewers  # noqa: PLC0415

    artifacts = _seed_run_with_two_legs(tmp_path, alpha_kind="issues", beta_kind="no_issues")
    _plan, indices = dispatch_reviewers._resume_plan_for_review_request_legs(
        artifacts / "reviewers", artifacts
    )
    assert indices == []


def test_blocked_on_reviewer_request_guidance_points_at_the_request_only_flag() -> None:
    """LIFE-6：真实入口（next_steps + 两份文档）必须都指向 request-only 重派，而不是整盘重跑。

    只在 CLI flag 上实现、指引仍写「带同一 --selection-seed 重跑 --execute --detached」，
    等于修复在标准操作流程上从未生效：照指引做，健康且已完成的那条腿仍会被重跑并覆写。
    """
    import inspect  # noqa: PLC0415

    import rvf_agent_decision_cli  # noqa: PLC0415

    flag = rvf_agent_decision_cli.REVIEW_REQUEST_REDISPATCH_FLAG
    assert flag == "--redispatch-review-request-legs-only"

    source = inspect.getsource(rvf_agent_decision_cli.run_dispatch)
    assert "REVIEW_REQUEST_REDISPATCH_FLAG" in source, (
        "blocked_on_reviewer_request 的 next_steps 必须引用该 flag 常量"
    )
    assert "--selection-seed <seed>" not in source, (
        "该终态下不该再指引带 --selection-seed 整盘重跑（重派模式根本不调用 route()）"
    )

    skill_directory = ROOT / "plugins/review-validate-fix/skills/review-validate-fix"
    for document in (skill_directory / "SKILL.md", skill_directory / "references/review-merge-policy.md"):
        assert flag in document.read_text(encoding="utf-8"), f"{document} 未描述 request-only 重派"


def test_detached_argv_forwards_the_request_only_redispatch_flag() -> None:
    """``--execute --detached`` 是主会话的标准用法：不透传这个 flag，修复在真正跑活的那条路径上作废。"""
    import argparse  # noqa: PLC0415

    import dispatch_reviewers  # noqa: PLC0415

    args = argparse.Namespace(
        registry="reg.json", probe_mode="fast", probe_timeout=1.0, main_harness="codex",
        transcript=None, main_harness_file=None, assume_available=None, require_external=False,
        selection_seed=None, redispatch_review_request_legs_only=True,
    )
    argv = dispatch_reviewers._build_detached_child_argv(
        args, run_id="r", run_dir="/tmp/r", repo=None, review_packet=None,
        session_context=None, scope_contract=None,
    )
    assert "--redispatch-review-request-legs-only" in argv, argv


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
