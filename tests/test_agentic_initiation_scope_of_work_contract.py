#!/usr/bin/env python3
"""agentic 发起路径的 scope-of-work 契约（2026-08-15 run 暴露的三个缺陷）。

护的不变量：

* **SOW-1** —— agentic 发起（agent 经 Stop hook 决策点显式 start）产出的 stub **不得**自称
  「用户显式触发的手动入口」。历史上这条路径硬写 ``target_flow="flow-manual"``，于是 stub 与同一
  份 prep payload 里的 ``dispatch_origin=agentic_initiation_start`` 直接矛盾，reviewer 读到假来源。
* **SOW-2** —— stub 带**机器可判**的标记，`dispatch` 的 contract gate 据它拒派；agent 写实质内容
  并删掉标记后 gate 放行。用显式标记而不是猜正文像不像 stub。
* **SOW-3** —— 存在**受支持的重渲入口**把改好的 scope-of-work 渲进已冻结的 review packet，且重渲
  会归档旧工件。没有它，`prepare_run_from_prep_file` 在 completed 态幂等直接返回，agent 照指引写
  好的正文永远进不了 packet（实测：agent 只能手改冻结的 packet，canonical 与 inputs/ 随之分裂）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import prepare_review_run  # noqa: E402
import rvf_agent_decision_cli  # noqa: E402
import rvf_dispatch_prompts  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_rvf_runtime_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离铁律：本文件**在进程内**直调 ``prepare_review_run.prepare_run``。

    ``RunLedger`` 在没有显式 run 时会回落读 ``RVF_RUN_ID`` / ``RVF_RUN_DIR``
    （``core/run_ledger/run_ledger.py:327,336``）——那是正确的生产行为（跑在某个 run 里的进程
    就该记进那个 run），但它意味着：只要本测试在一个 **source 过 review-env.sh 的 shell** 里跑
    （reviewer / validate-fix 子代理跑测试时正是如此），``prepare_run`` 就会把**活跃 run** 的
    ``scope-of-work.md`` / ``review-packet.md`` / ``inputs/`` 整组覆盖成测试夹具内容。

    这不是假设：2026-08-16 的 dogfood run `rvf-20260815T212538Z-…-d6fc827a` 就是这样被本文件
    当场毁掉冻结输入的（三份工件同一 mtime，内容变成 `/tmp/repo` / `tracked.txt`）。

    所以本文件必须清掉整个 RVF env 命名空间并把落盘根钉进 tmp。
    """
    monkeypatch.setattr(prepare_review_run, "bind_review_start_boundary_to_one_run", lambda receipt, run_id, run_dir: (run_id, Path(run_dir) if run_dir is not None else None))
    # 这里只验证 SOW 渲染/门禁；独立消息/Git gate 在专用集成套件运行，不在此重复构造。
    monkeypatch.setattr(prepare_review_run, "require_review_start_boundary_for_prepare", lambda **kw: {"test_scope": "scope-of-work"})
    monkeypatch.setattr(rvf_agent_decision_cli, "require_prepared_run_review_start_boundary", lambda **kw: {"test_scope": "scope-of-work"})
    for name in tuple(os.environ):
        if name.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "rvf-home"))
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(tmp_path / "run-logs"))
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "user-local-data"))


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


def _stub_text(target_flow: str, *, candidate_paths: list[str] | None = None) -> str:
    return rvf_dispatch_prompts.dispatch_scope_of_work_text(
        target_flow=target_flow,
        cwd="/tmp/repo",
        parent_session_id="session-x",
        parent_thread_path=Path("/tmp/transcript.jsonl"),
        prompt_path=None,
        run_id="rvf-x",
        run_dir=Path("/tmp/run"),
        candidate_paths=candidate_paths,
    )


def test_agentic_initiation_stub_does_not_claim_a_manual_user_trigger() -> None:
    """SOW-1：来源必须是「agent 经决策点显式 start」，不得复用手动入口那套说辞。"""
    text = _stub_text(rvf_dispatch_prompts.AGENTIC_INITIATION_FLOW)
    assert "agentic RVF initiation" in text, text[:400]
    assert rvf_dispatch_prompts.AGENTIC_INITIATION_FLOW in text
    assert "/review-validate-fix" not in text, (
        "agentic 发起的 stub 不得声称用户显式触发了手动入口——它与 "
        "dispatch_origin=agentic_initiation_start 直接矛盾。"
    )
    assert "post-user-prompt hook 在用户显式触发" not in text, text[:400]

    manual = _stub_text("flow-manual")
    assert "/review-validate-fix" in manual, "对照组：手动入口的 stub 本来就该这么说"


