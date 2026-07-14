"""Codex host transcript adapter。

解析 **Codex** rollout JSONL schema (``session_meta`` / ``turn_context`` /
``event_msg`` / ``response_item.function_call`` / ``response_item.custom_tool_call``
/ ``apply_patch`` custom-format patch 等)，归一成 ``core.transcript.models``。
Codex 把 ``apply_patch`` 调用走 ``custom_tool_call`` (``payload.input``)；
``exec_command`` 等仍走 ``function_call`` (``payload.arguments``)；两条路径都识别。
output 既可能是 ``function_call_output`` 也可能是 ``custom_tool_call_output``。

``apply_patch`` custom-format patch-text 的纯文本解析 helper 已上提到
``core.transcript.patch_parsing``（host-中性：Codex rollout 与 Claude Bash 共用）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import _rvf_pyroot  # noqa: F401  — 确保 pyroot 在 sys.path 上，供 core.* import（由 facade 预置 scripts_dir）

from core.transcript.io import (  # noqa: E402
    SCHEMA_VERSION,
    _byte_range_for_line,
    _norm_text,
    _read_jsonl_with_offsets,
    _safe_json_loads,
    _truncate,
)
from core.transcript.models import TranscriptRecord  # noqa: E402
from core.transcript.patch_parsing import (  # noqa: E402
    apply_patch_hunk_line_range_for_path,
    apply_patch_operation_to_artifact_verb,
    parse_apply_patch_operations_without_repo,
    synthesize_apply_patch_text_from_patch_apply_end,
)
from session_label import text_from_message_payload  # noqa: E402 — S9b：Codex rollout user-message 文本抽取（leaf, stdlib-only，无环）
from core.session_scope_allocation.session_change_manifest import parse_apply_patch  # noqa: E402
from adapters.codex.subagent import codex_sessions_root  # noqa: E402 — Codex sessions 根目录（$CODEX_HOME/sessions > ~/.codex/sessions）；subagent 仅依赖 core，无环


def read_codex_originator(rollout_path: Path) -> str | None:
    """从 rollout JSONL 首个 ``session_meta`` record 读 ``originator`` 字段。

    Codex CLI / Codex Desktop 在 rollout 起始 session_meta 中写入 ``originator``
    字符串（例如 ``"Codex Desktop"`` / ``"codex_cli_rs"`` 等）。我们把这个值
    promote 到 manifest 的 ``host_originator`` 字段，作为 host kind 之外的更
    细粒度信号。读取失败 / 字段缺失返回 None，不抛异常。
    """
    if not rollout_path.exists():
        return None
    try:
        with rollout_path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict):
                    originator = payload.get("originator")
                    if isinstance(originator, str) and originator:
                        return originator
                return None
    except OSError:
        return None
    return None


def _artifact_refs_from_patch_text(
    patch_text: str, repo: Path | None, line_number: int
) -> tuple[list[dict[str, Any]], int]:
    """把 apply_patch 补丁文本解析为 ``artifact_refs``（path / lines / op）。
    apply_patch 工具调用（旧 Codex）与由 patch_apply_end 合成的补丁文本（新 Codex）
    共用这段，使两代 schema 的 trajectory artifact 提炼走同一路径。"""
    if repo is not None:
        ops, _ = parse_apply_patch(repo, patch_text, line_number)
    else:
        ops, _ = parse_apply_patch_operations_without_repo(patch_text, line_number)
    artifact_refs: list[dict[str, Any]] = [
        {
            "path": op.get("path"),
            "lines": apply_patch_hunk_line_range_for_path(patch_text, op.get("path")),
            "op": apply_patch_operation_to_artifact_verb(op.get("operation")),
        }
        for op in ops
    ]
    return artifact_refs, len(ops)


def _apply_patch_paths(patch_text: str, repo: Path | None) -> set[str]:
    """一段 apply_patch 补丁文本覆盖的路径集合（repo 可用时归一化）。供 patch_apply_end 与
    旧式 apply_patch 工具调用做**路径级去重**，两侧走同一 parser，故可直接比较。"""
    if repo is not None:
        _ops, paths = parse_apply_patch(repo, patch_text, 0)
    else:
        _ops, paths = parse_apply_patch_operations_without_repo(patch_text, 0)
    return set(paths)


def _distill_codex_record(
    record: dict[str, Any],
    *,
    repo: Path | None,
    rollout_filename: str,
    byte_offsets: list[int],
    has_legacy_apply_patch_tool_call: bool = False,
    legacy_apply_patch_paths: set[str] | None = None,
) -> TranscriptRecord | None:
    line_number = record.get("_line_number") or 0
    raw_ref = {
        "file": rollout_filename,
        "line": line_number,
        "byte_range": _byte_range_for_line(byte_offsets, line_number - 1),
    }
    ts = record.get("timestamp")
    record_type = record.get("type")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else None

    def _rec(**kwargs: Any) -> TranscriptRecord:
        return TranscriptRecord(
            schema_version=SCHEMA_VERSION,
            ts=ts,
            source="codex",
            raw_ref=raw_ref,
            **kwargs,
        )

    if record_type == "session_meta" and payload:
        return _rec(
            kind="phase_marker",
            call_id=None,
            marker="session_meta",
            summary=_truncate(
                f"session_meta id={payload.get('id')} cwd={payload.get('cwd')}"
            ),
            artifact_refs=[],
        )

    if record_type == "turn_context" and payload:
        return _rec(
            kind="phase_marker",
            call_id=None,
            marker="turn_context",
            summary=_truncate(json.dumps(payload, ensure_ascii=False)),
            artifact_refs=[],
        )

    if record_type == "event_msg" and payload:
        sub = payload.get("type")
        if sub in {"task_started", "task_complete"}:
            return _rec(
                kind="phase_marker",
                call_id=None,
                marker=sub,
                summary=_truncate(
                    f"{sub} turn={payload.get('turn_id', '')}".strip()
                ),
                artifact_refs=[],
            )
        if sub == "agent_message":
            message = payload.get("message")
            if not isinstance(message, str):
                return None
            return _rec(
                kind="message",
                role="assistant",
                call_id=None,
                summary=_truncate(message),
                artifact_refs=[],
            )
        if sub == "user_message":
            message = payload.get("message")
            if not isinstance(message, str):
                return None
            return _rec(
                kind="message",
                role="user",
                call_id=None,
                summary=_truncate(message),
                artifact_refs=[],
            )
        if sub == "exec_command_end":
            return _rec(
                kind="tool_result",
                tool="exec_command",
                call_id=payload.get("call_id"),
                summary=_truncate(
                    f"exec_command_end exit={payload.get('exit_code')} duration_ms={payload.get('duration_ms')}"
                ),
                artifact_refs=[],
            )
        if sub == "patch_apply_end":
            # 新 Codex（gpt-5.6-sol 世代）把编辑工具统一成 custom_tool_call name=``exec``、
            # apply_patch 以 JS 文本内嵌在 input，旧的 apply_patch 工具调用消失——那条
            # tool_call 分支不再产出 artifact_refs，issue↔patch 因果归属会全盲。
            # patch_apply_end 是跨版本稳定、结构化、带 success 的编辑真相源：由它合成
            # apply_patch 补丁文本、复用同一 artifact 提炼，恢复 rvf-analyze 因果链。
            # 旧格式两者并存（apply_patch tool_call 已带 artifact_refs），此时 patch_apply_end
            # 保持旧行为（返回 None、不进 distilled），避免同一编辑被双重归属，且旧格式
            # trajectory 逐字节不变。去重按**编辑路径**精确判定，而非「transcript 里存在任意
            # 旧式调用即全局跳过」的布尔 gate：Codex resume / 切换模型的混合 session 里，后段
            # 新编辑的 patch_apply_end 覆盖的路径未被旧式覆盖，必须照常产出 artifact_refs，
            # 否则 rvf-analyze 因果链缺失（RVF-001 F1）。
            patch_text = synthesize_apply_patch_text_from_patch_apply_end(payload)
            if not patch_text:
                return None
            # 无任何旧式调用 → 照常产出（纯新格式）；否则仅当覆盖到旧式未覆盖的新路径时产出，
            # 旧式已覆盖或路径无法解析（空集）时返回 None，保旧格式 trajectory 逐字节等价。
            _pae_paths = _apply_patch_paths(patch_text, repo)
            _legacy_paths = legacy_apply_patch_paths or set()
            if has_legacy_apply_patch_tool_call and not (_pae_paths - _legacy_paths):
                return None
            artifact_refs, n_ops = _artifact_refs_from_patch_text(patch_text, repo, line_number)
            return _rec(
                kind="tool_result",
                tool="apply_patch",
                call_id=payload.get("call_id"),
                summary=_truncate(
                    f"patch_apply_end success={payload.get('success')} {n_ops} file(s)"
                ),
                artifact_refs=artifact_refs,
            )
        return None

    if record_type == "response_item" and payload:
        sub = payload.get("type")
        if sub in ("function_call", "custom_tool_call"):
            tool = payload.get("name")
            # function_call carries arguments string; custom_tool_call uses input.
            # apply_patch on current Codex versions arrives as custom_tool_call.
            arguments = (
                payload.get("arguments") if sub == "function_call" else payload.get("input")
            )
            artifact_refs: list[dict[str, Any]] = []
            summary_parts: list[str] = [f"call {tool}"]
            if tool == "apply_patch":
                patch_text = arguments if isinstance(arguments, str) else None
                if patch_text:
                    refs, n_ops = _artifact_refs_from_patch_text(patch_text, repo, line_number)
                    artifact_refs.extend(refs)
                    summary_parts.append(f"{n_ops} hunk(s)")
            elif tool == "exec_command":
                args = _safe_json_loads(arguments) or {}
                cmd = args.get("cmd") if isinstance(args, dict) else None
                if isinstance(cmd, str):
                    summary_parts.append(_truncate(cmd, 256))
            else:
                if isinstance(arguments, str):
                    summary_parts.append(_truncate(arguments, 256))
            return _rec(
                kind="tool_call",
                tool=tool,
                call_id=payload.get("call_id"),
                summary=_truncate(" ".join(summary_parts)),
                artifact_refs=artifact_refs,
            )
        if sub in ("function_call_output", "custom_tool_call_output"):
            output_text = payload.get("output")
            if isinstance(output_text, dict):
                output_text = output_text.get("output") or output_text.get("text") or json.dumps(output_text, ensure_ascii=False)
            if not isinstance(output_text, str):
                output_text = ""
            return _rec(
                kind="tool_result",
                tool=None,
                call_id=payload.get("call_id"),
                summary=_truncate(output_text),
                artifact_refs=[],
            )
        if sub == "message":
            role = payload.get("role") or "assistant"
            text = _norm_text(payload.get("content")) or ""
            return _rec(
                kind="message",
                role=role,
                call_id=None,
                summary=_truncate(text),
                artifact_refs=[],
            )
        if sub == "reasoning":
            text = _norm_text(payload.get("summary"))
            if text is None:
                # keep an indicator without leaking encrypted_content blob
                text = "<encrypted reasoning>"
            return _rec(
                kind="reasoning",
                call_id=None,
                summary=_truncate(text),
                artifact_refs=[],
            )
        return None

    return None


def distill_codex_jsonl(
    *,
    rollout_path: Path,
    rollout_filename: str,
    repo: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records, offsets = _read_jsonl_with_offsets(rollout_path)
    distilled: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    # 旧 Codex 对同一编辑同时发 apply_patch 工具调用与 patch_apply_end；工具调用已带 artifact_refs。
    # 按**编辑路径**精确去重（而非全局布尔）：预收集所有旧式 apply_patch 工具调用覆盖的路径；某
    # patch_apply_end 覆盖的路径若被旧式完整覆盖才跳过。resume / 切换模型的混合 session 里后段新
    # 编辑（无旧式工具调用）覆盖的路径未被覆盖，照常产出 artifact_refs（RVF-001 F1，与 manifest 同源）。
    has_legacy_apply_patch_tool_call = False
    legacy_apply_patch_paths: set[str] = set()
    for rec in records:
        p = rec.get("payload")
        if (
            isinstance(p, dict)
            and p.get("type") in ("function_call", "custom_tool_call")
            and p.get("name") == "apply_patch"
        ):
            has_legacy_apply_patch_tool_call = True
            arguments = p.get("arguments") if p.get("type") == "function_call" else p.get("input")
            if isinstance(arguments, str) and arguments:
                legacy_apply_patch_paths |= _apply_patch_paths(arguments, repo)
    for record in records:
        out = _distill_codex_record(
            record,
            repo=repo,
            rollout_filename=rollout_filename,
            byte_offsets=offsets,
            has_legacy_apply_patch_tool_call=has_legacy_apply_patch_tool_call,
            legacy_apply_patch_paths=legacy_apply_patch_paths,
        )
        if out is None:
            continue
        kind = out.kind or "unknown"
        counts[kind] = counts.get(kind, 0) + 1
        distilled.append(out.to_dict())
    index = {
        "schema_version": SCHEMA_VERSION,
        "rollout_file": rollout_filename,
        "record_count": len(distilled),
        "kind_counts": counts,
    }
    return distilled, index


# ─────────────────────────────────────────────────────────────────────────────
# S9b：Codex rollout / goal-mode 解析缝（从共享审查引擎抽出）
#
# Codex rollout JSONL 的逐行解析（latest_user_message / user_messages_containing）、
# session_meta 读取（session_meta_from_path / session_id_from_path）、以及 /goal
# mode 扫描（codex_goal_mode_context_from_event 及其私有 helper）属 Codex-specific，
# 落在此 Codex transcript adapter。共享审查引擎经
# ``from adapters.codex.transcript import (...)`` re-import 下列入口委派执行。
#
# 本 adapter 只 import stdlib + ``session_label``（leaf, stdlib-only）；绝不 import
# 引擎，杜绝 ``adapters → engine`` 循环。下面三个 host-中性符号
# （``SESSION_PATH_KEYS`` / ``event_session_paths`` / ``source_marks_subagent``）
# 本应住 core/；S9b 阶段引擎尚未进 core/，故按本仓既有惯例（router / dispatcher /
# rvf_handoff / rvf_analyze_advisory / rvf_analyze_thread 各自 inline 一份
# SESSION_PATH_KEYS 以避免循环 import）在此自带副本，与
# ``stop_review_validate_fix.SESSION_PATH_KEYS`` 保持一致；待 S10 中性体迁
# core/ 后统一上提共享。
# ─────────────────────────────────────────────────────────────────────────────

SESSION_PATH_KEYS = (
    "transcript_path",
    "session_path",
    "conversation_path",
    "log_path",
    "session_file",
)


def event_session_paths(event: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for key in SESSION_PATH_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    return paths


def source_marks_subagent(source: Any) -> bool:
    return isinstance(source, dict) and isinstance(source.get("subagent"), dict)


CODEX_GOAL_CONTINUATION_MARKER = "Continue working toward the active thread goal"
CODEX_GOAL_INCOMPLETE_STATUSES = {
    "active",
    "paused",
    "budgetlimited",
    "budget_limited",
    "budget-limited",
}


def latest_user_message(path: Path) -> str | None:
    latest: str | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue

                if record.get("type") == "event_msg" and payload.get("type") == "user_message":
                    message = payload.get("message")
                    if isinstance(message, str):
                        latest = message
                    continue

                if record.get("type") == "response_item":
                    if payload.get("type") == "message" and payload.get("role") == "user":
                        text = text_from_message_payload(payload)
                        if text:
                            latest = text
    except OSError:
        return None
    return latest


def user_messages_containing(path: Path, marker: str) -> list[str]:
    messages: list[str] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue

                text = ""
                if record.get("type") == "event_msg" and payload.get("type") == "user_message":
                    message = payload.get("message")
                    text = message if isinstance(message, str) else ""
                elif record.get("type") == "response_item":
                    if payload.get("type") == "message" and payload.get("role") == "user":
                        text = text_from_message_payload(payload)

                if marker in text:
                    messages.append(text)
    except OSError:
        return []
    return messages


def session_id_from_path(path: Path) -> str | None:
    meta = session_meta_from_path(path)
    value = meta.get("id")
    return value if isinstance(value, str) and value else None


def session_meta_from_path(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    return {}
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                return payload if isinstance(payload, dict) else {}
    except (OSError, UnicodeDecodeError):
        return {}
    return {}


def codex_transcript_index_by_session_id(root: Path | None = None) -> dict[str, Path]:
    """Map session_id (UUIDv7, 36 chars) → newest matching Codex rollout path.

    Codex transcript filenames are ``rollout-<timestamp>-<uuid>.jsonl``. We pull
    the trailing 36 chars before ``.jsonl`` as the session id and keep the most
    recently-modified file for each id. Defaults to the Codex sessions root
    (``$CODEX_HOME/sessions`` > ``~/.codex/sessions``).
    """
    base = root or codex_sessions_root()
    idx: dict[str, Path] = {}
    if not base.is_dir():
        return idx
    try:
        for path in base.rglob("rollout-*.jsonl"):
            name = path.name
            if not name.endswith(".jsonl") or len(name) < 42:
                continue
            sid = name[-42:-6]
            if sid.count("-") != 4:
                continue
            existing = idx.get(sid)
            if existing is None:
                idx[sid] = path
                continue
            try:
                if path.stat().st_mtime > existing.stat().st_mtime:
                    idx[sid] = path
            except OSError:
                continue
    except OSError:
        pass
    return idx


def read_codex_session_origin_fields(path: Path) -> dict[str, Any]:
    """从 Codex rollout 的 ``session_meta`` 读取来源展示字段。

    返回 ``{cwd, originator, branch, commit}``（commit 截断到 8 位）；当
    transcript 无 ``session_meta`` payload 时返回空 dict，调用方据此判断是否
    做 enrich。复用 ``session_meta_from_path`` 的首 20 行扫描，避免重复解析。
    """
    payload = session_meta_from_path(path)
    if not payload:
        return {}
    git = payload.get("git")
    if not isinstance(git, dict):
        git = {}
    return {
        "cwd": payload.get("cwd") or "",
        "originator": payload.get("originator") or "",
        "branch": git.get("branch") or "",
        "commit": (git.get("commit_hash") or "")[:8],
    }


def _normalized_goal_status(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().replace(" ", "").replace("-", "").replace("_", "").lower()


def _goal_status_from_mapping(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    goal = value.get("goal")
    if isinstance(goal, dict):
        status = _normalized_goal_status(goal.get("status"))
        if status is not None:
            return status
    return _normalized_goal_status(
        value.get("goal_status")
        or value.get("goalStatus")
        or value.get("thread_goal_status")
        or value.get("threadGoalStatus")
    )


def _goal_status_from_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return _goal_status_from_mapping(parsed)


def _record_goal_status(payload: Any) -> str | None:
    status = _goal_status_from_mapping(payload)
    if status is not None:
        return status
    if not isinstance(payload, dict):
        return None
    return _goal_status_from_text(payload.get("output"))


def _record_text_contains_goal_continuation(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("role") != "developer":
        return False
    for key in ("content", "message", "text"):
        value = payload.get(key)
        if isinstance(value, str) and CODEX_GOAL_CONTINUATION_MARKER in value:
            return True
        if isinstance(value, list):
            for item in value:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("text"), str)
                    and CODEX_GOAL_CONTINUATION_MARKER in item["text"]
                ):
                    return True
    return False


def _codex_session_meta_details(path: Path) -> dict[str, Any]:
    meta = session_meta_from_path(path)
    source = meta.get("source")
    originator = meta.get("originator")
    cli_version = meta.get("cli_version")
    codex_originator = isinstance(originator, str) and "codex" in originator.lower()
    return {
        "is_codex": codex_originator or isinstance(cli_version, str),
        "is_subagent": source_marks_subagent(source),
        "originator": originator if isinstance(originator, str) else None,
        "cli_version": cli_version if isinstance(cli_version, str) else None,
    }


def _scan_codex_goal_transcript(path: Path) -> dict[str, Any]:
    latest_status: str | None = None
    has_goal_continuation = False
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload")
                status = _record_goal_status(payload)
                if status is not None:
                    latest_status = status
                if _record_text_contains_goal_continuation(payload):
                    has_goal_continuation = True
    except (OSError, UnicodeDecodeError):
        return {
            "readable": False,
            "latest_goal_status": None,
            "has_goal_continuation": False,
        }
    return {
        "readable": True,
        "latest_goal_status": latest_status,
        "has_goal_continuation": has_goal_continuation,
    }


def codex_goal_mode_context_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Temporary stop-hook guard: skip RVF while a Codex main session is in /goal."""
    if source_marks_subagent(event.get("source")):
        return None

    for path in event_session_paths(event):
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue

        meta_details = _codex_session_meta_details(resolved)
        if not meta_details["is_codex"] or meta_details["is_subagent"]:
            continue

        goal_scan = _scan_codex_goal_transcript(resolved)
        latest_status = goal_scan.get("latest_goal_status")
        if isinstance(latest_status, str):
            if latest_status not in CODEX_GOAL_INCOMPLETE_STATUSES:
                continue
        elif not goal_scan.get("has_goal_continuation"):
            continue

        return {
            "transcript_path": str(resolved),
            "goal_status": latest_status,
            "goal_continuation_marker": bool(goal_scan.get("has_goal_continuation")),
            "codex_originator": meta_details.get("originator"),
            "codex_cli_version": meta_details.get("cli_version"),
            "temporary_fix": True,
        }
    return None
