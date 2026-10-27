#!/usr/bin/env python3
"""只读重放已记录的冻结大包，把新输入及核验报告写到独立交接目录。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"))
import build_review_packet
import run_alternative_reviewer
import check_review_result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--investigation-evidence-file", type=Path, required=True)
    parser.add_argument("--run-logs-root", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    evidence = json.loads(args.investigation_evidence_file.read_text())
    output = args.output_directory.resolve()
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    for case in evidence["cases"]:
        artifacts = args.run_logs_root / "runs" / case["runId"] / "artifacts"
        if output.is_relative_to(artifacts.resolve()):
            raise ValueError("重放产物不能覆盖原始 run")
        original = (artifacts / "review-packet.md").read_bytes()
        assert hashlib.sha256(original).hexdigest() == case["packetSha256"], "原始包摘要与调查记录不一致"
        metadata = json.loads((artifacts / "review-packet.metadata.json").read_text())
        manifest = json.loads((artifacts / "session-manifest.json").read_text())
        contract = json.loads((artifacts / "inputs/scope.contract.json").read_text())
        scope = manifest["tracker"]["tracker_scope"]
        assert set(scope["unit_ids"]) == set(contract["primary_units"])
        repo = Path(metadata["repo"])
        source = build_review_packet.FrozenCandidatePacketGitSource(repo, metadata["frozen_candidate_commit"])
        target = output / case["token"]
        target.mkdir(exist_ok=True)
        # 直接使用冻结 renderer，避免查询、迁移或刷新其他任务的活动 tracker。
        packet, metadata = build_review_packet.build_frozen_scope_packet(source, scope, metadata,
            (artifacts / "scope-of-work.md").read_text(), scope["paths"], [], metadata["max_file_bytes"],
            metadata["owned_diff_base_ref"], metadata["excluded_path_prefixes"], target / "frozen-review-evidence",
            "历史原始包：" + str(artifacts / "review-packet.md"))
        packet_path = target / "review-packet.md"
        packet_path.write_text(packet)
        (target / "review-packet.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        replay_contract = {**contract, **{field: metadata[field] for field in (
            "frozen_evidence_complete", "frozen_evidence_index", "frozen_evidence_index_sha256")}}
        replay_contract_path = target / "replay-scope.contract.json"
        replay_contract_path.write_text(json.dumps(replay_contract, ensure_ascii=False, indent=2))
        evidence_readable, evidence_errors = check_review_result.frozen_input_evidence_completeness(replay_contract)
        prompt = run_alternative_reviewer.build_prompt(run_alternative_reviewer.DEFAULT_PROMPT, None, packet_path,
            repo, replay_contract_path)
        (target / "reviewer.prompt.txt").write_text(prompt)
        units = metadata["frozen_evidence_units"]
        report = {"run_id": case["runId"], "original_packet_bytes": len(original), "new_packet_bytes": len(packet.encode()),
            "final_prompt_bytes": len(prompt.encode()), "scope_unit_count": len(scope["unit_ids"]),
            "indexed_unit_count": len(units), "verified_unit_count": sum(unit["evidence_status"] == "verified" for unit in units),
            "evidence_gaps": metadata["frozen_evidence_gaps"], "full_history_inline": "## Full Git Diff" in packet,
            "all_indexed_evidence_readable": evidence_readable, "evidence_read_errors": evidence_errors,
            "source_packet_sha256": hashlib.sha256(original).hexdigest(),
            "source_packet_unchanged": hashlib.sha256((artifacts / "review-packet.md").read_bytes()).hexdigest() == case["packetSha256"]}
        reports.append(report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    (output / "replay-results.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0 if all(report["verified_unit_count"] == report["scope_unit_count"] and report["final_prompt_bytes"] <= 128 * 1024
                    and report["all_indexed_evidence_readable"] and not report["full_history_inline"]
                    and report["source_packet_unchanged"] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