def test_agentic_initiation_stub_seeds_the_candidate_paths_as_a_skeleton() -> None:
    """决策点已经算过候选路径，stub 拿它当骨架——agent 逐条填，而不是对着空白页现编。"""
    text = _stub_text(
        rvf_dispatch_prompts.AGENTIC_INITIATION_FLOW,
        candidate_paths=["core/a.py", "plugins/b.py"],
    )
    assert "├── core/\n│   └── a.py\n└── plugins/\n    └── b.py" in text, text[:700]
    assert "每个文件" in text and "完整的仓库相对路径" in text
    assert "待填" in text

    empty = _stub_text(rvf_dispatch_prompts.AGENTIC_INITIATION_FLOW, candidate_paths=[])
    assert "决策点未带候选路径" in empty, empty[:600]


@pytest.mark.parametrize(
    "target_flow",
    [rvf_dispatch_prompts.AGENTIC_INITIATION_FLOW, "flow-manual"],
)
def test_stub_marker_is_machine_detectable_and_clears_once_removed(target_flow: str) -> None:
    """SOW-2：两条会产出 stub 的 flow 都带机器可判标记，删掉标记即视为已覆盖。"""
    text = _stub_text(target_flow)
    assert rvf_dispatch_prompts.SCOPE_OF_WORK_STUB_MARKER in text
    assert rvf_dispatch_prompts.scope_of_work_is_untouched_stub(text)

    written = text.replace(rvf_dispatch_prompts.SCOPE_OF_WORK_STUB_MARKER, "").strip()
    assert not rvf_dispatch_prompts.scope_of_work_is_untouched_stub(written)
    assert not rvf_dispatch_prompts.scope_of_work_is_untouched_stub(None)


@pytest.mark.parametrize(
    "target_flow",
    [rvf_dispatch_prompts.AGENTIC_INITIATION_FLOW, "flow-manual"],
)
def test_removing_only_the_top_marker_line_clears_the_gate(target_flow: str) -> None:
    """SOW-2 的**指引—判据一致性**：严格照 stub 正文的说明操作就必须放行。

    正文写的是「删掉本文件**顶部**那一整行 stub 标记」，而 agentic stub 的正文里又把同一个字面量
    用反引号复述了一遍（指认要删哪一行）。判据若是全文 substring，照指引只删顶部那行之后页脚的
    复述仍在 ⇒ gate 继续拒派，agent 无路可走。既有用例用 ``replace`` 全删或整文件覆盖，恰好绕开
    了这条路径，所以这个矛盾没被测到。
    """
    text = _stub_text(target_flow)
    assert rvf_dispatch_prompts.scope_of_work_is_untouched_stub(text)

    lines = text.splitlines()
    marker_line_indexes = [
        index
        for index, line in enumerate(lines)
        if line.strip() == rvf_dispatch_prompts.SCOPE_OF_WORK_STUB_MARKER
    ]
    assert marker_line_indexes == [2], (target_flow, lines[:6])

    # 只删顶部那一行——正文里的复述原样留着。
    written = "\n".join(lines[: marker_line_indexes[0]] + lines[marker_line_indexes[0] + 1 :])
    assert not rvf_dispatch_prompts.scope_of_work_is_untouched_stub(written), (
        "照 stub 自己的指引操作之后仍被判成 stub —— 指引与判据自相矛盾"
    )
    if target_flow == rvf_dispatch_prompts.AGENTIC_INITIATION_FLOW:
        assert rvf_dispatch_prompts.SCOPE_OF_WORK_STUB_MARKER in written, (
            "前提：正文确实复述了该字面量，本用例才有意义"
        )


def test_quoting_the_marker_in_a_real_scope_of_work_is_not_a_stub() -> None:
    """本仓自审时 scope-of-work 正文里就会出现这个字面量（在改的就是它）。

    全文 substring 判定会把一份写满实质内容的正文误判成 stub，从而永远派不出去。
    """
    written = (
        "# Scope of Work\n\n"
        "本轮改的正是 stub 标记的判定方式：\n"
        f"把 `{rvf_dispatch_prompts.SCOPE_OF_WORK_STUB_MARKER}` 从全文 substring 收紧成顶部独立行。\n"
    )
    assert not rvf_dispatch_prompts.scope_of_work_is_untouched_stub(written)


def test_kanban_and_self_rising_stubs_carry_no_marker() -> None:
    """反面：那两条 flow 的 scope-of-work 由后续 prepare 接手，不该被 dispatch gate 拦。"""
    for target_flow in ("flow-2-branch", "flow-1-self-rising", "flow-3-inplace"):
        text = _stub_text(target_flow)
        assert not rvf_dispatch_prompts.scope_of_work_is_untouched_stub(text), target_flow


