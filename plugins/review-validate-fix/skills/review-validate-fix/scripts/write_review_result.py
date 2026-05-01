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


def base_payload(kind: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "issues": [],
        "requests": [],
    }


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
    payload = base_payload("no_issues")
    write_payload(Path(args.out), payload)
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
    else:
        if existing.get("kind") != "issues":
            return fail("cannot append issue to a non-issues review result")
        payload = existing
        if not isinstance(payload.get("issues"), list):
            return fail("existing issues field must be an array")

    issue: dict[str, Any] = {
        "path": args.path,
        "line": args.line,
        "message": args.message.strip(),
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
    else:
        if existing.get("kind") != "request":
            return fail("cannot append request to a completed review result")
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
    no_issues.set_defaults(func=command_no_issues, required_fields=())

    issue = subparsers.add_parser("issue", help="Append an issue to the result artifact.")
    issue.add_argument("--out", required=True, help="Review result artifact path.")
    issue.add_argument("--path", required=True, help="Relative repo path.")
    issue.add_argument("--line", required=True, type=int, help="1-based line number.")
    issue.add_argument("--message", required=True, help="Issue explanation.")
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
