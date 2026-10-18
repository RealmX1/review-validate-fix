#!/usr/bin/env python3
"""从冻结的统计 JSON 生成中文报告和可导出的占比图，不读取生产运行目录。"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties


ROOT = Path(__file__).resolve().parent
SUMMARY = json.loads((ROOT / "rvf-codex-resource-summary.json").read_text())
DETAILS = json.loads((ROOT / "rvf-codex-run-component-measurements.json").read_text())
PRIMARY = SUMMARY["primary_7_days"]
COMPARISON = SUMMARY["comparison_30_days"]
COHORT_IDS = set(PRIMARY["principal_complete_run_ids"])
COHORT = [r for r in DETAILS["runs"] if r["run_id"] in COHORT_IDS]
STAGES = PRIMARY["stages"]
ORDER = ["main_prepare", "main_review_coordination", "main_post_review_combined", "external_reviewer", "validation_subagent", "main_host_auxiliary_subagent", "analyze"]


def percent(value):
    return f"{value * 100:.2f}%" if value is not None else "未知"


def million(value):
    return f"{value / 1_000_000:.3f} M"


def minute(value):
    return f"{value / 60:.2f} 分钟" if value is not None else "未知"


def table(headers, rows):
    return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |", *("| " + " | ".join(str(value) for value in row) + " |" for row in rows)])


def draw_chart():
    candidates = [Path("/System/Library/Fonts/PingFang.ttc"), Path("/System/Library/Fonts/STHeiti Light.ttc")]
    font_path = next((p for p in candidates if p.exists()), None)
    if font_path:
        font = FontProperties(fname=str(font_path))
        matplotlib.rcParams["font.family"] = font.get_name()
    matplotlib.rcParams["svg.fonttype"] = "path"
    matplotlib.rcParams["svg.hashsalt"] = "rvf-codex-resource-share-2026-10-18"
    colors = ["#ced8e3", "#a8bbcf", "#7c9ab8", "#477998", "#4e587c", "#d7cbdf", "#ee8b35"]
    fig, axis = plt.subplots(figsize=(12, 5.8))
    fig.patch.set_facecolor("#fafafa")
    axis.set_facecolor("#fafafa")
    for index, metric in enumerate(["total_tokens", "noncached_input_plus_output_tokens"]):
        total = PRIMARY["observed_usage_principal_cohort"][metric]
        left = 0
        for stage, color in zip(ORDER, colors):
            width = STAGES[stage]["usage"][metric] / total * 100
            axis.barh(2 - index, width, left=left, color=color, height=.48, edgecolor="white", linewidth=1)
            if stage == "analyze":
                axis.text(101.5, 2 - index, f"Analyze {width:.2f}%", va="center", color="#b85a0c", fontweight="bold", fontsize=12)
            left += width
    share = PRIMARY["analyze_elapsed_share"] * 100
    axis.barh(0, 100 - share, height=.48, color="#a8bbcf", edgecolor="white")
    axis.barh(0, share, left=100 - share, height=.48, color=colors[-1], edgecolor="white")
    axis.text(101.5, 0, f"Analyze {share:.2f}%", va="center", color="#b85a0c", fontweight="bold", fontsize=12)
    axis.set_yticks([2, 1, 0], ["总 Token（含缓存输入）", "非缓存输入＋输出", "逐轮全流程墙钟时间"])
    axis.set_xlim(0, 126)
    axis.set_xticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
    axis.tick_params(axis="both", length=0, labelsize=11)
    for spine in axis.spines.values():
        spine.set_visible(False)
    axis.set_title("Analyze 在 Codex RVF 中的占比", loc="left", fontsize=18, fontweight="bold", pad=28)
    labels = ["主 Agent 准备", "review 期间协调", "归并／验证修复／交接", "独立 reviewer", "验证修复子代理", "宿主辅助子代理", "Analyze（含其子代理）"]
    handles = [plt.Rectangle((0, 0), 1, 1, color=color) for color in colors]
    axis.legend(handles, labels, loc="upper left", bbox_to_anchor=(0, -.20), ncol=3, frameon=False, fontsize=10)
    fig.text(.02, .015, f"截止 {SUMMARY['cutoff_utc']}；Token：{len(COHORT)} 轮主要组件可观测；时间：18 轮。健康探测 Token 缺失；墙钟时间含等待。", fontsize=9, color="#555555")
    fig.subplots_adjust(left=.22, right=.95, top=.81, bottom=.31)
    svg_path = ROOT / "rvf-analyze-resource-share-overview.svg"
    fig.savefig(svg_path, facecolor=fig.get_facecolor(), metadata={"Date": SUMMARY["cutoff_utc"]})
    # Matplotlib 默认在 path 属性的续行末尾保留空格，规范化后便于 Git 检查。
    svg_path.write_text("\n".join(line.rstrip() for line in svg_path.read_text().splitlines()) + "\n")
    fig.savefig(ROOT / "rvf-analyze-resource-share-overview.png", dpi=180, facecolor=fig.get_facecolor())
    plt.close(fig)


def write_report():
    totals = PRIMARY["observed_usage_principal_cohort"]
    analysis = PRIMARY["analyze_usage_principal_cohort"]
    components = [r for r in DETAILS["components"] if r["run_id"] in COHORT_IDS]
    stage_rows = []
    for stage in ORDER:
        value = STAGES[stage]
        stage_rows.append([value["label"], value["component_count"], million(value["usage"]["total_tokens"]), percent(value["usage"]["total_tokens"] / totals["total_tokens"]), million(value["usage"]["noncached_input_plus_output_tokens"]), percent(value["usage"]["noncached_input_plus_output_tokens"] / totals["noncached_input_plus_output_tokens"])])
    timing_rows = []
    for stage in ORDER:
        value = STAGES[stage]
        if stage == "analyze":
            timing_rows.append(["Analyze 主线程", 18, minute(PRIMARY["analyze_wall_seconds_median"]), minute(PRIMARY["analyze_wall_seconds_p90"]), minute(PRIMARY["analyze_wall_seconds_sum"])])
        else:
            timing_rows.append([value["label"], value["component_count"], minute(value["process_wall_seconds_median"]), minute(value["process_wall_seconds_p90"]), minute(value["process_wall_seconds_sum"])])
    project_rows = [[project, value["run_count"], value["principal_complete_run_count"], percent(value["analyze_observed_total_token_share"]), percent(value["analyze_observed_noncached_plus_output_share"]), percent(value["analyze_elapsed_share"])] for project, value in SUMMARY["primary_by_project"].items()]
    version_rows = [[version, value["run_count"], value["principal_complete_run_count"], percent(value["analyze_observed_total_token_share"]), percent(value["analyze_elapsed_share"])] for version, value in SUMMARY["comparison_by_recorded_deployment_label"].items()]
    run_rows = []
    primary_all = [r for r in DETAILS["runs"] if r["started_at_utc"] >= SUMMARY["primary_window_start_utc"]]
    for run in primary_all:
        run_rows.append([run["run_id"], f"{run['project']}/{run['task_id']}", minute(run["main_wall_seconds"]), minute(run.get("analyze_wall_seconds")), percent(run.get("analyze_observed_total_token_share")) if run["principal_component_tokens_complete"] else "分母缺失", percent(run.get("analyze_observed_noncached_plus_output_share")) if run["principal_component_tokens_complete"] else "分母缺失", "是" if run.get("analyze_formal_archive_complete_at_inspection") else ("否" if run.get("analyze_status") == "completed" else "未运行／未结束")])
    latest = max(COHORT, key=lambda r: r["started_at_utc"])
    latest_main = sum(row["usage"]["total_tokens"] for row in components if row["run_id"] == latest["run_id"] and row["stage"].startswith("main_") and row["usage"])
    longest = max((r for r in DETAILS["runs"] if r.get("full_wall_seconds")), key=lambda r: r["full_wall_seconds"])
    archive_todos = [(r["run_id"], r.get("analyze_formal_archive_todo_count")) for r in primary_all if r.get("analyze_status") == "completed" and not r.get("analyze_formal_archive_complete_at_inspection")]
    text = f"""# Codex RVF 各组件耗时、Token 与 Analyze 占比实测

