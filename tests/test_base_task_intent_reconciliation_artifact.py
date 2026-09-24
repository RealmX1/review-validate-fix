"""N5 / K4：消费 base-branch-sync 的 base↔task intent 复核 artifact。

**这一族测试守的不变量只有一条：消费端永不阻断。** `consumed` / `missing` / `invalid` 三种情形走
同一条放行路径，区别只在标注与统计。任何一条断言若被改成「非法就抛」，就等于把本轮计划正在拆的
fail-closed 死路又装回来一条。

跨仓契约见 `~/.cross-repository-task-coordination/base-branch-sync-rvf-land-deadlock-removal/`。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import base_task_intent_reconciliation_artifact as intent_module  # noqa: E402
import prepare_review_run  # noqa: E402


PRE_REVIEW_BASE_SYNC_MARKER_LINE = (
    "RVF-PRE-REVIEW-BASE-SYNC-SKILL: sync-local-base-into-task-worktree-only\n"
)


def _clean_git_environment() -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update(
        {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    )
    return environment


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        env=_clean_git_environment(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_canonical_artifact(path: Path, payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    Path(f"{path}.sha256").write_text(digest + "\n", encoding="ascii")
    return digest


def _opt_in_repo(tmp_path: Path, *, name: str = "repo") -> dict[str, object]:
    """建一个已 opt-in 且已吸收 local base 的 task worktree（detached HEAD，与生产同形）。"""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "RVF Intent Test")
    _git(repo, "config", "user.email", "rvf-intent@example.test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "base")
    task_start = _git(repo, "rev-parse", "HEAD")

    (repo / "AGENTS.md").write_text(PRE_REVIEW_BASE_SYNC_MARKER_LINE, encoding="utf-8")
    _git(repo, "add", "AGENTS.md")
    _git(repo, "commit", "-q", "-m", "base enables pre-review sync")
    local_base_tip = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "--detach", "main")
    task_tip_after_absorption = _git(repo, "rev-parse", "HEAD")
    (repo / "tracked.txt").write_text("changed by the agent\n", encoding="utf-8")
    return {
        "repo": repo,
        "task_start": task_start,
        "local_base_tip": local_base_tip,
        "task_tip_after_absorption": task_tip_after_absorption,
    }


def _bootstrap_receipt(
    fixture: dict[str, object], *, base_ref: str = "main"
) -> tuple[dict[str, object], str, Path]:
    repo = Path(str(fixture["repo"]))
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir"))
    common_dir = Path(
        _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()
    # 这三个数组会被 validated_bootstrap_receipt 用固定 tips 现场复算并逐字比对，
    # 硬编码必然漂移 —— 一律用与生产同款的 `git diff --name-only -z` 现算。
    def _diff_paths(older: str, newer: str) -> list[str]:
        completed = subprocess.run(
            ["git", "diff", "--name-only", "-z", older, newer, "--"],
            cwd=repo,
            env=_clean_git_environment(),
            check=True,
            capture_output=True,
        )
        return sorted({os.fsdecode(path) for path in completed.stdout.split(b"\0") if path})

    base_delta_paths = _diff_paths(str(fixture["task_start"]), str(fixture["local_base_tip"]))
    task_change_paths = _diff_paths(str(fixture["task_start"]), str(fixture["task_start"]))
    receipt = {
        "base_delta_paths": base_delta_paths,
        "task_change_paths": task_change_paths,
        "interaction_primary_paths": sorted(set(base_delta_paths) & set(task_change_paths)),
        "schema_name": "local-base-into-task-worktree-bootstrap-receipt",
        "schema_version": 1,
        "base_ref": base_ref,
        "remote_name": "origin",
        "absorbed_base_tip": fixture["local_base_tip"],
        "base_task_merge_base_tip": fixture["task_start"],
        "task_tip_before_base_absorption": fixture["task_start"],
        "task_tip_after_base_absorption": fixture["task_tip_after_absorption"],
        "git_common_directory_realpath": str(common_dir),
        "task_worktree_realpath": str(repo.resolve()),
        "boundary_status": "VERIFIED",
        "lock_status": "RELEASED",
        "target_repository_hooks_status": "DISABLED",
        "wip_status": "RESTORED_AND_DROPPED",
    }
    receipt_path = (
        git_dir / "base-branch-sync" / "local-base-into-task-worktree-bootstrap-receipt.json"
    )
    digest = _write_canonical_artifact(receipt_path, receipt)
    return receipt, digest, receipt_path


def _intent_payload(
    receipt: dict[str, object], receipt_digest: str, **overrides: object
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_name": intent_module.INTENT_ARTIFACT_SCHEMA_NAME,
        "schema_version": intent_module.INTENT_ARTIFACT_SCHEMA_VERSION,
        "paradigm": intent_module.INTAKE_ROUND_PARADIGM,
        "bootstrap_receipt_digest": receipt_digest,
        "task_worktree_realpath": receipt["task_worktree_realpath"],
        "git_common_directory_realpath": receipt["git_common_directory_realpath"],
        "base_ref": receipt["base_ref"],
        "remote_name": receipt["remote_name"],
        "absorbed_base_tip": receipt["absorbed_base_tip"],
        "task_tip_after_base_absorption": receipt["task_tip_after_base_absorption"],
        "base_delta_intent_summary": "base 把 hook 出口受众收敛成单一契约",
        "mechanical_adaptation_paths": ["adapters/host_adapter.py"],
        "unresolved_questions": ["Kimi 侧的 systemMessage 是否仍需保留兼容分支？"],
        "created_at_utc": "2026-08-26T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def _write_intent_artifact(repo: Path, payload: dict[str, object]) -> Path:
    path = Path(intent_module.expected_intent_artifact_path(repo))
    _write_canonical_artifact(path, payload)
    return path


# --------------------------------------------------------------------------- 单元层


def test_intent_artifact_bound_to_this_rounds_receipt_is_consumed(tmp_path: Path) -> None:
    fixture = _opt_in_repo(tmp_path)
    repo = Path(str(fixture["repo"]))
    receipt, digest, _receipt_path = _bootstrap_receipt(fixture)
    _write_intent_artifact(repo, _intent_payload(receipt, digest))

    outcome = intent_module.consumed_base_task_intent_reconciliation(repo, receipt, digest)

    assert outcome["status"] == intent_module.INTENT_ARTIFACT_STATUS_CONSUMED
    assert outcome["reason"] is None
    assert outcome["payload"]["base_delta_intent_summary"].startswith("base 把 hook")

    rendered = intent_module.intent_context_markdown(outcome)
    assert "base 把 hook 出口受众收敛成单一契约" in rendered
    assert "adapters/\n└── host_adapter.py" in rendered
    assert "Kimi 侧的 systemMessage" in rendered
    # 归因分类的自报性质必须逐字写进注入文本——reviewer 不能把它当已核实事实用。
    assert "自报值" in rendered and "没有任何机器校验" in rendered


def test_missing_intent_artifact_is_annotated_and_never_raises(tmp_path: Path) -> None:
    fixture = _opt_in_repo(tmp_path)
    repo = Path(str(fixture["repo"]))
    receipt, digest, _receipt_path = _bootstrap_receipt(fixture)

    outcome = intent_module.consumed_base_task_intent_reconciliation(repo, receipt, digest)

    assert outcome["status"] == intent_module.INTENT_ARTIFACT_STATUS_MISSING
    assert outcome["payload"] is None
    rendered = intent_module.intent_context_markdown(outcome)
    # 反向断言：缺失时**不得**渲染出任何 intent 正文块，否则 reviewer 会误以为上游已交付。
    assert "### base delta 的意图" not in rendered
    assert "归因分类" not in rendered
    # 但必须显式声明「本轮没有」，否则沉默会被读成「base 没变化」。
    assert "`missing`" in rendered
    assert "不要据此推断 base delta 为空" in rendered


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("wrong_schema_name", lambda p, d: p.update({"schema_name": "something-else"})),
        ("wrong_schema_version", lambda p, d: p.update({"schema_version": 999})),
        ("full_handback_paradigm", lambda p, d: p.update({"paradigm": "FULL_HANDBACK"})),
        ("replayed_receipt_digest", lambda p, d: p.update({"bootstrap_receipt_digest": "0" * 64})),
        ("foreign_worktree", lambda p, d: p.update({"task_worktree_realpath": "/tmp/somewhere"})),
        ("stale_absorbed_base_tip", lambda p, d: p.update({"absorbed_base_tip": "f" * 40})),
        ("summary_not_a_string", lambda p, d: p.update({"base_delta_intent_summary": 42})),
        ("mechanical_paths_not_a_list", lambda p, d: p.update({"mechanical_adaptation_paths": "a"})),
    ],
)
def test_invalid_intent_artifact_is_annotated_invalid_and_never_raises(
    tmp_path: Path, label: str, mutate
) -> None:
    fixture = _opt_in_repo(tmp_path, name=f"repo-{label}")
    repo = Path(str(fixture["repo"]))
    receipt, digest, _receipt_path = _bootstrap_receipt(fixture)
    payload = _intent_payload(receipt, digest)
    mutate(payload, digest)
    _write_intent_artifact(repo, payload)

    outcome = intent_module.consumed_base_task_intent_reconciliation(repo, receipt, digest)

    assert outcome["status"] == intent_module.INTENT_ARTIFACT_STATUS_INVALID, label
    assert outcome["payload"] is None
    assert outcome["reason"]
    # 非法与缺失在渲染上同型：都不得漏出未经校验的 intent 正文。
    rendered = intent_module.intent_context_markdown(outcome)
    assert "### base delta 的意图" not in rendered


def test_tampered_bytes_are_invalid_because_sidecar_and_canonical_form_are_checked(
    tmp_path: Path,
) -> None:
    fixture = _opt_in_repo(tmp_path)
    repo = Path(str(fixture["repo"]))
    receipt, digest, _receipt_path = _bootstrap_receipt(fixture)
    artifact_path = _write_intent_artifact(repo, _intent_payload(receipt, digest))

    # 改内容但不改 sidecar：必须被第一层（sidecar 自洽）挡住。
    artifact_path.write_bytes(artifact_path.read_bytes().replace(b"base \xe6\x8a\x8a", b"BASE \xe6\x8a\x8a"))
    assert (
        intent_module.consumed_base_task_intent_reconciliation(repo, receipt, digest)["status"]
        == intent_module.INTENT_ARTIFACT_STATUS_INVALID
    )

    # 非 canonical 字节（缩进过的合法 JSON）+ 匹配的 sidecar：必须被 canonical 那层挡住。
    pretty = json.dumps(_intent_payload(receipt, digest), ensure_ascii=False, indent=2).encode()
    artifact_path.write_bytes(pretty)
    Path(f"{artifact_path}.sha256").write_text(
        hashlib.sha256(pretty).hexdigest() + "\n", encoding="ascii"
    )
    assert (
        intent_module.consumed_base_task_intent_reconciliation(repo, receipt, digest)["status"]
        == intent_module.INTENT_ARTIFACT_STATUS_INVALID
    )


def test_unknown_receipt_digest_cannot_silently_disable_the_anti_replay_anchor(
    tmp_path: Path,
) -> None:
    """digest 传 None 时**不能**当成通过——否则 anti-replay 锚会被静默旁路。"""
    fixture = _opt_in_repo(tmp_path)
    repo = Path(str(fixture["repo"]))
    receipt, digest, _receipt_path = _bootstrap_receipt(fixture)
    _write_intent_artifact(repo, _intent_payload(receipt, digest))

    outcome = intent_module.consumed_base_task_intent_reconciliation(repo, receipt, None)

    assert outcome["status"] == intent_module.INTENT_ARTIFACT_STATUS_INVALID
    assert "bootstrap_receipt_digest" in str(outcome["reason"])


def test_no_bootstrap_receipt_reports_missing_rather_than_failing(tmp_path: Path) -> None:
    fixture = _opt_in_repo(tmp_path)
    repo = Path(str(fixture["repo"]))
    outcome = intent_module.consumed_base_task_intent_reconciliation(repo, None, None)
    assert outcome["status"] == intent_module.INTENT_ARTIFACT_STATUS_MISSING


def test_consumption_outcome_log_appends_every_status_not_just_the_successful_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "rvf-home"))
    monkeypatch.delenv(
        intent_module.ENV_INTENT_ARTIFACT_CONSUMPTION_OUTCOME_LOG_ROOT, raising=False
    )
    common_directory = "/Users/someone/GitHub/cline-kanban/.git"
    for status in (
        intent_module.INTENT_ARTIFACT_STATUS_CONSUMED,
        intent_module.INTENT_ARTIFACT_STATUS_MISSING,
        intent_module.INTENT_ARTIFACT_STATUS_INVALID,
    ):
        intent_module.record_intent_artifact_consumption_outcome(
            outcome={"status": status, "reason": f"reason-{status}", "digest": None},
            git_common_directory=common_directory,
            task_worktree="/Users/someone/.cline/worktrees/cc088/cline-kanban",
            rvf_run_id=f"rvf-{status}",
        )

    records = intent_module.read_intent_artifact_consumption_outcomes("cline-kanban")
    assert [record["intent_artifact_status"] for record in records] == [
        "consumed",
        "missing",
        "invalid",
    ]
    for record in records:
        assert set(record) == {
            "appended_at_utc",
            "repository_name",
            "rvf_run_id",
            "task_worktree",
            "intent_artifact_status",
            "reason",
            "intent_artifact_digest",
        }
    # 落点必须在 $RVF_HOME 下，不在任何 per-worktree git-dir 里。
    log_path = tmp_path / "rvf-home" / "intent-artifact-consumption-outcomes" / "cline-kanban.jsonl"
    assert log_path.is_file()


# --------------------------------------------------------------------------- prepare 端到端


def _scope_of_work(tmp_path: Path, run_id: str) -> Path:
    scope_of_work = tmp_path / f"{run_id}-scope-of-work.md"
    scope_of_work.write_text("# Scope of work\n\n本轮改了 tracked.txt。\n", encoding="utf-8")
    return scope_of_work


def _prepare(tmp_path: Path, repo: Path, *, run_id: str) -> dict[str, object]:
    scope_of_work = _scope_of_work(tmp_path, run_id)
    return prepare_review_run.prepare_run(
        repo=repo,
        session_context=scope_of_work,
        session_manifest=None,
        transcript=None,
        base_dir=tmp_path / "runs",
        max_file_bytes=200_000,
        max_packet_bytes=0,
        primary_files=[],
        background_files=[],
        exclude_path_prefixes=[],
        rvf_run_id=run_id,
        rvf_run_dir=tmp_path / "runs" / run_id,
    )


def test_prepare_defaults_to_missing_artifact_and_still_starts_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N8c：两侧部署不同步时 artifact 必然缺失——这是**默认路径**，必须照常开跑。

    并反向断言 reviewer 拿到的 scope-of-work 里**没有** intent 正文块，否则会被误读成
    「上游 intent 复核已交付」。
    """
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "rvf-home"))
    fixture = _opt_in_repo(tmp_path)
    repo = Path(str(fixture["repo"]))
    _bootstrap_receipt(fixture)

    result = _prepare(tmp_path, repo, run_id="rvf-intent-missing")

    assert result["intent_artifact_status"] == "missing"
    status_artifact = Path(str(result["intent_artifact_status_file"]))
    assert json.loads(status_artifact.read_text())["intent_artifact_status"] == "missing"

    scope_of_work = Path(str(result["artifacts_dir"])) / "scope-of-work.md"
    text = scope_of_work.read_text(encoding="utf-8")
    assert "`intent_artifact_status`: `missing`" in text
    assert "### base delta 的意图" not in text
    assert "归因分类" not in text

    records = intent_module.read_intent_artifact_consumption_outcomes(repo.name)
    assert [record["intent_artifact_status"] for record in records] == ["missing"]


