#!/usr/bin/env python3
"""把一次 RVF run 的轨迹捕获成结构化产物。

切分逻辑（plan v2）:
- 同会话场景：父 codex rollout 中包含 RVF 手动触发文本的最近一条
  user message 作为 RVF 起点，按行/字节切片成 pre-rvf 与 post-rvf。
- 分叉会话场景：通过 `<run_dir>/artifacts/origin.json` 取父会话 rollout 路径，
  整份作为 pre-rvf；当前 fork 会话 rollout 整份作为 post-rvf。

产物布局:
<run_dir>/artifacts/trajectory/
├── pre-rvf/
│   ├── rollout.jsonl           # 切片或拷贝（视场景）
│   └── manifest.json                 # source_kind / sha256 / 行范围 / cut_marker
└── rvf/
    ├── rollout.jsonl
    ├── rollout.manifest.json
    ├── trajectory.jsonl              # 蒸馏后统一 schema
    ├── trajectory.index.json         # 反向索引
    ├── reviewers/<id>/{trajectory.jsonl, trajectory.manifest.json}
    └── subagents/<agent_id>/{rollout.jsonl, trajectory.jsonl,
                              trajectory.index.json, manifest.json}
                                       # spawn_agent 子代理（reviewer / validate-fix
                                       # 等）独立 rollout 拷贝 + 蒸馏；spawn metadata
                                       # （role / nickname / prompt / call_id）写在
                                       # manifest.json::spawn 下。

Host 耦合说明:
本模块支持 **Codex** rollout JSONL 与 **Claude Code** transcript NDJSON 两种
schema，按 transcript 文件首条 record type 探测（``trajectory_distill.detect_transcript_format``）
后分派到对应 helper：

- Codex 路径：``_codex_user_message_text`` / ``find_rvf_start_in_jsonl`` /
  ``trajectory_distill.distill_codex_jsonl``。识别 record type
  ``event_msg`` / ``response_item`` / ``session_meta`` / ``turn_context``。
- Claude 路径：``_claude_user_message_text`` / ``find_rvf_start_in_claude_jsonl``
  / ``trajectory_distill.distill_claude_jsonl``。识别 record type
  ``user`` / ``assistant`` / ``summary`` / ``system``。

探测失败 (空文件 / 异常 schema) → fallback 到 Codex 路径；这保证既有 Codex-only
用例无回归。两个解析器栈互不交叉——新增 host 时应当加新的 ``_<host>_*``
平行实现而非扩展任一现有解析器。

Cline Kanban dispatch（flow-2-branch / flow-2-inplace）覆盖：触发 capture
的 stop hook event 只知道 parent Codex transcript，但被 dispatch 的 task
agent（Claude Code）的 UserPromptSubmit hook 会把自己的 ``child_session_id``
/ ``child_transcript_path`` 自回填进 ``<run_dir>/artifacts/origin.json``
（持久通道，不依赖短 TTL 的 prep file）。``capture_run`` 读到 origin.json
的 child 字段后即把 child Claude transcript 作为 post、parent Codex
transcript 作为 pre，复用既有 forked 分支产出正确轨迹。该 hook 由 Claude
plugin 自带的 ``hooks/hooks.json``（UserPromptSubmit）触发，无需独立 install
脚本。因此本模块现可捕获 same-session（含 Claude）、forked Codex 父子、以及
Cline Kanban Codex→Claude 跨 host dispatch。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from subagent_capture import capture_all_subagents  # noqa: E402
from trajectory_distill import (  # noqa: E402
    HOST_CLAUDE,
    HOST_CODEX,
    HOST_KIND,
    detect_transcript_format,
    distill_claude_jsonl,
    distill_codex_jsonl,
    distill_reviewer_stream,
    read_codex_originator,
    write_jsonl,
)

SCHEMA_VERSION = 1
LARGE_FILE_BYTES = 200 * 1024 * 1024  # 200 MB

RVF_SKILL_TRIGGER = "$review-validate-fix"
RVF_START_TRIGGERS = (
    RVF_SKILL_TRIGGER,
    "/review-validate-fix",
    ":review-validate-fix",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _host_meta(src: Path | None, *, host_kind: str | None = None) -> dict[str, Any]:
    """构造 ``{host, host_originator}``。

    host 来源优先级：
    1. 显式传入的 ``host_kind`` 参数（capture_run 内已探测过 transcript 时复用）。
    2. 从 ``src`` 调 ``detect_transcript_format`` 探测（Codex/Claude）。
    3. fallback 到 ``HOST_CODEX`` —— 保证 src 不可读 / 无法探测时既有 Codex
       behavior 不回归。

    ``host_originator`` 仅 Codex 场景从首条 ``session_meta.payload.originator``
    抽取；Claude transcript 没有等价字段，留 None。
    """
    if host_kind is None:
        if src is not None:
            host_kind = detect_transcript_format(src) or HOST_CODEX
        else:
            host_kind = HOST_CODEX
    originator: str | None = None
    if src is not None and src.exists() and host_kind == HOST_CODEX:
        originator = read_codex_originator(src)
    return {"host": host_kind, "host_originator": originator}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclasses.dataclass(frozen=True)
class CutPoint:
    line_index: int  # 0-based; line index of the FIRST post-rvf record
    byte_offset: int  # byte offset where the cut should happen (start of that line)
    timestamp: str | None
    marker_matched: str | None
    line_count_total: int


def _iter_jsonl_with_offsets(path: Path):
    """Yield (line_index, byte_start, byte_end, parsed) for each line.

    parsed is None on JSONDecodeError. line_index is 0-based.
    """
    if not path.exists():
        return
    data = path.read_bytes()
    pos = 0
    line_index = 0
    for raw in data.splitlines(keepends=True):
        end = pos + len(raw)
        text = raw.rstrip(b"\n\r").decode("utf-8", errors="replace")
        parsed: dict[str, Any] | None = None
        if text.strip():
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    parsed = obj
            except (json.JSONDecodeError, ValueError):
                parsed = None
        yield line_index, pos, end, parsed
        pos = end
        line_index += 1


def _codex_user_message_text(record: dict[str, Any]) -> str | None:
    """从一条 Codex rollout JSONL record 中抽出 user message 文本。

    专门解析 Codex schema (``event_msg.user_message`` 或
    ``response_item.message[role=user]``)。其他 host（如 Claude Code）的
    transcript schema 不同，需要写 ``_claude_user_message_text`` 平行实现，
    不要扩展本函数的 record type 分支。
    """
    if record.get("type") == "event_msg":
        payload = record.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "user_message":
            text = payload.get("message")
            return text if isinstance(text, str) else None
    if record.get("type") == "response_item":
        payload = record.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "message" and payload.get("role") == "user":
            content = payload.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                if parts:
                    return "\n".join(parts)
    return None


def _claude_user_message_text(record: dict[str, Any]) -> str | None:
    """从一条 Claude Code NDJSON record 中抽出 user message 文本。

    Claude Code transcript 中 user message 形式为 ``{"type": "user", "message":
    {"role": "user", "content": <string|list[content_block]>}}``。content 可以是：
    - 纯字符串 → 直接返回；
    - list[block]：``{type: text, text: ...}`` 块拼接；遇到 ``tool_result`` 块
      表明这是 tool 输出回流（不是真正用户输入），整条 record 不算 user message
      触发，返回 None；其余块（``image`` 等）不抽取。

    其他 host（Codex）的 transcript schema 不同，应当走 ``_codex_user_message_text``。
    """
    if record.get("type") != "user":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    if message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content if content else None
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == "tool_result":
            return None
        if bt == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    if not parts:
        return None
    return "\n".join(parts)


def find_rvf_start_in_claude_jsonl(
    path: Path,
    *,
    since_timestamp: str | None = None,
) -> CutPoint | None:
    """与 ``find_rvf_start_in_jsonl`` 等价，但解析 Claude Code NDJSON schema。

    通过 ``_claude_user_message_text`` 抽取 user message；其他行为（取最近一条
    匹配、``since_timestamp`` 过滤、``CutPoint`` 字段语义）与 Codex 版本完全
    一致——pre = [0, byte_offset)，post = [byte_offset, end)。
    """
    last_index = -1
    latest: CutPoint | None = None
    for line_index, byte_start, _byte_end, record in _iter_jsonl_with_offsets(path):
        last_index = line_index
        if record is None:
            continue
        text = _claude_user_message_text(record)
        if not text:
            continue
        matched = next((trigger for trigger in RVF_START_TRIGGERS if trigger in text), None)
        if matched is None:
            continue
        ts = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        if since_timestamp is not None:
            if ts is None or ts < since_timestamp:
                continue
        latest = CutPoint(
            line_index=line_index,
            byte_offset=byte_start,
            timestamp=ts,
            marker_matched=matched,
            line_count_total=last_index + 1,
        )
    if latest is None:
        return None
    return dataclasses.replace(latest, line_count_total=last_index + 1)


def find_rvf_start_in_jsonl(
    path: Path,
    *,
    since_timestamp: str | None = None,
) -> CutPoint | None:
    """扫 JSONL，找最近一条包含 RVF 手动触发文本的 user message。

    返回该 user message **行**的 cut point；pre = [0, byte_offset)，post = [byte_offset, end)。

    ``since_timestamp``（ISO8601 UTC 字符串，例如 ``"2026-05-04T04:18:29Z"``）：仅匹配
    ``record["timestamp"] >= since_timestamp`` 的 trigger 行。用于同会话连续两次 RVF
    的场景——第二次 finalize 不应把第一次的 RVF trigger 当 cut。本仓库 timestamp
    统一带 ``Z`` 后缀的 UTC ISO8601，字典序即时间序，无需 datetime 解析。

    Host 耦合：此函数透过 ``_codex_user_message_text`` 解析 user message，
    只识别 Codex rollout schema。若未来要支持 Claude Code transcript，
    应当新增 ``find_rvf_start_in_claude_jsonl`` 平行实现而非在此扩展。
    """
    last_index = -1
    latest: CutPoint | None = None
    for line_index, byte_start, _byte_end, record in _iter_jsonl_with_offsets(path):
        last_index = line_index
        if record is None:
            continue
        text = _codex_user_message_text(record)
        if not text:
            continue
        matched = next((trigger for trigger in RVF_START_TRIGGERS if trigger in text), None)
        if matched is None:
            continue
        ts = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        if since_timestamp is not None:
            if ts is None or ts < since_timestamp:
                continue
        latest = CutPoint(
            line_index=line_index,
            byte_offset=byte_start,
            timestamp=ts,
            marker_matched=matched,
            line_count_total=last_index + 1,  # tentative; updated below if more lines follow
        )
    if latest is None:
        return None
    return dataclasses.replace(latest, line_count_total=last_index + 1)


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.open("rb"))


def _read_origin(run_dir: Path) -> dict[str, Any] | None:
    origin_path = run_dir / "artifacts" / "origin.json"
    try:
        payload = json.loads(origin_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_current_transcript(event: dict[str, Any]) -> Path | None:
    for key in ("transcript_path", "session_path", "conversation_path", "session_file"):
        value = event.get(key)
        if isinstance(value, str) and value:
            candidate = Path(value).expanduser()
            if candidate.exists():
                return candidate
    return None


def _write_pre_slice(
    *,
    src: Path,
    dst_jsonl: Path,
    dst_manifest: Path,
    cut: CutPoint,
    source_kind: str,
    source_session_id: str | None,
    host_kind: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """同会话切片：写出 src 的 [0, cut.byte_offset) 字节到 dst_jsonl。"""
    dst_jsonl.parent.mkdir(parents=True, exist_ok=True)
    src_size = src.stat().st_size
    host_meta = _host_meta(src, host_kind=host_kind)
    if src_size > LARGE_FILE_BYTES:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "too_large_pointer_only",
            **host_meta,
            "source_kind": source_kind,
            "source_path": str(src),
            "source_size_bytes": src_size,
            "source_sha256": _sha256_file(src),
            "source_session_id": source_session_id,
            "cut": dataclasses.asdict(cut) if cut else None,
            "generated_at": _utc_now(),
        }
        if extra_meta:
            manifest.update(extra_meta)
        dst_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest
    with src.open("rb") as src_handle:
        chunk = src_handle.read(cut.byte_offset)
    dst_jsonl.write_bytes(chunk)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        **host_meta,
        "source_kind": source_kind,
        "source_path": str(src),
        "source_size_bytes": src_size,
        "source_sha256": _sha256_file(src),
        "source_session_id": source_session_id,
        "slice": {
            "byte_range": [0, cut.byte_offset],
            "line_range": [0, cut.line_index],  # exclusive of cut.line_index
        },
        "cut": dataclasses.asdict(cut),
        "captured_sha256": _sha256_file(dst_jsonl),
        "captured_size_bytes": dst_jsonl.stat().st_size,
        "generated_at": _utc_now(),
    }
    if extra_meta:
        manifest.update(extra_meta)
    dst_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _write_full_copy(
    *,
    src: Path,
    dst_jsonl: Path,
    dst_manifest: Path,
    source_kind: str,
    source_session_id: str | None,
    host_kind: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dst_jsonl.parent.mkdir(parents=True, exist_ok=True)
    host_meta = _host_meta(src, host_kind=host_kind)
    if not src.exists():
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "source_unavailable",
            **host_meta,
            "source_kind": source_kind,
            "source_path": str(src),
            "source_session_id": source_session_id,
            "generated_at": _utc_now(),
        }
        if extra_meta:
            manifest.update(extra_meta)
        dst_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest
    src_size = src.stat().st_size
    src_sha = _sha256_file(src)
    if src_size > LARGE_FILE_BYTES:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "too_large_pointer_only",
            **host_meta,
            "source_kind": source_kind,
            "source_path": str(src),
            "source_size_bytes": src_size,
            "source_sha256": src_sha,
            "source_session_id": source_session_id,
            "generated_at": _utc_now(),
        }
        if extra_meta:
            manifest.update(extra_meta)
        dst_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest
    shutil.copyfile(src, dst_jsonl)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        **host_meta,
        "source_kind": source_kind,
        "source_path": str(src),
        "source_size_bytes": src_size,
        "source_sha256": src_sha,
        "source_session_id": source_session_id,
        "captured_sha256": src_sha,
        "captured_size_bytes": src_size,
        "generated_at": _utc_now(),
    }
    if extra_meta:
        manifest.update(extra_meta)
    dst_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _write_post_slice(
    *,
    src: Path,
    dst_jsonl: Path,
    dst_manifest: Path,
    cut: CutPoint,
    source_session_id: str | None,
    host_kind: str | None = None,
) -> dict[str, Any]:
    dst_jsonl.parent.mkdir(parents=True, exist_ok=True)
    src_size = src.stat().st_size
    src_sha = _sha256_file(src)
    host_meta = _host_meta(src, host_kind=host_kind)
    if src_size > LARGE_FILE_BYTES:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "too_large_pointer_only",
            **host_meta,
            "source_kind": "same-session-slice",
            "source_path": str(src),
            "source_size_bytes": src_size,
            "source_sha256": src_sha,
            "source_session_id": source_session_id,
            "cut": dataclasses.asdict(cut),
            "generated_at": _utc_now(),
        }
        dst_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest
    with src.open("rb") as handle:
        handle.seek(cut.byte_offset)
        rest = handle.read()
    dst_jsonl.write_bytes(rest)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        **host_meta,
        "source_kind": "same-session-slice",
        "source_path": str(src),
        "source_size_bytes": src_size,
        "source_sha256": src_sha,
        "source_session_id": source_session_id,
        "slice": {
            "byte_range": [cut.byte_offset, src_size],
            "line_range": [cut.line_index, _count_lines(src)],
        },
        "cut": dataclasses.asdict(cut),
        "captured_sha256": _sha256_file(dst_jsonl),
        "captured_size_bytes": dst_jsonl.stat().st_size,
        "generated_at": _utc_now(),
    }
    dst_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _capture_reviewers(
    *,
    artifacts_dir: Path,
    out_dir: Path,
) -> list[dict[str, Any]]:
    reviewers_root = artifacts_dir / "reviewers"
    if not reviewers_root.is_dir():
        return []
    summaries: list[dict[str, Any]] = []
    for child in sorted(reviewers_root.iterdir()):
        if not child.is_dir():
            continue
        reviewer_id = child.name
        # 选最近 mtime 的 reviewer.stdout(.<n>).txt（运行多次会出现 stdout.2.txt 等 unique 命名）
        candidates = sorted(child.glob("reviewer.stdout*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            continue
        stdout_path = candidates[0]
        out_reviewer_dir = out_dir / "reviewers" / reviewer_id
        out_reviewer_dir.mkdir(parents=True, exist_ok=True)
        distilled = distill_reviewer_stream(stdout_path=stdout_path, reviewer_id=reviewer_id)
        traj_path = out_reviewer_dir / "trajectory.jsonl"
        write_jsonl(distilled, traj_path)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "reviewer_id": reviewer_id,
            "source_path": str(stdout_path),
            "source_sha256": _sha256_file(stdout_path),
            "record_count": len(distilled),
            "generated_at": _utc_now(),
        }
        (out_reviewer_dir / "trajectory.manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summaries.append(manifest)
    return summaries


def capture_run(
    *,
    run_dir: Path,
    event: dict[str, Any] | None,
    repo: Path | None = None,
) -> dict[str, Any]:
    """把指定 RVF run 的 trajectory 全套产物写入 <run_dir>/artifacts/trajectory/。"""

    artifacts_dir = run_dir / "artifacts"
    out_dir = artifacts_dir / "trajectory"
    pre_dir = out_dir / "pre-rvf"
    rvf_dir = out_dir / "rvf"
    pre_dir.mkdir(parents=True, exist_ok=True)
    rvf_dir.mkdir(parents=True, exist_ok=True)

    origin = _read_origin(run_dir)
    event = event or {}
    current_transcript = _resolve_current_transcript(event)
    event_session_id = event.get("session_id") if isinstance(event.get("session_id"), str) else None

    # Cline Kanban dispatch（flow-2-branch / flow-2-inplace）：触发 capture 的
    # stop hook event 只知道 parent Codex transcript，但被 dispatch 的 task
    # agent 的 UserPromptSubmit hook 已把自己的 child_session_id /
    # child_transcript_path 自回填进 origin.json。优先采用 child（task agent）
    # transcript，让下方 forked 分支捕获真正的 RVF 工作而非父会话 dispatch 前
    # 的对话。仅当 origin.json 显式带 child 字段、child transcript 存在、且
    # child_session_id ≠ parent(origin.session_id) 时生效——因此 same-session
    # manual / followup 与既有 Codex forked 路径（origin.json 无 child 字段）
    # 行为完全不变。
    if isinstance(origin, dict):
        child_tp = origin.get("child_transcript_path")
        child_sid = origin.get("child_session_id")
        origin_sid = origin.get("session_id")
        if (
            isinstance(child_tp, str)
            and child_tp.strip()
            and isinstance(child_sid, str)
            and child_sid.strip()
            and child_sid.strip()
            != (origin_sid.strip() if isinstance(origin_sid, str) else None)
        ):
            child_path = Path(child_tp).expanduser()
            if child_path.is_file():
                current_transcript = child_path.resolve()
                event_session_id = child_sid.strip()

    # 探测 transcript host schema：post 走 current_transcript，pre 走 parent_transcript
    # （forked 场景）或 current_transcript 本身（same-session 切片）。探测失败 fallback
    # 到 HOST_CODEX 以保证既有 Codex-only 用例无回归。
    post_host_kind = (
        detect_transcript_format(current_transcript) if current_transcript is not None else None
    ) or HOST_CODEX

    pre_kind: str = "none"
    post_kind: str = "none"

    parent_transcript: Path | None = None
    parent_session_id: str | None = None
    if isinstance(origin, dict):
        ptp = origin.get("transcript_path") or origin.get("transcript_file")
        if isinstance(ptp, str) and ptp:
            parent_transcript = Path(ptp).expanduser()
        psid = origin.get("session_id")
        if isinstance(psid, str):
            parent_session_id = psid

    forked = bool(
        parent_session_id
        and event_session_id
        and parent_session_id != event_session_id
    )

    pre_host_kind = (
        detect_transcript_format(parent_transcript) if parent_transcript is not None else None
    ) or (post_host_kind if not forked else HOST_CODEX)

    pre_manifest: dict[str, Any]
    post_manifest: dict[str, Any]

    if forked and parent_transcript is not None:
        pre_kind = "forked-source-full"
        post_kind = "forked-target-full"
        pre_manifest = _write_full_copy(
            src=parent_transcript,
            dst_jsonl=pre_dir / "rollout.jsonl",
            dst_manifest=pre_dir / "manifest.json",
            source_kind=pre_kind,
            source_session_id=parent_session_id,
            host_kind=pre_host_kind,
            extra_meta={"event_session_id": event_session_id},
        )
        if current_transcript is not None:
            post_manifest = _write_full_copy(
                src=current_transcript,
                dst_jsonl=rvf_dir / "rollout.jsonl",
                dst_manifest=rvf_dir / "rollout.manifest.json",
                source_kind=post_kind,
                source_session_id=event_session_id,
                host_kind=post_host_kind,
            )
        else:
            post_manifest = {
                "schema_version": SCHEMA_VERSION,
                "status": "rollout_unavailable",
                **_host_meta(None, host_kind=post_host_kind),
                "source_kind": post_kind,
                "source_session_id": event_session_id,
                "generated_at": _utc_now(),
            }
            (rvf_dir / "rollout.manifest.json").write_text(
                json.dumps(post_manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    else:
        # 同会话或无法判定 → 同会话切片逻辑
        if current_transcript is not None:
            # 当 summary.json 里有 prepare 时刻 timestamp 时，作为 marker 的下界，
            # 防止同一会话内连续两次 RVF 时错把第一次的 marker 当成本 run 的 cut。
            since_timestamp: str | None = None
            try:
                summary_path = run_dir / "summary.json"
                if summary_path.is_file():
                    summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
                    if isinstance(summary_payload, dict):
                        ts_value = summary_payload.get("timestamp")
                        if isinstance(ts_value, str) and ts_value:
                            since_timestamp = ts_value
            except (OSError, json.JSONDecodeError):
                since_timestamp = None
            if post_host_kind == HOST_CLAUDE:
                cut = find_rvf_start_in_claude_jsonl(
                    current_transcript,
                    since_timestamp=since_timestamp,
                )
            else:
                cut = find_rvf_start_in_jsonl(
                    current_transcript,
                    since_timestamp=since_timestamp,
                )
            if cut is not None:
                pre_kind = "same-session-slice"
                post_kind = "same-session-slice"
                pre_host_kind = post_host_kind  # 同 transcript，同 host
                pre_manifest = _write_pre_slice(
                    src=current_transcript,
                    dst_jsonl=pre_dir / "rollout.jsonl",
                    dst_manifest=pre_dir / "manifest.json",
                    cut=cut,
                    source_kind=pre_kind,
                    source_session_id=event_session_id,
                    host_kind=pre_host_kind,
                )
                post_manifest = _write_post_slice(
                    src=current_transcript,
                    dst_jsonl=rvf_dir / "rollout.jsonl",
                    dst_manifest=rvf_dir / "rollout.manifest.json",
                    cut=cut,
                    source_session_id=event_session_id,
                    host_kind=post_host_kind,
                )
            else:
                # 找不到 marker → 全部归 post，pre 为空 manifest
                pre_kind = "none"
                pre_manifest = {
                    "schema_version": SCHEMA_VERSION,
                    "status": "no_marker_found",
                    **_host_meta(current_transcript, host_kind=post_host_kind),
                    "source_kind": pre_kind,
                    "source_path": str(current_transcript),
                    "source_session_id": event_session_id,
                    "generated_at": _utc_now(),
                }
                (pre_dir / "manifest.json").write_text(
                    json.dumps(pre_manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                post_kind = "same-session-full"
                post_manifest = _write_full_copy(
                    src=current_transcript,
                    dst_jsonl=rvf_dir / "rollout.jsonl",
                    dst_manifest=rvf_dir / "rollout.manifest.json",
                    source_kind=post_kind,
                    source_session_id=event_session_id,
                    host_kind=post_host_kind,
                )
        else:
            pre_manifest = {
                "schema_version": SCHEMA_VERSION,
                "status": "rollout_unavailable",
                **_host_meta(None),
                "source_kind": "none",
                "generated_at": _utc_now(),
            }
            (pre_dir / "manifest.json").write_text(
                json.dumps(pre_manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            post_manifest = {
                "schema_version": SCHEMA_VERSION,
                "status": "rollout_unavailable",
                **_host_meta(None),
                "source_kind": "none",
                "generated_at": _utc_now(),
            }
            (rvf_dir / "rollout.manifest.json").write_text(
                json.dumps(post_manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    # 蒸馏 post-rvf：按 post_host_kind 选 distiller。rollout 文件名统一为
    # host-中性的 rollout.jsonl（Codex / Claude 共用同名）；host 区分由
    # manifest 的 host 字段表达。
    distill_index: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "rollout_file": "rollout.jsonl",
        "record_count": 0,
        "kind_counts": {},
    }
    rvf_rollout = rvf_dir / "rollout.jsonl"
    if rvf_rollout.exists():
        if post_host_kind == HOST_CLAUDE:
            distilled, distill_index = distill_claude_jsonl(
                rollout_path=rvf_rollout,
                rollout_filename="rollout.jsonl",
                repo=repo,
            )
        else:
            distilled, distill_index = distill_codex_jsonl(
                rollout_path=rvf_rollout,
                rollout_filename="rollout.jsonl",
                repo=repo,
            )
        write_jsonl(distilled, rvf_dir / "trajectory.jsonl")
        (rvf_dir / "trajectory.index.json").write_text(
            json.dumps(distill_index, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    reviewer_summaries = _capture_reviewers(artifacts_dir=artifacts_dir, out_dir=rvf_dir)

    # 抓取子代理 transcript（host 归一，S2/handoff A2）。子代理内部的 write-op
    # 不出现在主轨迹，必须各自拉回来才能让 causality.json / subagent_patch_event_count
    # 看到真正的 fix patch。Codex 经主 rollout 的 spawn_agent + ~/.codex/sessions glob；
    # Claude 经原始父 transcript 同名目录 <uuid>/subagents/。host 分派在
    # subagent_capture facade，故此处按 post_host_kind 传 host_kind 与原始 transcript。
    subagent_manifests: list[dict[str, Any]] = []
    if rvf_rollout.exists():
        subagent_manifests = capture_all_subagents(
            main_rollout_path=rvf_rollout,
            dst_root=rvf_dir / "subagents",
            repo=repo,
            host_kind=post_host_kind,
            original_transcript=current_transcript,
        )

    # Summary-level host: 取 post 轨迹的 host_kind（capture_run 入口已探测过，
    # 此处沿用而非重新探测）。host_originator 优先取 post rollout（即 RVF 自身
    # 轨迹）；同会话 slice 与 forked-target 都会写出 rvf/rollout.jsonl，
    # 可直接读取；缺失时退到 post_manifest.host_originator / pre_manifest.host_originator。
    rvf_rollout_for_host = rvf_dir / "rollout.jsonl"
    summary_host_meta = _host_meta(
        rvf_rollout_for_host if rvf_rollout_for_host.exists() else None,
        host_kind=post_host_kind,
    )
    if summary_host_meta.get("host_originator") is None:
        for candidate in (post_manifest, pre_manifest):
            originator = candidate.get("host_originator") if isinstance(candidate, dict) else None
            if isinstance(originator, str) and originator:
                summary_host_meta["host_originator"] = originator
                break

    summary = {
        "schema_version": SCHEMA_VERSION,
        **summary_host_meta,
        "trajectory_dir": str(out_dir),
        "pre_rvf_source_kind": pre_kind,
        "post_rvf_source_kind": post_kind,
        "pre_manifest": pre_manifest,
        "post_manifest": post_manifest,
        "distill_index": distill_index,
        "reviewers": reviewer_summaries,
        "subagents": subagent_manifests,
        "generated_at": _utc_now(),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture RVF run trajectory artifacts.")
    parser.add_argument("--run-dir", required=True, help="Target RVF run directory.")
    parser.add_argument(
        "--event-json",
        help="Path to a JSON file containing the stop hook event payload.",
    )
    parser.add_argument("--repo", help="Optional repo root to normalize patch paths.")
    args = parser.parse_args()
    run_dir = Path(args.run_dir).expanduser().resolve()
    event: dict[str, Any] = {}
    if args.event_json:
        try:
            event = json.loads(Path(args.event_json).expanduser().read_text(encoding="utf-8"))
            if not isinstance(event, dict):
                event = {}
        except (OSError, json.JSONDecodeError):
            event = {}
    repo = Path(args.repo).expanduser().resolve() if args.repo else None
    summary = capture_run(run_dir=run_dir, event=event, repo=repo)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
