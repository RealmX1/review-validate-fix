#!/usr/bin/env python3
"""交叉核对冻结明细、独立原始响应与统计分母，写入持久验证回执。"""
import collections
import gzip
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def seconds(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def main():
    summary = json.loads((ROOT / "rvf-codex-resource-summary.json").read_text())
    detail = json.loads((ROOT / "rvf-codex-run-component-measurements.json").read_text())
    ledger = json.loads(gzip.decompress((ROOT / "rvf-codex-response-token-ledger.json.gz").read_bytes()))
    evidence = json.loads((ROOT / "rvf-codex-source-evidence-manifest.json").read_text())
    checks = []

    def verify(label, condition):
        checks.append({"check": label, "passed": bool(condition)})
        assert condition, label

    checked = subprocess.run([sys.executable, str(ROOT / "rvf-codex-stage-resource-measurement.py"), "--self-check"], capture_output=True, text=True)
    verify("继承基线、计数重置、重复快照、响应去重与缓存口径的合成自检", checked.returncode == 0)
    response_ids = [r["identity"] for r in ledger if r["identity"].startswith("resp_")]
    verify("所有组件之间不存在重复的 Codex response_id", len(response_ids) == len(set(response_ids)))
    verify("每条用量满足输入加输出等于总量，缓存及 reasoning 为子集", all(r["usage"]["input_tokens"] + r["usage"]["output_tokens"] == r["usage"]["total_tokens"] and 0 <= r["usage"]["cached_input_tokens"] <= r["usage"]["input_tokens"] and 0 <= r["usage"]["reasoning_output_tokens"] <= r["usage"]["output_tokens"] for r in ledger))
    grouped = collections.defaultdict(int)
    for row in ledger:
        grouped[(row["run_id"], row["component_id"]) ] += row["usage"]["total_tokens"]
    verify("逐响应明细与逐组件总量相等", all(row["usage"] is None or row["usage"]["total_tokens"] == grouped[(row["run_id"], row["component_id"])] for row in detail["components"]))
    verify("每条核算记录的来源均有 SHA256 清单", all(row["source"] in evidence and (not row.get("wire_source") or row["wire_source"] in evidence) for row in ledger))
    primary_ids = set(summary["primary_7_days"]["principal_complete_run_ids"])
    primary = [r for r in detail["runs"] if r["run_id"] in primary_ids]
    primary_components = [r for r in detail["components"] if r["run_id"] in primary_ids]
    verify("主样本分母等于所有已归属组件之和", sum(r["observed_usage"]["total_tokens"] for r in primary) == sum(r["usage"]["total_tokens"] for r in primary_components) == summary["primary_7_days"]["observed_usage_principal_cohort"]["total_tokens"])
    verify("Analyze 分子包括后台主线程和其派生子代理", sum(r["usage"]["total_tokens"] for r in primary_components if r["stage"] == "analyze") == summary["primary_7_days"]["analyze_usage_principal_cohort"]["total_tokens"])
    completed = [r for r in detail["runs"] if r["started_at_utc"] >= summary["primary_window_start_utc"] and r.get("analyze_status") == "completed"]
    footer_checks = []
    for run in completed:
        row = next(r for r in detail["components"] if r["run_id"] == run["run_id"] and r["component_id"] == "analyze")
        footer_checks.append({"run_id": run["run_id"], "footer_tokens_used": run["analyze_footer_tokens_used"], "noncached_input_plus_output": row["usage"]["noncached_input_plus_output_tokens"] if row["usage"] else None})
    verify("所有已结束 Analyze 页脚与该主线程非缓存输入加输出一致", all(r["footer_tokens_used"] == r["noncached_input_plus_output"] for r in footer_checks))
    verify("全流程墙钟由主流程、派发间隔和 Analyze 主线程组成", all(abs(r["full_wall_seconds"] - r["main_wall_seconds"] - r["scaffold_and_analyze_dispatch_gap_seconds"] - r["analyze_wall_seconds"]) < 0.001 for r in completed))
    verify("主样本所有主流程与 Analyze 均在截止前结束", all(r["finalized_at_cutoff"] and r.get("analyze_status") == "completed" and seconds(r["analyze_finished_at_utc"]) <= seconds(summary["cutoff_utc"]) for r in primary))

    ordered = sorted(primary, key=lambda r: r["observed_usage"]["total_tokens"])
    selected = {max(primary, key=lambda r: r["started_at_utc"])["run_id"]: "最新轮次", ordered[len(ordered) // 2]["run_id"]: "总 Token 中位附近轮次", ordered[-1]["run_id"]: "总 Token 最大轮次"}
    samples = []
    for run_id, reason in selected.items():
        run = next(r for r in primary if r["run_id"] == run_id)
        main_rows = [r for r in primary_components if r["run_id"] == run_id and r["stage"] in ("main_prepare", "main_review_coordination", "main_post_review_combined")]
        path = Path(main_rows[0]["source"])
        start, end = seconds(run["started_at_utc"]), seconds(run["finalize_completed_at_utc"])
        observed, seen = 0, set()
        # 独立读取源文件，直接累加逐响应原字段，不调用采集器的解析函数。
        for raw in path.open():
            record = json.loads(raw)
            if record.get("type") != "token_usage_record":
                continue
            payload = record.get("payload", {})
            if payload.get("response_id") in seen or not start <= seconds(record["timestamp"]) <= end:
                continue
            seen.add(payload.get("response_id"))
            observed += payload.get("usage", {}).get("total_tokens", 0)
        expected = sum(r["usage"]["total_tokens"] for r in main_rows)
        verify(f"原始响应独立抽样：{reason} {run_id}", observed == expected)
        samples.append({"run_id": run_id, "selection_reason": reason, "source": str(path), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "raw_response_total_tokens": observed, "reported_main_total_tokens": expected})
    output = {"cutoff_utc": summary["cutoff_utc"], "status": "passed", "checks": checks, "self_check_stdout": checked.stdout,
              "ledger_record_count": len(ledger), "distinct_codex_response_id_count": len(response_ids),
              "analyze_footer_cross_checks": footer_checks, "independent_raw_response_samples": samples}
    (ROOT / "resource-accounting-verification.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(f"{len(checks)} 项核算验证全部通过；{len(ledger)} 条用量记录，{len(samples)} 个独立原始响应抽样。")


if __name__ == "__main__":
    main()
