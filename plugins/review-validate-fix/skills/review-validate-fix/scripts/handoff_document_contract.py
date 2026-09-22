#!/usr/bin/env python3
"""handoff.md 正文的机器可读契约（唯一事实源）。

handoff.md 的正文由主会话 agent 撰写，但有几处是**被脚本消费**的结构，不是自由散文：

- ``handoff_status`` / ``review_status`` 两行 —— ``write_reviewed_task_candidate_evidence``
  的完成门控读它。
- canonical 计数句「共 N 条，V 真实修复 / F 误报 / E 升级」与 ``[fixed]`` /
  ``[false_positive]`` / ``[elevated]`` 条目 —— 同上门控要求二者自洽。
- ``## Handoff intake hints`` 的标签列表与 ``## 验证`` 段 —— ``rvf_handoff_intake`` 的
  deterministic intake 读它们。

历史上这些正则内联在 ``write_reviewed_task_candidate_evidence.completed_review`` 里，
而口径又被 ``references/handoff-template.md`` 用自然语言复述一遍。本模块把正则收敛为单一
事实源，供 candidate-evidence 门控与 run finalize 的契约体检共用。

本模块保持纯函数 + 零重依赖（不 import run ledger / intake），以免 finalize 与 evidence
两条链路互相牵连。
"""

from __future__ import annotations

import re

HANDOFF_STATUS_COMPLETED_RE = re.compile(r"handoff_status:\s*COMPLETED", re.IGNORECASE)
REVIEW_STATUS_COMPLETED_RE = re.compile(r"review_status:\s*COMPLETED", re.IGNORECASE)
HANDOFF_STATUS_LINE_RE = re.compile(r"^\s*-\s*handoff_status:\s*(.+?)\s*$", re.MULTILINE)
REVIEW_STATUS_LINE_RE = re.compile(r"^\s*-\s*review_status:\s*(.+?)\s*$", re.MULTILINE)
UNRESOLVED_OUTCOME_RE = re.compile(r"\[(?:elevated|failed)\]", re.IGNORECASE)
CANONICAL_ISSUE_SUMMARY_RE = re.compile(
    r"共\s*(\d+)\s*条\s*[，,]\s*(\d+)\s*真实修复\s*/\s*(\d+)\s*误报\s*/\s*(\d+)\s*升级"
)
FIXED_OUTCOME_RE = re.compile(r"\[fixed\]", re.IGNORECASE)
FALSE_POSITIVE_OUTCOME_RE = re.compile(r"\[false_positive\]", re.IGNORECASE)
ELEVATED_OUTCOME_RE = re.compile(r"\[elevated\]", re.IGNORECASE)

INTAKE_HINTS_SECTION_TITLE = "Handoff intake hints"
#: 与 ``rvf_handoff_intake.INTAKE_HINT_LABELS`` 对应的必填子集：intake 真正据此决定
#: 「哪些路径算已审、哪些改动该带回主会话、拿什么命令复验」。
REQUIRED_INTAKE_HINT_LABELS = (
    "reviewed scope paths",
    "accepted changes",
    "main-session validation commands",
)
#: 与 ``rvf_handoff_intake.VALIDATION_SECTION_HEADINGS`` 对齐的验证段标题。
VALIDATION_SECTION_TITLES = (
    "验证",
    "Validation",
    "Scoped verification (in-contract)",
    "Full-suite results (unrelated)",
)


def completion_statuses_complete(text: str) -> bool:
    """``handoff_status`` 与 ``review_status`` 是否都标记为 COMPLETED。"""
    return bool(HANDOFF_STATUS_COMPLETED_RE.search(text)) and bool(
        REVIEW_STATUS_COMPLETED_RE.search(text)
    )


def has_unresolved_outcomes(text: str) -> bool:
    """是否仍存在 ``[elevated]`` / ``[failed]`` 条目。"""
    return bool(UNRESOLVED_OUTCOME_RE.search(text))


def canonical_issue_summary(text: str) -> tuple[int, int, int, int] | None:
    """解析 canonical 计数句，返回 (总数, 真实修复, 误报, 升级)。"""
    match = CANONICAL_ISSUE_SUMMARY_RE.search(text)
    if match is None:
        return None
    canonical, fixed, false_positive, elevated = map(int, match.groups())
    return canonical, fixed, false_positive, elevated


def outcome_marker_counts(text: str) -> dict[str, int]:
    """统计正文里的结果标记条目数。"""
    return {
        "fixed": len(FIXED_OUTCOME_RE.findall(text)),
        "false_positive": len(FALSE_POSITIVE_OUTCOME_RE.findall(text)),
        "elevated": len(ELEVATED_OUTCOME_RE.findall(text)),
    }


def _has_section(text: str, title: str) -> bool:
    return re.search(rf"^##+\s+{re.escape(title)}\s*$", text, re.MULTILINE) is not None


def handoff_document_violations(text: str) -> list[str]:
    """run finalize 用的正文契约体检；返回中文违规描述列表，空列表表示合规。

    与 candidate-evidence 门控的区别：这里**不**把 ``[elevated]`` 当违规——带升级项收尾是
    合法终态，只是不构成可复用的 landing candidate。
    """
    violations: list[str] = []

    handoff_status = HANDOFF_STATUS_LINE_RE.search(text)
    review_status = REVIEW_STATUS_LINE_RE.search(text)
    if handoff_status is None:
        violations.append("缺少 `- handoff_status:` 行")
    elif not HANDOFF_STATUS_COMPLETED_RE.search(text):
        violations.append(f"handoff_status 仍为 `{handoff_status.group(1)}`，未标记 COMPLETED")
    if review_status is None:
        violations.append("缺少 `- review_status:` 行")

    summary = canonical_issue_summary(text)
    if summary is None:
        violations.append("缺少 canonical 计数句「共 N 条，V 真实修复 / F 误报 / E 升级」")
    else:
        canonical, fixed, false_positive, elevated = summary
        counts = outcome_marker_counts(text)
        if fixed + false_positive + elevated != canonical:
            violations.append(
                f"canonical 计数句自相矛盾：{fixed}+{false_positive}+{elevated} != {canonical}"
            )
        for label, declared in (
            ("fixed", fixed),
            ("false_positive", false_positive),
            ("elevated", elevated),
        ):
            if counts[label] != declared:
                violations.append(
                    f"计数句声明 {declared} 条 [{label}]，正文实际 {counts[label]} 条"
                )

    if not _has_section(text, INTAKE_HINTS_SECTION_TITLE):
        violations.append(f"缺少 `## {INTAKE_HINTS_SECTION_TITLE}` 区块")
    else:
        for label in REQUIRED_INTAKE_HINT_LABELS:
            if re.search(rf"^\s*-\s*{re.escape(label)}\s*:", text, re.MULTILINE) is None:
                violations.append(f"Handoff intake hints 缺少 `{label}:` 标签")

    if not any(_has_section(text, title) for title in VALIDATION_SECTION_TITLES):
        violations.append("缺少验证段（`## 验证` 或等价标题）")

    return violations