def _prepare_one_run(tmp_path: Path) -> dict:
    # 二道保险：即便 env 隔离失效，也显式钉死本轮 run 的落点，绝不让 prepare_run 去 env 里
    # 捞一个活跃 run 来写（见 _isolate_rvf_runtime_environment 的事故记录）。
    run_dir = tmp_path / "isolated-run"
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "RVF SOW Test")
    _git(repo, "config", "user.email", "rvf-sow@example.test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "tracked.txt").write_text("changed by the agent\n", encoding="utf-8")

    scope_of_work = tmp_path / "startup-scope-of-work.md"
    scope_of_work.write_text(
        _stub_text(rvf_dispatch_prompts.AGENTIC_INITIATION_FLOW, candidate_paths=["tracked.txt"]),
        encoding="utf-8",
    )
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
        allow_missing_session_context=False,
        rvf_run_id="rvf-scope-of-work-contract-test",
        rvf_run_dir=run_dir,
    )


def test_rerender_entry_pushes_the_edited_scope_of_work_into_the_frozen_packet(
    tmp_path: Path,
) -> None:
    """SOW-3：改盘上的 scope-of-work 后，受支持的重渲入口把它渲进 packet 并归档旧工件。"""
    result = _prepare_one_run(tmp_path)
    artifacts_dir = Path(result["artifacts_dir"])
    packet_path = Path(result["review_packet"])
    scope_of_work_path = artifacts_dir / "scope-of-work.md"

    # 冻结的 renderer 命令必须被留下来——没有它就没法「复用同一个 renderer」重渲。
    build_command_path = (
        artifacts_dir / "inputs" / prepare_review_run.REVIEW_PACKET_BUILD_COMMAND_FILENAME
    )
    assert build_command_path.is_file(), sorted(p.name for p in (artifacts_dir / "inputs").iterdir())

    original_packet = packet_path.read_text(encoding="utf-8")
    assert rvf_dispatch_prompts.SCOPE_OF_WORK_STUB_MARKER in original_packet, (
        "前提：start 确实把 stub 内联冻进了 packet（这正是 D2/D3 的成因）"
    )

    # agent 把 stub 改成真实内容。
    scope_of_work_path.write_text(
        "# Scope of Work: agentic RVF initiation\n\n"
        "本轮把 tracked.txt 从 base 改成了 agent 写的内容，审查时请盯 UNIQUEMARKERFORTEST。\n",
        encoding="utf-8",
    )
    # 光改盘上的文件不会进 packet——这正是需要显式重渲入口的原因。
    assert "UNIQUEMARKERFORTEST" not in packet_path.read_text(encoding="utf-8")

    rerender = prepare_review_run.rerender_scope_of_work_into_review_packet(artifacts_dir)

    assert rerender["round"] == 1
    rerendered_packet = packet_path.read_text(encoding="utf-8")
    assert "UNIQUEMARKERFORTEST" in rerendered_packet
    assert rvf_dispatch_prompts.SCOPE_OF_WORK_STUB_MARKER not in rerendered_packet
    # canonical 与 inputs/ 不得分裂——真实 run 里正是它们各说各话，审计消费者看到两个事实源。
    assert (artifacts_dir / "inputs" / "review-packet.md").read_text(encoding="utf-8") == rerendered_packet
    assert "UNIQUEMARKERFORTEST" in (artifacts_dir / "inputs" / "scope-of-work.md").read_text(
        encoding="utf-8"
    )
    # 「冻结输入可审计」不能因为重渲而破功：旧工件整组归档。
    archive_dir = Path(rerender["archive_dir"])
    assert (archive_dir / "review-packet.md").read_text(encoding="utf-8") == original_packet
    assert (archive_dir / "inputs" / "scope-of-work.md").is_file()

    second = prepare_review_run.rerender_scope_of_work_into_review_packet(artifacts_dir)
    assert second["round"] == 2, "重渲轮次必须递增，绝不覆盖上一轮归档"


