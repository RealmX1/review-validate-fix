#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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

# 唯一**携带 finding** 的完成态：一旦写进 canonical，就不能再被换成别的 kind。
# （另一个完成态 ``no_issues`` 只携带 audit_summary，被取代不会藏掉任何缺陷，故不设终态。）
FINDING_BEARING_TERMINAL_KIND = "issues"


def ensure_supersession_allowed(existing: dict[str, Any], new_kind: str) -> None:
    """归档式换 kind 的唯一收口闸：**已记录 issues 的工件不得被换成别的 kind**。

    为什么归档不够（2026-08-15 RVF-003）：归档保住的是**取证**能力，不是**判定**能力。
    canonical ``kind`` 的读者——``check_review_result.classify``、``dispatch_reviewers``
    的 ``_leg_produced_review_result`` / ``_leg_review_request_pending``、
    ``write_reviewed_task_candidate_evidence`` 的 protocol 校验、``analysis_artifacts``
    的 issue 抽取、以及 ``references/review-merge-policy.md`` 的合并规则——**没有一个**
    会去合并 ``superseded`` 链。于是 ``issues → no_issues`` 之后，已找到的 REAL issue 只
    躺在归档里，下游一律按 clean 合并：假绿，而且是最贵的那种（评审跑过了、结论被吞了）。

    为什么**只**把 ``issues`` 设成终态，而不是把两个完成态都设成终态：另一个方向
    （``no_issues → issues``）恰恰是在**补**结论，把它一并封死等于让「先误判 clean、
    后发现真 bug」的 reviewer 永远交不出那条 issue——那是同一类假绿的镜像。同理
    ``no_issues → request``（clean 退回待处理）是 fail-closed 方向，也不封。

    误报怎么办：交给 validate/fix 环节判 ``false_positive``，而不是让 reviewer 把它从
    结论里抹掉——前者留下判定痕迹，后者不留。开新一轮评审怎么办：由
    ``dispatch_reviewers.py --redispatch-review-request-legs-only`` 先把旧工件轮转进
    归档目录，新一轮从**没有 canonical 文件**的干净起点开始，本闸自然不触发。
    """
    old_kind = str(existing.get("kind") or "unknown")
    if old_kind == FINDING_BEARING_TERMINAL_KIND and new_kind != FINDING_BEARING_TERMINAL_KIND:
        raise ValueError(
            f"已记录 issues 的结果工件是终态，不能改写成 {new_kind}：canonical 消费者只读 "
            "kind、不会合并 superseded 链，这一步会把已发现的 REAL issue 从最终结论里抹掉，"
            "下游按 clean 合并。误报请在 validate/fix 环节判 false_positive；要开新一轮评审，"
            "请由 dispatch_reviewers.py --redispatch-review-request-legs-only 先轮转旧工件。"
        )


def base_payload(kind: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "issues": [],
        "requests": [],
    }