统计截止：**{SUMMARY['cutoff_utc']}**。主窗口为 {SUMMARY['primary_window_start_utc']} 至截止；对照窗口为此前 30 天。对象是所有项目中由 Cline Kanban 管理、主 Agent 为 Codex 的实际 RVF 轮次。

## 直接结论

**Analyze 占主要可观测组件总 Token 的 {percent(PRIMARY['analyze_observed_total_token_share'])}；按“非缓存输入＋输出”统计，占 {percent(PRIMARY['analyze_observed_noncached_plus_output_share'])}；占本轮 RVF 启动至 Analyze 结束的墙钟时长 {percent(PRIMARY['analyze_elapsed_share'])}。**

按非缓存输入＋输出看，独立 reviewer 与验证修复子代理合计 {percent((STAGES['external_reviewer']['usage']['noncached_input_plus_output_tokens'] + STAGES['validation_subagent']['usage']['noncached_input_plus_output_tokens']) / totals['noncached_input_plus_output_tokens'])}；Analyze 的消耗占比低于这两类，也低于主 Agent 的归并／修复／交接区间。

- Token 来自 {PRIMARY['principal_complete_run_count']} 轮，覆盖 18 轮已结束 Analyze 的 {percent(PRIMARY['principal_complete_coverage_of_completed_analyze'])}；累计分母 {totals['total_tokens']:,}，Analyze {analysis['total_tokens']:,}。非缓存输入＋输出的分母为 {totals['noncached_input_plus_output_tokens']:,}，Analyze {analysis['noncached_input_plus_output_tokens']:,}。
- Analyze 主线程每轮中位数 **{minute(PRIMARY['analyze_wall_seconds_median'])}**，P90 {minute(PRIMARY['analyze_wall_seconds_p90'])}。18 轮主流程墙钟累计 {minute(PRIMARY['main_wall_seconds_sum'])}，Analyze 累计 {minute(PRIMARY['analyze_wall_seconds_sum'])}，从启动到 Analyze 结束累计 {minute(PRIMARY['full_wall_seconds_sum'])}。这些是逐轮相加，不能当作机器上顺序执行的实际总等待。
- 当前代码在 finalize 后派发 detached tmux 线程，原 Task 无需等待 Analyze。**其后台持续时间不等于主任务增加同等等待**；共享额度或机器资源的间接竞争，现有记录不足以量化。
- 18 次 Analyze 进程均以 0 退出；采集时仅 **{PRIMARY['formal_archive_complete_analyze_count']}/18** 通过正式归档检查，其余 {18 - PRIMARY['formal_archive_complete_analyze_count']} 份仍有 TODO。所有这些已发生的消耗均保留计账。

