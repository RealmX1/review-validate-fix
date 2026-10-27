#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import _rvf_pyroot  # noqa: F401
from core.review_run_qualification_mutation_lock import guard_review_run_qualification_mutation, run_directory_for_qualification_artifact
from reviewer_request_progress_identity import reviewer_request_content_identity


SCHEMA_VERSION = 1
REQUEST_TYPES = {
    "lock_request",
    "standard_request",
    "measurement_request",
    "subtask_request",
    "context_request",
}
STANDARD_DOMAINS = {"simplification", "security", "performance"}
SUBTASK_TYPES = {
    "read_only_investigation",
    "security_check",
    "performance_measurement",
    "simplification_probe",
}
CONTEXT_NEEDS = {"file", "manifest", "packet", "prior-output", "test-result"}
ISSUE_KINDS = {"REAL", "NIT", "ELEVATE"}
ISSUE_SEVERITIES = {"high", "medium", "low"}
# 与 check_review_result.ISSUE_SCOPE_RELATIONS 同一词表。
ISSUE_SCOPE_RELATION_IN_CURRENT_ROUND_SCOPE = "in_current_round_scope"
ISSUE_SCOPE_RELATION_OUTSIDE_CURRENT_ROUND_SCOPE = "outside_current_round_scope"
ISSUE_SCOPE_RELATIONS = {
    ISSUE_SCOPE_RELATION_IN_CURRENT_ROUND_SCOPE,
    ISSUE_SCOPE_RELATION_OUTSIDE_CURRENT_ROUND_SCOPE,
}


