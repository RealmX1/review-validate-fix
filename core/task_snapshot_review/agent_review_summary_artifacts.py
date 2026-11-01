"""程序生成匿名审阅材料与简短后台事件，完整脚本只留在机器原件。"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .persistent_artifacts import exclusive_lock, read_json, write_bytes, write_json
from .execution_lifecycle import ensure_execution_not_revoked, execution_is_revoked
from .main_agent_context_supplements import registered_main_agent_context_supplements, pending_main_agent_context_supplements

_INLINE_VALIDATION_SCRIPT_FLAGS = {"-c", "-e", "--command"}


def material_sha256(material: object) -> str:
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _redact_validation_argument(text: str, argument: str) -> str:
    placeholder = "（完整验证脚本见机器原件）"
    if len(argument) > 16:
        return text.replace(argument, placeholder)
    return re.sub(rf"(?<![A-Za-z0-9_]){re.escape(argument)}(?![A-Za-z0-9_])", placeholder, text)


def concise_agent_review_evidence_excerpt(value: object, validation_commands: list[list[str]]) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    for command in validation_commands:
        for index, argument in enumerate(command):
            if not argument:
                continue
            inline_script = index > 0 and command[index - 1] in _INLINE_VALIDATION_SCRIPT_FLAGS
            if len(argument) > 160 or inline_script or "\n" in argument:
                text = _redact_validation_argument(text, argument)
    return text if len(text) <= 1000 else text[:1000] + "…（完整依据见角色原件）"


def notice_consumption_receipt_file(run_directory: Path, notice_id: str) -> Path:
    return run_directory / "agent-review-notice-consumption-receipts" / f"{material_sha256(notice_id)}.json"


def agent_review_notice_was_consumed(run_directory: Path, notice: dict) -> bool:
    path = notice_consumption_receipt_file(run_directory, notice["noticeId"])
    if not path.exists():
        return False
    receipt = read_json(path)
    if (receipt.get("run_directory") != str(run_directory) or receipt.get("notice") != notice
            or receipt.get("notice_sha256") != material_sha256(notice)):
        raise RuntimeError("主会话消费回执缺少精确 run、通知或摘要证据，保留原件并补证")
    return True


def _current_final_notice_can_be_consumed(run_directory: Path, notice: dict) -> None:
    current = read_json(run_directory / "business-result.json")
    if current.get("agentContinuationNotice") != notice:
        raise ValueError("最终审阅尚未覆盖全部已登记补充或当前结果修订")
    # 需判断的当前修订可以在补充仍未覆盖时登记消费；成功修订则必须已经覆盖。
    if pending_main_agent_context_supplements(run_directory) and current.get("disposition") != "agent_judgment_required":
        raise ValueError("最终审阅尚未覆盖全部已登记补充或当前结果修订")


def _drop_notice_from_manifest(run_directory: Path, notice_id: str) -> None:
    manifest_file = run_directory / "agent-continuation-notices.json"
    if not manifest_file.exists():
        return
    manifest = read_json(manifest_file)
    remaining = [item for item in manifest.get("notices", []) if item.get("noticeId") != notice_id]
    if remaining != manifest.get("notices"):
        write_json(manifest_file, {"notices": remaining})


def _without_consumed_notices(run_directory: Path, notices: list[dict]) -> list[dict]:
    return [notice for notice in notices if not agent_review_notice_was_consumed(run_directory, notice)]


def acknowledge_agent_review_notice(run_directory: Path, notice_id: str) -> dict:
    run_directory = run_directory.expanduser().resolve()
    with exclusive_lock(run_directory / "main-agent-context-supplements.lock"), exclusive_lock(run_directory / "execution-revocation.lock"):
        ensure_execution_not_revoked(run_directory)
        manifest_file = run_directory / "agent-continuation-notices.json"
        manifest = read_json(manifest_file)
        receipt_file = notice_consumption_receipt_file(run_directory, notice_id)
        if receipt_file.exists():
            receipt = read_json(receipt_file)
            if receipt.get("run_directory") != str(run_directory) or receipt.get("notice", {}).get("noticeId") != notice_id:
                raise ValueError("主会话消费回执与精确 run 或通知身份不匹配")
            agent_review_notice_was_consumed(run_directory, receipt["notice"])
            _drop_notice_from_manifest(run_directory, notice_id)
            if receipt["notice"]["stage"] == "final_agent_review":
                current = read_json(run_directory / "business-result.json")
                if current.get("agentContinuationNotice") != receipt["notice"]:
                    raise ValueError("历史消费事实保留，当前最终结果已失效，先覆盖最新补充并审阅新修订")
                if pending_main_agent_context_supplements(run_directory) and current.get("disposition") != "agent_judgment_required":
                    raise ValueError("最终审阅尚未覆盖全部已登记补充或当前结果修订")
            return receipt
        notice = next((n for n in manifest["notices"] if n["noticeId"] == notice_id), None)
        if notice is None:
            raise ValueError("该通知已失效，先读取最新摘要并覆盖已登记补充")
        if notice["stage"] == "final_agent_review":
            _current_final_notice_can_be_consumed(run_directory, notice)
        receipt = {"run_directory": str(run_directory), "notice": notice,
                   "source": "main_agent_explicit_notice_acknowledgement", "notice_sha256": material_sha256(notice)}
        write_json(receipt_file, receipt)
        write_json(manifest_file, {"notices": [n for n in manifest["notices"] if n["noticeId"] != notice_id]})
        return receipt


def publish_reviewer_findings_for_parallel_review(run_directory: Path, record: dict, findings: list[dict]) -> None:
    with exclusive_lock(run_directory / "main-agent-context-supplements.lock"):
        ensure_execution_not_revoked(run_directory)
        snapshot = {"run_directory": str(run_directory), "candidate_commit": record["candidate_commit"],
                    "findings_sha256": material_sha256(findings), "findings": findings}
        path = run_directory / "reviewer-findings.json"
        if path.exists() and read_json(path) != snapshot:
            raise ValueError("匿名 reviewer findings 原件已改变，保留历史并恢复冻结材料")
        if not path.exists():
            write_json(path, snapshot)
        summary = (f"# RVF 匿名发现\n\nrun：{run_directory}\n\n候选：{record['candidate_commit']}\n\n"
                   f"findings_sha256：{snapshot['findings_sha256']}\n\n"
                   "已授权的 Validate-Fixer 与主会话并行验真，不等待主会话。主会话只补充独有事实；补充不改变需求、范围或授权。\n\n")
        plan_file = run_directory / "execution-plan.json"
        commands = read_json(plan_file).get("validation_commands", []) if plan_file.exists() else []
        for finding in findings:
            summary += f"- {finding['finding_id']} · {finding.get('path', '未指定路径')}:{finding.get('line', '?')} · {concise_agent_review_evidence_excerpt(finding['message'], commands)}\n"
            if finding.get("evidence"):
                summary += f"  证据：{concise_agent_review_evidence_excerpt(finding['evidence'], commands)}\n"
        summary += (f"\n补充入口：`rvf submit-review-context --run-dir {run_directory} --supplement-id <唯一ID> "
                    f"--candidate-commit {record['candidate_commit']} --findings-sha256 {snapshot['findings_sha256']} --context-file <持久事实文件>`\n")
        reviewer_notice_id = f"rvf-{record['run_id']}-reviewers-{snapshot['findings_sha256']}"
        summary += f"\n阅读本阶段后登记消费：`rvf acknowledge-review-notice --run-dir {run_directory} --notice-id {reviewer_notice_id}`\n"
        write_bytes(run_directory / "reviewer-findings-summary.md", summary.encode())
        manifest_file = run_directory / "agent-continuation-notices.json"
        manifest = read_json(manifest_file) if manifest_file.exists() else {"notices": []}
        notices = _without_consumed_notices(run_directory, manifest["notices"])
        if not any(item["stage"] == "reviewer_findings_available" for item in notices):
            notice = {"noticeId": f"rvf-{record['run_id']}-reviewers-{snapshot['findings_sha256']}", "stage": "reviewer_findings_available",
                      "text": (f"【RVF 后台事件】独立评审 {record['reviewer_count']}/{record['reviewer_count']} 已完成，匿名发现 {len(findings)} 项。\n"
                               f"已授权的 Validate-Fixer 独立推进；主会话可补充独有事实，无需等待。\n审阅摘要：{run_directory / 'reviewer-findings-summary.md'}\n按本任务已有授权继续。")}
            if not agent_review_notice_was_consumed(run_directory, notice):
                notices.append(notice)
        if notices != manifest["notices"]:
            write_json(manifest_file, {"notices": notices})


def publish_agent_review_summary(run_directory: Path, record: dict, result: dict) -> dict | None:
    """调用方持有补充登记锁；结果资格与已登记补充在同一边界确定。"""
    ensure_execution_not_revoked(run_directory)
    validation = result["closeout"]["validation"]
    validation = validation if isinstance(validation, dict) else {}
    completed = int(validation.get("completed_commands", 0))
    total = len(validation.get("commands", []))
    dispositions = []
    responses = []
    result_files = sorted((run_directory / "fixer").glob("attempt-*/result.json"))
    if result_files:
        latest = read_json(result_files[-1])
        dispositions = latest.get("dispositions", [])
        responses = latest.get("context_supplement_responses", [])
    supplements = registered_main_agent_context_supplements(run_directory)
    pending_supplements = pending_main_agent_context_supplements(run_directory, supplements)
    if execution_is_revoked(run_directory):
        return None
    findings_file = run_directory / "findings.json"
    findings = {item["finding_id"]: item for item in read_json(findings_file).get("findings", [])} if findings_file.exists() else {}
    disposition_ids = {item["finding_id"] for item in dispositions}
    undispositioned = [findings[item_id] for item_id in sorted(findings) if item_id not in disposition_ids]
    material = {"disposition": result["disposition"], "tested_tree": validation.get("tested_tree"),
                "requires_current_result_notice_match": True,
                "candidate_commit": record.get("validated_candidate_commit", record["candidate_commit"]),
                "dispositions": dispositions, "context_supplement_responses": responses, "supplements": supplements,
                "validation_status": validation.get("status", "unknown"), "completed_commands": completed, "total_commands": total,
                "judgment_status": result["status"] if result["disposition"] == "agent_judgment_required" else None,
                "pending_supplement_ids": [item["supplement_id"] for item in pending_supplements],
                **({"undispositioned_finding_ids": [item["finding_id"] for item in undispositioned]} if undispositioned else {})}
    revision = material_sha256(material)
    revision_directory = run_directory / "agent-review-result-revisions" / revision
    summary_file = revision_directory / "agent-review-summary.md"
    if execution_is_revoked(run_directory):
        return None
    if not summary_file.exists():
        summary = (f"# RVF 主会话审阅摘要\n\nrun：{run_directory}\n\n结果修订：{revision}\n\n"
                   f"业务处置：{result['disposition']}；状态：{result['status']}\n\n"
                   f"评审：{record['reviewer_count']}/{record['reviewer_count']}；发现：{record.get('finding_count', 0)}\n\n"
                   f"被测完整树：{validation.get('tested_tree', 'unknown')}\n\n"
                   f"验证：{validation.get('status', 'unknown')}；完成 {completed}/{total}\n\n"
                   f"补丁：{run_directory / 'fixer.patch'}\n\n")
        for item in dispositions:
            finding = findings.get(item["finding_id"], {})
            summary += (f"- {item['finding_id']} · {item['status']} · {finding.get('path', '未指定路径')}:{finding.get('line', '?')}\n"
                        f"  问题：{concise_agent_review_evidence_excerpt(finding.get('message', '见匿名发现'), validation.get('commands', []))}\n"
                        f"  处置及修复依据：{concise_agent_review_evidence_excerpt(item['reason'], validation.get('commands', []))}\n")
            if item.get("elevation_impact"):
                summary += f"  重大处置影响：{item['elevation_impact']}\n"
        for finding in undispositioned:
            summary += (f"- {finding['finding_id']} · 待处理 · {finding.get('path', '未指定路径')}:{finding.get('line', '?')}\n"
                        f"  问题：{concise_agent_review_evidence_excerpt(finding.get('message', '见匿名发现'), validation.get('commands', []))}\n"
                        "  处置及修复依据：预算耗尽前尚未获得独立处置。\n")
        summary += "\n主会话事实补充：\n"
        for supplement in supplements:
            response = next((item for item in responses if item["supplement_id"] == supplement["supplement_id"]), {})
            summary += f"- {supplement['supplement_id']} · {response.get('status', 'unresolved')} · {concise_agent_review_evidence_excerpt(response.get('reason', '尚未独立验真'), validation.get('commands', []))}\n"
        unresolved = [item for item in dispositions if item["status"] == "unresolved"]
        summary += (f"\n主会话需判断：{'存在未解决项、重大处置或候选冲突，请先核对以上逐项依据。' if result['disposition'] == 'agent_judgment_required' or unresolved or undispositioned else '最终核对实际补丁、需求及已有授权。'}\n\n"
                    f"机器原件按需读取：{run_directory / 'validation.json'}（完整命令、环境及日志指针）；无需整份读取 business-result.json。\n")
        summary += f"\n最终审阅后登记本修订消费：`rvf acknowledge-review-notice --run-dir {run_directory} --notice-id rvf-{record['run_id']}-final-{revision}`\n"
        if result_files:
            summary += f"\n逐项完整处置原件（按需）：{result_files[-1]}\n"
        write_json(revision_directory / "result.json", result)
        write_bytes(summary_file, summary.encode())
    write_bytes(run_directory / "agent-review-summary.md", summary_file.read_bytes())
    fixed_count = sum(item["status"] == "fixed" for item in dispositions)
    unresolved_count = sum(item["status"] == "unresolved" for item in dispositions) + len(pending_supplements) + len(undispositioned)
    elevated_count = sum(item["status"] == "elevated" for item in dispositions)
    requires_judgment = result["disposition"] == "agent_judgment_required"
    headline = "需主会话判断，尚未达到正常完成条件。" if requires_judgment else "验真修复及最终验证已完成，待主会话最终审阅。"
    if requires_judgment:
        headline += f"状态：{result['status']}。"
    notice = {"noticeId": f"rvf-{record['run_id']}-final-{revision}", "stage": "final_agent_review",
              "requiresCurrentResultNoticeMatch": True,
              "text": (f"【RVF 后台事件】{headline}\n评审 {record['reviewer_count']}/{record['reviewer_count']}；{fixed_count} 项已修复；"
                       f"未解决 {unresolved_count} 项，升级处置 {elevated_count} 项；"
                       f"验证 {validation.get('status', 'unknown')}，{completed}/{total}。\n"
                       f"审阅摘要：{summary_file}\n按本任务已有授权继续。")}
    if execution_is_revoked(run_directory):
        return None
    manifest_file = run_directory / "agent-continuation-notices.json"
    manifest = read_json(manifest_file) if manifest_file.exists() else {"notices": []}
    notices = _without_consumed_notices(run_directory, [item for item in manifest["notices"] if item["stage"] != "final_agent_review"])
    if not agent_review_notice_was_consumed(run_directory, notice):
        notices.append(notice)
    if execution_is_revoked(run_directory):
        return None
    write_json(manifest_file, {"notices": notices})
    if execution_is_revoked(run_directory):
        write_json(manifest_file, {"notices": [item for item in notices if item.get("noticeId") != notice["noticeId"]]})
        return None
    return notice
