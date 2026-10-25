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
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rvf_pyroot  # noqa: E402,F401
from rvf_handoff_intake import (  # noqa: E402
    build_payload,
    git_environment_with_user_configuration_without_repository_location_overrides,
    parse_sections,
    repo_snapshot,
)
from seal_round_baseline_to_head import seal_round_baseline_to_head  # noqa: E402
from validation_result_cache import (  # noqa: E402
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
    candidate_evidence_qualification,
    merge_dispositions_errors_that_would_refuse_candidate_evidence,
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
# commit 已发生，但 commit 期间 pre-commit 等 hook 改变了提交内容：内容摘要不再等于验证时的摘要
# （``reason_code=candidate_content_changed_during_commit``），或提交实际包含了 stage 集合外的路径
# （``reason_code=committed_paths_outside_stage_set``）。提交进去的内容未经审查/验证，因此不封窗、不写 evidence。
RVF_LAND_EXIT_CANDIDATE_CONTENT_CHANGED_DURING_COMMIT = 26
# 提交前拒绝：evidence 会走合并处置强校验（reviewer 报过 issue 或处置工件存在）而处置缺失/不合法/过期——
# 放行则提交后 evidence 必然 refused_fail_closed。未 stage、未 commit。
RVF_LAND_EXIT_MERGE_DISPOSITIONS_CHECK_FAILED_BEFORE_COMMIT = 27
RVF_LAND_EXIT_CANDIDATE_EVIDENCE_QUALIFICATION_FAILED_BEFORE_COMMIT = 28

RVF_LAND_PREPARE_PLAN_ARTIFACT_NAME = "rvf-land-prepare-plan.json"
RVF_LAND_PREPARE_PLAN_SCHEMA_NAME = "rvf-land-prepare-plan"
RVF_LAND_PREPARE_PLAN_SCHEMA_VERSION = 2
# 显式开关：只提交 stage 集合、把 scope 外脏文件原样留在工作树（不 stage、不 revert）、跳过 evidence。
# 主会话 worktree 有并行 WIP 是常态（跨 worktree 模式）；不加开关时 scope 外脏文件仍是 exit 12。
LEAVE_UNRELATED_DIRTY_PATHS_FLAG = "--commit-staged-scope-only-and-leave-unrelated-dirty-paths"
# 零执行复用成立 ⇔ 当前工作树全量内容摘要等于 RVF run 验证时的摘要（同 worktree 模式下还额外依赖
# 「修复就应用在当前 worktree」）。跨 worktree 模式只有在这一条成立时才证明「提交进去的就是 RVF 审过并
# 验证过的字节」，才允许封窗；否则主会话在 fork 之后落在同一文件的改动会被封窗一并宣称已审。
VALIDATION_MODE_REUSED_RUN_VALIDATION_INDEX_WITH_ZERO_EXECUTION = "reused_run_validation_index_with_zero_execution"
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
# 自动 stage 集合的上界只取 scope contract 的这三个字段（reviewed 主审边界）。evidence 的落地 delta
# 收敛判据还额外包含 ``background_files``（``REVIEWED_SCOPE_PATH_UNIVERSE_FIELD_NAMES``），那是「落地
# delta 碰到它不算越界」的判据，不是「它属于本次工作」的判据，不能拿来决定替谁提交。
LAND_AUTOMATICALLY_STAGEABLE_SCOPE_CONTRACT_FIELD_NAMES = ("primary_files", "fix_allowlist", "task_change_paths")
# 不得自动 stage 的 scope contract 字段：其他会话 / 未归属的脏文件。显式出现在上面三个字段里的路径例外。
LAND_NOT_AUTOMATICALLY_STAGEABLE_SCOPE_CONTRACT_FIELD_NAMES = ("protected_files", "background_files")


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


def run_git(repo_root: Path, *arguments: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=git_environment_with_user_configuration_without_repository_location_overrides(),
    )
    if check and completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"git {arguments[0]} failed")
    return completed


def head_oid(repo_root: Path) -> str | None:
    completed = run_git(repo_root, "rev-parse", "--verify", "HEAD")
    return completed.stdout.strip() if completed.returncode == 0 else None


