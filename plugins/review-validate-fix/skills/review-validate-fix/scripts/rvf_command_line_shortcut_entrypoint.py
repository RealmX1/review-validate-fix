#!/usr/bin/env python3
"""RVF：固定任务快照、接续评审及按需问题收集。"""
from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sys
import uuid
from pathlib import Path
import _rvf_pyroot
from core.rvf_user_local_data_paths import resolved_rvf_user_local_data_paths
from core.task_snapshot_review.github_issue_observations import collect_report, flush_pending_reports, list_repository_issues, schedule_pending_issue_delivery
from core.task_snapshot_review.persistent_artifacts import read_json, write_json
from core.task_snapshot_review.task_snapshot import create_snapshot
from core.task_snapshot_review.workflow import resume_review, load_reviewer_configuration


def main(argv=None) -> int:
    def interrupted(signum,frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review-task-snapshot", help="固定显式任务范围并运行独立双审")
    review.add_argument("--repo", type=Path, default=Path.cwd())
    review.add_argument("--base-ref", required=True, help="解析一次的本地基线 ref 或 commit")
    review.add_argument("--scope", action="append", required=True, help="可重复的仓内相对路径；全仓范围显式使用 .")
    review.add_argument("--requirements-file", type=Path, required=True)
    review.add_argument("--run-directory", type=Path)
    review.add_argument("--prepare-only", action="store_true")
    review.add_argument("--reviewer-config", type=Path, action="append")
    resume = commands.add_parser("resume-task-review", help="复用精确 run，接续未完成评审、修复及验证")
    resume.add_argument("--run-dir", type=Path, required=True)
    resume.add_argument("--reviewer-config", type=Path, action="append")
    resume.add_argument("--fix", action="store_true")
    resume.add_argument("--fixer-config", type=Path)
    resume.add_argument("--validation-commands", type=Path, help="最终验证 argv 数组列表 JSON")
    resume.add_argument('--context-response-file',type=Path,help='为缺失腿补充通用机器证据，保持两路独立')
    collect = commands.add_parser("collect-issues", help="主 Agent 统一收集问题；不可达时持久保留")
    collect.add_argument("--report", type=Path)
    collect.add_argument("--repo", type=Path, default=Path.cwd())
    collect.add_argument("--flush", action="store_true")
    collect.add_argument("--list-repository", help="匹配旧人工 Issue 前读取开放与关闭问题")
    analyze = commands.add_parser("analyze", help="按需汇总原始运行事实，不产生完成门禁")
    analyze.add_argument("--run-dir", type=Path, required=True)
    arguments = parser.parse_args(argv)
    root = resolved_rvf_user_local_data_paths().user_local_data_root
    if arguments.command in ('review-task-snapshot','resume-task-review'):
        try:
            schedule_pending_issue_delivery(root,Path(__file__).resolve())
        except (OSError,ValueError,RuntimeError):
            pass
    try:
        if arguments.command == "review-task-snapshot":
            run = arguments.run_directory or root / "task-snapshot-reviews" / f"task-review-{uuid.uuid4().hex}"
            record = create_snapshot(arguments.repo, base_ref=arguments.base_ref, scope_paths=arguments.scope,
                                     requirements=arguments.requirements_file.read_bytes(), run_directory=run)
            result = {"status":record["status"],"run_directory":str(run.resolve())}
            if not arguments.prepare_only:
                result = resume_review(run, reviewer_configs=[load_reviewer_configuration(path) for path in arguments.reviewer_config] if arguments.reviewer_config else None)
        elif arguments.command == "resume-task-review":
            validations = json.loads(arguments.validation_commands.read_text()) if arguments.validation_commands else None
            result = resume_review(arguments.run_dir,
                                   reviewer_configs=[load_reviewer_configuration(path) for path in arguments.reviewer_config] if arguments.reviewer_config else None,
                                   fix=arguments.fix, fixer_config=load_reviewer_configuration(arguments.fixer_config) if arguments.fixer_config else None,
                                   validation_commands=validations,context_response_file=arguments.context_response_file)
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
            run = arguments.run_dir.resolve()
            record = read_json(run / "run.json")
            attempts = [{"role":path.parent.parent.name, "attempt":path.parent.name, **read_json(path)} for path in sorted(run.glob("*/attempt-*/outcome.json"))]
            result = {"run_directory":str(run),"status":record["status"],"base_commit":record["base_commit"],
                      "candidate_commit":record["candidate_commit"],"attempts":attempts,"analysis_required_for_completion":False}
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("status") in ("review_incomplete","fix_incomplete","findings_unresolved","validation_failed","candidate_changed") else 0
    except KeyboardInterrupt:
        print(json.dumps({'status':'interrupted','message':'已停止当前执行，原始进度保留；明确接续时复用原 run。'},ensure_ascii=False))
        return 130
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(json.dumps({"status":"error","message":str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
