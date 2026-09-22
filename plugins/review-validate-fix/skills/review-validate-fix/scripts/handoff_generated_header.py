#!/usr/bin/env python3
"""handoff.md 中「脚本拥有的确定性表头区块」的渲染 / 写入 / 复原。

handoff.md 按所有权分成两个区：

- **本模块渲染的 marker 区块**：run 身份（run id / run dir / 目标仓库 / 起始 git HEAD）、
  `## Origin` 溯源、`## Review scope` 路径。这些事实在 prepare 阶段已经全部落在 run
  artifacts 里（``summary.json`` / ``artifacts/origin.json`` /
  ``artifacts/inputs/scope.contract.json`` / ``artifacts/before-workspace-snapshot.json``），
  不该让主会话 agent 凭上下文默写、再在重写时默写第二遍。
- **marker 之外的正文**：原始任务、Handoff intake hints、issue 处理结果、验证、继续指引，
  由主会话 agent 拥有。

实测历史轨迹里主会话有约三分之一的 run 会把 handoff.md 整份删除重写（Codex 下是字面的
``apply_patch: Delete File`` + ``Add File``），因此写入必须幂等，并且能在 marker 丢失后
复原区块而不破坏 agent 正文——``apply_generated_header`` 就是这个契约的实现。

键名与区块标题必须与 ``references/handoff-template.md`` 及 ``rvf_handoff_intake.py``
的解析器（``parse_key_values`` / ``parse_sections``）逐字一致，否则打断 deterministic intake。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

HANDOFF_DOCUMENT_TITLE = "# Review-validate-fix 交接上下文"
GENERATED_HEADER_BEGIN = (
    "<!-- rvf:generated-header:begin — 由 scripts/handoff_generated_header.py 渲染；"
    "勿手改，run finalize 会重渲该区块 -->"
)
GENERATED_HEADER_END = "<!-- rvf:generated-header:end -->"
GENERATED_HEADER_BLOCK_RE = re.compile(
    r"<!--\s*rvf:generated-header:begin.*?-->.*?<!--\s*rvf:generated-header:end\s*-->",
    re.DOTALL,
)
AGENT_OWNED_BODY_HINT = (
    "<!-- 以下正文由主会话 agent 拥有；结构见 references/handoff-template.md -->"
)
STATUS_SECTION_TITLE = "## 状态"
AGENT_OWNED_STATUS_STUB = (
    f"{STATUS_SECTION_TITLE}\n\n- handoff_status: PENDING\n- review_status: PENDING\n"
)
UNAVAILABLE = "unavailable"

#: 表头区块独占的 ``## 状态`` 键；agent 若整份重写并自行默写了这些行，复原时先剥掉。
SCRIPT_OWNED_STATUS_KEYS = (
    "run id",
    "run dir",
    "目标仓库",
    "Review 开始时的 git HEAD",
)
#: 表头区块独占的二级区块标题（同上，复原前先剥掉 agent 自写的同名区块）。
SCRIPT_OWNED_SECTION_TITLES = ("Origin", "Review scope")


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _text_or_none(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _first_available(*values: Any) -> str:
    for value in values:
        text = _text_or_none(value)
        if text is not None:
            return text
    return UNAVAILABLE


def _git_head(repo: str | None) -> str | None:
    if not repo:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _text_or_none(completed.stdout) if completed.returncode == 0 else None


def collect_generated_header_facts(run_dir: Path) -> dict[str, Any]:
    """从 run artifacts 采集表头所需的确定性事实。

    每个字段都有明确的事实源优先级，缺失时降级为 ``unavailable``，绝不猜测。
    """
    run_dir = Path(run_dir).expanduser().resolve()
    artifacts_dir = run_dir / "artifacts"
    summary = _read_json_object(run_dir / "summary.json")
    contract = _read_json_object(artifacts_dir / "inputs" / "scope.contract.json")
    start_snapshot = _read_json_object(artifacts_dir / "before-workspace-snapshot.json")
    origin_path = artifacts_dir / "origin.json"
    origin = _read_json_object(origin_path)
    stop_event = _read_json_object(artifacts_dir / "stop-event.json")

    repo = _first_available(contract.get("repo"), summary.get("repo"), start_snapshot.get("repo"))
    head = _first_available(
        start_snapshot.get("head"),
        _git_head(repo if repo != UNAVAILABLE else None),
    )
    return {
        "run_dir": run_dir,
        "artifacts_dir": artifacts_dir,
        "run_id": _first_available(summary.get("run_id"), contract.get("run_id"), run_dir.name),
        "repo": repo,
        "head": head,
        "summary": summary,
        "contract": contract,
        "origin": origin,
        "origin_path": origin_path if origin else None,
        "stop_event": stop_event,
    }


def _existing_artifact_path(facts: dict[str, Any], declared: Any, *relative_names: str) -> str:
    """优先用 contract 声明的路径；否则回退到 artifacts 下的约定文件名。"""
    declared_text = _text_or_none(declared)
    if declared_text:
        return declared_text
    artifacts_dir: Path = facts["artifacts_dir"]
    for name in relative_names:
        candidate = artifacts_dir / name
        if candidate.is_file():
            return str(candidate)
    return UNAVAILABLE


#: 无冻结 parent origin 时，退到「触发本 run 的那个会话」的 transcript。它未必等于
#: fork 拓扑里的父会话，因此必须同时把 name source 标成该 fallback，让读者知道这条
#: transcript 的来历，而不是伪装成已冻结的 parent origin。
STOP_EVENT_TRANSCRIPT_NAME_SOURCE = "stop_event_transcript_fallback"


def _origin_lines(facts: dict[str, Any]) -> list[str]:
    origin = facts["origin"]
    summary = facts["summary"]
    origin_path = facts["origin_path"]
    frozen_transcript = _first_available(
        origin.get("transcript_path"), summary.get("parent_thread_path")
    )
    stop_event_transcript = _text_or_none(facts["stop_event"].get("transcript_path"))
    used_stop_event_fallback = frozen_transcript == UNAVAILABLE and stop_event_transcript is not None
    transcript = stop_event_transcript if used_stop_event_fallback else frozen_transcript
    name_source = _first_available(
        origin.get("name_source"),
        summary.get("parent_conversation_name_source"),
        STOP_EVENT_TRANSCRIPT_NAME_SOURCE if used_stop_event_fallback else None,
    )
    lines = [
        "## Origin",
        "",
        "- original Codex conversation: "
        + _first_available(origin.get("label"), summary.get("parent_conversation_ref")),
        f"- conversation name source: {name_source}",
        "- original Codex URL: "
        + _first_available(origin.get("codex_url"), summary.get("parent_codex_url")),
        f"- original transcript: {transcript}",
        "- origin metadata: " + (str(origin_path) if origin_path is not None else UNAVAILABLE),
    ]
    # Kanban 四行只在 run 真的派生自 Cline Kanban task 时出现；否则整段省略，
    # 避免 Origin 区出现一片 unavailable 噪音（与 handoff-template.md 的规则一致）。
    task_id = _text_or_none(origin.get("kanban_task_id")) or _text_or_none(
        summary.get("parent_kanban_task_id")
    )
    if task_id:
        attempt_id = _first_available(
            origin.get("kanban_attempt_id"), summary.get("parent_kanban_attempt_id")
        )
        lines.extend(
            [
                f"- source Kanban task id: {task_id}",
                f"- source Kanban attempt id: {attempt_id}",
                "- source Kanban task title at trigger: "
                + _first_available(
                    origin.get("kanban_task_title"), summary.get("parent_kanban_task_title")
                ),
                f"- generated Kanban task: {task_id} / {attempt_id}",
            ]
        )
    return lines


def _review_scope_lines(facts: dict[str, Any]) -> list[str]:
    contract = facts["contract"]
    artifacts_dir: Path = facts["artifacts_dir"]
    scope_contract_path = artifacts_dir / "inputs" / "scope.contract.json"
    primary_files = contract.get("primary_files")
    primary_units = contract.get("primary_units")
    file_count = len(primary_files) if isinstance(primary_files, list) else 0
    unit_count = len(primary_units) if isinstance(primary_units, list) else 0
    if unit_count:
        scope_summary = f"{unit_count} 个 tracker unit"
    elif file_count:
        scope_summary = f"{file_count} 个文件"
    else:
        scope_summary = UNAVAILABLE
    scope_summary += (
        f"（scope_mode={_first_available(contract.get('scope_mode'))}"
        f", review_kind={_first_available(contract.get('review_kind'))}"
        "；完整清单以 scope contract 为准）"
    )
    return [
        "## Review scope",
        "",
        "- scope-of-work: "
        + _existing_artifact_path(
            facts, contract.get("scope_of_work_path"), "scope-of-work.md", "inputs/scope-of-work.md"
        ),
        "- session manifest: "
        + _existing_artifact_path(
            facts,
            contract.get("session_manifest_path"),
            "session-manifest.json",
            "inputs/session-manifest.json",
        ),
        "- review packet: "
        + _existing_artifact_path(
            facts, contract.get("review_packet_path"), "review-packet.md", "inputs/review-packet.md"
        ),
        "- scope contract: "
        + (str(scope_contract_path) if scope_contract_path.is_file() else UNAVAILABLE),
        f"- 主审查文件 / 范围: {scope_summary}",
    ]


def generated_header_block(run_dir: Path) -> str:
    """渲染含首尾 marker 的完整表头区块。"""
    facts = collect_generated_header_facts(run_dir)
    lines = [
        GENERATED_HEADER_BEGIN,
        "",
        f"- run id: {facts['run_id']}",
        f"- run dir: {facts['run_dir']}",
        f"- 目标仓库: {facts['repo']}",
        f"- Review 开始时的 git HEAD: {facts['head']}",
        "",
        *_origin_lines(facts),
        "",
        *_review_scope_lines(facts),
        "",
        GENERATED_HEADER_END,
    ]
    return "\n".join(lines)


def strip_script_owned_content(text: str) -> str:
    """剥掉 agent 自行默写的表头内容（脚本独占的状态键 + Origin / Review scope 区块）。

    只在 marker 丢失（agent 整份重写过）时使用：重新插入区块前先去重，避免同名区块并存
    让 ``rvf_handoff_intake.parse_key_values`` 读到两份互相覆盖的值。

    代价是明确的：agent 写在 ``## Origin`` / ``## Review scope`` 里的**额外**散文会被一并
    丢弃。这是刻意的取舍——这两个区块归脚本所有，叙述内容应写在 ``## 原始任务`` 起的正文里。
    """
    kept: list[str] = []
    skipping_section = False
    for line in text.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            skipping_section = heading.group(1).strip() in SCRIPT_OWNED_SECTION_TITLES
            if skipping_section:
                continue
        if skipping_section:
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            key = stripped[2:].split(":", 1)[0].strip()
            if key in SCRIPT_OWNED_STATUS_KEYS:
                continue
        kept.append(line)
    return "\n".join(kept)


def _insert_block(text: str, block: str) -> str:
    """把表头区块插到 ``## 状态`` 的状态行之后；没有该区块时插到文档标题之后。"""
    lines = text.splitlines()
    insert_at = len(lines)
    if any(line.strip() == STATUS_SECTION_TITLE for line in lines):
        status_index = next(
            index for index, line in enumerate(lines) if line.strip() == STATUS_SECTION_TITLE
        )
        insert_at = status_index + 1
        for index in range(status_index + 1, len(lines)):
            if lines[index].startswith("## "):
                insert_at = index
                break
            if lines[index].strip():
                insert_at = index + 1
    else:
        for index, line in enumerate(lines):
            if line.startswith("# "):
                insert_at = index + 1
                break
        else:
            insert_at = 0
    head = list(lines[:insert_at])
    while head and not head[-1].strip():
        head.pop()
    tail = lines[insert_at:]
    while tail and not tail[0].strip():
        tail.pop(0)
    return "\n".join([*head, "", block, "", *tail]).strip() + "\n"


