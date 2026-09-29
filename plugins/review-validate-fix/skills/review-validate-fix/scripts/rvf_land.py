#!/usr/bin/env python3
"""rvf-land 单步化：``prepare`` 与 ``commit`` 两条命令包住 land 的全部机械步骤。

land 过去平均要 agent 发 18 个请求：intake、逐条 ``validation_result_cache.py`` 复用、翻
validation-records、stage、``diff --check``、commit、封窗、写 evidence 各一次，外加每一步之间的
状态确认。其中只有「sanity-check 已应用的改动、必要时做最小修正、定稿 commit message」需要判断，
其余全部进本脚本：

- ``prepare``：intake → 硬门槛（退出码区分）→ 精简 scoped diff、handoff 的 issue 处理结果、
  ``validation_reusable``、预填 commit message 草稿；并把本次判定落成 prepare plan 供 commit 复核。
- ``commit``：验证（能复用就零执行，否则按结构化 argv 经缓存重跑）→ stage → ``diff --cached
  --check`` → commit → 确认 clean → 封窗 → 写 candidate evidence → 输出一份 JSON。

**零执行复用的依据**：candidate evidence 要求每条 validation record 的内容摘要都等于提交后的
内容摘要，而内容摘要按文件系统计算、与 HEAD 无关（``validation_result_cache.
candidate_content_manifest_sha256``）。没有补修正、且没有无关脏文件（硬门槛 12 保证）时，
提交前后内容摘要相同，run 自己的 index 已经满足 evidence 门槛——不必再执行任何验证。
有补修正时内容摘要变了，才按 ``artifacts/handoff-validation-commands.json`` 的 argv 重跑。

base-branch-sync 不并入本脚本，仍由 agent 在之后按 skill 调用。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401
from rvf_handoff_intake import build_payload, parse_sections, repo_snapshot  # noqa: E402
from seal_round_baseline_to_head import seal_round_baseline_to_head  # noqa: E402
from validation_result_cache import (  # noqa: E402
    RVF_RUN_DIRECTORY_ENVIRONMENT_VARIABLE,
    candidate_content_manifest_sha256,
    canonical_json_bytes,
    load_handoff_validation_commands,
    repository_identity,
    sha256_hex,
    summarize_run_validation_index,
    write_json_with_digest,
)
from write_reviewed_task_candidate_evidence import (  # noqa: E402
    CandidateEvidenceNotApplicable,
    reviewed_scope_path_universe,
    write_evidence,
)


VALIDATION_RESULT_CACHE_SCRIPT = Path(__file__).resolve().with_name("validation_result_cache.py")

RVF_LAND_EXIT_OK = 0
RVF_LAND_EXIT_USAGE_ERROR = 2
# prepare / commit 共用的硬门槛退出码（数值即契约，SKILL 按它分支）。
RVF_LAND_EXIT_CROSS_WORKTREE_HANDOFF = 10
RVF_LAND_EXIT_REVIEWED_SCOPE_CONTRADICTS_DIRTY_PATHS = 11
RVF_LAND_EXIT_UNRELATED_DIRTY_PATHS_WOULD_REMAIN = 12
RVF_LAND_EXIT_RUN_DIRECTORY_UNRESOLVED = 13
# commit 独有。
RVF_LAND_EXIT_PREPARE_PLAN_MISSING_OR_STALE = 20
RVF_LAND_EXIT_VALIDATION_FAILED = 21
RVF_LAND_EXIT_VALIDATION_NOT_REPLAYABLE = 22
RVF_LAND_EXIT_STAGED_DIFF_CHECK_FAILED = 23
RVF_LAND_EXIT_GIT_COMMIT_FAILED = 24
RVF_LAND_EXIT_WORKTREE_NOT_CLEAN_AFTER_COMMIT = 25
# commit 已发生，但 commit 期间（pre-commit 等 hook 改写文件）内容摘要不再等于验证时的摘要：
# 提交进去的字节未经验证，因此不封窗、不写 evidence。
RVF_LAND_EXIT_CANDIDATE_CONTENT_CHANGED_DURING_COMMIT = 26

RVF_LAND_PREPARE_PLAN_ARTIFACT_NAME = "rvf-land-prepare-plan.json"
RVF_LAND_PREPARE_PLAN_SCHEMA_NAME = "rvf-land-prepare-plan"
RVF_LAND_PREPARE_PLAN_SCHEMA_VERSION = 1
# commit 把实际验证结果替换进这一行；agent 定稿 message 时保留它即可，不必手抄验证结果。
RVF_LAND_COMMIT_MESSAGE_VALIDATION_SUMMARY_PLACEHOLDER = "[[rvf-land-validation-summary]]"
# 草稿里需要 agent 填写的占位；commit 时仍残留即拒绝，防止把草稿原样提交。
RVF_LAND_COMMIT_MESSAGE_DRAFT_UNFILLED_MARKERS = ("<type>(<scope>)", "<中文一句话摘要>", "<sanity-check 结论>")
# EXPERIMENT_NODE: prepare 回显 scoped diff 的行数上限——太小 agent 会再发请求读全量 diff，太大则把大 diff 整份灌进上下文
RVF_LAND_SCOPED_DIFF_DEFAULT_MAX_LINES = 600
HANDOFF_SECTION_HEADINGS_SHOWN_TO_LAND_AGENT = (
    "本次 review",
    "Issue 处理结果",
    "Validate/fix 分组与处理结果",
    "Review Findings",
    "Fixes",
    "需要开发者决策的升级事项",
)
UNMERGED_PORCELAIN_STATUSES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
# 只剥离「指向别处仓库」的 git 位置变量；hooks、身份、签名等用户配置保持与手动 commit 一致。
GIT_REPOSITORY_LOCATION_ENVIRONMENT_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)


class RvfLandHardGateFailure(Exception):
    def __init__(self, exit_code: int, reason_code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.reason_code = reason_code
        self.message = message
        self.details = details

    def as_payload(self) -> dict[str, Any]:
        # 失败输出尾部是多行纯文本，单独走 stderr；塞进 JSON 会变成一整行转义串，agent 反而难读。
        return {
            "exit_code": self.exit_code,
            "reason_code": self.reason_code,
            "message": self.message,
            **{key: value for key, value in self.details.items() if key != "failure_output"},
        }


def git_environment_for_user_commit() -> dict[str, str]:
    environment = dict(os.environ)
    for name in GIT_REPOSITORY_LOCATION_ENVIRONMENT_VARIABLES:
        environment.pop(name, None)
    return environment


def run_git(repo_root: Path, *arguments: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=git_environment_for_user_commit(),
    )
    if check and completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"git {arguments[0]} failed")
    return completed


def head_oid(repo_root: Path) -> str | None:
    completed = run_git(repo_root, "rev-parse", "--verify", "HEAD")
    return completed.stdout.strip() if completed.returncode == 0 else None


def prepare_plan_artifact_path(run_dir: Path) -> Path:
    return run_dir / "artifacts" / RVF_LAND_PREPARE_PLAN_ARTIFACT_NAME


def read_json_object_or_none(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def repo_relative_path(repo_root: Path, raw_path: str) -> str:
    candidate = Path(raw_path).expanduser()
    absolute = candidate if candidate.is_absolute() else (Path.cwd() / candidate)
    try:
        return absolute.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"--extra-path 必须位于当前 worktree 内：{raw_path}") from exc


# ---------------------------------------------------------------------------
# 事实收集（prepare / commit 共用，同一份口径）
# ---------------------------------------------------------------------------


def applied_fix_attempt_dispositions(run_dir: Path) -> list[dict[str, Any]]:
    """从 fix ledger（``fix-attempts/*/{attempt,result}.json`` + ``fix-issues/*.json``）读每条 issue 的处置。

    handoff 正文格式因 agent 而异，「已采纳哪些 RVF-NNN」以机器 ledger 为准，不解析 markdown。
    同一 issue 多次 attempt 时优先取已 apply 的那一次，其次取最新一次。
    """
    artifacts = run_dir / "artifacts"
    titles: dict[str, str] = {}
    for issue_path in sorted((artifacts / "fix-issues").glob("*.json")):
        issue = read_json_object_or_none(issue_path)
        if issue and isinstance(issue.get("issue_id"), str):
            titles[issue["issue_id"]] = str(issue.get("title") or "")
    chosen: dict[str, tuple[tuple[int, float], dict[str, Any]]] = {}
    attempts_root = artifacts / "fix-attempts"
    for attempt_directory in sorted(attempts_root.iterdir()) if attempts_root.is_dir() else []:
        result_path = attempt_directory / "result.json"
        result = read_json_object_or_none(result_path)
        attempt = read_json_object_or_none(attempt_directory / "attempt.json") or {}
        if not result or not isinstance(result.get("issue_id"), str):
            continue
        # ``rvf_fix_attempt.py apply`` 成功合回后把 attempt.json 的 status 改写成 applied；
        # result.json 的 status 保持 stop 时的 fix 结论（fixed / false_positive / ...）。
        applied = attempt.get("status") == "applied"
        # 本修复的补丁路径 = stop 写入的 ``changed_paths``（按 patch_paths 限定的 ``{path, op}`` 列表）；
        # ``all_changed_paths`` 是 stop 当时整个 attempt 工作树的脏路径，不属于本修复，不能并入。
        fix_patch_changed_paths: list[str] = []
        for entry in result.get("changed_paths") or []:
            path = entry.get("path") if isinstance(entry, dict) else entry
            if isinstance(path, str) and path:
                fix_patch_changed_paths.append(path)
        disposition = {
            "issue_id": result["issue_id"],
            "title": titles.get(result["issue_id"], ""),
            "fix_status": result.get("status"),
            "fix_applied_to_worktree": applied,
            "changed_paths": list(dict.fromkeys(fix_patch_changed_paths)),
        }
        rank = (1 if applied else 0, result_path.stat().st_mtime)
        previous = chosen.get(result["issue_id"])
        if previous is None or rank > previous[0]:
            chosen[result["issue_id"]] = (rank, disposition)
    for issue_id, title in titles.items():
        if issue_id not in chosen:
            chosen[issue_id] = (
                (0, 0.0),
                {
                    "issue_id": issue_id,
                    "title": title,
                    "fix_status": None,
                    "fix_applied_to_worktree": False,
                    "changed_paths": [],
                },
            )
    return [chosen[issue_id][1] for issue_id in sorted(chosen)]


def reviewed_scope_contract(run_dir: Path) -> dict[str, Any] | None:
    return read_json_object_or_none(run_dir / "artifacts" / "inputs" / "scope.contract.json")


def land_stageable_path_universe(
    intake: dict[str, Any], contract: dict[str, Any] | None, dispositions: list[dict[str, Any]]
) -> list[str]:
    """允许 stage 的路径全集 = reviewed scope 上界 ∪ 已合回 fix 的改动路径，再剔除 handoff 标注的 protected 路径。

    reviewed scope 上界：scope contract 给得出路径全集时**只用它**——它与 evidence 的落地 delta 收敛判据
    同口径（``reviewed_scope_path_universe``）；handoff 的 ``reviewed_scope_paths`` 是从粘贴正文/manifest
    解析出来的，不能把上界撑到 contract 之外，否则提交会越过 evidence 判据、而 evidence 拒绝发生在提交之后。
    contract 缺失或全集为空时才退回 handoff 的 ``reviewed_scope_paths``。

    已合回 fix 的补丁路径照常并入：其中 scope expansion 路径是 fix attempt 显式声明过的，但它们不在
    contract 全集里、evidence 会拒绝——prepare 用 ``stageable_dirty_paths_outside_candidate_evidence_
    landing_universe`` 在提交前把这一后果摆出来。
    """
    contract_universe = reviewed_scope_path_universe(contract) if contract is not None else set()
    universe = set(contract_universe) if contract_universe else set(intake.get("reviewed_scope_paths") or [])
    for disposition in dispositions:
        if disposition["fix_applied_to_worktree"]:
            universe.update(disposition["changed_paths"])
    protected = {
        path
        for path in (intake.get("intake_hints") or {}).get("protected_paths") or []
        if isinstance(path, str)
    }
    return sorted(path for path in universe if path and path not in protected)


def partition_dirty_entries(
    dirty_entries: list[dict[str, str]], stageable_paths: set[str]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """rename / copy 条目会把新旧两端一起 ``git add``，所以**两端都**在可 stage 集合内才算 in scope；
    只要有一端在集合外（scope 外或 protected），整条归为 scope 外脏文件（exit 12），由 agent 用
    ``--extra-path`` 显式确认那一端，而不是被另一端顺带拖进提交。"""
    in_scope: list[dict[str, str]] = []
    unrelated: list[dict[str, str]] = []
    for entry in dirty_entries:
        paths = dirty_entry_paths(entry)
        (in_scope if paths and paths <= stageable_paths else unrelated).append(entry)
    return in_scope, unrelated


def dirty_entry_paths(entry: dict[str, str]) -> set[str]:
    return {entry.get("path"), entry.get("old_path")} - {None, ""}


def dirty_paths_outside(entries: list[dict[str, str]], allowed_paths: set[str]) -> list[str]:
    return sorted({path for entry in entries for path in dirty_entry_paths(entry) if path not in allowed_paths})


def reviewed_scope_dirty_gate_failures(
    dirty_entries: list[dict[str, str]],
    stageable_paths: set[str],
    in_scope: list[dict[str, str]],
    unrelated: list[dict[str, str]],
) -> list[RvfLandHardGateFailure]:
    failures: list[RvfLandHardGateFailure] = []
    if dirty_entries and not stageable_paths:
        failures.append(
            RvfLandHardGateFailure(
                RVF_LAND_EXIT_REVIEWED_SCOPE_CONTRADICTS_DIRTY_PATHS,
                "reviewed_scope_unknown_with_dirty_worktree",
                "工作树有改动，但 handoff / scope contract / fix ledger 都给不出 reviewed scope，无法把改动绑定到这轮 RVF。",
            )
        )
    unmerged = [entry["path"] for entry in in_scope if entry.get("status") in UNMERGED_PORCELAIN_STATUSES]
    if unmerged:
        failures.append(
            RvfLandHardGateFailure(
                RVF_LAND_EXIT_REVIEWED_SCOPE_CONTRADICTS_DIRTY_PATHS,
                "reviewed_scope_paths_unmerged",
                "reviewed scope 内有未解决的冲突文件。",
                paths=unmerged,
            )
        )
    if unrelated:
        failures.append(
            RvfLandHardGateFailure(
                RVF_LAND_EXIT_UNRELATED_DIRTY_PATHS_WOULD_REMAIN,
                "unrelated_dirty_paths_would_remain_after_commit",
                "存在 reviewed scope 之外的脏文件：commit 后工作树不会 clean，evidence 无法绑定唯一 HEAD。"
                "若它们属于本次工作（例如测试注册、补修正的直接 fallout），用 --extra-path 显式纳入；"
                "否则先把它们移出本 worktree 再重跑。",
                paths=dirty_paths_outside(unrelated, stageable_paths),
            )
        )
    return failures


def validation_reuse_assessment(repo_root: Path, run_dir: Path) -> dict[str, Any]:
    _root, common_dir = repository_identity(repo_root)
    content_digest = candidate_content_manifest_sha256(repo_root)
    index_summary = summarize_run_validation_index(
        run_dir,
        current_candidate_content_manifest_sha256=content_digest,
        expected_git_common_directory=common_dir,
    )
    try:
        structured_commands = load_handoff_validation_commands(run_dir)
        structured_commands_status = "missing" if structured_commands is None else "present"
    except ValueError as exc:
        structured_commands = None
        structured_commands_status = f"corrupt: {exc}"
    replayable_names = {entry["name"] for entry in structured_commands or []}
    unreplayable_index_names = sorted(
        {record.get("name") for record in index_summary["records"]} - replayable_names - {None}
    )
    validation_reusable = bool(index_summary["all_records_passed_for_current_content"])
    if validation_reusable:
        validation_plan = "reuse_run_validation_index_with_zero_execution"
    elif structured_commands and not unreplayable_index_names:
        validation_plan = "rerun_structured_argv_through_validation_result_cache"
    else:
        validation_plan = "blocked_no_structured_argv_to_replay"
    return {
        "current_candidate_content_manifest_sha256": content_digest,
        "validation_reusable": validation_reusable,
        "validation_plan": validation_plan,
        "run_validation_index_summary": index_summary,
        "structured_validation_commands_status": structured_commands_status,
        "structured_validation_command_names": [entry["name"] for entry in structured_commands or []],
        "index_record_names_without_structured_argv": unreplayable_index_names,
    }


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def render_scoped_diff(repo_root: Path, in_scope_entries: list[dict[str, str]], max_lines: int) -> dict[str, Any]:
    tracked_paths = sorted(
        {
            path
            for entry in in_scope_entries
            if entry.get("status") != "??"
            for path in (entry.get("path"), entry.get("old_path"))
            if path
        }
    )
    untracked_paths = sorted(entry["path"] for entry in in_scope_entries if entry.get("status") == "??")
    stat_lines: list[str] = []
    diff_lines: list[str] = []
    if tracked_paths:
        stat_lines += run_git(repo_root, "diff", "HEAD", "--stat=160", "--", *tracked_paths).stdout.splitlines()
        diff_lines += run_git(repo_root, "diff", "HEAD", "--no-color", "-U2", "--", *tracked_paths).stdout.splitlines()
    for path in untracked_paths:
        stat_lines.append(f" {path} (untracked)")
        diff_lines += run_git(
            repo_root, "diff", "--no-index", "--no-color", "-U2", "--", os.devnull, path
        ).stdout.splitlines()
    truncated = len(diff_lines) > max_lines
    return {
        "stat": "\n".join(stat_lines),
        "diff": "\n".join(diff_lines[:max_lines]),
        "diff_total_line_count": len(diff_lines),
        "diff_truncated": truncated,
        "full_diff_command": (
            "git diff HEAD -- " + " ".join(tracked_paths) if tracked_paths else None
        ),
    }


def target_worktree_scoped_diff(
    intake: dict[str, Any], stageable: set[str], args: argparse.Namespace
) -> dict[str, Any] | None:
    """跨 worktree 模式下，RVF 的修复在另一个 worktree 里：agent 要看的是那边的 scoped diff 才能移植。"""
    target_snapshot = intake.get("target_repo_snapshot") or {}
    if not args.cross_worktree or not intake.get("rvf_worktree_differs_from_current"):
        return None
    if not target_snapshot.get("is_git_repo"):
        return None
    target_dirty = [entry for entry in target_snapshot.get("dirty_paths") or [] if isinstance(entry, dict)]
    target_in_scope, _target_unrelated = partition_dirty_entries(target_dirty, stageable)
    return {
        "target_worktree": target_snapshot["repo"],
        **render_scoped_diff(Path(target_snapshot["repo"]), target_in_scope, args.diff_max_lines),
    }


def commit_message_draft(run_id: str | None, dispositions: list[dict[str, Any]]) -> str:
    accepted = [item for item in dispositions if item["fix_status"] == "fixed" and item["fix_applied_to_worktree"]]
    not_accepted = [item for item in dispositions if item not in accepted]
    lines = [
        "<type>(<scope>): <中文一句话摘要>",
        "",
        f"RVF run：{run_id or '<unknown>'}",
        "sanity-check：<sanity-check 结论>",
        "已采纳（fixed 且已合回工作树）：",
    ]
    lines += [f"- {item['issue_id']} {item['title']}".rstrip() for item in accepted] or ["- 无"]
    lines.append("未采纳：")
    lines += [
        f"- {item['issue_id']}（{item['fix_status'] or '无 fix 结论'}）{item['title']}".rstrip()
        for item in not_accepted
    ] or ["- 无"]
    lines += ["验证：", RVF_LAND_COMMIT_MESSAGE_VALIDATION_SUMMARY_PLACEHOLDER]
    return "\n".join(lines)


def build_prepare_report(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    text, handoff_path = read_handoff_text(args)
    repo_argument = Path(args.repo).expanduser().resolve()
    intake = build_payload(text, repo_argument, handoff_path)
    current_snapshot = intake["current_repo_snapshot"]
    if not current_snapshot.get("is_git_repo"):
        raise ValueError(f"--repo 不是 git worktree：{repo_argument}")
    repo_root = Path(current_snapshot["repo"]).resolve()
    run_dir_text = args.run_dir or intake.get("run_dir")
    run_dir = Path(run_dir_text).expanduser().resolve() if run_dir_text else None

    gate_failures: list[RvfLandHardGateFailure] = []
    if run_dir is None or not (run_dir / "artifacts").is_dir():
        gate_failures.append(
            RvfLandHardGateFailure(
                RVF_LAND_EXIT_RUN_DIRECTORY_UNRESOLVED,
                "run_directory_unresolved",
                "无法从 handoff 定位 RVF run 目录（或目录不存在）；用 --run-dir 显式给出。",
                run_dir=str(run_dir) if run_dir else None,
            )
        )
    same_common_dir = intake.get("target_repo_same_git_common_dir_as_current")
    if same_common_dir is None:
        gate_failures.append(
            RvfLandHardGateFailure(
                RVF_LAND_EXIT_CROSS_WORKTREE_HANDOFF,
                "handoff_target_repository_unknown",
                "handoff 与 run summary 都给不出目标仓库，无法证明它描述的就是当前 worktree 的改动。",
            )
        )
    elif same_common_dir is False:
        gate_failures.append(
            RvfLandHardGateFailure(
                RVF_LAND_EXIT_CROSS_WORKTREE_HANDOFF,
                "handoff_targets_another_repository",
                "handoff 的目标仓库与当前 worktree 不共享 git common dir（不是同一个仓库）。",
                target_repo=intake.get("target_repo"),
            )
        )
    elif intake.get("rvf_worktree_differs_from_current") and not args.cross_worktree:
        gate_failures.append(
            RvfLandHardGateFailure(
                RVF_LAND_EXIT_CROSS_WORKTREE_HANDOFF,
                "handoff_targets_another_worktree",
                "handoff 指向同仓的另一个 worktree：这是跨 worktree handoff，改走 $rvf-handoff-intake / "
                "/rvf-handoff-commit（它们用 --cross-worktree 模式调用本脚本）。",
                target_repo=intake.get("target_repo"),
            )
        )

    contract = reviewed_scope_contract(run_dir) if run_dir is not None else None
    dispositions = applied_fix_attempt_dispositions(run_dir) if run_dir is not None else []
    extra_paths = sorted({repo_relative_path(repo_root, path) for path in args.extra_path})
    stageable_universe = land_stageable_path_universe(intake, contract, dispositions)
    stageable = set(stageable_universe) | set(extra_paths)
    dirty_entries = [entry for entry in current_snapshot.get("dirty_paths") or [] if isinstance(entry, dict)]
    in_scope, unrelated = partition_dirty_entries(dirty_entries, stageable)
    gate_failures += reviewed_scope_dirty_gate_failures(dirty_entries, stageable, in_scope, unrelated)

    validation = (
        validation_reuse_assessment(repo_root, run_dir)
        if run_dir is not None and (run_dir / "artifacts").is_dir()
        else None
    )
    sections = parse_sections(text)
    # handoff 正文未必带 run id（粘贴的往往只是摘要段）；run 目录里的 scope contract 才是权威来源。
    run_id = intake.get("run_id") or (contract or {}).get("run_id")
    report: dict[str, Any] = {
        "schema": "rvf-land-prepare-report",
        "run_id": run_id,
        "run_dir": str(run_dir) if run_dir else None,
        "repo_root": str(repo_root),
        "head_oid": head_oid(repo_root),
        "cross_worktree_mode": bool(args.cross_worktree),
        "target_repo": intake.get("target_repo"),
        "hard_gate_failures": [failure.as_payload() for failure in gate_failures],
        "stageable_dirty_paths": [entry["path"] for entry in in_scope],
        "unrelated_dirty_paths": dirty_paths_outside(unrelated, stageable),
        # 会进提交、但不在 evidence 落地 delta 收敛判据（scope contract 路径全集）内的路径：contract 已 opt-in
        # evidence 时，提交后 evidence 必然 refused_fail_closed（提交不回滚）。来源只可能是 --extra-path 或已合回 fix 的 scope expansion 路径。
        "stageable_dirty_paths_outside_candidate_evidence_landing_universe": (
            dirty_paths_outside(in_scope, reviewed_scope_path_universe(contract)) if contract is not None else []
        ),
        "extra_paths": extra_paths,
        "nothing_to_commit": not in_scope,
        "issue_dispositions_from_fix_ledger": dispositions,
        "handoff_sections": {
            heading: sections[heading]
            for heading in HANDOFF_SECTION_HEADINGS_SHOWN_TO_LAND_AGENT
            if sections.get(heading)
        },
        "handoff_status_completed": bool(re.search(r"handoff_status:\s*COMPLETED", text, re.IGNORECASE)),
        "validation": validation,
        "scoped_diff": render_scoped_diff(repo_root, in_scope, args.diff_max_lines),
        "target_worktree_scoped_diff": target_worktree_scoped_diff(intake, stageable, args),
        "commit_message_draft": commit_message_draft(run_id, dispositions),
    }
    # 退出码取**第一条**失败：门槛按「run 目录 → 仓库身份 → scope 矛盾 → 无关脏文件」的根因顺序追加，
    # run 目录都找不到时「目标仓库未知」只是它的派生症状，不该抢答。
    exit_code = gate_failures[0].exit_code if gate_failures else RVF_LAND_EXIT_OK
    cross_repository = any(
        failure.reason_code in {"handoff_target_repository_unknown", "handoff_targets_another_repository"}
        for failure in gate_failures
    )
    if run_dir is not None and (run_dir / "artifacts").is_dir() and not cross_repository:
        # 11/12 在 commit 时按当时的工作树重新判定（补修正可能改变它们），所以只要仓库身份与
        # run 目录成立就落 plan；10 与 13 不会因补修正而改变，commit 直接信 plan。
        plan = {
            "schema": RVF_LAND_PREPARE_PLAN_SCHEMA_NAME,
            "schema_version": RVF_LAND_PREPARE_PLAN_SCHEMA_VERSION,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "repo_root": str(repo_root),
            "head_oid_at_prepare": report["head_oid"],
            "cross_worktree_mode": bool(args.cross_worktree),
            "cross_worktree_gate_failed": any(
                failure.exit_code == RVF_LAND_EXIT_CROSS_WORKTREE_HANDOFF for failure in gate_failures
            ),
            "stageable_path_universe": stageable_universe,
            "extra_paths": extra_paths,
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
        write_json_with_digest(prepare_plan_artifact_path(run_dir), plan)
        report["prepare_plan_path"] = str(prepare_plan_artifact_path(run_dir))
    return report, exit_code


def render_prepare_report_text(report: dict[str, Any], exit_code: int) -> str:
    lines = [
        f"RVF_LAND_PREPARE exit={exit_code} run_id={report['run_id']} head={report['head_oid']}",
        f"run_dir: {report['run_dir']}",
        f"repo_root: {report['repo_root']}  cross_worktree_mode={report['cross_worktree_mode']}",
    ]
    if report["hard_gate_failures"]:
        lines.append("\n## 硬门槛未通过（不得 commit）")
        for failure in report["hard_gate_failures"]:
            lines.append(f"- exit {failure['exit_code']} {failure['reason_code']}：{failure['message']}")
            for path in failure.get("paths") or []:
                lines.append(f"  - {path}")
    else:
        lines.append("\n## 硬门槛：全部通过")
    if not report["handoff_status_completed"]:
        lines.append("⚠️ handoff 未标 handoff_status: COMPLETED——RVF 可能没跑完，evidence 会被拒。")
    lines.append("\n## 将要 stage 的路径（reviewed scope ∩ dirty）")
    lines += [f"- {path}" for path in report["stageable_dirty_paths"]] or ["- <无：没有可提交的改动，commit 只做验证/封窗/evidence>"]
    outside_evidence = report["stageable_dirty_paths_outside_candidate_evidence_landing_universe"]
    if outside_evidence:
        lines.append("⚠️ 以下路径会进提交，但不在 scope contract 路径全集内：contract 已 opt-in evidence 时，提交后 evidence 必然 refused_fail_closed（提交不回滚）。")
        lines += [f"  - {path}" for path in outside_evidence]
    validation = report["validation"]
    if validation is not None:
        index = validation["run_validation_index_summary"]
        lines.append(
            f"\n## 验证：validation_reusable={str(validation['validation_reusable']).lower()} "
            f"plan={validation['validation_plan']}"
        )
        for record in index["records"]:
            lines.append(
                f"- {record.get('name')}: {record.get('status')}"
                f" matches_current_content={record.get('matches_current_candidate_content')}"
            )
        if not index["records"]:
            lines.append(f"- <run validation index {index['index_status']}>")
        if validation["validation_plan"] == "blocked_no_structured_argv_to_replay":
            lines.append(
                "- 当前内容与 run 的验证记录不一致，且没有可重放的结构化 argv"
                f"（{validation['structured_validation_commands_status']}；缺 argv 的记录："
                f"{', '.join(validation['index_record_names_without_structured_argv']) or '无'}）。"
                "commit 前请逐条执行：validation_result_cache.py run --repo . --name <handoff-validation-NNN> "
                f"--rvf-run-dir {report['run_dir']} -- <argv...>"
            )
    lines.append("\n## Issue 处置（fix ledger）")
    for item in report["issue_dispositions_from_fix_ledger"]:
        lines.append(
            f"- {item['issue_id']} fix_status={item['fix_status']} applied={item['fix_applied_to_worktree']} {item['title']}"
        )
    if not report["issue_dispositions_from_fix_ledger"]:
        lines.append("- <fix ledger 为空>")
    for heading, body in report["handoff_sections"].items():
        lines += [f"\n## handoff · {heading}", body]
    diff = report["scoped_diff"]
    target_diff = report.get("target_worktree_scoped_diff")
    if target_diff is not None:
        lines += [
            f"\n## 目标 worktree（{target_diff['target_worktree']}）的 scoped diff——待移植到当前 worktree",
            target_diff["stat"] or "<空>",
            target_diff["diff"] or "<空>",
        ]
    lines += ["\n## 当前 worktree scoped diff --stat", diff["stat"] or "<空>", "\n## 当前 worktree scoped diff（-U2）", diff["diff"] or "<空>"]
    if diff["diff_truncated"]:
        lines.append(
            f"… 已截断：共 {diff['diff_total_line_count']} 行，只显示前 {report.get('diff_max_lines', '')} 行；"
            f"全量：{diff['full_diff_command']}"
        )
    lines += [
        "\n## commit message 草稿（填好占位；保留验证占位行，commit 会替换成实际验证结果）",
        report["commit_message_draft"],
    ]
    return "\n".join(lines)


def read_handoff_text(args: argparse.Namespace) -> tuple[str, Path | None]:
    if args.stdin:
        return sys.stdin.read(), None
    path = Path(args.handoff or args.handoff_text_file).expanduser()
    return path.read_text(encoding="utf-8"), (path if args.handoff else None)


def command_prepare(args: argparse.Namespace) -> int:
    report, exit_code = build_prepare_report(args)
    report["diff_max_lines"] = args.diff_max_lines
    if args.format == "json":
        print(json.dumps({"exit_code": exit_code, **report}, ensure_ascii=False, indent=2))
    else:
        print(render_prepare_report_text(report, exit_code))
    return exit_code


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------


def load_prepare_plan(run_dir: Path) -> dict[str, Any]:
    path = prepare_plan_artifact_path(run_dir)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    try:
        encoded = path.read_bytes()
        payload = json.loads(encoded.decode("utf-8"))
        recorded_digest = sidecar.read_bytes()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RvfLandHardGateFailure(
            RVF_LAND_EXIT_PREPARE_PLAN_MISSING_OR_STALE,
            "prepare_plan_missing",
            f"找不到可用的 prepare plan（{exc}）；先跑 rvf_land.py prepare。",
        ) from exc
    if (
        not isinstance(payload, dict)
        or encoded != canonical_json_bytes(payload)
        or recorded_digest != f"{sha256_hex(encoded)}\n".encode("ascii")
        or payload.get("schema") != RVF_LAND_PREPARE_PLAN_SCHEMA_NAME
        or payload.get("schema_version") != RVF_LAND_PREPARE_PLAN_SCHEMA_VERSION
    ):
        raise RvfLandHardGateFailure(
            RVF_LAND_EXIT_PREPARE_PLAN_MISSING_OR_STALE,
            "prepare_plan_corrupt",
            "prepare plan 损坏或 schema 不符；重跑 rvf_land.py prepare。",
        )
    return payload


def replay_structured_validation_commands(repo_root: Path, run_dir: Path) -> list[dict[str, Any]]:
    commands = load_handoff_validation_commands(run_dir) or []
    outcomes: list[dict[str, Any]] = []
    for entry in commands:
        invocation = [
            sys.executable,
            str(VALIDATION_RESULT_CACHE_SCRIPT),
            "run",
            "--repo",
            str(repo_root),
            "--name",
            entry["name"],
            "--working-directory",
            entry["working_directory_relative_to_worktree"],
            "--validation-context-kind",
            entry["validation_context_kind"],
            "--rvf-run-dir",
            str(run_dir),
        ]
        for key in entry["environment_keys"]:
            invocation += ["--environment-key", key]
        if entry["requires_base_worktree_context"]:
            invocation.append("--requires-base-worktree-context")
        completed = subprocess.run(
            [*invocation, "--", *entry["argv"]],
            capture_output=True,
            text=True,
            check=False,
            cwd=repo_root,
        )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            result = {}
        outcome = {
            "name": entry["name"],
            "cache_status": result.get("cache_status"),
            "status": result.get("status") or "error",
            "exit_code": completed.returncode,
        }
        outcomes.append(outcome)
        if completed.returncode != 0 or outcome["status"] != "passed":
            # 失败尾部由 validation_result_cache 自己打到 stderr；原样转给 agent，不让它再去翻记录。
            raise RvfLandHardGateFailure(
                RVF_LAND_EXIT_VALIDATION_FAILED,
                "validation_failed",
                f"验证 {entry['name']} 未通过（cache_status={outcome['cache_status']}，exit={completed.returncode}）；"
                "未 stage、未 commit。",
                validation_outcomes=outcomes,
                failure_output=(completed.stderr.strip() or completed.stdout.strip())[-12000:],
            )
    return outcomes


def ensure_validation_satisfies_evidence_gate(repo_root: Path, run_dir: Path) -> dict[str, Any]:
    assessment = validation_reuse_assessment(repo_root, run_dir)
    if assessment["validation_reusable"]:
        return {
            "mode": "reused_run_validation_index_with_zero_execution",
            "outcomes": [
                {"name": record["name"], "cache_status": "reused_run_index", "status": record["status"]}
                for record in assessment["run_validation_index_summary"]["records"]
            ],
            "candidate_content_manifest_sha256": assessment["current_candidate_content_manifest_sha256"],
        }
    if assessment["validation_plan"] != "rerun_structured_argv_through_validation_result_cache":
        raise RvfLandHardGateFailure(
            RVF_LAND_EXIT_VALIDATION_NOT_REPLAYABLE,
            "validation_not_replayable",
            "当前内容与 run 的验证记录不一致，且没有可重放的结构化 argv（旧 run 或 argv 工件缺失/损坏）。"
            "逐条执行 validation_result_cache.py run --repo . --name <handoff-validation-NNN> "
            f"--rvf-run-dir {run_dir} -- <argv...> 后再重跑 commit。",
            structured_validation_commands_status=assessment["structured_validation_commands_status"],
            index_record_names_without_structured_argv=assessment["index_record_names_without_structured_argv"],
        )
    outcomes = replay_structured_validation_commands(repo_root, run_dir)
    reassessment = validation_reuse_assessment(repo_root, run_dir)
    if not reassessment["validation_reusable"]:
        raise RvfLandHardGateFailure(
            RVF_LAND_EXIT_VALIDATION_FAILED,
            "validation_index_not_current_after_replay",
            "重跑后 run 的验证记录仍不全是「当前内容 + passed」（验证期间内容被改动，或 index 里有未重放的记录）。",
            validation_outcomes=outcomes,
            run_validation_index_summary=reassessment["run_validation_index_summary"],
        )
    return {
        "mode": "reran_structured_argv_through_validation_result_cache",
        "outcomes": outcomes,
        "candidate_content_manifest_sha256": reassessment["current_candidate_content_manifest_sha256"],
    }


def validation_summary_lines(validation: dict[str, Any]) -> list[str]:
    labels = {
        "reused_run_index": "复用 RVF run 验证记录，零执行",
        "hit": "缓存命中",
        "executed": "已执行",
    }
    return [
        f"- {outcome['name']}：{outcome['status']}（{labels.get(outcome.get('cache_status'), outcome.get('cache_status'))}）"
        for outcome in validation["outcomes"]
    ] or ["- 无验证记录"]


def read_commit_message(message_file: str) -> str:
    raw = sys.stdin.read() if message_file == "-" else Path(message_file).expanduser().read_text(encoding="utf-8")
    message = raw.strip()
    if not message:
        raise ValueError("commit message 为空")
    unfilled = [marker for marker in RVF_LAND_COMMIT_MESSAGE_DRAFT_UNFILLED_MARKERS if marker in message]
    if unfilled:
        raise ValueError(f"commit message 仍含草稿占位：{', '.join(unfilled)}")
    return message


def command_commit(args: argparse.Namespace) -> int:
    run_dir_text = args.run_dir or os.environ.get(RVF_RUN_DIRECTORY_ENVIRONMENT_VARIABLE, "").strip()
    if not run_dir_text:
        raise ValueError(f"需要 --run-dir（或环境变量 {RVF_RUN_DIRECTORY_ENVIRONMENT_VARIABLE}）")
    run_dir = Path(run_dir_text).expanduser().resolve()
    message = read_commit_message(args.message_file)
    plan = load_prepare_plan(run_dir)
    repo_root = Path(repository_identity(Path(args.repo).expanduser().resolve())[0]).resolve()
    if plan.get("cross_worktree_gate_failed"):
        raise RvfLandHardGateFailure(
            RVF_LAND_EXIT_CROSS_WORKTREE_HANDOFF,
            "handoff_targets_another_worktree",
            "prepare 判定为跨 worktree handoff；改走 --cross-worktree 模式重跑 prepare。",
        )
    if Path(plan["repo_root"]).resolve() != repo_root or plan.get("head_oid_at_prepare") != head_oid(repo_root):
        raise RvfLandHardGateFailure(
            RVF_LAND_EXIT_PREPARE_PLAN_MISSING_OR_STALE,
            "prepare_plan_stale",
            "当前 worktree 或 HEAD 与 prepare 时不一致；重跑 rvf_land.py prepare。",
            plan_repo_root=plan["repo_root"],
            plan_head_oid=plan.get("head_oid_at_prepare"),
        )
    extra_paths = sorted(
        set(plan.get("extra_paths") or []) | {repo_relative_path(repo_root, path) for path in args.extra_path}
    )
    stageable = set(plan.get("stageable_path_universe") or []) | set(extra_paths)
    dirty_entries = [entry for entry in repo_snapshot(repo_root).get("dirty_paths") or [] if isinstance(entry, dict)]
    in_scope, unrelated = partition_dirty_entries(dirty_entries, stageable)
    failures = reviewed_scope_dirty_gate_failures(dirty_entries, stageable, in_scope, unrelated)
    if failures:
        raise failures[0]

    validation = ensure_validation_satisfies_evidence_gate(repo_root, run_dir)

    stage_paths = sorted({path for entry in in_scope for path in (entry.get("path"), entry.get("old_path")) if path})
    if stage_paths:
        run_git(repo_root, "add", "-A", "--", *stage_paths, check=True)
    has_staged_changes = run_git(repo_root, "diff", "--cached", "--quiet").returncode == 1
    commit_oid: str | None = None
    if has_staged_changes:
        diff_check = run_git(repo_root, "diff", "--cached", "--check")
        if diff_check.returncode != 0:
            run_git(repo_root, "reset", "-q", "--", *stage_paths)
            raise RvfLandHardGateFailure(
                RVF_LAND_EXIT_STAGED_DIFF_CHECK_FAILED,
                "staged_diff_check_failed",
                "git diff --cached --check 未通过；已撤销本次 stage。",
                diff_check_output=diff_check.stdout[-6000:],
            )
        final_message = message.replace(
            RVF_LAND_COMMIT_MESSAGE_VALIDATION_SUMMARY_PLACEHOLDER, "\n".join(validation_summary_lines(validation))
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
            handle.write(final_message + "\n")
            message_path = Path(handle.name)
        try:
            committed = run_git(repo_root, "commit", "-q", "-F", str(message_path))
        finally:
            message_path.unlink(missing_ok=True)
        if committed.returncode != 0:
            raise RvfLandHardGateFailure(
                RVF_LAND_EXIT_GIT_COMMIT_FAILED,
                "git_commit_failed",
                "git commit 失败（stage 保留，便于排查）。",
                git_output=(committed.stdout + committed.stderr)[-6000:],
            )
        commit_oid = head_oid(repo_root)

    worktree_clean = not run_git(repo_root, "status", "--porcelain=v1", "-uall").stdout.strip()
    # 零执行复用 / 重放验证都以「提交前后内容摘要不变」为前提；commit 会跑仓库 hooks，
    # pre-commit 改写文件（纳入提交或留在工作树）即打破前提。此时提交的字节未经验证：
    # 不封窗（否则下一次 Stop 把它当已审跳过）、不写 evidence；commit 已发生，如实报告不回滚。
    content_digest_after_commit = candidate_content_manifest_sha256(repo_root)
    content_changed_during_commit = content_digest_after_commit != validation["candidate_content_manifest_sha256"]
    candidate_evidence: dict[str, Any]
    if content_changed_during_commit:
        round_baseline_seal: dict[str, Any] = {
            "sealed": False,
            "reason": "skipped_content_changed_during_commit",
        }
        candidate_evidence = {
            "status": "skipped_content_changed_during_commit",
            "reason": "commit 期间内容摘要变化（通常是 pre-commit hook 改写文件），提交的字节未经验证。",
        }
    else:
        # 封窗与 evidence 以函数方式调用：与 Stop hook 读同一个状态根（``resolved_rvf_*``），
        # 这是下一次 Stop 判定「无候选」的前提——seal 写一个根、Stop 读另一个根正是旧事故的成因。
        round_baseline_seal = seal_round_baseline_to_head(str(repo_root))
        if not worktree_clean:
            candidate_evidence = {
                "status": "skipped_worktree_not_clean_after_commit",
                "reason": "commit 后工作树不 clean，无法把已审内容绑定到唯一 HEAD/tree。",
            }
        else:
            try:
                candidate_evidence = write_evidence(repo_root, run_dir)
            except CandidateEvidenceNotApplicable as exc:
                candidate_evidence = {"status": "skipped_not_applicable", "reason": str(exc)}
            except (OSError, RuntimeError, ValueError) as exc:
                candidate_evidence = {"status": "refused_fail_closed", "reason": str(exc)}
    if content_changed_during_commit:
        exit_code = RVF_LAND_EXIT_CANDIDATE_CONTENT_CHANGED_DURING_COMMIT
    elif worktree_clean:
        exit_code = RVF_LAND_EXIT_OK
    else:
        exit_code = RVF_LAND_EXIT_WORKTREE_NOT_CLEAN_AFTER_COMMIT
    result = {
        "status": "committed" if commit_oid else "nothing_to_commit",
        "exit_code": exit_code,
        "run_id": plan.get("run_id"),
        "run_dir": str(run_dir),
        "repo_root": str(repo_root),
        "commit_oid": commit_oid,
        "commit_subject": message.splitlines()[0] if commit_oid else None,
        "staged_paths": stage_paths,
        "validation": validation,
        "worktree_clean_after_commit": worktree_clean,
        "candidate_content_manifest_sha256_after_commit": content_digest_after_commit,
        "round_baseline_seal": round_baseline_seal,
        "candidate_evidence": candidate_evidence,
    }
    if content_changed_during_commit:
        result["reason_code"] = "candidate_content_changed_during_commit"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result["exit_code"]


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="rvf-land 单步化：prepare / commit。")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    prepare = subparsers.add_parser("prepare", help="intake + 硬门槛 + scoped diff / 验证复用判定 / commit 草稿。")
    source = prepare.add_mutually_exclusive_group(required=True)
    source.add_argument("--handoff", help="handoff markdown 文件路径。")
    source.add_argument("--handoff-text-file", help="包含粘贴 handoff 正文的临时文件。")
    source.add_argument("--stdin", action="store_true", help="从 stdin 读 handoff 正文（用单引号 heredoc）。")
    prepare.add_argument("--repo", default=".")
    prepare.add_argument("--run-dir", help="覆盖从 handoff 推断的 run 目录。")
    prepare.add_argument("--extra-path", action="append", default=[], help="reviewed scope 之外、确属本次工作的路径。")
    prepare.add_argument(
        "--cross-worktree",
        action="store_true",
        help="跨 worktree handoff（$rvf-handoff-intake / /rvf-handoff-commit）：同仓另一 worktree 不再判 exit 10。",
    )
    prepare.add_argument("--diff-max-lines", type=int, default=RVF_LAND_SCOPED_DIFF_DEFAULT_MAX_LINES)
    prepare.add_argument("--format", choices=["text", "json"], default="text")

    commit = subparsers.add_parser("commit", help="验证 → stage → commit → 封窗 → evidence，输出 JSON。")
    commit.add_argument("--run-dir", help=f"RVF run 目录；缺省回退到 {RVF_RUN_DIRECTORY_ENVIRONMENT_VARIABLE}。")
    commit.add_argument("--repo", default=".")
    commit.add_argument("--message-file", required=True, help="commit message 文件；'-' 表示从 stdin 读。")
    commit.add_argument("--extra-path", action="append", default=[], help="补修正触及的 reviewed scope 外路径。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        if args.subcommand == "prepare":
            return command_prepare(args)
        return command_commit(args)
    except RvfLandHardGateFailure as failure:
        print(json.dumps({"status": "refused", **failure.as_payload()}, ensure_ascii=False, indent=2))
        failure_output = failure.details.get("failure_output")
        if failure_output:
            print(failure_output, file=sys.stderr)
        return failure.exit_code
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"rvf_land: {exc}", file=sys.stderr)
        return RVF_LAND_EXIT_USAGE_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
