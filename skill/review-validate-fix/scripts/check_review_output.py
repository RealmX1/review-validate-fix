#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


PATH_LINE_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)\b")
ISSUE_PREFIX_RE = re.compile(r"^\d+\.\s+")
NUMBERED_RE = re.compile(r"^\d+\.\s+")
MALFORMED_NUMBERED_RE = re.compile(r"^\d+\)\s+")
LOCK_REQUEST_RE = re.compile(r"^RVF_LOCK_REQUEST\b\s+.+")
FORBIDDEN_RE = re.compile(r"\b(REAL|FALSE_POSITIVE|ELEVATE)\b|</?handoff-context\b[^>]*>")
FORBIDDEN_CONTINUATION_RE = re.compile(
    r"^(没有问题|没问题|無問題|无问题|修复说明|修復說明|已修复|已修復|已修改)\b",
    re.IGNORECASE,
)
LEADING_PROSE_WORDS = {
    "about",
    "after",
    "around",
    "at",
    "before",
    "because",
    "for",
    "from",
    "here",
    "in",
    "inside",
    "near",
    "note",
    "notice",
    "on",
    "see",
    "the",
    "this",
    "warning",
    "with",
    "without",
    "因为",
    "因為",
    "文件",
    "路径",
    "路徑",
    "这里",
    "這裡",
    "此处",
    "此處",
}


def has_path_line_prefix(text: str) -> bool:
    """Return true when text starts with a plausible git path followed by :line."""

    match = PATH_LINE_RE.match(text)
    if match is None:
        return False
    path = match.group("path")
    if not path or path.startswith(("-", "#")) or path != path.strip():
        return False
    first_component = path.split("/", 1)[0]
    if any(char in first_component for char in "，。；"):
        return False
    first_component_words = first_component.split()
    if len(first_component_words) > 2:
        return False
    first_word = first_component_words[0].casefold().strip(":：,，.;。；") if first_component_words else ""
    if first_word in LEADING_PROSE_WORDS:
        return False
    return True


def is_forbidden_continuation(line: str) -> bool:
    return FORBIDDEN_CONTINUATION_RE.match(line) is not None


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

    issue_lines: list[str] = []
    continuation_count = 0
    invalid_lines: list[str] = []
    for line in lines:
        issue_match = ISSUE_PREFIX_RE.match(line)
        if issue_match is not None:
            if has_path_line_prefix(line[issue_match.end() :]):
                issue_lines.append(line)
            else:
                invalid_lines.append(line)
            continue
        malformed_match = MALFORMED_NUMBERED_RE.match(line)
        if malformed_match is not None:
            invalid_lines.append(line)
            continue
        if has_path_line_prefix(line):
            invalid_lines.append(line)
            continue
        if issue_lines and not is_forbidden_continuation(line):
            continuation_count += 1
            continue
        invalid_lines.append(line)

    if not issue_lines:
        errors.append("no numbered path:line issue items found")
    elif invalid_lines:
        errors.append("every issue must start with a numbered path:line item")

    return {
        "valid": not errors,
        "kind": "issues" if issue_lines and not errors else "invalid",
        "issue_count": len(issue_lines) if not errors else 0,
        "continuation_line_count": continuation_count if not errors else 0,
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