def supersede_existing_result(out: Path, existing: dict[str, Any], new_kind: str) -> dict[str, Any]:
    """把当前这份**另一 kind** 的结果工件归档，并返回新 payload（带指向归档的溯源链）。

    为什么需要它（2026-08-15 run 实测的死结）：旧规则是「kind 不一致就报错」，于是一份
    ``kind: request`` 的结果在同一 run 内**永远**转不成 ``issues``——reviewer 契约又禁止它删改
    工件，两条规则合起来把整轮评审锁死（实测废掉一整轮 codex 评审）。而同一份代码里
    ``no-issues`` 却是**无条件原地覆写**：``request → no_issues`` 放行、``request → issues`` 禁止，
    契约不对称，合法 request 可以无归档地凭空消失。

    统一口径：**同 kind ⇒ 原地追加/覆写；换 kind ⇒ 先归档旧工件，再起一份新的**——但换 kind
    先过 ``ensure_supersession_allowed``：**已记录 issues 的工件是终态**，因为归档只保住取证
    能力、保不住判定能力（canonical 消费者不读 ``superseded`` 链，见该函数 docstring）。

    崩溃语义：先 ``os.replace`` 归档、再写新文件。中间崩溃 ⇒ canonical 文件缺失但内容完整躺在
    归档里（可恢复、且绝不会出现「半份混合内容」）。归档目录与 canonical 同父目录，故 replace
    在同一文件系统内，是原子的。闸在 ``os.replace`` **之前**，故被拒时磁盘一个字节都没动。
    """
    ensure_supersession_allowed(existing, new_kind)
    archive_dir = out.parent / SUPERSEDED_RESULT_ARTIFACTS_DIRNAME
    archive_dir.mkdir(parents=True, exist_ok=True)
    old_kind = str(existing.get("kind") or "unknown")
    sequence = 1 + sum(1 for _ in archive_dir.glob("review-result.*.json"))
    archive_path = archive_dir / f"review-result.{sequence:02d}.{old_kind}.json"
    os.replace(out, archive_path)

    payload = base_payload(new_kind)
    lineage = [
        entry
        for entry in (existing.get("superseded") or [])
        if isinstance(entry, dict)
    ]
    lineage.append(
        {
            "kind": old_kind,
            "path": str(archive_path),
            "superseded_at": datetime.now(timezone.utc).isoformat(),
            "superseded_by_kind": new_kind,
        }
    )
    payload["superseded"] = lineage
    return payload


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    ensure_out_allowed(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def optional_fields(args: argparse.Namespace, names: tuple[str, ...]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name in names:
        value = getattr(args, name, None)
        if value:
            fields[name.replace("_", "-")] = value
    return fields


def command_no_issues(args: argparse.Namespace) -> int:
    out = Path(args.out)
    existing = read_existing(out)
    if existing is not None and existing.get("kind") != "no_issues":
        # 换 kind ⇒ 先归档。历史上这里是**无条件覆写**：一份合法的 request（或已找到的 issues）
        # 会被一句 no-issues 无声抹掉，事后连它存在过都查不到。
        # 注意 ``issues → no_issues`` 现在被 ``ensure_supersession_allowed`` 直接拒绝：归档
        # 保住了取证、保不住判定，canonical 读者只看 kind，这条路等于把 REAL issue 洗成 clean。
        payload = supersede_existing_result(out, existing, "no_issues")
    else:
        payload = base_payload("no_issues")
        if existing is not None:
            payload["superseded"] = [
                entry for entry in (existing.get("superseded") or []) if isinstance(entry, dict)
            ]
    payload["audit_summary"] = args.audit_summary.strip()
    write_payload(out, payload)
    return 0


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
    elif existing.get("kind") != "issues":
        # 换 kind ⇒ 归档旧工件后起新的。旧规则在这里直接报错，于是 ``request → issues`` 在同一
        # run 内**永远**走不通（而 ``request → no_issues`` 却放行）——那正是把整轮评审锁死的死结。
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
    issue.update(optional_fields(args, ("evidence_command", "confidence", "source")))
    payload.setdefault("issues", []).append(issue)
    payload["requests"] = []
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_payload(out, payload)
    return 0


def append_request(args: argparse.Namespace, request: dict[str, Any]) -> int:
    out = Path(args.out)
    existing = read_existing(out)
    if existing is None:
        payload = base_payload("request")
    elif existing.get("kind") != "request":
        # 与 no_issues 同一口径：换 kind ⇒ 归档旧工件再起新的（``no_issues → request`` 是
        # 「已完成退回待处理」，fail-closed 方向，放行）。唯一被拒的是 ``issues → request``：
        # 它同样会把已找到的 REAL issue 从 canonical 结论里摘掉（见 ensure_supersession_allowed）。
        payload = supersede_existing_result(out, existing, "request")
    else:
        payload = existing
        if not isinstance(payload.get("requests"), list):
            return fail("existing requests field must be an array")

    payload["issues"] = []
    payload.setdefault("requests", []).append(request)
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
        },
    )


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
    context.set_defaults(func=command_context_request, required_fields=("reason",))

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
