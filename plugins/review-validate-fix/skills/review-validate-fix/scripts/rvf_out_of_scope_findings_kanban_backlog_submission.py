#!/usr/bin/env python3
"""把 RVF 发现的「与本轮工作不紧密相关」的真实问题提交到 Kanban backlog。

约定（2026-10-04 开发者修订）：范围外问题照常通过 review、照常被合并采纳；到 validate-fix 阶段
**只验证、不修**，验证属实后由 validator 经 ``rvf fix stop --status
validated_and_submitted_to_kanban_backlog --kanban-backlog-finding-file <json>`` 提交到这里。
fixer 修复途中顺带发现的其他范围外问题、rvf-land 阶段主会话发现的问题，也走同一个提交入口。

两个入口：
- 库函数 ``submit_out_of_scope_findings_to_kanban_backlog``：供 ``rvf fix stop`` 进程内调用；
- CLI ``submit``（即 ``rvf backlog submit``）：供 rvf-land 阶段直接提交，以及重试 pending。

查重分两层：
1. 本模块的本地指纹去重：只拦完全重复（同仓、同路径、同一段规范化后的问题描述）；
2. 看板 dedicated support agent 的关系分诊：看板侧能力，尚未存在，见下方 INTEGRATION-POINT。
   在它上线之前，每张卡都带 ``KANBAN_SUPPORT_AGENT_RELATION_TRIAGE_STATUS=not_triaged`` 标记，
   由卡片自己的 agent 开工前先查重；support agent 上线后补查这些卡。

失败语义：Kanban 不可用永远不阻断 handoff ⇒ 写持久 pending 记录（``$RVF_HOME`` 下），下次任何一次提交先
重试本仓的 pending。输入 JSON 不合法 ⇒ ``OutOfScopeFindingInputInvalid``（调用方据此在写任何东西之前拒绝）。

持久性不变量：非预演时，每条通过校验的 finding 在函数返回前**恰好**留下一条持久事实——created（ledger 已记）、
pending（pending 文件 + ledger 已记）、duplicate 或 skipped；本地落盘失败（ledger / pending 写不进去）就抛出，
调用方据此不得把 attempt / issue 推进到「已提交」终态。建卡前先落 pending intent，建成后再升级为 created。
只读预演（``preview_without_creating``）不写 pending、不写 ledger、不写 run 工件，失败如实返回。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
import cline_kanban_client  # noqa: E402
from core.session_scope_allocation.scope_marker_identity_key import (  # noqa: E402
    resolve_repository_identity_for_scope_marker_key,
)
from validation_result_cache import write_json_with_digest  # noqa: E402


OUT_OF_SCOPE_FINDINGS_KANBAN_BACKLOG_LEDGER_DIRECTORY_NAME = "out-of-scope-findings-submitted-to-kanban-backlog"
PENDING_KANBAN_TASK_PROMPTS_DIRECTORY_NAME = "pending-task-prompts-awaiting-kanban-runtime"
RUN_ARTIFACT_RELATIVE_PATH = Path("artifacts/out-of-scope-findings-kanban-backlog-submissions.json")

# 卡片上的机器可读标记（开发者要求：卡上明确写「RVF 发现」；未经看板查重时再加未查重标记）。
KANBAN_TASK_TITLE_PREFIX_FOR_RVF_FINDING = "[RVF 发现]"
KANBAN_TASK_ORIGIN_MARKER_LINE = "KANBAN_TASK_ORIGIN=rvf_validated_out_of_scope_finding"
KANBAN_SUPPORT_AGENT_RELATION_TRIAGE_NOT_TRIAGED_MARKER_LINE = "KANBAN_SUPPORT_AGENT_RELATION_TRIAGE_STATUS=not_triaged"

FINDING_SOURCE_VALIDATE_ONLY_ATTEMPT_VALIDATOR = "validate_only_attempt_validator"
FINDING_SOURCE_VALIDATE_THEN_FIX_ATTEMPT_FIXER = "validate_then_fix_attempt_fixer"
FINDING_SOURCE_MAIN_SESSION_WITHOUT_VALIDATOR = "main_session_without_validator"
FINDING_SOURCES = (
    FINDING_SOURCE_VALIDATE_ONLY_ATTEMPT_VALIDATOR,
    FINDING_SOURCE_VALIDATE_THEN_FIX_ATTEMPT_FIXER,
    FINDING_SOURCE_MAIN_SESSION_WITHOUT_VALIDATOR,
)
FINDING_SEVERITIES = ("high", "medium", "low")

SUBMISSION_OUTCOME_CREATED = "created_kanban_backlog_task"
SUBMISSION_OUTCOME_PREVIEWED = "previewed_without_creating_kanban_task"
SUBMISSION_OUTCOME_PREVIEW_FAILED = "preview_failed_without_creating_kanban_task_or_leaving_any_record"
SUBMISSION_OUTCOME_PENDING = "pending_kanban_runtime_unavailable"
SUBMISSION_OUTCOME_SKIPPED_DUPLICATE = "skipped_already_submitted_identical_finding"
SUBMISSION_OUTCOME_SKIPPED_BACKGROUND_WIP = "skipped_finding_on_other_session_uncommitted_background_path"

KANBAN_TASK_TITLE_MAXIMUM_CHARACTERS = 90

# pending 记录（建卡 intent）上的崩溃恢复字段。远端 ``kanban task create`` 没有幂等键，「远端已建卡、本地还没
# 记下」的窗口无法彻底消除；这几个字段把窗口缩到最小，并让重试能识别「上次可能已建过」。
# 建卡调用发起前写入时间戳、观察到调用结果（成功或报错）后清掉；重试时仍在 ⇒ 上次调用发起后进程中断、结果未知。
PENDING_RECORD_FIELD_KANBAN_TASK_CREATE_INVOCATION_STARTED_AT_OUTCOME_NOT_OBSERVED = (
    "kanban_task_create_invocation_started_at_outcome_not_observed"
)
# 一旦识别出「上次调用结果未知」就置 True、永不清除：之后每次重建都在卡片 prompt 里带「可能重复」提示。
PENDING_RECORD_FIELD_KANBAN_TASK_POSSIBLY_ALREADY_CREATED_BY_UNOBSERVED_INVOCATION = (
    "kanban_task_possibly_already_created_by_unobserved_invocation"
)
# 卡已建成、但追加 ledger 失败时尽力写回的 task id：重试只补记账、不再建卡。
PENDING_RECORD_FIELD_KANBAN_TASK_ID_CREATED_BUT_LEDGER_APPEND_FAILED = "kanban_task_id_created_but_ledger_append_failed"


class OutOfScopeFindingInputInvalid(ValueError):
    """``--kanban-backlog-finding-file`` / ``--main-session-findings-file`` 内容不合法。"""


# --------------------------------------------------------------------------- #
# 输入
# --------------------------------------------------------------------------- #


def _is_safe_relative_repo_path(value: str) -> bool:
    path = Path(value)
    return bool(value.strip()) and value.strip() == value and not path.is_absolute() and ".." not in path.parts


def load_out_of_scope_findings_file(path: Path) -> list[dict[str, Any]]:
    """读 finding 文件：``{"findings": [...]}`` 或单个 finding 对象。字段：

    - ``title``（必填，一句话标题）、``path``（必填，仓内相对路径）、``problem_statement``（必填）；
    - ``line``（可选，≥1 的整数）、``severity``（可选，high/medium/low，缺省 medium）；
    - ``evidence``（可选，复现命令或代码依据）、``group_id``（可选，同根因的多条合成一张卡）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OutOfScopeFindingInputInvalid(f"cannot read finding file {path}: {exc}") from exc
    raw_findings = payload.get("findings") if isinstance(payload, dict) and "findings" in payload else [payload]
    if not isinstance(raw_findings, list) or not raw_findings:
        raise OutOfScopeFindingInputInvalid(f"{path}: findings must be a non-empty array")
    return normalize_out_of_scope_findings(raw_findings, label=str(path))


