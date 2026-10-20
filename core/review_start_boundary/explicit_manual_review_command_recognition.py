"""UPS 与开审分界共用的纯文本主 skill 识别；不读宿主或执行工作流。"""

from __future__ import annotations

import re

# 主 skill 可以在前置文字之后；完整命名空间保持 UPS 的既有文本 fallback 规则。
# 姊妹 skill 的命名空间后缀以 :rvf- 开头，不能只按主 skill 前缀命中。
RVF_MANUAL_TRIGGER_RE = re.compile(
    r"(?:^|\s)[\$/:]review-validate-fix(?!:rvf-)\b",
    re.MULTILINE,
)
# Codex 的其它命名空间由 UPS 结构化读取确认；消费端只在 provenance 核验后接受其原文。
RVF_STRUCTURED_NAMESPACE_MAIN_SKILL_COMMAND_RE = re.compile(
    r"(?:^|\s)\$[^\s:$]+:review-validate-fix(?![\w:-])",
)
RVF_SIBLING_TRIGGER_RE = re.compile(
    r"\s*[\$/:](?:rvf:|review-validate-fix:)?rvf-[a-z0-9][a-z0-9-]*\b",
)
RVF_HANDOFF_RUN_IDENTIFIER_RE = re.compile(r"\b(rvf-\d{8}T\d{6}Z-[A-Za-z0-9_.:-]+)")
RVF_HANDOFF_SECTION_HEADING_RE = re.compile(r"^##+\s+(.+?)\s*$")
RVF_HANDOFF_SECTION_MARKERS = (
    "handoff intake hints",
    "deterministic intake hints",
    "validate/fix",
    "处理结果",
    "repo delta",
    "继续指引",
    "升级事项",
    "本次 review",
)


def looks_like_rvf_handoff_body(text: str | None) -> bool:
    """run id 与 handoff 专属章节同时出现，才将正文中的命令视为交接引用。"""
    if not text or not RVF_HANDOFF_RUN_IDENTIFIER_RE.search(text):
        return False
    for line in text.splitlines():
        heading = RVF_HANDOFF_SECTION_HEADING_RE.match(line)
        if heading and any(marker in heading.group(1).strip().lower() for marker in RVF_HANDOFF_SECTION_MARKERS):
            return True
    return False


def manual_review_command_text_is_eligible(text: str, *, has_verified_manual_ups_provenance: bool = False) -> bool:
    """只证明原文具有合法主 skill 调用形态；原生消息、来源与快照仍须单独核验。"""
    command_present = RVF_MANUAL_TRIGGER_RE.search(text) or (
        has_verified_manual_ups_provenance and RVF_STRUCTURED_NAMESPACE_MAIN_SKILL_COMMAND_RE.search(text)
    )
    return bool(command_present) and not (
        RVF_SIBLING_TRIGGER_RE.match(text) or looks_like_rvf_handoff_body(text)
    )