def paths_changed_by_commit_relative_to_its_parent(repo_root: Path, commit_oid: str) -> set[str]:
    # ``--no-renames``：rename 拆成删旧、加新两端，与 stage 集合（新旧两端都列入）同口径；``--root``：首个提交没有父提交。
    listed = run_git(
        repo_root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "--no-renames", "-z", commit_oid, check=True
    )
    return {path for path in listed.stdout.split("\0") if path}


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
    最新 attempt 决定完成状态；实际应用过的各次补丁共同决定改动路径。
    """
    artifacts = run_dir / "artifacts"
    titles: dict[str, str] = {}
    for issue_path in sorted((artifacts / "fix-issues").glob("*.json")):
        issue = read_json_object_or_none(issue_path)
        if issue and isinstance(issue.get("issue_id"), str):
            titles[issue["issue_id"]] = str(issue.get("title") or "")
    chosen: dict[str, tuple[tuple[int, str], dict[str, Any]]] = {}
    applied_paths_by_issue: dict[str, set[str]] = {}
    confirmed_paths_by_issue: dict[str, set[str]] = {}
    attempts_root = artifacts / "fix-attempts"
    for attempt_directory in sorted(attempts_root.iterdir()) if attempts_root.is_dir() else []:
        result_path = attempt_directory / "result.json"
        result = read_json_object_or_none(result_path) or {}
        attempt = read_json_object_or_none(attempt_directory / "attempt.json") or {}
        issue_id = attempt.get("issue_id") or result.get("issue_id")
        if not isinstance(issue_id, str):
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
            "issue_id": issue_id,
            "title": titles.get(issue_id, ""),
            "fix_status": result.get("status"),
            "fix_applied_to_worktree": applied,
            "changed_paths": list(dict.fromkeys(fix_patch_changed_paths)),
            "confirmed_acknowledged_paths": attempt.get("confirmed_acknowledged_protected_or_background_paths") or [],
        }
        if applied:
            applied_paths_by_issue.setdefault(issue_id, set()).update(fix_patch_changed_paths)
            confirmed_paths_by_issue.setdefault(issue_id, set()).update(disposition["confirmed_acknowledged_paths"])
        # 完成状态不能让旧 applied attempt 遮住更新的失败；正式资格由结构化完成检查裁定。
        rank = (0, str(attempt.get("created_at") or ""))
        previous = chosen.get(issue_id)
        if previous is None or rank > previous[0]:
            chosen[issue_id] = (rank, disposition)
    for issue_id, title in titles.items():
        if issue_id not in chosen:
            chosen[issue_id] = (
                (0, ""),
                {
                    "issue_id": issue_id,
                    "title": title,
                    "fix_status": None,
                    "fix_applied_to_worktree": False,
                    "changed_paths": [],
                },
            )
    for issue_id, (_rank, disposition) in chosen.items():
        # 完成结论取最新 attempt；路径与确认只取历史实际 applied 的补丁，不能被后续误报或失败抹掉。
        disposition["changed_paths"] = sorted(applied_paths_by_issue.get(issue_id, set()))
        disposition["confirmed_acknowledged_paths"] = sorted(confirmed_paths_by_issue.get(issue_id, set()))
    return [chosen[issue_id][1] for issue_id in sorted(chosen)]


def reviewed_scope_contract(run_dir: Path) -> dict[str, Any] | None:
    return read_json_object_or_none(run_dir / "artifacts" / "inputs" / "scope.contract.json")


def scope_contract_path_list(contract: dict[str, Any], field_names: tuple[str, ...]) -> set[str]:
    return {
        path
        for field_name in field_names
        for path in contract.get(field_name) or []
        if isinstance(path, str) and path
    }


def path_matches_scope_contract_path_pattern(path: str, patterns: set[str]) -> bool:
    """与 ``rvf_fix_attempt._prefix_matches`` 同口径：精确路径，或以 ``/`` 结尾 / 目录名形式的前缀。"""
    for raw_pattern in patterns:
        pattern = raw_pattern.strip().replace("\\", "/").lstrip("/")
        if not pattern:
            continue
        if path == pattern.rstrip("/") or path.startswith(pattern.rstrip("/") + "/"):
            return True
    return False


def land_stageable_path_universe(
    intake: dict[str, Any], contract: dict[str, Any] | None, dispositions: list[dict[str, Any]]
) -> list[str]:
    """允许**自动** stage 的路径全集 = reviewed 主审边界 ∪ 已合回 fix 的改动路径，再剔除不得自动 stage 的路径。

    reviewed 主审边界：scope contract 给得出路径全集时只用它的 ``primary_files ∪ fix_allowlist ∪
    task_change_paths``；handoff 的 ``reviewed_scope_paths`` 是从粘贴正文/manifest 解析出来的，不能把上界撑到
    contract 之外，否则提交会越过 evidence 判据、而 evidence 拒绝发生在提交之后。contract 缺失或全集为空时
    才退回 handoff 的 ``reviewed_scope_paths``。

    **background 不自动 stage**：``background_files`` 是 prepare 时其他会话 / 未归属的脏文件。evidence 的落地
    delta 收敛判据把它算进路径全集（``reviewed_scope_path_universe``，既有口径不动），但把它自动 stage 等于
    替别人提交 WIP、再封窗宣称已审。确属本次工作（例如本会话用 shell 改的文件归属落空）时用
    ``--extra-path`` 显式纳入。

    不得自动 stage = contract ``protected_files ∪ background_files`` ∪ handoff 标注的 protected 路径。例外：
    显式出现在 contract 主审边界里的路径照常可 stage——``rvf scope --include`` 把 background 路径并入
    ``primary_files`` 时不会把它从 background / protected 里删掉；这与 fix attempt 的口径一致（fix_allowlist
    内的路径不受 protected 拦截，protected 只拦边界之外的路径）。handoff 标注的 protected 路径没有例外。

    已合回 fix 的补丁路径照常并入；冻结全集之外的路径由同源资格检查区分合法已应用扩展与未审追加，
    ``stageable_dirty_paths_outside_candidate_evidence_landing_universe`` 仅展示这份差集。
    """
    contract_has_path_universe = contract is not None and bool(reviewed_scope_path_universe(contract))
    reviewed_primary_boundary = (
        scope_contract_path_list(contract, LAND_AUTOMATICALLY_STAGEABLE_SCOPE_CONTRACT_FIELD_NAMES)
        if contract is not None
        else set()
    )
    universe = (
        set(reviewed_primary_boundary)
        if contract_has_path_universe
        else set(intake.get("reviewed_scope_paths") or [])
    )
    for disposition in dispositions:
        universe.update(disposition["changed_paths"])
    contract_not_automatically_stageable_patterns = (
        scope_contract_path_list(contract, LAND_NOT_AUTOMATICALLY_STAGEABLE_SCOPE_CONTRACT_FIELD_NAMES)
        if contract is not None
        else set()
    )
    handoff_protected = {
        path
        for path in (intake.get("intake_hints") or {}).get("protected_paths") or []
        if isinstance(path, str)
    }
    confirmed_applied_paths = {
        path for disposition in dispositions
        for path in disposition.get("confirmed_acknowledged_paths", [])
        if path in disposition["changed_paths"]
    }
    return sorted(
        path
        for path in universe
        if path
        and path not in handoff_protected
        and (
            path in reviewed_primary_boundary
            or path in confirmed_applied_paths
            or not path_matches_scope_contract_path_pattern(path, contract_not_automatically_stageable_patterns)
        )
    )


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
    *,
    leave_unrelated_dirty_paths_uncommitted: bool,
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
    if unrelated and not leave_unrelated_dirty_paths_uncommitted:
        failures.append(
            RvfLandHardGateFailure(
                RVF_LAND_EXIT_UNRELATED_DIRTY_PATHS_WOULD_REMAIN,
                "unrelated_dirty_paths_would_remain_after_commit",
                "存在 reviewed scope 之外的脏文件：commit 后工作树不会 clean，evidence 无法绑定唯一 HEAD。"
                "若它们属于本次工作（例如测试注册、补修正的直接 fallout），用 --extra-path 显式纳入；"
                f"若是别人的并行改动且必须保留，加 {LEAVE_UNRELATED_DIRTY_PATHS_FLAG} 只提交 stage 集合"
                "（evidence 跳过）；否则先把它们移出本 worktree 再重跑。",
                paths=dirty_paths_outside(unrelated, stageable_paths),
            )
        )
    return failures


def worktree_path_content_fingerprint(repo_root: Path, path: str) -> str:
    """单个路径的工作树内容指纹（不含 git 状态）：普通文件按字节 + 可执行位，符号链接按目标，不存在记 absent。"""
    filesystem_path = repo_root / path
    try:
        metadata = filesystem_path.lstat()
    except FileNotFoundError:
        return "absent"
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink:" + sha256_hex(os.fsencode(os.readlink(filesystem_path)))
    if stat.S_ISREG(metadata.st_mode):
        executable_marker = "x" if metadata.st_mode & 0o111 else "-"
        return f"file{executable_marker}:" + sha256_hex(filesystem_path.read_bytes())
    return "other"


def dirty_path_fingerprints(repo_root: Path, dirty_entries: list[dict[str, str]]) -> dict[str, str]:
    """每个脏路径的「porcelain 状态 + 内容指纹」：内容变了、或被别人 ``git add`` 过（状态变了）都会反映出来。"""
    fingerprints: dict[str, str] = {}
    for entry in dirty_entries:
        for path in sorted(dirty_entry_paths(entry)):
            fingerprints[path] = f"{entry.get('status')}|{worktree_path_content_fingerprint(repo_root, path)}"
    return fingerprints


def dirty_paths_changed_between_fingerprints(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


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


def land_candidate_evidence_qualification(
    repo: Path, run_dir: Path, in_scope: list[dict[str, str]], unrelated: list[dict[str, str]],
    *, leave_unrelated: bool, cross_worktree: bool,
) -> dict[str, Any]:
    if leave_unrelated and unrelated:
        return {"status": "skipped_unrelated_dirty_paths_left_uncommitted_by_explicit_flag",
                "reason_code": "unrelated_dirty_paths_explicitly_preserved"}
    contract = reviewed_scope_contract(run_dir)
    if cross_worktree and contract and Path(str(contract.get("repo", ""))).resolve() != repo:
        return {"status": "skipped_cross_worktree_candidate_identity", "reason_code": "cross_worktree_contract_has_different_subject"}
    paths = {path for entry in in_scope for path in dirty_entry_paths(entry)}
    return candidate_evidence_qualification(repo, run_dir, pending_stage_paths=paths)


def command_check_evidence(args: argparse.Namespace) -> int:
    repo = Path(repository_identity(Path(args.repo).expanduser().resolve())[0]).resolve()
    run_dir = Path(args.run_dir).expanduser().resolve(strict=True)
    contract = reviewed_scope_contract(run_dir)
    handoff = run_dir / "artifacts/handoff.md"
    handoff_text = handoff.read_text(encoding="utf-8") if handoff.is_file() and not handoff.is_symlink() else ""
    intake = build_payload(handoff_text, repo, handoff)
    if intake.get("run_identity_status") == "invalid":
        raise ValueError("交接 run 身份不符：" + "；".join(intake.get("run_identity_errors", [])))
    stageable = set(land_stageable_path_universe(intake, contract, applied_fix_attempt_dispositions(run_dir)))
    stageable.update(repo_relative_path(repo, path) for path in args.extra_path)
    entries = repo_snapshot(repo).get("dirty_paths") or []
    in_scope, unrelated = partition_dirty_entries(entries, stageable)
    qualification = land_candidate_evidence_qualification(repo, run_dir, in_scope, unrelated,
        leave_unrelated=args.commit_staged_scope_only_and_leave_unrelated_dirty_paths, cross_worktree=False)
    result = {"schema": "rvf-candidate-evidence-qualification", "schema_version": 1,
              "repo_root": str(repo), "run_dir": str(run_dir), "run_id": (contract or {}).get("run_id"), **qualification}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return RVF_LAND_EXIT_CANDIDATE_EVIDENCE_QUALIFICATION_FAILED_BEFORE_COMMIT if qualification["status"] == "refused_fail_closed" else 0


def persist_land_commit_result(run_dir: Path, repo: Path, result: dict[str, Any]) -> None:
    plan = read_json_object_or_none(prepare_plan_artifact_path(run_dir)) or {}
    payload = {"schema": "rvf-land-commit-execution-result", "schema_version": 1,
               "run_id": plan.get("run_id"), "run_dir": str(run_dir), "repo_root": str(repo),
               "head_oid_before_commit": plan.get("head_oid_at_prepare"), "head_oid_after_commit": head_oid(repo),
               "candidate_content_manifest_sha256": candidate_content_manifest_sha256(repo), "result": dict(result)}
    digest = sha256_hex(canonical_json_bytes(payload))
    path = run_dir / "artifacts/rvf-land-commit-execution-results" / f"{digest}.json"
    write_json_with_digest(path, payload)
    write_json_with_digest(run_dir / "artifacts/rvf-land-commit-result.json", payload)
    result.update({"land_commit_result_path": str(path), "land_commit_result_sha256": digest})


def build_prepare_report(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    text, handoff_path = read_handoff_text(args)
    repo_argument = Path(args.repo).expanduser().resolve()
    intake = build_payload(text, repo_argument, handoff_path)
    if intake.get("run_identity_status") == "invalid":
        raise ValueError("交接 run 身份不符：" + "；".join(intake.get("run_identity_errors", [])))
    if args.run_dir:
        selected_run_directory = Path(args.run_dir).expanduser().resolve()
        handoff_run_directory = intake.get("run_dir")
        if handoff_run_directory and Path(handoff_run_directory).resolve() != selected_run_directory:
            raise ValueError("显式 run 目录与交接绑定的 run 目录不符")
        if (selected_run_directory / "artifacts").is_dir():
            # 粘贴摘要可由显式目录补证，但其声明仍必须与该目录的正式身份一致。
            intake = build_payload(text, repo_argument, selected_run_directory / "artifacts/handoff.md")
            if intake.get("run_identity_status") == "invalid":
                raise ValueError("交接 run 身份不符：" + "；".join(intake.get("run_identity_errors", [])))
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
    gate_failures += reviewed_scope_dirty_gate_failures(
        dirty_entries,
        stageable,
        in_scope,
        unrelated,
        leave_unrelated_dirty_paths_uncommitted=args.commit_staged_scope_only_and_leave_unrelated_dirty_paths,
    )
    if run_dir is not None and (run_dir / "artifacts").is_dir() and intake.get("run_identity_status") != "verified":
        gate_failures.append(RvfLandHardGateFailure(
            RVF_LAND_EXIT_REVIEWED_SCOPE_CONTRADICTS_DIRTY_PATHS,
            "run_identity_unavailable",
            "精确 run 的 summary 或 scope contract 身份恢复可核验后重验；当前不生成正式计划。",
        ))

    validation = (
        validation_reuse_assessment(repo_root, run_dir)
        if run_dir is not None and (run_dir / "artifacts").is_dir() and intake.get("run_identity_status") == "verified"
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
        "leave_unrelated_dirty_paths_uncommitted": bool(
            args.commit_staged_scope_only_and_leave_unrelated_dirty_paths
        ),
        # 冻结范围之外的路径差集。资格检查另行证明已应用扩展；extra-path 不构成该证明。
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
        "candidate_evidence_qualification": (
            land_candidate_evidence_qualification(repo_root, run_dir, in_scope, unrelated,
                leave_unrelated=args.commit_staged_scope_only_and_leave_unrelated_dirty_paths,
                cross_worktree=args.cross_worktree) if run_dir is not None else None
        ),
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
    if (run_dir is not None and (run_dir / "artifacts").is_dir() and not cross_repository
            and intake.get("run_identity_status") == "verified"):
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
            "leave_unrelated_dirty_paths_uncommitted": bool(
                args.commit_staged_scope_only_and_leave_unrelated_dirty_paths
            ),
            # commit 在 stage 前与之比对，列出 prepare 之后变化过的路径（含 agent 自己的最小修正，
            # 也含并发改动 / 别人的 git add）；验证只会因内容变化而重跑，不会告诉 agent 是哪些路径变了。
            "dirty_path_fingerprints_at_prepare": dirty_path_fingerprints(repo_root, dirty_entries),
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
    lines.append("\n## 将要 stage 的路径（reviewed 主审边界 ∩ dirty；background / protected 不自动 stage）")
    lines += [f"- {path}" for path in report["stageable_dirty_paths"]] or ["- <无：没有可提交的改动，commit 只做验证/封窗/evidence>"]
    outside_evidence = report["stageable_dirty_paths_outside_candidate_evidence_landing_universe"]
    if outside_evidence:
        lines.append("以下路径在冻结范围之外；已声明且已应用的扩展可通过资格检查，其余情况在提交前拒绝。")
        lines += [f"  - {path}" for path in outside_evidence]
    qualification = report.get("candidate_evidence_qualification") or {}
    lines.append(f"\nEvidence 资格：{qualification.get('status')} / {qualification.get('reason_code')}")
    if qualification.get("reason"):
        lines.append(str(qualification["reason"]))
    if report["unrelated_dirty_paths"] and report["leave_unrelated_dirty_paths_uncommitted"]:
        lines.append(f"\n## scope 外脏文件：按 {LEAVE_UNRELATED_DIRTY_PATHS_FLAG} 原样保留，不 stage、不提交（evidence 跳过）")
        lines += [f"- {path}" for path in report["unrelated_dirty_paths"]]
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
            "mode": VALIDATION_MODE_REUSED_RUN_VALIDATION_INDEX_WITH_ZERO_EXECUTION,
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
    # 只认显式 --run-dir：reviewer 子进程与 Codex task shell 都带着 RVF_RUN_DIR（指向活 run），
    # 环境变量回退会把别处 shell 的 land 引到那个 run 上。
    run_dir = Path(args.run_dir).expanduser().resolve()
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
    dirty_entries = [entry for entry in repo_snapshot(repo_root).get("dirty_paths") or [] if isinstance(entry, dict)]
    dirty_paths_changed_since_prepare = dirty_paths_changed_between_fingerprints(
        plan.get("dirty_path_fingerprints_at_prepare") or {}, dirty_path_fingerprints(repo_root, dirty_entries)
    )
    try:
        result = land_commit_after_prepare_plan_checks(args, plan, run_dir, repo_root, message, dirty_entries)
    except RvfLandHardGateFailure as failure:
        failure.details.setdefault("dirty_paths_changed_since_prepare", dirty_paths_changed_since_prepare)
        raise
    result["dirty_paths_changed_since_prepare"] = dirty_paths_changed_since_prepare
    try:
        persist_land_commit_result(run_dir, repo_root, result)
    except (OSError, RuntimeError, ValueError) as exc:
        result.update({"exit_code": RVF_LAND_EXIT_USAGE_ERROR, "reason_code": "land_commit_result_persistence_failed",
                       "land_commit_result_persistence_error": str(exc)})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result["exit_code"]


def land_commit_after_prepare_plan_checks(
    args: argparse.Namespace,
    plan: dict[str, Any],
    run_dir: Path,
    repo_root: Path,
    message: str,
    dirty_entries: list[dict[str, str]],
) -> dict[str, Any]:
    extra_paths = sorted(
        set(plan.get("extra_paths") or []) | {repo_relative_path(repo_root, path) for path in args.extra_path}
    )
    leave_unrelated_dirty_paths_uncommitted = bool(
        args.commit_staged_scope_only_and_leave_unrelated_dirty_paths
        or plan.get("leave_unrelated_dirty_paths_uncommitted")
    )
    stageable = set(plan.get("stageable_path_universe") or []) | set(extra_paths)
    in_scope, unrelated = partition_dirty_entries(dirty_entries, stageable)
    failures = reviewed_scope_dirty_gate_failures(
        dirty_entries,
        stageable,
        in_scope,
        unrelated,
        leave_unrelated_dirty_paths_uncommitted=leave_unrelated_dirty_paths_uncommitted,
    )
    if failures:
        raise failures[0]
    unrelated_dirty_paths_left_uncommitted = dirty_paths_outside(unrelated, stageable)
    # 处置预检只在 evidence 真会写时才拦：按显式开关保留 scope 外脏文件时 commit 后工作树不 clean，
    # evidence 必然以 skipped_unrelated_dirty_paths_left_uncommitted_by_explicit_flag 跳过，预检拦下只会凭空多一道闸。
    evidence_will_be_skipped_for_unrelated_dirty_paths_left_uncommitted = bool(
        leave_unrelated_dirty_paths_uncommitted and unrelated_dirty_paths_left_uncommitted
    )
    merge_dispositions_errors = (
        ()
        if evidence_will_be_skipped_for_unrelated_dirty_paths_left_uncommitted
        else merge_dispositions_errors_that_would_refuse_candidate_evidence(repo_root, run_dir)
    )
    if merge_dispositions_errors:
        raise RvfLandHardGateFailure(
            RVF_LAND_EXIT_MERGE_DISPOSITIONS_CHECK_FAILED_BEFORE_COMMIT,
            "merge_dispositions_check_failed",
            "reviewer 报过 issue（或已有处置工件），但合并处置缺失/不合法/过期，提交后 evidence 必然 refused_fail_closed；"
            "用 `rvf merge write` 补齐处置（`rvf merge check` 复核）后重跑 commit。未 stage、未 commit。",
            merge_dispositions_errors=list(merge_dispositions_errors),
        )

    validation = ensure_validation_satisfies_evidence_gate(repo_root, run_dir)

    qualification = land_candidate_evidence_qualification(
        repo_root, run_dir, in_scope, unrelated, leave_unrelated=leave_unrelated_dirty_paths_uncommitted,
        cross_worktree=bool(plan.get("cross_worktree_mode")),
    )
    if qualification["status"] == "refused_fail_closed":
        raise RvfLandHardGateFailure(
            RVF_LAND_EXIT_CANDIDATE_EVIDENCE_QUALIFICATION_FAILED_BEFORE_COMMIT,
            qualification["reason_code"],
            "提交前 evidence 资格检查拒绝；未 stage、未 commit。按 qualification 原因修正后重跑。",
            candidate_evidence_qualification=qualification,
        )

    stage_paths = sorted({path for entry in in_scope for path in (entry.get("path"), entry.get("old_path")) if path})
    already_staged_deletions = {
        entry["path"] for entry in in_scope if entry.get("status", "").startswith("D")
        and not (repo_root / entry["path"]).exists() and not (repo_root / entry["path"]).is_symlink()
    }
    paths_needing_add = sorted(set(stage_paths) - already_staged_deletions)
    if paths_needing_add:
        staged = run_git(repo_root, "add", "-A", "--", *paths_needing_add)
        if staged.returncode != 0:
            raise RvfLandHardGateFailure(RVF_LAND_EXIT_GIT_COMMIT_FAILED, "git_stage_failed", "git add 失败；未 commit。",
                                         git_output=staged.stdout + staged.stderr)
    # 检查与提交都限定在 stage 集合：``commit --only`` 只提交列出的路径，别人预先 stage（或在
    # 本命令运行期间 git add）的 scope 外路径原样留在 index，既不进提交，也不影响这里的判定。
    has_staged_changes = (
        bool(stage_paths) and run_git(repo_root, "diff", "--cached", "--quiet", "--", *stage_paths).returncode == 1
    )
    commit_oid: str | None = None
    if has_staged_changes:
        diff_check = run_git(repo_root, "diff", "--cached", "--check", "--", *stage_paths)
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
            committed = run_git(repo_root, "commit", "-q", "--only", "-F", str(message_path), "--", *stage_paths)
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
    # ``commit --only`` 让 hook 跑在临时 index 上：pre-commit ``git add`` 了 stage 集合外的路径时，它照样进入本次提交，
    # 真实 index 却不含它（残留 D+?? 等状态）。这些路径的字节可以完全没变——内容摘要判据与「无关脏文件」判据都
    # 看不见——只能直接比对提交实际包含的路径。任何模式都查，不限于保留开关。
    committed_paths_outside_stage_set = (
        sorted(paths_changed_by_commit_relative_to_its_parent(repo_root, commit_oid) - set(stage_paths))
        if commit_oid
        else []
    )

    dirty_paths_remaining_after_commit = sorted(
        {
            path
            for entry in repo_snapshot(repo_root).get("dirty_paths") or []
            if isinstance(entry, dict)
            for path in dirty_entry_paths(entry)
        }
    )
    worktree_clean = not dirty_paths_remaining_after_commit
    # 开关只放行 commit 前就已列出的 scope 外脏文件；commit 之后多出来的脏路径照常算「不 clean」。
    dirty_paths_remaining_beyond_declared_unrelated = sorted(
        set(dirty_paths_remaining_after_commit)
        - (set(unrelated_dirty_paths_left_uncommitted) if leave_unrelated_dirty_paths_uncommitted else set())
    )
    # 零执行复用 / 重放验证都以「提交前后内容摘要不变」为前提；commit 会跑仓库 hooks，
    # pre-commit 改写文件（纳入提交或留在工作树）即打破前提。此时提交的字节未经验证：
    # 不封窗（否则下一次 Stop 把它当已审跳过）、不写 evidence；commit 已发生，如实报告不回滚。
    content_digest_after_commit = candidate_content_manifest_sha256(repo_root)
    content_changed_during_commit = content_digest_after_commit != validation["candidate_content_manifest_sha256"]
    candidate_evidence: dict[str, Any]
    if committed_paths_outside_stage_set:
        # 与内容被改写同一处置：提交里混进了未经审查、未经验证的路径，不封窗、不写 evidence，如实报告不回滚。
        round_baseline_seal: dict[str, Any] = {
            "sealed": False,
            "reason": "skipped_committed_paths_outside_stage_set",
        }
        candidate_evidence = {
            "status": "skipped_committed_paths_outside_stage_set",
            "reason": "commit 期间 hook 把 stage 集合外的路径加入了本次提交（真实 index 未含它们），这些路径未经审查。",
        }
    elif content_changed_during_commit:
        round_baseline_seal = {
            "sealed": False,
            "reason": "skipped_content_changed_during_commit",
        }
        candidate_evidence = {
            "status": "skipped_content_changed_during_commit",
            "reason": "commit 期间内容摘要变化（通常是 pre-commit hook 改写文件），提交的字节未经验证。",
        }
    else:
        if (
            plan.get("cross_worktree_mode")
            and validation["mode"] != VALIDATION_MODE_REUSED_RUN_VALIDATION_INDEX_WITH_ZERO_EXECUTION
        ):
            # 跨 worktree：tracker 的已审单元按 worktree 记账，fork 里审过的内容在当前 worktree 不算已审；
            # 只有「当前内容 = RVF 验证过的内容」时封窗才不会把主会话 fork 之后的改动一并宣称已审。
            # 不封窗的代价只是下一次 Stop 可能再问一次是否 review 这段提交。
            # EXPERIMENT_NODE: 跨 worktree 封窗的证明粒度——现为「全量内容摘要一致」；可实验改为 stage 集合逐路径与 RVF worktree 比对，放宽主会话有无关并行 WIP 时的封窗
            round_baseline_seal = {
                "sealed": False,
                "reason": "skipped_cross_worktree_landed_content_not_proven_identical_to_rvf_validated_content",
            }
        else:
            # 封窗与 evidence 以函数方式调用：与 Stop hook 读同一个状态根（``resolved_rvf_*``），
            # 这是下一次 Stop 判定「无候选」的前提——seal 写一个根、Stop 读另一个根正是旧事故的成因。
            round_baseline_seal = seal_round_baseline_to_head(str(repo_root))
        if dirty_paths_remaining_beyond_declared_unrelated:
            candidate_evidence = {
                "status": "skipped_worktree_not_clean_after_commit",
                "reason": "commit 后工作树不 clean，无法把已审内容绑定到唯一 HEAD/tree。",
            }
        elif not worktree_clean:
            candidate_evidence = {
                "status": "skipped_unrelated_dirty_paths_left_uncommitted_by_explicit_flag",
                "reason": f"按 {LEAVE_UNRELATED_DIRTY_PATHS_FLAG} 保留了 scope 外脏文件，工作树不 clean，"
                "evidence 无法绑定唯一 HEAD/tree。",
            }
        elif qualification["status"] == "skipped_cross_worktree_candidate_identity":
            candidate_evidence = qualification
        else:
            try:
                candidate_evidence = write_evidence(repo_root, run_dir)
            except CandidateEvidenceNotApplicable as exc:
                candidate_evidence = {"status": "skipped_not_applicable", "reason": str(exc)}
            except (OSError, RuntimeError, ValueError) as exc:
                candidate_evidence = {"status": "refused_fail_closed", "reason": str(exc),
                                      "reason_code": getattr(exc, "reason_code", "candidate_evidence_write_failed_after_commit")}
    if committed_paths_outside_stage_set or content_changed_during_commit:
        exit_code = RVF_LAND_EXIT_CANDIDATE_CONTENT_CHANGED_DURING_COMMIT
    elif dirty_paths_remaining_beyond_declared_unrelated:
        exit_code = RVF_LAND_EXIT_WORKTREE_NOT_CLEAN_AFTER_COMMIT
    else:
        exit_code = RVF_LAND_EXIT_OK
    result = {
        "status": "committed" if commit_oid else "nothing_to_commit",
        "exit_code": exit_code,
        "run_id": plan.get("run_id"),
        "run_dir": str(run_dir),
        "repo_root": str(repo_root),
        "cross_worktree_mode": bool(plan.get("cross_worktree_mode")),
        "commit_oid": commit_oid,
        "commit_subject": message.splitlines()[0] if commit_oid else None,
        "staged_paths": stage_paths,
        "committed_paths_outside_stage_set": committed_paths_outside_stage_set,
        "unrelated_dirty_paths_left_uncommitted": (
            unrelated_dirty_paths_left_uncommitted if leave_unrelated_dirty_paths_uncommitted else []
        ),
        "validation": validation,
        "worktree_clean_after_commit": worktree_clean,
        "dirty_paths_remaining_beyond_declared_unrelated": dirty_paths_remaining_beyond_declared_unrelated,
        "candidate_content_manifest_sha256_after_commit": content_digest_after_commit,
        "round_baseline_seal": round_baseline_seal,
        "candidate_evidence": candidate_evidence,
        "candidate_evidence_qualification": qualification,
    }
    if committed_paths_outside_stage_set:
        result["reason_code"] = "committed_paths_outside_stage_set"
    elif content_changed_during_commit:
        result["reason_code"] = "candidate_content_changed_during_commit"
    return result


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="rvf-land 单步化：prepare / commit。")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    prepare = subparsers.add_parser("prepare", help="intake + 硬门槛 + scoped diff / 验证复用判定 / commit 草稿。")
    source = prepare.add_mutually_exclusive_group(required=True)
    source.add_argument("--handoff", help="handoff markdown 文件路径。")
    source.add_argument("--handoff-text-file", help="包含粘贴 handoff 正文的临时文件。")
    source.add_argument("--stdin", action="store_true", help="从 stdin 读 handoff 正文（用单引号 heredoc）。")
    prepare.add_argument("--repo", default=".")
    prepare.add_argument("--run-dir", help="显式指定本轮 run 目录，须与 handoff 身份一致。")
    prepare.add_argument("--extra-path", action="append", default=[], help="reviewed scope 之外、确属本次工作的路径。")
    prepare.add_argument(
        "--cross-worktree",
        action="store_true",
        help="跨 worktree handoff（$rvf-handoff-intake / /rvf-handoff-commit）：同仓另一 worktree 不再判 exit 10。",
    )
    prepare.add_argument(
        LEAVE_UNRELATED_DIRTY_PATHS_FLAG,
        action="store_true",
        help="scope 外脏文件（别人的并行改动）原样留在工作树：不判 exit 12、不 stage、不提交，evidence 跳过。"
        "prepare 带上即写进 plan，commit 自动沿用。",
    )
    prepare.add_argument("--diff-max-lines", type=int, default=RVF_LAND_SCOPED_DIFF_DEFAULT_MAX_LINES)
    prepare.add_argument("--format", choices=["text", "json"], default="text")

    commit = subparsers.add_parser("commit", help="验证 → stage → commit → 封窗 → evidence，输出 JSON。")
    commit.add_argument("--run-dir", required=True, help="RVF run 目录（prepare 输出的 run_dir）；只认显式参数。")
    commit.add_argument("--repo", default=".")
    commit.add_argument("--message-file", required=True, help="commit message 文件；'-' 表示从 stdin 读。")
    commit.add_argument("--extra-path", action="append", default=[], help="补修正触及的 reviewed scope 外路径。")
    commit.add_argument(
        LEAVE_UNRELATED_DIRTY_PATHS_FLAG,
        action="store_true",
        help="同 prepare 的同名开关（prepare 已带上时可省）。",
    )
    qualification = subparsers.add_parser("check-evidence", help="完整 evidence 资格 dry-run：不 stage、不写 run 或 evidence。")
    qualification.add_argument("--repo", default=".")
    qualification.add_argument("--run-dir", required=True)
    qualification.add_argument("--extra-path", action="append", default=[])
    qualification.add_argument(LEAVE_UNRELATED_DIRTY_PATHS_FLAG, action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        if args.subcommand == "prepare":
            return command_prepare(args)
        if args.subcommand == "check-evidence":
            return command_check_evidence(args)
        return command_commit(args)
    except RvfLandHardGateFailure as failure:
        result = {"status": "refused", **failure.as_payload()}
        if args.subcommand == "commit":
            run_dir = Path(args.run_dir).expanduser().resolve()
            plan = read_json_object_or_none(prepare_plan_artifact_path(run_dir)) or {}
            try:
                repo = Path(repository_identity(Path(args.repo).expanduser().resolve())[0]).resolve()
                if plan.get("repo_root") == str(repo):
                    persist_land_commit_result(run_dir, repo, result)
            except (OSError, RuntimeError, ValueError) as exc:
                result["land_commit_result_persistence_error"] = str(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        failure_output = failure.details.get("failure_output")
        if failure_output:
            print(failure_output, file=sys.stderr)
        return failure.exit_code
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"rvf_land: {exc}", file=sys.stderr)
        return RVF_LAND_EXIT_USAGE_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
