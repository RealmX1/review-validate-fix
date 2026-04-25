#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ISSUE_RE = re.compile(r"^\d+\.\s+.+:\d+\b")
LOCK_REQUEST_RE = re.compile(r"^RVF_LOCK_REQUEST\b\s+.+")
FORBIDDEN_RE = re.compile(r"\b(REAL|FALSE_POSITIVE|ELEVATE)\b|</?handoff-context\b[^>]*>")


def classify(text: str) -> dict[str, object]:
    stripped = text.strip()
    if stripped == "NO_ISSUES":
        return {"valid": True, "kind": "no_issues", "issue_count": 0, "errors": []}

    errors: list[str] = []
    if not stripped:
        errors.append("empty output")
    if "NO_ISSUES" in stripped:
        errors.append("NO_ISSUES must be the only output")
    if FORBIDDEN_RE.search(stripped):
        errors.append("review output contains validate/fix or handoff markers")

    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    lock_request_lines = [line for line in lines if LOCK_REQUEST_RE.match(line)]
    if lock_request_lines:
        if len(lock_request_lines) != len(lines):
            errors.append("lock request output must contain only RVF_LOCK_REQUEST lines")
        return {
            "valid": not errors,
            "kind": "lock_request" if not errors else "invalid",
            "issue_count": 0,
            "lock_request_count": len(lock_request_lines) if not errors else 0,
            "lock_requests": lock_request_lines if not errors else [],
            "errors": errors,
        }

    issue_lines = [line for line in lines if ISSUE_RE.match(line)]
    if not issue_lines:
        errors.append("no numbered path:line issue items found")
    elif len(issue_lines) != len(lines):
        errors.append("every nonblank line must be a numbered path:line issue item")

    return {
        "valid": not errors,
        "kind": "issues" if issue_lines and not errors else "invalid",
        "issue_count": len(issue_lines) if not errors else 0,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate review-validate-fix review output contract.")
    parser.add_argument("output_file", nargs="?", help="Review output file. Reads stdin when omitted.")
    parser.add_argument("--json", action="store_true", help="Print JSON classification.")
    args = parser.parse_args()

    if args.output_file:
        text = Path(args.output_file).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()

    result = classify(text)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["valid"]:
        print(result["kind"])
    else:
        for error in result["errors"]:
            print(error, file=sys.stderr)

    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
