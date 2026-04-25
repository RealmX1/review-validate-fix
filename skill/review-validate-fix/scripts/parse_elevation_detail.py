#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys


REQUIRED_FIELDS = ("title", "stuck_reason", "issue_restate", "options")


def read_text(path: str | None) -> str:
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    return sys.stdin.read()


def parse_block(block: str) -> dict:
    data: dict[str, object] = {"options": []}
    current = None

    for raw_line in block.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.startswith("title:"):
            data["title"] = line.split(":", 1)[1].strip()
            current = None
        elif line.startswith("stuck_reason:"):
            data["stuck_reason"] = line.split(":", 1)[1].strip()
            current = None
        elif line.startswith("issue_restate:"):
            data["issue_restate"] = line.split(":", 1)[1].strip()
            current = None
        elif line.startswith("options:"):
            current = "options"
        elif current == "options" and re.match(r"^\s*-\s+[A-Z]:", line):
            data["options"].append(line.strip()[2:].strip())
        elif current == "options" and data["options"]:
            data["options"][-1] = f"{data['options'][-1]} {line.strip()}"

    return data


def validate_block(data: dict, index: int) -> list[str]:
    errors = []
    for field in REQUIRED_FIELDS:
        value = data.get(field)
        if value is None or value == "" or value == []:
            errors.append(f"block {index}: 缺少 {field}")
    options = data.get("options", [])
    if isinstance(options, list) and len(options) < 2:
        errors.append(f"block {index}: options 少于 2 个，降级为需要手动提供候选方向")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse elevation-detail fenced blocks.")
    parser.add_argument("path", nargs="?", help="Input file. Defaults to stdin.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    args = parser.parse_args()

    text = read_text(args.path)
    blocks = re.findall(r"```elevation-detail\s*\n(.*?)\n```", text, flags=re.DOTALL)

    parsed = []
    errors = []
    for index, block in enumerate(blocks, start=1):
        data = parse_block(block)
        parsed.append(data)
        errors.extend(validate_block(data, index))

    if not blocks:
        errors.append("未找到 elevation-detail fenced block")

    result = {
        "ok": not errors,
        "count": len(blocks),
        "blocks": parsed,
        "errors": errors,
    }

    indent = 2 if args.pretty else None
    print(json.dumps(result, ensure_ascii=False, indent=indent))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