这里的 Token 百分比是**已观测组件口径**。{PRIMARY['health_probe_attempt_count_in_principal_cohort']} 次健康探测的 usage 未完整保存，不能宣称全量精确占比；在其余归属成立且仅遗漏非负的探测用量时，两个 Token 百分比是相应全量占比的上界。它们也不是美元费用或订阅额度消耗比例。

![Analyze 占比](rvf-analyze-resource-share-overview.svg)

## 各组件的 Token 消耗

同一 {len(COHORT)} 轮样本；M = 1,000,000 Token。reviewer 含健康检查失败后启动的 fallback；Analyze 一栏包括其主线程及其派生的辅助子代理。

{table(['组件', '独立记录数', '总 Token', '总 Token 占比', '非缓存输入＋输出', '该口径占比'], stage_rows)}

主 Agent 三个区间是按实际 reviewer 开始／结束时间窗划分；“归并、验证修复、最终验证与交接”缺少可靠的独立 phase 事件，合并展示。它们的 Token 归属可复算，但不能解释为每条响应都经过语义分类。宿主自动派生的 `skill_learner` 单列为辅助子代理。

Analyze 的缓存输入占自身输入的 {percent(analysis['cached_input_tokens'] / analysis['input_tokens'])}，全部组件合计为 {percent(totals['cached_input_tokens'] / totals['input_tokens'])}。大量重复上下文被缓存后，两种 Token 占比明显不同。已逐一核对 18 个 Analyze 主线程：`codex exec` 日志末尾的 `tokens used` **等于该线程非缓存输入＋输出**，而不是包含缓存读取的总 Token；额外辅助子代理另行相加。