def normalize_out_of_scope_findings(raw_findings: Sequence[Any], *, label: str) -> list[dict[str, Any]]:
    errors: list[str] = []
    findings: list[dict[str, Any]] = []
    for position, raw in enumerate(raw_findings, 1):
        where = f"{label} findings[{position}]"
        if not isinstance(raw, dict):
            errors.append(f"{where} must be an object")
            continue
        title = raw.get("title")
        repo_path = raw.get("path")
        problem_statement = raw.get("problem_statement")
        line = raw.get("line")
        severity = raw.get("severity", "medium")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"{where}.title must be a non-empty string")
        if not isinstance(repo_path, str) or not _is_safe_relative_repo_path(repo_path):
            errors.append(f"{where}.path must be a relative repo path without '..'")
        if not isinstance(problem_statement, str) or not problem_statement.strip():
            errors.append(f"{where}.problem_statement must be a non-empty string")
        if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
            errors.append(f"{where}.line must be an integer >= 1 when present")
        if severity not in FINDING_SEVERITIES:
            errors.append(f"{where}.severity must be one of {list(FINDING_SEVERITIES)}")
        evidence = raw.get("evidence")
        if evidence is not None and not isinstance(evidence, str):
            errors.append(f"{where}.evidence must be a string when present")
        group_id = raw.get("group_id")
        if group_id is not None and (not isinstance(group_id, str) or not group_id.strip()):
            errors.append(f"{where}.group_id must be a non-empty string when present")
        if errors:
            continue
        findings.append(
            {
                "title": title.strip(),
                "path": repo_path,
                "line": line,
                "severity": severity,
                "problem_statement": problem_statement.strip(),
                "evidence": evidence.strip() if isinstance(evidence, str) and evidence.strip() else None,
                "group_id": group_id.strip() if isinstance(group_id, str) else None,
            }
        )
    if errors:
        raise OutOfScopeFindingInputInvalid("; ".join(errors))
    return findings


# --------------------------------------------------------------------------- #
# 本地指纹去重 ledger（第 1 层查重）
# --------------------------------------------------------------------------- #