def test_rerender_refuses_when_the_frozen_command_is_missing_or_points_elsewhere(
    tmp_path: Path,
) -> None:
    """fail-closed：命令缺失 / --output 指向别处一律抛，绝不猜一条命令重跑（会覆盖别轮工件）。"""
    result = _prepare_one_run(tmp_path)
    artifacts_dir = Path(result["artifacts_dir"])
    command_path = (
        artifacts_dir / "inputs" / prepare_review_run.REVIEW_PACKET_BUILD_COMMAND_FILENAME
    )

    recorded = json.loads(command_path.read_text(encoding="utf-8"))
    hijacked = list(recorded["args"])
    hijacked[hijacked.index("--output") + 1] = str(tmp_path / "somewhere-else.md")
    command_path.write_text(
        json.dumps({**recorded, "args": hijacked}, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="--output"):
        prepare_review_run.rerender_scope_of_work_into_review_packet(artifacts_dir)

    command_path.unlink()
    with pytest.raises(ValueError, match="缺少冻结的 packet 渲染命令"):
        prepare_review_run.rerender_scope_of_work_into_review_packet(artifacts_dir)


def test_dispatch_gate_judges_the_frozen_packet_not_the_mutable_file_on_disk(
    tmp_path: Path,
) -> None:
    """SOW-2 的真正判据：gate 校验 **reviewer 会读到的那一份**，即冻进 packet 的 Session Context。

    只读盘上的 ``scope-of-work.md`` 拦不住这条 gate 本来要拦的失败模式：agent 覆盖了盘上文件、
    删掉了标记，但没跑 rerender 就 dispatch —— gate 放行，packet 里冻的仍是 stub，整轮空审。
    另一半是「声明过的路径读不到」：它与「压根没声明路径」语义不同，不该共用放行分支，否则
    「把文件删掉」就是绕过这条 gate 最省事的办法。
    """
    result = _prepare_one_run(tmp_path)
    artifacts_dir = Path(result["artifacts_dir"])
    scope_of_work_path = artifacts_dir / "scope-of-work.md"
    artifacts = {
        "scope_of_work": str(scope_of_work_path),
        "review_packet": str(Path(result["review_packet"])),
    }

    gate = rvf_agent_decision_cli._scope_of_work_stub_gate(artifacts)
    assert gate is not None
    assert gate["reason"] == rvf_agent_decision_cli.SCOPE_OF_WORK_GATE_REASON_STILL_STUB, gate

    # agent 写了真实内容、删掉了标记，但**没重渲**。
    scope_of_work_path.write_text(
        "# Scope of Work\n\n本轮唯一改动是 tracked.txt，审查请盯 UNIQUEMARKERFORTEST。\n",
        encoding="utf-8",
    )
    gate = rvf_agent_decision_cli._scope_of_work_stub_gate(artifacts)
    assert gate is not None, "只看盘上文件的 gate 会在这里放行 —— 那正是 RVF-001"
    assert gate["reason"] == rvf_agent_decision_cli.SCOPE_OF_WORK_GATE_REASON_PACKET_STALE, gate

    prepare_review_run.rerender_scope_of_work_into_review_packet(artifacts_dir)
    assert rvf_agent_decision_cli._scope_of_work_stub_gate(artifacts) is None, "重渲之后才该放行"

    # 声明过却读不到 / 内容为空 ⇒ 拒派；压根没声明 ⇒ 放行（manual-all-uncommitted 的合法路径）。
    scope_of_work_path.unlink()
    gate = rvf_agent_decision_cli._scope_of_work_stub_gate(artifacts)
    assert gate is not None and gate["reason"] == (
        rvf_agent_decision_cli.SCOPE_OF_WORK_GATE_REASON_UNREADABLE
    ), gate
    scope_of_work_path.write_text("   \n", encoding="utf-8")
    gate = rvf_agent_decision_cli._scope_of_work_stub_gate(artifacts)
    assert gate is not None and gate["reason"] == (
        rvf_agent_decision_cli.SCOPE_OF_WORK_GATE_REASON_EMPTY
    ), gate
    assert rvf_agent_decision_cli._scope_of_work_stub_gate({}) is None


def test_dispatch_gate_is_not_fooled_by_the_marker_appearing_in_the_packet_diff(
    tmp_path: Path,
) -> None:
    """gate 只认 packet 里 ``## Session Context`` 那一段，不做全文 substring。

    packet 还内联着整个 diff 与全部 untracked 文件；本仓自审时那些内容本身就含 stub 标记字面量，
    全文匹配会把一份已经重渲好的 packet 判成 stub。
    """
    result = _prepare_one_run(tmp_path)
    artifacts_dir = Path(result["artifacts_dir"])
    scope_of_work_path = artifacts_dir / "scope-of-work.md"
    packet_path = Path(result["review_packet"])
    artifacts = {"scope_of_work": str(scope_of_work_path), "review_packet": str(packet_path)}

    scope_of_work_path.write_text(
        "# Scope of Work\n\n本轮改的正是 stub 判定方式，审查请盯它。\n", encoding="utf-8"
    )
    prepare_review_run.rerender_scope_of_work_into_review_packet(artifacts_dir)
    assert rvf_agent_decision_cli._scope_of_work_stub_gate(artifacts) is None

    # 模拟「被审代码里出现了该字面量」：追加一段带独立标记行的证据到 packet 尾部。
    packet_path.write_text(
        packet_path.read_text(encoding="utf-8")
        + f"\n## Untracked file: notes.md\n\n{rvf_dispatch_prompts.SCOPE_OF_WORK_STUB_MARKER}\n",
        encoding="utf-8",
    )
    assert rvf_agent_decision_cli._scope_of_work_stub_gate(artifacts) is None, (
        "diff/untracked 证据里的标记字面量不是本轮 scope-of-work 的状态"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