def fail(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def is_relative_repo_path(value: str) -> bool:
    if not value or value.strip() != value:
        return False
    path = Path(value)
    if path.is_absolute():
        return False
    if value in {".", ".."}:
        return False
    return ".." not in path.parts


def ensure_out_allowed(path: Path) -> None:
    run_dir = os.environ.get("RVF_RUN_DIR")
    if not run_dir:
        return
    resolved = path.expanduser().resolve()
    allowed_root = Path(run_dir).expanduser().resolve()
    if resolved != allowed_root and allowed_root not in resolved.parents:
        raise ValueError(f"--out must be inside RVF_RUN_DIR: {allowed_root}")


def read_existing(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("existing result root must be a JSON object")
    return payload


SUPERSEDED_RESULT_ARTIFACTS_DIRNAME = "superseded-review-result-artifacts"



def ensure_supersession_allowed(existing: dict[str, Any], new_kind: str) -> None:
    """发现不可清除；仍未解决的补证不可被完成结果遮盖。"""
    if existing.get("issues") and new_kind == "no_issues":
        raise ValueError("已记录 issues，不能改写为 no_issues；误报由 validate/fix 判定")
    if new_kind in {"issues", "no_issues"} and any(
        request.get("status", "pending") != "resolved" for request in existing.get("requests", [])
    ):
        raise ValueError("存在未解决 request，须记录补证响应并由 reviewer 确认解决")


def base_payload(kind: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "issues": [],
        "requests": [],
    }


@guard_review_run_qualification_mutation(lambda arguments: run_directory_for_qualification_artifact(arguments["out"]))
def supersede_existing_result(out: Path, existing: dict[str, Any], new_kind: str) -> dict[str, Any]:
    """归档旧状态，同时保留 canonical 中的全部发现与请求历史。"""
    ensure_supersession_allowed(existing, new_kind)
    archive_dir = out.parent / SUPERSEDED_RESULT_ARTIFACTS_DIRNAME
    archive_dir.mkdir(parents=True, exist_ok=True)
    old_kind = str(existing.get("kind") or "unknown")
    sequence = 1 + sum(1 for _ in archive_dir.glob("review-result.*.json"))
    archive_path = archive_dir / f"review-result.{sequence:02d}.{old_kind}.json"
    archive_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    payload = dict(existing, kind=new_kind)
    payload["superseded"] = [*existing.get("superseded", []), {
        "kind": old_kind, "path": str(archive_path),
        "superseded_at": datetime.now(timezone.utc).isoformat(), "superseded_by_kind": new_kind,
    }]
    return payload


@guard_review_run_qualification_mutation(lambda arguments: run_directory_for_qualification_artifact(arguments["path"]))
def write_payload(path: Path, payload: dict[str, Any]) -> None:
    ensure_out_allowed(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    from core.run_ledger.review_run_lifecycle import atomic_write_json
    atomic_write_json(path, payload)


def optional_fields(args: argparse.Namespace, names: tuple[str, ...]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name in names:
        value = getattr(args, name, None)
        if value:
            fields[name.replace("_", "-")] = value
    return fields


@guard_review_run_qualification_mutation(lambda arguments: run_directory_for_qualification_artifact(Path(arguments["args"].out)))
def command_no_issues(args: argparse.Namespace) -> int:
    out = Path(args.out)
    existing = read_existing(out)
    if existing is None:
        payload = base_payload("no_issues")
    elif existing.get("kind") != "no_issues":
        payload = supersede_existing_result(out, existing, "no_issues")
    else:
        payload = existing
    payload["audit_summary"] = args.audit_summary.strip()
    write_payload(out, payload)
    return 0


@guard_review_run_qualification_mutation(lambda arguments: run_directory_for_qualification_artifact(Path(arguments["args"].out)))
def command_issue(args: argparse.Namespace) -> int:
    if not is_relative_repo_path(args.path):
        return fail("--path must be a relative repo path without '..'")
    if args.line < 1:
        return fail("--line must be >= 1")
    if not args.message.strip():
        return fail("--message must not be empty")

    out = Path(args.out)
    existing = read_existing(out)
    if existing is None:
        payload = base_payload("issues")
    elif existing.get("kind") not in {"issues", "request"}:
        # clean 后发现问题时归档旧结论；已有 request 则继续保留未完成状态。
        payload = supersede_existing_result(out, existing, "issues")
    else:
        payload = existing
        if not isinstance(payload.get("issues"), list):
            return fail("existing issues field must be an array")

    issue: dict[str, Any] = {
        "path": args.path,
        "line": args.line,
        "message": args.message.strip(),
        "kind": args.kind,
        "severity": args.severity,
    }
    # 下划线键名直接写，不走 optional_fields（那里会把下划线转成连字符）。
    issue["scope_relation"] = args.scope_relation
    issue.update(optional_fields(args, ("evidence_command", "confidence", "source")))
    if issue not in payload.setdefault("issues", []):
        payload["issues"].append(issue)
    if args.current_round_scope_audit_summary and args.current_round_scope_audit_summary.strip():
        payload["audit_summary"] = args.current_round_scope_audit_summary.strip()
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_payload(out, payload)
    return 0


@guard_review_run_qualification_mutation(lambda arguments: run_directory_for_qualification_artifact(Path(arguments["args"].out)))
def append_request(args: argparse.Namespace, request: dict[str, Any]) -> int:
    out = Path(args.out)
    existing = read_existing(out)
    payload = base_payload("request") if existing is None else (
        existing if existing.get("kind") == "request" else supersede_existing_result(out, existing, "request")
    )
    request_id = reviewer_request_content_identity(request)
    request.update(request_id=request_id, status="pending")
    previous = next((item for item in payload.setdefault("requests", []) if reviewer_request_content_identity(item) == request_id), None)
    if previous is None:
        payload["requests"].append(request)
    elif previous.get("status") == "resolved":
        previous.setdefault("resolution_history", []).append({field: previous.get(field)
            for field in ("response", "resolution", "resolved_at")})
        for field in ("response", "resolution", "resolved_at"):
            previous.pop(field, None)
        previous.update(status="pending", request_id=request_id)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_payload(out, payload)
    return 0


def command_lock_request(args: argparse.Namespace) -> int:
    return append_request(
        args,
        {
            "type": "lock_request",
            "name": args.name.strip(),
            "command": args.command.strip(),
            "reason": args.reason.strip(),
        },
    )


def command_standard_request(args: argparse.Namespace) -> int:
    if args.domain not in STANDARD_DOMAINS:
        return fail("--domain must be one of: " + ", ".join(sorted(STANDARD_DOMAINS)))
    return append_request(
        args,
        {
            "type": "standard_request",
            "domain": args.domain,
            "scope": args.scope.strip(),
            "reason": args.reason.strip(),
        },
    )


def command_measurement_request(args: argparse.Namespace) -> int:
    return append_request(
        args,
        {
            "type": "measurement_request",
            "metric": args.metric.strip(),
            "command": args.command.strip(),
            "reason": args.reason.strip(),
        },
    )


def command_subtask_request(args: argparse.Namespace) -> int:
    if args.type not in SUBTASK_TYPES:
        return fail("--type must be one of: " + ", ".join(sorted(SUBTASK_TYPES)))
    return append_request(
        args,
        {
            "type": "subtask_request",
            "subtask_type": args.type,
            "scope": args.scope.strip(),
            "reason": args.reason.strip(),
        },
    )


def command_context_request(args: argparse.Namespace) -> int:
    if args.need not in CONTEXT_NEEDS:
        return fail("--need must be one of: " + ", ".join(sorted(CONTEXT_NEEDS)))
    return append_request(
        args,
        {
            "type": "context_request",
            "need": args.need,
            "reason": args.reason.strip(),
            **({"target": args.target} if getattr(args, "target", None) else {}),
        },
    )


@guard_review_run_qualification_mutation(lambda arguments: run_directory_for_qualification_artifact(Path(arguments["args"].out)))
def command_request_progress(args: argparse.Namespace) -> int:
    out = Path(args.out)
    payload = read_existing(out)
    if payload is None:
        raise ValueError("缺少 canonical review-result")
    request = next((item for item in payload.get("requests", []) if reviewer_request_content_identity(item) == args.request_id), None)
    if request is None:
        raise ValueError("request_id 不属于本腿结果")
    request["request_id"] = args.request_id
    if args.command == "respond-request":
        if request.get("status") == "resolved":
            raise ValueError("已解决的 request 不能覆写响应")
        request.setdefault("response_history", []).append({"response": args.response,
            "recorded_at": datetime.now(timezone.utc).isoformat()})
        request.update(status="responded", response=args.response)
    else:
        if not request.get("response"):
            raise ValueError("缺少补证响应，不能确认解决")
        request.update(status="resolved", resolution=args.resolution,
                       resolved_at=datetime.now(timezone.utc).isoformat())
    write_payload(out, payload)
    return 0


@guard_review_run_qualification_mutation(lambda arguments: run_directory_for_qualification_artifact(Path(arguments["args"].out)))
def command_complete(args: argparse.Namespace) -> int:
    out = Path(args.out)
    existing = read_existing(out)
    if existing is None:
        raise ValueError("缺少 canonical review-result")
    kind = "issues" if existing.get("issues") else "no_issues"
    payload = supersede_existing_result(out, existing, kind) if existing.get("kind") != kind else existing
    ensure_supersession_allowed(payload, kind)
    payload["audit_summary"] = args.audit_summary.strip()
    payload["reviewer_confirmed_complete_at"] = datetime.now(timezone.utc).isoformat()
    write_payload(out, payload)
    return 0


def require_non_empty(args: argparse.Namespace, fields: tuple[str, ...]) -> int | None:
    for field in fields:
        value = getattr(args, field)
        if isinstance(value, str) and not value.strip():
            return fail(f"--{field.replace('_', '-')} must not be empty")
    return None


def add_common_request_args(parser: argparse.ArgumentParser, fields: tuple[str, ...]) -> None:
    parser.add_argument("--out", required=True, help="Review result artifact path.")
    for field in fields:
        parser.add_argument(f"--{field.replace('_', '-')}", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write canonical review-validate-fix reviewer result artifacts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    no_issues = subparsers.add_parser("no-issues", help="Write a clean review result.")
    no_issues.add_argument("--out", required=True, help="Review result artifact path.")
    no_issues.add_argument(
        "--audit-summary",
        required=True,
        help=(
            "1-3 句中文：实际审了哪些代码/逻辑、核对了哪些 docstring/契约/边界、"
            "排除了哪些风险类别。这是 clean 结论的可追溯依据，不要写“跑了测试通过”这类绿测试确认。"
        ),
    )
    no_issues.set_defaults(func=command_no_issues, required_fields=("audit_summary",))

    issue = subparsers.add_parser("issue", help="Append an issue to the result artifact.")
    issue.add_argument("--out", required=True, help="Review result artifact path.")
    issue.add_argument("--path", required=True, help="Relative repo path.")
    issue.add_argument("--line", required=True, type=int, help="1-based line number.")
    issue.add_argument("--message", required=True, help="Issue explanation.")
    issue.add_argument(
        "--kind",
        required=True,
        choices=sorted(ISSUE_KINDS),
        help="Issue kind: REAL (true bug/regression), NIT (cosmetic), ELEVATE (needs human design call).",
    )
    issue.add_argument(
        "--severity",
        required=True,
        choices=sorted(ISSUE_SEVERITIES),
        help="Issue severity: high (release blocker), medium (should fix soon), low (deferrable).",
    )
    issue.add_argument(
        "--scope-relation",
        choices=sorted(ISSUE_SCOPE_RELATIONS),
        default=ISSUE_SCOPE_RELATION_IN_CURRENT_ROUND_SCOPE,
        help=(
            "本条问题是否属于本轮范围。本轮改动造成、暴露或加剧的一律算 in_current_round_scope（缺省）；"
            "与本轮改动无直接连带、但真实存在的问题（例如已提交代码里的既有缺陷）用 "
            "outside_current_round_scope——它照常进入合并，到 validate-fix 阶段只验证不修、"
            "由 validator 提交到 Kanban backlog。其他会话未提交的 WIP 不要报。"
        ),
    )
    issue.add_argument(
        "--current-round-scope-audit-summary",
        help=(
            "可选：1-3 句中文，说明本轮范围内实际审了什么、为何判定范围内没有问题。"
            "当你报出的 issue 全部是 outside_current_round_scope 时应当写（等价于 no-issues 的 --audit-summary）。"
        ),
    )
    issue.add_argument("--evidence-command", help="Optional command used as evidence.")
    issue.add_argument("--confidence", help="Optional confidence label.")
    issue.add_argument("--source", help="Optional local note; main merge still owns provenance.")
    issue.set_defaults(func=command_issue, required_fields=("message",))

    lock = subparsers.add_parser("lock-request", help="Append a command lock request.")
    add_common_request_args(lock, ("name", "command", "reason"))
    lock.set_defaults(func=command_lock_request, required_fields=("name", "command", "reason"))

    standard = subparsers.add_parser("standard-request", help="Append a standards request.")
    add_common_request_args(standard, ("domain", "scope", "reason"))
    standard.set_defaults(func=command_standard_request, required_fields=("scope", "reason"))

    measurement = subparsers.add_parser("measurement-request", help="Append a measurement request.")
    add_common_request_args(measurement, ("metric", "command", "reason"))
    measurement.set_defaults(func=command_measurement_request, required_fields=("metric", "command", "reason"))

    subtask = subparsers.add_parser("subtask-request", help="Append a controlled subtask request.")
    add_common_request_args(subtask, ("type", "scope", "reason"))
    subtask.set_defaults(func=command_subtask_request, required_fields=("scope", "reason"))

    context = subparsers.add_parser("context-request", help="Append a missing context request.")
    add_common_request_args(context, ("need", "reason"))
    context.add_argument("--target", help="所需材料的确切路径，供 runner 自动重验可读条件。")
    context.set_defaults(func=command_context_request, required_fields=("reason",))

    for command, field in (("respond-request", "response"), ("resolve-request", "resolution")):
        progress = subparsers.add_parser(command, help="记录补证响应或 reviewer 处理结果。")
        add_common_request_args(progress, ("request_id", field))
        progress.set_defaults(func=command_request_progress, required_fields=("request_id", field))
    complete = subparsers.add_parser("complete", help="无待解决请求时确认完成，保留全部 findings。")
    add_common_request_args(complete, ("audit_summary",))
    complete.set_defaults(func=command_complete, required_fields=("audit_summary",))

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    missing = require_non_empty(args, args.required_fields)
    if missing is not None:
        return missing
    try:
        return args.func(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