def rvf_home_directory() -> Path:
    configured = os.environ.get("RVF_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".rvf"


def out_of_scope_findings_ledger_directory() -> Path:
    return rvf_home_directory() / OUT_OF_SCOPE_FINDINGS_KANBAN_BACKLOG_LEDGER_DIRECTORY_NAME


def _normalized_problem_text(text: str) -> str:
    # 去掉全部空白：中文行文不靠空格分词，「同一句话多敲了空格」不该被当成另一个问题。
    return re.sub(r"\s+", "", text).lower()


def out_of_scope_finding_fingerprint(*, repository_identity: str, finding: Mapping[str, Any]) -> str:
    message_digest = hashlib.sha256(
        _normalized_problem_text(f"{finding['title']}\n{finding['problem_statement']}").encode("utf-8")
    ).hexdigest()
    material = f"{repository_identity}\n{str(finding['path']).strip('/')}\n{message_digest}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@contextmanager
def _exclusive_flock_on_sibling_lock_file(protected_path: Path) -> Iterator[None]:
    """对 ``<protected_path>.lock`` 加排他 flock：保护 ledger 追加、pending 重试与 run 回执的 read-modify-write。"""
    protected_path.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{protected_path}.lock", "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _read_ledger_entries(ledger_path: Path) -> list[dict[str, Any]]:
    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _append_ledger_entries(ledger_path: Path, entries: Sequence[Mapping[str, Any]]) -> None:
    if not entries:
        return
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        # 机器会无预警重启：去重事实必须真正落盘，否则重启后同一 finding 会被再建一张卡。
        handle.flush()
        os.fsync(handle.fileno())


def _write_json_file_atomically_with_fsync(path: Path, payload: Mapping[str, Any]) -> None:
    """pending 记录用临时文件 + fsync + ``os.replace`` 写：崩溃后要么是旧内容要么是新内容，不会半截。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.writing.tmp")
    with open(temporary_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, path)


def _latest_ledger_entry_by_fingerprint(entries: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        fingerprint = entry.get("finding_fingerprint")
        if isinstance(fingerprint, str):
            latest[fingerprint] = entry
    return latest


# --------------------------------------------------------------------------- #
# 卡片内容
# --------------------------------------------------------------------------- #


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main_repository_path_for_kanban_project(repo: Path) -> Path:
    """Kanban 只登记主仓路径：``--project-path`` 必须指向 git common dir 的父目录，不能是 task worktree。"""
    completed = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        return Path(completed.stdout.strip()).parent
    return repo


def _render_kanban_task_title(findings: Sequence[Mapping[str, Any]]) -> str:
    title = findings[0]["title"]
    if len(findings) > 1:
        title = f"{title}（及另 {len(findings) - 1} 条同根因问题）"
    full = f"{KANBAN_TASK_TITLE_PREFIX_FOR_RVF_FINDING} {title}"
    if len(full) > KANBAN_TASK_TITLE_MAXIMUM_CHARACTERS:
        full = full[: KANBAN_TASK_TITLE_MAXIMUM_CHARACTERS - 1] + "…"
    return full


def render_kanban_task_prompt_for_out_of_scope_findings(
    *,
    findings: Sequence[Mapping[str, Any]],
    finding_source: str,
    run_id: str | None,
    run_dir: Path | None,
    attempt_id: str | None,
    issue_id: str | None,
    already_submitted_related_task_ids: Sequence[str],
) -> str:
    validated = finding_source != FINDING_SOURCE_MAIN_SESSION_WITHOUT_VALIDATOR
    lines = [
        KANBAN_TASK_ORIGIN_MARKER_LINE,
        # INTEGRATION-POINT(kanban dedicated support agent finding-relation triage)：看板 support agent
        # 上线后，提交前先调 `kanban support triage-incoming-work-item`；它判定「已有 task 完全覆盖」时
        # 不建新卡、把发现追加为已有卡的备注；其余情况建卡时去掉下面这行未查重标记。
        # INTEGRATION-POINT(Task Force fork relation consultation)：Task Force 落地后，由 support agent
        # （不是 RVF）在关系模糊时去问相关 task 的 agent 分身两者的具体关系；RVF 侧不需要改动。
        KANBAN_SUPPORT_AGENT_RELATION_TRIAGE_NOT_TRIAGED_MARKER_LINE,
        "",
        f"# {_render_kanban_task_title(findings)}",
        "",
        "这是 RVF（review-validate-fix）在另一轮工作中发现、但与那一轮工作不紧密相关的问题。"
        "按约定它当时没有被修，而是提交到这里。",
        "",
        "## 问题",
        "",
    ]
    for finding in findings:
        location = f"`{finding['path']}:{finding['line']}`" if finding.get("line") else f"`{finding['path']}`"
        lines.append(f"- {location}（{finding['severity']}）：{finding['problem_statement']}")
        if finding.get("evidence"):
            lines.append(f"  - 证据：{finding['evidence']}")
    lines.extend(
        [
            "",
            "## 验证情况",
            "",
            "- validator 已在原工作区核验属实。"
            if validated
            else "- 未经 validator 验证：rvf-land 阶段由主会话直接提交。",
            "",
            "## 来源",
            "",
            f"- RVF run：{run_id or '未知'}",
        ]
    )
    if run_dir is not None:
        lines.append(f"- run 目录：{run_dir}")
        lines.append(f"- handoff：{run_dir / 'artifacts' / 'handoff.md'}")
    lines.append(f"- 提交者：{finding_source}" + (f"（attempt {attempt_id}，issue {issue_id}）" if attempt_id else ""))
    if already_submitted_related_task_ids:
        lines.append(f"- 同组里已有问题提交过的卡：{', '.join(already_submitted_related_task_ids)}")
    lines.extend(
        [
            "",
            "## 开工前",
            "",
            "1. 先确认问题在当前代码里仍然成立（可能已被别的改动修掉）。",
            "2. 这张卡没经过看板查重：先用 `cline-kanban-survey-sibling-task-work-in-same-workspace` 查同仓是否已有"
            " task 在修同一问题；有则写明两者关系并停下，不重复修。",
            "3. 确认需要修之后再动手。",
            "",
        ]
    )
    return "\n".join(lines)


def _resolve_kanban_agent_id_for_run(run_dir: Path | None) -> str | None:
    """显式 ``CODEX_RVF_CLINE_KANBAN_AGENT_ID`` 钉死 > 镜像本 run 的主 harness > 交给 kanban 默认。"""
    pinned = (os.environ.get("CODEX_RVF_CLINE_KANBAN_AGENT_ID") or "").strip()
    if pinned:
        return pinned
    if run_dir is None:
        return None
    try:
        main_harness = json.loads(
            (run_dir / "artifacts" / "inputs" / "main-harness.json").read_text(encoding="utf-8")
        ).get("main_harness")
        from host_adapter_resolver import resolve_host_adapter  # noqa: PLC0415

        return resolve_host_adapter(main_harness if isinstance(main_harness, str) else None).cline_kanban_agent_id()
    except Exception:  # noqa: BLE001 - 解析不出就交给 kanban 自己的默认 executor
        return None


def _resolve_kanban_base_ref(run_dir: Path | None, repo: Path) -> str | None:
    """合同里的 base_ref（opt-in 仓）> ``CODEX_RVF_CLINE_KANBAN_BASE_REF`` > 本地 base 推断。"""
    if run_dir is not None:
        try:
            contract = json.loads((run_dir / "artifacts" / "inputs" / "scope.contract.json").read_text(encoding="utf-8"))
            if isinstance(contract.get("base_ref"), str) and contract["base_ref"].strip():
                return contract["base_ref"].strip()
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    configured = (os.environ.get("CODEX_RVF_CLINE_KANBAN_BASE_REF") or "").strip()
    if configured:
        return configured
    try:
        import prepare_review_run  # noqa: PLC0415

        return prepare_review_run.resolve_local_base_ref(repo, explicit_local_base_ref=None, committed_base_ref=None)
    except Exception:  # noqa: BLE001
        return None


def _background_or_protected_paths_for_run(run_dir: Path | None) -> set[str]:
    if run_dir is None:
        return set()
    try:
        contract = json.loads((run_dir / "artifacts" / "inputs" / "scope.contract.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    paths: set[str] = set()
    for key in ("background_files", "protected_files"):
        value = contract.get(key)
        if isinstance(value, list):
            paths.update(item for item in value if isinstance(item, str))
    return paths


# --------------------------------------------------------------------------- #
# 提交
# --------------------------------------------------------------------------- #


def _pending_prompt_directory(repository_identity: str) -> Path:
    return out_of_scope_findings_ledger_directory() / PENDING_KANBAN_TASK_PROMPTS_DIRECTORY_NAME / repository_identity


def _create_kanban_task(
    *,
    task_cmd: str,
    main_repository_path: Path,
    prompt: str,
    title: str,
    base_ref: str,
    agent_id: str | None,
    preview: bool,
) -> dict[str, Any]:
    return cline_kanban_client.create_task(
        task_cmd=task_cmd,
        repo=main_repository_path,
        prompt=prompt,
        base_ref=base_ref,
        title=title,
        agent_id=agent_id,
        # 不传 parent session：传了会让卡片 fork 父会话上下文，而卡片 prompt 要求自包含。
        parent_session_id=None,
        worktree_mode=None,
        prep_file_path=None,
        start_in_plan_mode=True,
        auto_review_enabled=False,
        auto_review_mode="commit",
        preview_resolved_settings_without_creating_task=preview,
    )


KANBAN_TASK_PROMPT_POSSIBLE_DUPLICATE_SECTION_TEMPLATE = (
    "\n## 可能重复\n\n"
    "上一次为这些问题建卡的调用发起后进程中断、结果没有记录下来，看板上可能已经有一张同标题的卡"
    "（「{title}」）。开工前先在同仓按标题查找；已有就把其中一张移到 trash 并停下，不重复修。\n"
)


def _ledger_entries_for_created_kanban_task(
    *, fingerprints: Sequence[str], kanban_task_id: str, extra_fields: Mapping[str, Any]
) -> list[dict[str, Any]]:
    return [
        {
            "finding_fingerprint": fingerprint,
            "outcome": SUBMISSION_OUTCOME_CREATED,
            "kanban_task_id": kanban_task_id,
            "recorded_at": _utc_now_iso(),
            **extra_fields,
        }
        for fingerprint in fingerprints
    ]


def _record_created_kanban_task_and_remove_pending_intent(
    *,
    ledger_path: Path,
    pending_path: Path,
    pending_record: dict[str, Any],
    kanban_task_id: str,
    created_ledger_entry_extra_fields: Mapping[str, Any],
) -> None:
    """建卡成功后把 pending intent 升级为 created：先追加 ledger，再删 pending 文件。

    ledger 追加失败 ⇒ 尽力把 task id 写回 pending 记录再抛出，重试只补记账、不再建卡；写回也失败（例如整个
    ``$RVF_HOME`` 不可写）就落入残留窗口，重试会按「可能重复」重建。删 pending 失败 ⇒ 重试看到 ledger 已是
    created，只删文件。"""
    try:
        _append_ledger_entries(
            ledger_path,
            _ledger_entries_for_created_kanban_task(
                fingerprints=pending_record.get("finding_fingerprints") or [],
                kanban_task_id=kanban_task_id,
                extra_fields=created_ledger_entry_extra_fields,
            ),
        )
    except Exception:
        pending_record[PENDING_RECORD_FIELD_KANBAN_TASK_ID_CREATED_BUT_LEDGER_APPEND_FAILED] = kanban_task_id
        try:
            _write_json_file_atomically_with_fsync(pending_path, pending_record)
        except OSError:
            pass
        raise
    pending_path.unlink(missing_ok=True)


def _create_kanban_task_for_persisted_pending_intent(
    *,
    task_cmd: str,
    ledger_path: Path,
    pending_path: Path,
    pending_record: dict[str, Any],
    created_ledger_entry_extra_fields: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """对一条**已落盘**的 pending intent 发起建卡。返回 ``(kanban_task_id, error)``。

    残留窗口（如实写明，不是 exactly-once）：远端 ``kanban task create`` 没有幂等键；建卡调用返回之后、ledger
    记下之前进程被杀 / 机器重启，本地只剩「调用发起过、结果未知」的标记。重试时据此识别「上次可能已建过」，
    但无法可靠确认——``kanban task list`` 在大看板上经管道 capture 会截断（实测 ~58KB），不能拿来做远端查重——
    所以照常重建（不放跑优先），并在卡片 prompt 与回执里标「可能重复」，由卡片 agent 开工前按标题查重。
    另一处同类残留：调用报告失败、远端其实建成（例如响应里缺 task id），这里按「未建」保留 pending。"""
    if pending_record.get(PENDING_RECORD_FIELD_KANBAN_TASK_CREATE_INVOCATION_STARTED_AT_OUTCOME_NOT_OBSERVED):
        pending_record[PENDING_RECORD_FIELD_KANBAN_TASK_POSSIBLY_ALREADY_CREATED_BY_UNOBSERVED_INVOCATION] = True
    pending_record[PENDING_RECORD_FIELD_KANBAN_TASK_CREATE_INVOCATION_STARTED_AT_OUTCOME_NOT_OBSERVED] = _utc_now_iso()
    _write_json_file_atomically_with_fsync(pending_path, pending_record)
    prompt = pending_record["prompt"]
    if pending_record.get(PENDING_RECORD_FIELD_KANBAN_TASK_POSSIBLY_ALREADY_CREATED_BY_UNOBSERVED_INVOCATION):
        prompt += KANBAN_TASK_PROMPT_POSSIBLE_DUPLICATE_SECTION_TEMPLATE.format(title=pending_record["title"])
    try:
        created = _create_kanban_task(
            task_cmd=task_cmd,
            main_repository_path=Path(pending_record["main_repository_path"]),
            prompt=prompt,
            title=pending_record["title"],
            base_ref=pending_record["base_ref"],
            agent_id=pending_record.get("agent_id"),
            preview=False,
        )
    except Exception as exc:  # noqa: BLE001 - intent 已持久化：任何建卡失败都只是「仍 pending」
        pending_record[PENDING_RECORD_FIELD_KANBAN_TASK_CREATE_INVOCATION_STARTED_AT_OUTCOME_NOT_OBSERVED] = None
        pending_record["error"] = str(exc) or type(exc).__name__
        _write_json_file_atomically_with_fsync(pending_path, pending_record)
        return None, pending_record["error"]
    kanban_task_id = created["task_id"]
    _record_created_kanban_task_and_remove_pending_intent(
        ledger_path=ledger_path,
        pending_path=pending_path,
        pending_record=pending_record,
        kanban_task_id=kanban_task_id,
        created_ledger_entry_extra_fields=created_ledger_entry_extra_fields,
    )
    return kanban_task_id, None


def retry_pending_kanban_task_prompts_for_repository(
    *, repository_identity: str, task_cmd: str, ledger_path: Path
) -> list[dict[str, Any]]:
    """重试本仓的 pending 卡片；成功的在 ledger 追加带 task id 的记录并删 pending 文件。调用方持锁。"""
    directory = _pending_prompt_directory(repository_identity)
    outcomes: list[dict[str, Any]] = []
    if not directory.is_dir():
        return outcomes
    latest = _latest_ledger_entry_by_fingerprint(_read_ledger_entries(ledger_path))
    for pending_path in sorted(directory.glob("*.json")):
        try:
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            if not isinstance(pending, dict):
                raise ValueError("pending record root is not an object")
            fingerprints = [item for item in pending.get("finding_fingerprints") or [] if isinstance(item, str)]
            created_ledger_entry_extra_fields = {"retried_from_pending_prompt_path": str(pending_path)}
            if fingerprints and all(
                latest.get(fingerprint, {}).get("outcome") == SUBMISSION_OUTCOME_CREATED for fingerprint in fingerprints
            ):
                # 上次已建成并记了账，只是 pending 文件没删掉 ⇒ 只删文件。
                pending_path.unlink(missing_ok=True)
                outcomes.append(
                    {"pending_prompt_path": str(pending_path), "retried": True,
                     "kanban_task_id": latest[fingerprints[0]].get("kanban_task_id"),
                     "resolved_from_existing_ledger_record_without_creating_again": True}
                )
                continue
            known_task_id = pending.get(PENDING_RECORD_FIELD_KANBAN_TASK_ID_CREATED_BUT_LEDGER_APPEND_FAILED)
            if isinstance(known_task_id, str) and known_task_id:
                # 上次建成了、ledger 没记上但 task id 已写回 ⇒ 只补记账。
                _record_created_kanban_task_and_remove_pending_intent(
                    ledger_path=ledger_path,
                    pending_path=pending_path,
                    pending_record=pending,
                    kanban_task_id=known_task_id,
                    created_ledger_entry_extra_fields=created_ledger_entry_extra_fields,
                )
                outcomes.append(
                    {"pending_prompt_path": str(pending_path), "retried": True, "kanban_task_id": known_task_id,
                     "recovered_task_id_recorded_without_creating_again": True}
                )
                continue
            base_ref = pending.get("base_ref")
            if not isinstance(base_ref, str) or not base_ref.strip():
                # 当时没解析出基线 ⇒ 现在重新解析；仍失败就继续 pending，不拿占位基线硬建。
                run_dir_value = pending.get("run_dir")
                repository_path_value = pending.get("repository_path_for_base_ref_resolution")
                base_ref = _resolve_kanban_base_ref(
                    Path(run_dir_value) if isinstance(run_dir_value, str) and run_dir_value else None,
                    Path(repository_path_value or pending["main_repository_path"]),
                )
                if base_ref is None:
                    pending["error"] = "cannot resolve a local base ref for the new task"
                    _write_json_file_atomically_with_fsync(pending_path, pending)
                    outcomes.append({"pending_prompt_path": str(pending_path), "retried": False, "error": pending["error"]})
                    continue
                pending["base_ref"] = base_ref
            kanban_task_id, error = _create_kanban_task_for_persisted_pending_intent(
                task_cmd=task_cmd,
                ledger_path=ledger_path,
                pending_path=pending_path,
                pending_record=pending,
                created_ledger_entry_extra_fields=created_ledger_entry_extra_fields,
            )
        except (OSError, KeyError, ValueError, TypeError) as exc:
            outcomes.append({"pending_prompt_path": str(pending_path), "retried": False, "error": str(exc)})
            continue
        outcome: dict[str, Any] = {"pending_prompt_path": str(pending_path), "retried": kanban_task_id is not None}
        if kanban_task_id is not None:
            outcome["kanban_task_id"] = kanban_task_id
        else:
            outcome["error"] = error
        if pending.get(PENDING_RECORD_FIELD_KANBAN_TASK_POSSIBLY_ALREADY_CREATED_BY_UNOBSERVED_INVOCATION):
            outcome[PENDING_RECORD_FIELD_KANBAN_TASK_POSSIBLY_ALREADY_CREATED_BY_UNOBSERVED_INVOCATION] = True
        outcomes.append(outcome)
    return outcomes


def submit_out_of_scope_findings_to_kanban_backlog(
    *,
    repo: Path,
    findings: Sequence[Mapping[str, Any]],
    finding_source: str,
    run_dir: Path | None,
    run_id: str | None = None,
    attempt_id: str | None = None,
    issue_id: str | None = None,
    preview_without_creating: bool = False,
) -> dict[str, Any]:
    """提交一批已规范化的 finding，返回结构化回执。

    Kanban 侧错误永不抛（不可用 ⇒ pending）；本地落盘失败（ledger / pending / run 回执写不进去）照常抛出——
    返回即表示每条 finding 都已留下持久事实（见模块文档「持久性不变量」）。预演不留任何记录。"""
    if finding_source not in FINDING_SOURCES:
        raise OutOfScopeFindingInputInvalid(f"finding_source must be one of {list(FINDING_SOURCES)}")
    repo = repo.expanduser().resolve()
    repository_identity = resolve_repository_identity_for_scope_marker_key(repo)
    if repository_identity is None:
        raise OutOfScopeFindingInputInvalid(f"cannot resolve repository identity for {repo}")
    task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", cline_kanban_client.DEFAULT_TASK_CMD)
    main_repository_path = main_repository_path_for_kanban_project(repo)
    background_paths = _background_or_protected_paths_for_run(run_dir)
    ledger_path = out_of_scope_findings_ledger_directory() / f"{repository_identity}.jsonl"
    receipt: dict[str, Any] = {
        "finding_source": finding_source,
        "repository_identity": repository_identity,
        "kanban_project_path": str(main_repository_path),
        "ledger_path": str(ledger_path),
        "attempt_id": attempt_id,
        "issue_id": issue_id,
        "submitted_at": _utc_now_iso(),
        "groups": [],
        "retried_pending": [],
    }
    # 预演只读 ledger（不加锁：连锁文件 / 目录都不留）。
    ledger_lock = nullcontext() if preview_without_creating else _exclusive_flock_on_sibling_lock_file(ledger_path)
    with ledger_lock:
        if not preview_without_creating:
            receipt["retried_pending"] = retry_pending_kanban_task_prompts_for_repository(
                repository_identity=repository_identity, task_cmd=task_cmd, ledger_path=ledger_path
            )
        latest = _latest_ledger_entry_by_fingerprint(_read_ledger_entries(ledger_path))
        groups: dict[str, list[dict[str, Any]]] = {}
        for position, finding in enumerate(findings, 1):
            group_key = finding.get("group_id") or f"finding-{position}"
            groups.setdefault(group_key, []).append(dict(finding))
        base_ref = _resolve_kanban_base_ref(run_dir, repo)
        agent_id = _resolve_kanban_agent_id_for_run(run_dir)
        for group_key, members in groups.items():
            group_receipt: dict[str, Any] = {"group_id": group_key, "findings": []}
            to_submit: list[dict[str, Any]] = []
            related_task_ids: list[str] = []
            for member in members:
                fingerprint = out_of_scope_finding_fingerprint(repository_identity=repository_identity, finding=member)
                member["finding_fingerprint"] = fingerprint
                if member["path"] in background_paths:
                    group_receipt["findings"].append({**member, "outcome": SUBMISSION_OUTCOME_SKIPPED_BACKGROUND_WIP})
                    continue
                previous = latest.get(fingerprint)
                if previous is not None and previous.get("outcome") in {SUBMISSION_OUTCOME_CREATED, SUBMISSION_OUTCOME_PENDING}:
                    if previous.get("kanban_task_id"):
                        related_task_ids.append(str(previous["kanban_task_id"]))
                    group_receipt["findings"].append(
                        {**member, "outcome": SUBMISSION_OUTCOME_SKIPPED_DUPLICATE,
                         "previous_kanban_task_id": previous.get("kanban_task_id")}
                    )
                    continue
                to_submit.append(member)
            if not to_submit:
                group_receipt["outcome"] = "nothing_new_to_submit"
                receipt["groups"].append(group_receipt)
                continue
            title = _render_kanban_task_title(to_submit)
            prompt = render_kanban_task_prompt_for_out_of_scope_findings(
                findings=to_submit,
                finding_source=finding_source,
                run_id=run_id,
                run_dir=run_dir,
                attempt_id=attempt_id,
                issue_id=issue_id,
                already_submitted_related_task_ids=sorted(set(related_task_ids)),
            )
            group_receipt.update({"title": title, "base_ref": base_ref, "agent_id": agent_id})
            fingerprints = [member["finding_fingerprint"] for member in to_submit]
            outcome: str
            kanban_task_id: str | None = None
            error: str | None = None
            if preview_without_creating:
                # 只读预演：不写 pending、不写 ledger、不写 run 工件；失败如实返回，绝不落进 pending 分支
                # （否则下次任何提交 / retry-pending 会把这次预演真正建成卡）。
                if base_ref is None:
                    outcome, error = SUBMISSION_OUTCOME_PREVIEW_FAILED, "cannot resolve a local base ref for the new task"
                else:
                    try:
                        group_receipt["kanban_preview"] = _create_kanban_task(
                            task_cmd=task_cmd,
                            main_repository_path=main_repository_path,
                            prompt=prompt,
                            title=title,
                            base_ref=base_ref,
                            agent_id=agent_id,
                            preview=True,
                        )
                        outcome = SUBMISSION_OUTCOME_PREVIEWED
                    except Exception as exc:  # noqa: BLE001 - 预演失败如实返回
                        outcome, error = SUBMISSION_OUTCOME_PREVIEW_FAILED, str(exc) or type(exc).__name__
            else:
                # 先持久化 intent 再建卡：pending 文件（原子写）→ ledger PENDING。顺序不能反——ledger 先记了
                # PENDING 而 pending 文件没写成，之后的提交会把这条当「已提交」跳过，却永远没有卡（放跑）。
                pending_path = (
                    _pending_prompt_directory(repository_identity)
                    / f"{hashlib.sha256(''.join(fingerprints).encode()).hexdigest()[:24]}.json"
                )
                pending_record: dict[str, Any] = {
                    "title": title,
                    "prompt": prompt,
                    # 解析不出就存 None：重试时重新解析，不拿占位基线硬建。
                    "base_ref": base_ref,
                    "agent_id": agent_id,
                    "main_repository_path": str(main_repository_path),
                    "repository_path_for_base_ref_resolution": str(repo),
                    "run_dir": str(run_dir) if run_dir is not None else None,
                    "finding_fingerprints": fingerprints,
                    "error": None if base_ref is not None else "cannot resolve a local base ref for the new task",
                    "recorded_at": _utc_now_iso(),
                    PENDING_RECORD_FIELD_KANBAN_TASK_CREATE_INVOCATION_STARTED_AT_OUTCOME_NOT_OBSERVED: None,
                }
                _write_json_file_atomically_with_fsync(pending_path, pending_record)
                _append_ledger_entries(
                    ledger_path,
                    [
                        {
                            "finding_fingerprint": fingerprint,
                            "outcome": SUBMISSION_OUTCOME_PENDING,
                            "kanban_task_id": None,
                            "pending_prompt_path": str(pending_path),
                            "run_id": run_id,
                            "finding_source": finding_source,
                            "recorded_at": _utc_now_iso(),
                        }
                        for fingerprint in fingerprints
                    ],
                )
                if base_ref is None:
                    outcome, error = SUBMISSION_OUTCOME_PENDING, pending_record["error"]
                else:
                    kanban_task_id, error = _create_kanban_task_for_persisted_pending_intent(
                        task_cmd=task_cmd,
                        ledger_path=ledger_path,
                        pending_path=pending_path,
                        pending_record=pending_record,
                        created_ledger_entry_extra_fields={"run_id": run_id, "finding_source": finding_source},
                    )
                    outcome = SUBMISSION_OUTCOME_CREATED if kanban_task_id is not None else SUBMISSION_OUTCOME_PENDING
                if outcome == SUBMISSION_OUTCOME_PENDING:
                    group_receipt["pending_prompt_path"] = str(pending_path)
            group_receipt.update({"outcome": outcome, "kanban_task_id": kanban_task_id, "error": error})
            group_receipt["findings"].extend({**member, "outcome": outcome} for member in to_submit)
            receipt["groups"].append(group_receipt)
    if run_dir is not None and not preview_without_creating:
        record_submission_receipt_in_run_artifact(run_dir, receipt)
    receipt["handoff_markdown_lines"] = render_handoff_lines_for_submission_receipts([receipt])
    return receipt


def record_submission_receipt_in_run_artifact(run_dir: Path, receipt: Mapping[str, Any]) -> Path:
    path = run_dir / RUN_ARTIFACT_RELATIVE_PATH
    # 并行 attempt 会同时 stop：read-modify-write 不加锁会互相覆盖，handoff 静默漏掉已建卡的范围外问题。
    with _exclusive_flock_on_sibling_lock_file(path):
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            existing = {}
        submissions = existing.get("submissions") if isinstance(existing, dict) else None
        payload = {
            "schema": "rvf-out-of-scope-findings-kanban-backlog-submissions",
            "schema_version": 1,
            "submissions": [*(submissions if isinstance(submissions, list) else []), dict(receipt)],
        }
        write_json_with_digest(path, payload)
    return path


def read_submission_receipts_for_run(run_dir: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads((run_dir / RUN_ARTIFACT_RELATIVE_PATH).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    submissions = payload.get("submissions") if isinstance(payload, dict) else None
    return [item for item in submissions if isinstance(item, dict)] if isinstance(submissions, list) else []


def render_handoff_lines_for_submission_receipts(receipts: Sequence[Mapping[str, Any]]) -> list[str]:
    """渲染 handoff 的 ``## 范围外问题（已提交 Kanban backlog）`` 条目，主会话照抄即可。"""
    lines: list[str] = []
    for receipt in receipts:
        for group in receipt.get("groups") or []:
            outcome = group.get("outcome")
            if outcome == SUBMISSION_OUTCOME_CREATED:
                head = f"- 已建卡 `{group.get('kanban_task_id')}`：{group.get('title')}"
            elif outcome == SUBMISSION_OUTCOME_PENDING:
                head = f"- 待建卡（Kanban 不可用，已存 `{group.get('pending_prompt_path')}`）：{group.get('title')}"
            elif outcome == SUBMISSION_OUTCOME_PREVIEWED:
                head = f"- 预演（未建卡）：{group.get('title')}"
            elif outcome == SUBMISSION_OUTCOME_PREVIEW_FAILED:
                head = f"- 预演失败（未建卡、未留任何记录）：{group.get('title')}：{group.get('error')}"
            else:
                head = f"- 无新内容需要提交（组 {group.get('group_id')}）"
            lines.append(head)
            for finding in group.get("findings") or []:
                location = f"{finding.get('path')}:{finding.get('line')}" if finding.get("line") else finding.get("path")
                note = {
                    SUBMISSION_OUTCOME_SKIPPED_DUPLICATE: f"（已提交过，卡 {finding.get('previous_kanban_task_id') or '待建'}）",
                    SUBMISSION_OUTCOME_SKIPPED_BACKGROUND_WIP: "（落在其他会话未提交的背景改动上，未提交）",
                }.get(finding.get("outcome"), "")
                lines.append(f"  - `{location}` {finding.get('title')}{note}")
    return lines


# --------------------------------------------------------------------------- #
# CLI：rvf backlog submit
# --------------------------------------------------------------------------- #


def _run_id_for_run_dir(run_dir: Path | None) -> str | None:
    if run_dir is None:
        return None
    try:
        value = json.loads((run_dir / "summary.json").read_text(encoding="utf-8")).get("run_id")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, str) and value else None