## 各组件的时长

除 Analyze 为全部 18 个已结束主线程外，下表沿用 Token 主样本的 {len(COHORT)} 轮；reviewer、验证修复子代理按单个进程统计。父会话等待、独立 reviewer、子代理和后台 Analyze 可以重叠，**不能把本表累计列相加当作端到端耗时**。少数子代理只有 spawn 与最后 usage，持续时间是下界，明细中已标注。

{table(['组件', '样本数', '单组件中位数', 'P90', '持续时间累计'], timing_rows)}

确定性 finalize 在 {len(COHORT)} 轮中累计 {STAGES['finalize']['process_wall_seconds_sum']:.3f} 秒，中位数 {STAGES['finalize']['process_wall_seconds_median']:.3f} 秒，本身不调用 LLM。代码把 `completed_at` 写在 scaffold 之前，因此该数字**不是 scaffold 的独立耗时**；scaffold 没有单独的可靠起止事件，只能保留 finalize 结束到 Analyze 派发的混合间隔。它不与 LLM Analyze 的几分钟混为一项。

单轮 Analyze 总 Token 占比中位数 {percent(PRIMARY['per_run_analyze_total_token_share_median'])}、P90 {percent(PRIMARY['per_run_analyze_total_token_share_p90'])}；单轮时长占比中位数 {percent(PRIMARY['per_run_analyze_elapsed_share_median'])}、P90 {percent(PRIMARY['per_run_analyze_elapsed_share_p90'])}。主结论采用“分子之和／分母之和”，并非单轮比例的平均值。

## 样本覆盖、项目与历史对照

近 7 天找到 {PRIMARY['run_count']} 个实际轮次：{PRIMARY['completed_analyze_count']} 轮 Analyze 已结束、{PRIMARY['finalized_without_recorded_analyze_count']} 轮已 finalize 但没有线程启动记录、{PRIMARY['unfinished_at_cutoff_count']} 轮截至采样时尚未 finalize。后两类逐轮列出，不按零 Analyze 用量混入完成样本。已结束轮次中 `rvf-20261017T222629Z-agentic-initiation-start-be571a86` 的 `kimi-code` 用量无法恢复，排除于主 Token 分母，但保留在时长样本中。

{table(['项目', '实际轮次', 'Token 完整样本', 'Analyze 总 Token 占比', '非缓存输入＋输出占比', 'Analyze 墙钟占比'], project_rows)}

近 30 天找到 {COMPARISON['run_count']} 个 Codex RVF 轮次，其中 {COMPARISON['completed_analyze_count']} 轮 Analyze 已结束，只有 {COMPARISON['principal_complete_run_count']} 轮主要组件 Token 可恢复，覆盖率 {percent(COMPARISON['principal_complete_coverage_of_completed_analyze'])}；该可观测子集的 Analyze 总 Token 占比为 {percent(COMPARISON['analyze_observed_total_token_share'])}、非缓存输入＋输出占比为 {percent(COMPARISON['analyze_observed_noncached_plus_output_share'])}。它高度偏向近期数据，不能作为可靠的旧版本全量 Token 基线。

按 `summary.json` **记录的部署标签**分组如下；标签可能在后续操作中重写，不足以证明每个阶段始终使用同一版本代码。

{table(['记录的部署标签', '实际轮次', 'Token 完整样本', 'Analyze 总 Token 占比', 'Analyze 墙钟占比'], version_rows)}