def _fresh_document(block: str) -> str:
    return (
        f"{HANDOFF_DOCUMENT_TITLE}\n\n"
        f"{AGENT_OWNED_STATUS_STUB}\n"
        f"{block}\n\n"
        f"{AGENT_OWNED_BODY_HINT}\n"
    )


def apply_generated_header(
    run_dir: Path,
    *,
    handoff_path: Path | None = None,
) -> dict[str, Any]:
    """把最新表头写进 handoff.md，幂等。

    三种情形：
    - 文件不存在 → 建立「标题 + agent 拥有的状态行 + 表头区块」骨架（``created``）
    - marker 在 → 只替换区块内容，正文一字不动（``replaced`` / ``unchanged``）
    - marker 丢失（agent 整份重写过）→ 剥掉 agent 默写的同名内容后重新插入（``reinserted``）
    """
    run_dir = Path(run_dir).expanduser().resolve()
    target = (
        Path(handoff_path).expanduser()
        if handoff_path is not None
        else run_dir / "artifacts" / "handoff.md"
    )
    block = generated_header_block(run_dir)
    result: dict[str, Any] = {"handoff_path": str(target), "run_dir": str(run_dir)}

    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_fresh_document(block), encoding="utf-8")
        result["action"] = "created"
        return result

    original = target.read_text(encoding="utf-8")
    if GENERATED_HEADER_BLOCK_RE.search(original):
        updated = GENERATED_HEADER_BLOCK_RE.sub(lambda _match: block, original, count=1)
        result["action"] = "unchanged" if updated == original else "replaced"
    else:
        updated = _insert_block(strip_script_owned_content(original), block)
        result["action"] = "reinserted"
    if updated != original:
        target.write_text(updated, encoding="utf-8")
    return result


def generated_header_is_current(run_dir: Path, *, handoff_path: Path | None = None) -> bool:
    """handoff.md 里的表头区块是否与当前 run 事实逐字一致。"""
    run_dir = Path(run_dir).expanduser().resolve()
    target = (
        Path(handoff_path).expanduser()
        if handoff_path is not None
        else run_dir / "artifacts" / "handoff.md"
    )
    if not target.is_file():
        return False
    match = GENERATED_HEADER_BLOCK_RE.search(target.read_text(encoding="utf-8"))
    return bool(match) and match.group(0) == generated_header_block(run_dir)
