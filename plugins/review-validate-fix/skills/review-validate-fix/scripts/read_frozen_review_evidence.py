#!/usr/bin/env python3
"""从候选绑定的持久索引分页读取冻结证据。"""

import argparse
import json
import os
from pathlib import Path

import _rvf_pyroot  # noqa: F401
from core.session_scope_allocation.frozen_review_unit_evidence import read_frozen_evidence_page


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True)
    parser.add_argument("--unit-id", required=True)
    parser.add_argument("--content", choices=("patch", "before", "after"), default="patch")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--max-bytes", type=int, default=32768)
    parser.add_argument("--candidate-commit", default=os.environ.get("RVF_REVIEW_CANDIDATE_COMMIT"))
    args = parser.parse_args()
    try:
        index = json.loads(Path(args.index).read_text(encoding="utf-8"))
        if args.candidate_commit and index.get("candidate_commit") != args.candidate_commit:
            raise ValueError("冻结证据不属于本腿候选")
        unit = next(unit for unit in index["units"] if unit["unit_id"] == args.unit_id)
        descriptor = unit[args.content]
        page = read_frozen_evidence_page(Path(descriptor["file"]), expected_sha256=descriptor["sha256"],
                                         offset=args.offset, max_bytes=args.max_bytes)
        print(json.dumps({"candidate_commit": index["candidate_commit"], "unit_id": args.unit_id, **page}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        print(json.dumps({"error": str(exc), "evidence_status": "unverified"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
