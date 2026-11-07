#!/usr/bin/env python3
"""RVF：固定任务快照、接续评审及按需问题收集。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import uuid
from pathlib import Path
import _rvf_pyroot
from core.rvf_user_local_data_paths import resolved_rvf_user_local_data_paths
from core.task_snapshot_review.github_issue_observations import collect_report, flush_pending_reports, list_repository_issues, schedule_pending_issue_delivery
from core.task_snapshot_review.persistent_artifacts import git_text, read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot, reviewer_allocation_for_new_run
from core.task_snapshot_review.task_review_decision_facts import collect_task_review_decision_facts
from core.task_snapshot_review.workflow import resume_review, load_reviewer_configuration
from core.task_snapshot_review.execution_lifecycle import revoke_execution
from core.task_snapshot_review.main_agent_context_supplements import register_main_agent_context_supplement
from core.task_snapshot_review.agent_review_summary_artifacts import acknowledge_agent_review_notice
from core.task_snapshot_review.trajectory_analysis import analyze_runs
from adapters.cline_kanban.task_review_starting_point import (
    attach_task_review_run, record_task_review_start, resolve_task_review_starting_point,
)


def main(argv=None) -> int:
    active_run_directory = None
    def interrupted(signum,frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review-task-snapshot", help="固定显式任务范围并运行独立评审")
    review.add_argument("--repo", type=Path, default=Path.cwd())
    baseline = review.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--base-ref", help="解析一次的本地基线 ref 或 commit")
    baseline.add_argument("--base-from-kanban-task", help="从指定 Kanban task 的已核实证据取得固定起点")
    review.add_argument("--incremental-after-run", type=Path, help="明确选择同任务已完成 run 作为增量起点；范围扩大仍从原起点覆盖")
    review.add_argument("--scope", action="append", required=True, help="可重复的仓内相对路径；全仓范围显式使用 .")
    review.add_argument("--requirements-file", type=Path, required=True)
    review.add_argument("--run-directory", type=Path)
    review.add_argument("--prepare-only", action="store_true")
    review.add_argument("--reviewer-config", type=Path, action="append")
    review.add_argument("--reviewer-count", type=int, default=2, help="reviewer 总数，至少 2；2026-11-02 00:00 America/New_York 前最低三席，其中一席必选外部 Codex，其余排除 Codex CLI；创建时冻结，接续沿用")
    review.add_argument("--fix", action="store_true", help="授权自动验证并修复任务关联问题")
    review.add_argument("--fixer-config", type=Path)
    review.add_argument("--validation-commands", type=Path, help="冻结最终验证 argv 数组列表 JSON")
    review.add_argument("--preparation-commands", type=Path, help="冻结隔离工作树准备 argv 数组列表 JSON")
    review.add_argument("--machine-evidence-commands", type=Path, help="冻结共享机器事实采集 argv 数组列表 JSON")
    review.add_argument("--machine-context-file", type=Path, help="冻结机器上下文 JSON")
    review.add_argument("--maximum-semantic-repair-rounds", type=int, default=3)
    review.add_argument("--kanban-task-id", help="使用 Kanban 通用所属执行托管本 run")
    review.add_argument("--kanban-project-path", type=Path)
    review.add_argument("--kanban-cli", type=Path)
    starting = commands.add_parser('record-kanban-task-review-start', help='明确保存一段实现开始前的固定起点与范围')
    starting.add_argument('--repo', type=Path, default=Path.cwd())
    starting.add_argument('--task-id', required=True)
    starting.add_argument('--base-ref', required=True)
    starting.add_argument('--scope', action='append', required=True)
    resume = commands.add_parser("resume-task-review", help="复用精确 run，接续未完成评审、修复及验证")
    resume.add_argument("--run-dir", type=Path, required=True)
    resume.add_argument("--reviewer-config", type=Path, action="append")
    resume.add_argument("--fix", action="store_true")
    resume.add_argument("--fixer-config", type=Path)
    resume.add_argument("--validation-commands", type=Path, help="最终验证 argv 数组列表 JSON")
    resume.add_argument('--context-response-file',type=Path,help='为缺失席补充通用机器证据，保持各路独立')
    observation = commands.add_parser("observe-task-review-recovery-evidence", help="只读返回精确 run 的实质恢复摘要，缺证保留 unknown")
    observation.add_argument("--run-dir", type=Path, required=True)
    machine_request = commands.add_parser("register-reviewer-machine-evidence-request", help="追加与原始缺证请求绑定的冻结命令引用，不改 reviewer 原件")
    machine_request.add_argument("--run-dir", type=Path, required=True)
    machine_request.add_argument("--source-role-result-file", type=Path, required=True)
    machine_request.add_argument("--source-role-result-sha256", required=True)
    machine_request.add_argument("--source-request-index", type=int, required=True)
    machine_request.add_argument("--request-file", type=Path, required=True)
    supplement = commands.add_parser("submit-review-context", help="独立登记主会话事实补充，不等待后台 controller，也不重跑 reviewer")
    supplement.add_argument("--run-dir", type=Path, required=True)
    supplement.add_argument("--supplement-id", required=True)
    supplement.add_argument("--candidate-commit", required=True)
    supplement.add_argument("--findings-sha256", required=True)
    supplement.add_argument("--finding-id", action="append", default=[])
    supplement.add_argument("--context-file", type=Path, required=True)
    acknowledge = commands.add_parser("acknowledge-review-notice", help="登记主会话已消费的精确阶段通知，防止后台补发")
    acknowledge.add_argument("--run-dir", type=Path, required=True)
    acknowledge.add_argument("--notice-id", required=True)
    cancel = commands.add_parser("cancel-task-review", help="先持久撤销，再取消身份匹配的所属进程")
    cancel.add_argument("--run-dir", type=Path, required=True)
    cancel.add_argument("--reason", default="明确取消 RVF 执行")
    collect = commands.add_parser("collect-issues", help="主 Agent 统一收集问题；不可达时持久保留")
    collect.add_argument("--report", type=Path)
    collect.add_argument("--repo", type=Path, default=Path.cwd())
    collect.add_argument("--flush", action="store_true")
    collect.add_argument("--list-repository", help="匹配旧人工 Issue 前读取开放与关闭问题")
    analyze = commands.add_parser("analyze", help="按需汇总原始运行事实，不产生完成门禁")
    analyze.add_argument("--run-dir", type=Path, required=True, action="append")
    arguments = parser.parse_args(argv)
    root = resolved_rvf_user_local_data_paths().user_local_data_root
    if arguments.command in ('review-task-snapshot','resume-task-review'):
        try:
            schedule_pending_issue_delivery(root,Path(__file__).resolve())
        except (OSError,ValueError,RuntimeError):
            pass
    try:
        if arguments.command == "review-task-snapshot":
            evidence = None
            decision_fact_evidence = None
            review_task_id = arguments.base_from_kanban_task or os.environ.get('KANBAN_TASK_ID') or os.environ.get('CLINE_KANBAN_TASK_ID') or arguments.kanban_task_id
            if arguments.incremental_after_run and not arguments.base_from_kanban_task:
                parser.error('--incremental-after-run 需要 --base-from-kanban-task')
            if arguments.base_from_kanban_task:
                evidence = resolve_task_review_starting_point(arguments.repo, arguments.base_from_kanban_task,
                    scope_paths=arguments.scope, incremental_after_run=arguments.incremental_after_run)
                if evidence['status'] != 'verified':
                    try:
                        facts = collect_task_review_decision_facts(Path(evidence['repo']), task_id=review_task_id,
                                                                  starting_point_evidence=evidence)
                        evidence.update(decision_facts_file=facts['artifact_path'], decision_facts_summary=facts['summary'])
                    except (OSError, ValueError, RuntimeError) as error:
                        print(f'RVF 入口事实采集诊断：{error}', file=sys.stderr)
                    print(json.dumps(evidence, ensure_ascii=False))
                    return 2
            else:
                arguments.base_ref = git_text(arguments.repo, 'rev-parse', '--verify', '--end-of-options', f'{arguments.base_ref}^{{commit}}')
                if review_task_id:
                    try:
                        resolved = resolve_task_review_starting_point(arguments.repo, review_task_id, scope_paths=arguments.scope)
                        decision_fact_evidence = resolved
                        if resolved['status'] == 'verified' and resolved['base_commit'] == arguments.base_ref:
                            evidence = resolved
                    except (OSError, ValueError, RuntimeError) as error:
                        print(f'RVF 入口关联补证诊断：{error}', file=sys.stderr)
            run = arguments.run_directory or root / "task-snapshot-reviews" / f"task-review-{uuid.uuid4().hex}"
            reviewer_count, reviewer_harness_constraints = reviewer_allocation_for_new_run(arguments.reviewer_count)
            record = create_snapshot(arguments.repo, base_ref=evidence['base_commit'] if evidence else arguments.base_ref, scope_paths=arguments.scope,
                                     requirements=arguments.requirements_file.read_bytes(), run_directory=run,
                                     reviewer_count=reviewer_count, reviewer_harness_constraints=reviewer_harness_constraints,
                                     starting_point_evidence=evidence,
                                     decision_fact_starting_point_evidence=decision_fact_evidence,
                                     execution_plan={"fix": arguments.fix,
                                         "validation_commands": json.loads(arguments.validation_commands.read_text()) if arguments.validation_commands else [],
                                         "preparation_commands": json.loads(arguments.preparation_commands.read_text()) if arguments.preparation_commands else [],
                                         "machine_evidence_commands": json.loads(arguments.machine_evidence_commands.read_text()) if arguments.machine_evidence_commands else [],
                                         "maximum_semantic_repair_rounds": arguments.maximum_semantic_repair_rounds,
                                         **({"machine_context": json.loads(arguments.machine_context_file.read_text())} if arguments.machine_context_file else {})})
            if evidence:
                attach_task_review_run(arguments.repo, review_task_id, run)
            result = {"status":record["status"],"run_directory":str(run.resolve()),"reviewer_count":record["reviewer_count"]}
            active_run_directory = run.resolve()
            reviewer_configs = [load_reviewer_configuration(path) for path in arguments.reviewer_config] if arguments.reviewer_config else None
            if reviewer_configs:
                from core.task_snapshot_review.workflow import persistent_configuration
                write_json(run / "reviewer-configurations.json", {"configurations": [persistent_configuration(config) for config in reviewer_configs]})
            fixer_config = load_reviewer_configuration(arguments.fixer_config) if arguments.fixer_config else None
            if fixer_config:
                from core.task_snapshot_review.workflow import persistent_configuration
                write_json(run / "fixer-configuration.json", persistent_configuration(fixer_config))
            if arguments.kanban_task_id and not arguments.prepare_only:
                from adapters.cline_kanban.owned_command_execution import start_managed_review
                result = start_managed_review(run.resolve(), arguments.kanban_task_id,
                    project_path=arguments.kanban_project_path, kanban_cli=arguments.kanban_cli, entrypoint=Path(__file__).resolve())
            elif not arguments.prepare_only:
                result = resume_review(run, reviewer_configs=reviewer_configs, fixer_config=fixer_config)
            result.update(decision_facts_file=str(run.resolve() / 'decision-facts.json'),
                          decision_facts_summary=read_json(run / 'decision-facts.json')['summary'])
        elif arguments.command == 'record-kanban-task-review-start':
            result = record_task_review_start(arguments.repo, arguments.task_id,
                                              base_ref=arguments.base_ref, scope_paths=arguments.scope)
        elif arguments.command == "observe-task-review-recovery-evidence":
            from core.task_snapshot_review.review_recovery_evidence_observation import observe_task_review_recovery_evidence
            result = observe_task_review_recovery_evidence(arguments.run_dir)
        elif arguments.command == "register-reviewer-machine-evidence-request":
            from core.task_snapshot_review.reviewer_machine_evidence_recovery import register_reviewer_machine_evidence_request
            result = register_reviewer_machine_evidence_request(arguments.run_dir,
                source_role_result_file=arguments.source_role_result_file, source_role_result_sha256=arguments.source_role_result_sha256,
                source_request_index=arguments.source_request_index, request=read_json(arguments.request_file))
        elif arguments.command == "resume-task-review":
            active_run_directory = arguments.run_dir.expanduser().resolve()
            binding_file = active_run_directory / "kanban-command-execution-binding.json"
            receipt_file = active_run_directory / "kanban-command-execution-start-receipt.json"
            if binding_file.exists() and not os.environ.get("KANBAN_COMMAND_EXECUTION_ID"):
                receipt = read_json(receipt_file) if receipt_file.exists() else {}
                try:
                    accepted = receipt.get("returncode") == 0 and json.loads(receipt.get("stdout", "{}")).get("ok") is True
                except ValueError:
                    accepted = False
                if not accepted:
                    from adapters.cline_kanban.owned_command_execution import start_managed_review
                    binding = read_json(binding_file)
                    result = start_managed_review(active_run_directory, binding["task_id"], project_path=Path(binding["project_path"]),
                        kanban_cli=Path(binding["kanban_cli"]) if binding["kanban_cli"] else None, entrypoint=Path(__file__).resolve())
                    print(json.dumps(result, ensure_ascii=False))
                    return 0
            validations = json.loads(arguments.validation_commands.read_text()) if arguments.validation_commands else None
            result = resume_review(arguments.run_dir,
                                   reviewer_configs=[load_reviewer_configuration(path) for path in arguments.reviewer_config] if arguments.reviewer_config else None,
                                   fix=arguments.fix, fixer_config=load_reviewer_configuration(arguments.fixer_config) if arguments.fixer_config else None,
                                   validation_commands=validations,context_response_file=arguments.context_response_file)
        elif arguments.command == "acknowledge-review-notice":
            result = acknowledge_agent_review_notice(arguments.run_dir, arguments.notice_id)
        elif arguments.command == "submit-review-context":
            result = register_main_agent_context_supplement(arguments.run_dir, supplement_id=arguments.supplement_id,
                candidate_commit=arguments.candidate_commit, findings_sha256=arguments.findings_sha256,
                context_file=arguments.context_file, finding_ids=arguments.finding_id)
        elif arguments.command == "cancel-task-review":
            result = revoke_execution(arguments.run_dir.expanduser().resolve(), arguments.reason,
                source="rvf_cancel_task_review_command", request_basis="显式 cancel-task-review 命令；调用来源不推断为真人")
        elif arguments.command == "collect-issues":
            if arguments.list_repository:
                issues = list_repository_issues(arguments.list_repository)
                catalog=root/'github-issue-catalogs'/(hashlib.sha256(arguments.list_repository.encode()).hexdigest()+'.json')
                write_json(catalog,{'repository':arguments.list_repository,'issues':issues})
                result = {"repository":arguments.list_repository,"issue_count":len(issues),'catalog_file':str(catalog),
                          "issues":[{key:issue.get(key) for key in ("number","title","state")} for issue in issues[:20]]}
            elif arguments.flush:
                result = flush_pending_reports(storage_root=root)
            elif arguments.report:
                payload = read_json(arguments.report)
                reports = payload.get("reports", [payload])
                result = {"reports":[collect_report(report, storage_root=root, repo=arguments.repo) for report in reports]}
            else:
                parser.error("collect-issues 需要 --report、--flush 或 --list-repository")
        else:
            result = analyze_runs(arguments.run_dir)
        response = result
        if "agentContinuationNotice" in result:
            response = {key: value for key, value in result.items() if key != "task_snapshot_review_result_facts"}
            response["agent_review_summary_file"] = str(active_run_directory / "agent-review-summary.md")
        print(json.dumps(response, ensure_ascii=False))
        if result.get('status') == 'unknown':
            return 2
        return 1 if result.get("status") in ("review_incomplete","fix_incomplete","findings_unresolved","validation_failed","candidate_changed") else 0
    except KeyboardInterrupt:
        print(json.dumps({'status':'interrupted','disposition':'machine_evidence_pending','message':'当前前台等待已打断，后台工作及续接资格保留；明确撤销请使用 cancel-task-review。'},ensure_ascii=False))
        return 130
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(json.dumps({"status":"error","message":str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