def command_submit(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve() if args.run_dir else None
    repo = Path(args.repo or os.environ.get("RVF_REPO") or Path.cwd()).expanduser().resolve()
    try:
        findings = load_out_of_scope_findings_file(Path(args.main_session_findings_file).expanduser().resolve())
        receipt = submit_out_of_scope_findings_to_kanban_backlog(
            repo=repo,
            findings=findings,
            finding_source=FINDING_SOURCE_MAIN_SESSION_WITHOUT_VALIDATOR,
            run_dir=run_dir,
            run_id=_run_id_for_run_dir(run_dir),
            preview_without_creating=args.preview_kanban_task_without_creating,
        )
    except OutOfScopeFindingInputInvalid as exc:
        print(f"invalid finding input: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    print("\n".join(["", "## 范围外问题（已提交 Kanban backlog）", "", *receipt["handoff_markdown_lines"]]))
    return 0


def command_retry_pending(args: argparse.Namespace) -> int:
    repo = Path(args.repo or os.environ.get("RVF_REPO") or Path.cwd()).expanduser().resolve()
    repository_identity = resolve_repository_identity_for_scope_marker_key(repo)
    if repository_identity is None:
        print(f"cannot resolve repository identity for {repo}", file=sys.stderr)
        return 2
    ledger_path = out_of_scope_findings_ledger_directory() / f"{repository_identity}.jsonl"
    task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", cline_kanban_client.DEFAULT_TASK_CMD)
    with _exclusive_flock_on_sibling_lock_file(ledger_path):
        outcomes = retry_pending_kanban_task_prompts_for_repository(
            repository_identity=repository_identity, task_cmd=task_cmd, ledger_path=ledger_path
        )
    print(json.dumps({"retried_pending": outcomes}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit RVF out-of-scope findings (not tightly related to the current round) to Kanban backlog."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser(
        "submit",
        help="主会话直接提交（rvf-land 阶段等没有 validator 的场合）；卡片会标注「未经 validator 验证」。",
    )
    submit.add_argument("--main-session-findings-file", required=True)
    submit.add_argument("--run-dir", default=None)
    submit.add_argument("--repo", default=None)
    submit.add_argument(
        "--preview-kanban-task-without-creating",
        action="store_true",
        help="透传 kanban task create --preview：只解析生效设置，不建卡、不写 ledger 与 run 工件。",
    )
    submit.set_defaults(func=command_submit)
    retry = sub.add_parser("retry-pending", help="重试本仓因 Kanban 不可用而暂存的卡片。")
    retry.add_argument("--repo", default=None)
    retry.set_defaults(func=command_retry_pending)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