def test_prepare_injects_intent_context_when_the_artifact_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "rvf-home"))
    fixture = _opt_in_repo(tmp_path)
    repo = Path(str(fixture["repo"]))
    receipt, digest, _receipt_path = _bootstrap_receipt(fixture)
    _write_intent_artifact(repo, _intent_payload(receipt, digest))

    result = _prepare(tmp_path, repo, run_id="rvf-intent-consumed")

    assert result["intent_artifact_status"] == "consumed"
    text = (Path(str(result["artifacts_dir"])) / "scope-of-work.md").read_text(encoding="utf-8")
    assert "### base delta 的意图" in text
    assert "base 把 hook 出口受众收敛成单一契约" in text
    # 原有 scope-of-work 内容必须保留（追加，不覆盖）。
    assert "本轮改了 tracked.txt。" in text
    # 冻结副本与 review packet 都应含注入内容（注入发生在 packet 构建之前）。
    packet_text = Path(str(result["review_packet"])).read_text(encoding="utf-8")
    assert "base 把 hook 出口受众收敛成单一契约" in packet_text


def test_prepare_never_blocks_on_an_invalid_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N8b：存在但非法 ⇒ 与缺失同路放行，只是标注为 invalid。"""
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "rvf-home"))
    fixture = _opt_in_repo(tmp_path)
    repo = Path(str(fixture["repo"]))
    receipt, digest, _receipt_path = _bootstrap_receipt(fixture)
    _write_intent_artifact(repo, _intent_payload(receipt, digest, paradigm="FULL_HANDBACK"))

    result = _prepare(tmp_path, repo, run_id="rvf-intent-invalid")

    assert result["intent_artifact_status"] == "invalid"
    assert "paradigm" in str(result["intent_artifact_status_reason"])
    assert Path(str(result["review_packet"])).is_file()
    records = intent_module.read_intent_artifact_consumption_outcomes(repo.name)
    assert [record["intent_artifact_status"] for record in records] == ["invalid"]


def test_non_opt_in_repository_is_neither_annotated_nor_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 opt-in 仓库不参与本机制：写日志只会污染过渡机制退出条件的分母。"""
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "rvf-home"))
    repo = tmp_path / "plain-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "RVF Intent Test")
    _git(repo, "config", "user.email", "rvf-intent@example.test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = _prepare(tmp_path, repo, run_id="rvf-intent-not-applicable")

    assert result["intent_artifact_status"] is None
    assert result["intent_artifact_status_file"] is None
    assert intent_module.read_intent_artifact_consumption_outcomes(repo.name) == []


# --------------------------------------------------------------------------- N4：堵 O1 旁路


def test_prepare_refuses_an_explicit_base_ref_that_escapes_the_opt_in_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O1：不许拿一个存在但没有 marker 的旁支当 base ref，把 opt-in 门禁静默变成 not_applicable。"""
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "rvf-home"))
    fixture = _opt_in_repo(tmp_path)
    repo = Path(str(fixture["repo"]))
    # `sidebar` 停在加 marker **之前**那个提交上：分支存在、ref 合法，但读不到 marker。
    _git(repo, "branch", "sidebar", str(fixture["task_start"]))

    with pytest.raises(ValueError) as raised:
        prepare_review_run.prepare_run(
            repo=repo,
            session_context=_scope_of_work(tmp_path, "bypass"),
            session_manifest=None,
            transcript=None,
            base_dir=tmp_path / "runs",
            max_file_bytes=200_000,
            max_packet_bytes=0,
            primary_files=[],
            background_files=[],
            exclude_path_prefixes=[],
            rvf_run_id="rvf-o1-bypass",
            rvf_run_dir=tmp_path / "runs" / "rvf-o1-bypass",
            local_base_ref="sidebar",
        )
    assert "bypasses the repository's opt-in pre-review base sync marker" in str(raised.value)


def test_prepare_allows_an_explicit_base_ref_that_is_itself_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向对照：显式 ref 与独立解析不同、但它自己也带 marker ⇒ 合法用法，不得误伤。

    没有这条，上一条测试就可能靠「凡显式 ref 与独立解析不同即拒」蒙混过关，
    而那会打死「默认分支不叫 main、又没有 origin/HEAD」的仓库。
    """
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "rvf-home"))
    fixture = _opt_in_repo(tmp_path)
    repo = Path(str(fixture["repo"]))
    _git(repo, "branch", "release", str(fixture["local_base_tip"]))
    _bootstrap_receipt(fixture, base_ref="release")

    result = prepare_review_run.prepare_run(
        repo=repo,
        session_context=_scope_of_work(tmp_path, "opt-in-alias"),
        session_manifest=None,
        transcript=None,
        base_dir=tmp_path / "runs",
        max_file_bytes=200_000,
        max_packet_bytes=0,
        primary_files=[],
        background_files=[],
        exclude_path_prefixes=[],
        rvf_run_id="rvf-o1-alias",
        rvf_run_dir=tmp_path / "runs" / "rvf-o1-alias",
        local_base_ref="release",
    )
    assert result["scope_contract_payload"]["base_ref"] == "release"
    # 仍走完整 opt-in 路径（说明没有被误判成 not_applicable）
    assert result["intent_artifact_status"] == "missing"
