"""原生用户授权只作用于同一肯定条款中的问题、动作与目标。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_user_delegated_elevated_issue_disposition import (
    append_native_issue_action_request,
    canonical_bytes,
    delegated_issue_fixture,
    delegation,
    run_delegation,
    run_writer,
)


def replace_initial_native_authorization(request_file: Path, message_text: str, *, proposal: str | None = None) -> None:
    request = json.loads(request_file.read_bytes())
    authorization = request["authorization"]
    authorization["message_text"] = message_text
    native_path = Path(authorization["transcript_path"])
    records = [json.loads(line) for line in native_path.read_text().splitlines()]
    records[3]["payload"]["content"][0]["text"] = message_text
    records[4]["payload"]["item"]["content"][0]["text"] = message_text
    if proposal is not None:
        authorization["proposal_context_quote"] = proposal
        records[1]["payload"]["last_agent_message"] = proposal
        target_path = Path(request["target_receipt_path"])
        target = json.loads(target_path.read_bytes())
        target["task"]["prompt"] = proposal
        target_path.write_bytes(canonical_bytes(target))
    native_path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n")
    request_file.write_bytes(canonical_bytes(request))


@pytest.mark.parametrize("message_text", [
    "不要在当前的审查中将 RVF-002 委派给 target-task。",
    "Do not under any circumstances delegate RVF-002 to target-task.",
    "将 RVF-001 委派给 target-task；RVF-002 留在本任务中处理。",
    "将 RVF-002 委派给 another-task；target-task 只处理日志。",
    "将 RVF-001 委派给 target-task，并将 RVF-002 委派给 another-task。",
    "将 RVF-001 委派给 target-task 且 RVF-002 留在本任务处理。",
])
def test_native_direct_delegation_rejects_negation_and_cross_action_identifier_borrowing(tmp_path: Path, message_text: str) -> None:
    repo, run_dir, request = delegated_issue_fixture(tmp_path, mode="direct_delegation")
    replace_initial_native_authorization(request, message_text)
    result = run_delegation(tmp_path, repo, run_dir, "record", request)
    assert result.returncode in {11, 12}, result.stdout
    assert not (run_dir / delegation.DELEGATION_DIRECTORY / "ledger.json").exists()


@pytest.mark.parametrize("message_text", ["好像还不行", "好难，先观察一下", "可以吗？", "Yes, maybe later.", "可以。不同意这个方案。"])
def test_native_proposal_requires_unambiguous_affirmative_approval(tmp_path: Path, message_text: str) -> None:
    repo, run_dir, request = delegated_issue_fixture(tmp_path)
    replace_initial_native_authorization(request, message_text)
    result = run_delegation(tmp_path, repo, run_dir, "record", request)
    assert result.returncode == 11, result.stdout
    assert not (run_dir / delegation.DELEGATION_DIRECTORY / "ledger.json").exists()


@pytest.mark.parametrize("message_text,proposal_body", [
    ("可以。", "针对 RVF-002，建议先看这个任务的日志；采用独立副本和受控重载来可靠传播设置。"),
    ("可以。", "不要将 RVF-002 委派给其他任务；采用独立副本和受控重载来可靠传播设置。"),
    ("可以，请创建一个新 task 解决这个问题。", "不要将 RVF-002 委派给其他任务；采用独立副本和受控重载来可靠传播设置。"),
    ("可以。", "建议将 RVF-001 委派给独立任务；RVF-002 继续在本任务处理；采用独立副本和受控重载来可靠传播设置。"),
    ("可以，请创建一个新 task 解决这个问题。", "建议将 RVF-001 委派给独立任务；RVF-002 继续在本任务处理；采用独立副本和受控重载来可靠传播设置。"),
])
def test_native_approval_does_not_authorize_an_unrelated_or_negative_proposal(tmp_path: Path, message_text: str, proposal_body: str) -> None:
    repo, run_dir, request = delegated_issue_fixture(tmp_path)
    proposal = "针对 run rvf-candidate-evidence-test，" + proposal_body
    replace_initial_native_authorization(request, message_text, proposal=proposal)
    result = run_delegation(tmp_path, repo, run_dir, "record", request)
    assert result.returncode == 11, result.stdout
    assert not (run_dir / delegation.DELEGATION_DIRECTORY / "ledger.json").exists()


@pytest.mark.parametrize("message_text", [
    "请把实验分支撤销，不要动 RVF-002。",
    "请撤销备份。保留 RVF-002 的委派。",
    "不要在任何情况下撤销 RVF-002 的委派。",
    "Do not under any circumstances revoke the delegation of RVF-002.",
    "撤销 RVF-001 的委派；RVF-002 保留。",
    "请撤销备份并保留 RVF-002 的委派。",
    "撤销备份以保留 RVF-002 的委派。",
    "撤销 RVF-001 的委派并保留 RVF-002 的委派。",
    "请取消 RVF-002 的验证，但保留它的委派。",
    "请撤销 RVF-002 的备份。保留其委派。",
    "请取消 RVF-002 的委派验证，但保留委派。",
    "请把 RVF-002 的委派备份撤销。",
    "Cancel verification of RVF-002, but keep its delegation.",
    "Revoke the backup for RVF-002; keep its delegation.",
])
def test_native_revocation_cannot_borrow_an_issue_from_another_action(tmp_path: Path, message_text: str) -> None:
    repo, run_dir, original = delegated_issue_fixture(tmp_path)
    assert run_delegation(tmp_path, repo, run_dir, "record", original).returncode == 0
    request = append_native_issue_action_request(tmp_path, original, action="revoke", message_text=message_text, decision_id="unapproved-revocation")
    result = run_delegation(tmp_path, repo, run_dir, "record", request)
    assert result.returncode == 11, result.stdout
    assert len(delegation.read_ledger(run_dir)[1]) == 1
    assert len(delegation.accepted_delegations(repo, run_dir)) == 1
    assert run_writer(repo, run_dir).returncode == 0


@pytest.mark.parametrize("message_text", [
    "确认不是误报后撤销 RVF-002 的委派。",
    "不要继续委派，撤销 RVF-002。",
    "不要撤销 RVF-001 的委派；撤销 RVF-002 的委派。",
    "撤销 RVF-002。",
    "取消 RVF-002 的委派。",
    "请把 RVF-002 的委派撤销。",
    "Revoke the delegation of RVF-002.",
    "Cancel the delegation of RVF-002.",
    "Revoke RVF-002's delegation.",
])
def test_native_affirmative_revocation_preserves_unrelated_negative_actions(tmp_path: Path, message_text: str) -> None:
    repo, run_dir, original = delegated_issue_fixture(tmp_path)
    assert run_delegation(tmp_path, repo, run_dir, "record", original).returncode == 0
    request = append_native_issue_action_request(tmp_path, original, action="revoke", message_text=message_text, decision_id="approved-revocation")
    result = run_delegation(tmp_path, repo, run_dir, "record", request)
    assert result.returncode == 0, result.stdout
    assert len(delegation.read_ledger(run_dir)[1]) == 2
    assert delegation.accepted_delegations(repo, run_dir) == []


@pytest.mark.parametrize("message_text", [
    "将 RVF-002 委派给 target-task。",
    "不要将 RVF-001 委派给 other-task；将 RVF-002 委派给 target-task。",
    "不要撤销 RVF-001；将 RVF-002 委派给 target-task。",
    "不得将 RVF-001 委派给 other-task；将 RVF-002 委派给 target-task。",
    "不要将 RVF-002 委派给 another-task；将 RVF-002 委派给 target-task。",
])
def test_native_direct_delegation_preserves_unrelated_negative_actions(tmp_path: Path, message_text: str) -> None:
    repo, run_dir, request = delegated_issue_fixture(tmp_path, mode="direct_delegation")
    replace_initial_native_authorization(request, message_text)
    result = run_delegation(tmp_path, repo, run_dir, "record", request)
    assert result.returncode == 0, result.stdout
    assert len(delegation.accepted_delegations(repo, run_dir)) == 1
    assert run_writer(repo, run_dir).returncode == 0


@pytest.mark.parametrize("message_text", [
    "可以。", "好的。", "同意这个方案。", "可以，同意这个方案，请创建一个新 task 解决这个问题。",
    "可以，不要撤销 RVF-002 的委派。", "可以，不要将 RVF-002 委派给 another-task。",
    "可以，另外日志好像还不完整，请先检查日志。", "可以，后续同步是否需要等它完成？",
])
def test_native_explicit_approval_keeps_the_proposal_and_target_receipt_chain(tmp_path: Path, message_text: str) -> None:
    repo, run_dir, request = delegated_issue_fixture(tmp_path)
    replace_initial_native_authorization(request, message_text)
    result = run_delegation(tmp_path, repo, run_dir, "record", request)
    assert result.returncode == 0, result.stdout
    assert len(delegation.accepted_delegations(repo, run_dir)) == 1
    assert run_writer(repo, run_dir).returncode == 0


def test_native_explicit_task_creation_accepts_the_source_user_approval_of_a_solution_proposal(tmp_path: Path) -> None:
    repo, run_dir, request_file = delegated_issue_fixture(tmp_path)
    # 原样保留来源用户消息及提案的相关段落；run 身份仅由隔离 fixture 提供。
    message_text = ("可以，那请你规划创建 handoff，并创建一个新 task 解决这个问题.\n\n\n"
                    "那么现在你将你的工作 think back to local main 能够进行了吗？还是说一定要等它完成以后才能继续？")
    proposal = ("针对 run rvf-candidate-evidence-test：\n"
                "**第三，广播不能重新制造两个写入者。**  \n"
                "协调器应把增量变更交给拥有副本的原进程，由它写入并重载；协调器直接覆盖活副本，会重新出现 RVF-002 那种争写窗口。"
                "某个会话暂时不可达时，应持久记录未应用的变化，在恢复后接续，不能把“广播已发送”当成“全部已生效”。\n\n"
                "**因此，我会选择“独立副本＋增量合并＋受控重载＋阶段版本绑定”作为方向。** "
                "它既保留你的隔离收益，也避免一次全局广播同时改变大量在途 RVF 工作的执行环境。本轮只做了隔离核验与方案分析，没有修改真实会话或实施传播机制。")
    replace_initial_native_authorization(request_file, message_text, proposal=proposal)
    request = json.loads(request_file.read_bytes())
    request["authorization"]["target_binding_quote"] = "增量合并＋受控重载＋阶段版本绑定"
    request_file.write_bytes(canonical_bytes(request))
    result = run_delegation(tmp_path, repo, run_dir, "record", request_file)
    assert result.returncode == 0, result.stdout
    assert len(delegation.accepted_delegations(repo, run_dir)) == 1
    assert run_writer(repo, run_dir).returncode == 0


@pytest.mark.parametrize("message_text", [
    "可以，但 RVF-002 继续留在本任务处理。",
    "可以，将 RVF-002 委派给 another-task。",
    "可以，先别创建新任务。",
])
def test_native_approval_cannot_override_the_user_action_scope_or_explicit_target(tmp_path: Path, message_text: str) -> None:
    repo, run_dir, request = delegated_issue_fixture(tmp_path)
    replace_initial_native_authorization(request, message_text)
    result = run_delegation(tmp_path, repo, run_dir, "record", request)
    assert result.returncode == 11, result.stdout
    assert not (run_dir / delegation.DELEGATION_DIRECTORY / "ledger.json").exists()


def test_native_brief_approval_cannot_change_an_explicit_proposal_target(tmp_path: Path) -> None:
    repo, run_dir, request = delegated_issue_fixture(tmp_path)
    proposal = ("针对 run rvf-candidate-evidence-test，建议将 RVF-002 委派给 another-task；"
                "采用独立副本和受控重载来可靠传播设置。")
    replace_initial_native_authorization(request, "可以。", proposal=proposal)
    result = run_delegation(tmp_path, repo, run_dir, "record", request)
    assert result.returncode == 11, result.stdout
    assert not (run_dir / delegation.DELEGATION_DIRECTORY / "ledger.json").exists()
