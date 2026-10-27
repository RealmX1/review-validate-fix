"""完成门禁测试共享的合法双审结果 fixture。"""

import json
from pathlib import Path


def write_protocol_valid_completed_dual_reviewer_fixture(run_dir: Path) -> None:
    """完成证明必须有两份合法结果和正常退出，不能只放空 JSON 文件。"""
    inputs = run_dir / "artifacts/inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    contract = inputs / "scope.contract.json"
    if not contract.is_file():
        contract.write_text("{}")
    reviewers = []
    for reviewer_id in ("completion-reviewer-a", "completion-reviewer-b"):
        directory = run_dir / "artifacts/reviewers" / reviewer_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "review-result.json").write_text(json.dumps({"schema_version": 1,
            "kind": "no_issues", "issues": [], "requests": [], "audit_summary": "已独立审查完整范围"}))
        reviewers.append({"reviewer_id": reviewer_id, "returncode": 0})
    (run_dir / "artifacts/reviewers/reviewer-plan.json").write_text(json.dumps({
        "schema_version": 1, "status": "completed", "reviewers": reviewers, "effective_reviewer_count": 2}))