30 天累计墙钟占比只有 {percent(COMPARISON['analyze_elapsed_share'])}，但不能据此声称 Analyze 近期变慢：`{longest['run_id']}` 从创建到结束横跨 {longest['full_wall_seconds'] / 86400:.2f} 天，包含停顿等待；它显著放大分母。30 天的单轮时长占比中位数为 {percent(COMPARISON['per_run_analyze_elapsed_share_median'])}，更接近近期水平。

## 为什么现有汇总不能直接回答此问题

1. 现有 `usage-summary.json` 在 finalize 时生成，没有包含随后独立运行的 Analyze，也没有汇总外部 reviewer 和子代理。
2. 近 7 天的 18 轮已结束样本中，现有汇总全部从本轮 RVF 之前开始计时／计 Token。最新 `{latest['run_id']}` 的原汇总为 {latest['existing_usage_summary_total_tokens']:,} Token，本轮主 Agent 重算为 {latest_main:,}；两者之间有先前任务历史，不能直接作为 Analyze 分母。
3. 新版 Codex 的 collaboration 子代理可以只有 session metadata 的 `source.subagent.thread_spawn`，不再出现旧 `collab_agent_spawn_end`。本次以原生父子关系补齐主流程子代理和 Analyze 辅助子代理，并按生成时间排除实现阶段的历史代理。
4. 正式归档完成必须检查 `summary.md` 无 TODO 且 `causality.json` 可解析，不能只看退出码。最新日志明确报告沙箱拒绝写入 `~/.rvf`、内容只保存于 fallback 位置；本次仅报告现状，不修改这些运行产物。

本报告边界为**run 创建至 finalize，加该 run 的后台 Analyze**；创建前的实现／发起决策对话、finalize 后另行执行的 `rvf-land` 或 `base-branch-sync` 不纳入。它回答本轮 RVF 组件占比，不回答整个开发 Task 相对于“完全不做 RVF”的额外成本。

## 逐轮明细与复查

{table(['run_id', '所属 Task', '主流程时长', 'Analyze 时长', 'Analyze 总 Token 占比', '非缓存输入＋输出占比', '正式归档完成'], run_rows)}

可复查产物：

- `rvf-codex-resource-summary.json`：窗口、分组、占比与数据覆盖。
- `rvf-codex-run-component-measurements.json`：逐轮和逐组件的时间、来源、用量、缺失与归档状态。
- `rvf-codex-response-token-ledger.json.gz`：压缩保存逐响应／provider 记录、原文件行号与归属，既不复制对话正文，也不复制凭据；Python 可用 `json.loads(gzip.decompress(path.read_bytes()))` 读取。
- `rvf-codex-source-evidence-manifest.json`：实际读取文件的 SHA256 与字节数；原日志未来追加后，可用已保存的核算明细复算本次结果。
- `rvf-codex-run-inventory.json`：完整筛选清单，包括 dispatcher、其他宿主及未知宿主。
- `resource-accounting-verification.json`：核算自检、全局响应去重、Token 恒等式、分组件合计与独立原始响应抽样结果。

复算采集（只读原始运行记录，只向调查目录写统计文件）：

```bash
python3 rvf-codex-stage-resource-measurement.py --self-check
python3 rvf-codex-stage-resource-measurement.py --cutoff-utc {SUMMARY['cutoff_utc']}
python3 verify-rvf-codex-resource-accounting.py
uv run --no-project --with matplotlib python render-rvf-codex-resource-report.py
```

本次只新增调查产物；未修改 RVF 主程序、正式 Analyze 归档或其他 Task 工作树。开工前已吸收本地 `main` `851a00d`，没有 handback 或 push。兄弟任务首轮 CLI 调查成功，持久化复采时 CLI 降级至 board state；范围对照与降级详情见 `bootstrap-and-sibling-task-survey-evidence.json`。既有 `d7741` 研究整个开发会话的 RVF 额外成本，本次收窄为近期 Codex RVF 内部组件占比；`1c622`、`c2c9d` 是 Analyze 归档及定位的历史对照，无同文件活跃冲突。
"""
    (ROOT / "rvf-codex-stage-duration-token-share-report.md").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    draw_chart()
    write_report()
    print("已从冻结统计生成中文报告、SVG 与 PNG。")
