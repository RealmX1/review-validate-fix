#!/usr/bin/env python3
"""把一次 RVF run 的轨迹捕获成结构化产物。

切分逻辑（plan v2）:
- 同会话场景：父 codex rollout 中包含 RVF 起点 marker，按行/字节切片成
  pre-rvf 与 post-rvf。
- 分叉会话场景：通过 `<run_dir>/artifacts/origin.json` 取父会话 rollout 路径，
  整份作为 pre-rvf；当前 fork 会话 rollout 整份作为 post-rvf。

产物布局:
<run_dir>/artifacts/trajectory/
├── pre-rvf/
│   ├── rollout.codex.jsonl           # 切片或拷贝（视场景）
│   └── manifest.json                 # source_kind / sha256 / 行范围 / cut_marker
└── rvf/
    ├── rollout.codex.jsonl
    ├── rollout.codex.manifest.json
    ├── trajectory.jsonl              # 蒸馏后统一 schema
    ├── trajectory.index.json         # 反向索引
    ├── reviewers/<id>/{trajectory.jsonl, trajectory.manifest.json}
    └── subagents/<agent_id>/{rollout.codex.jsonl, trajectory.jsonl,
                              trajectory.index.json, manifest.json}
                                       # spawn_agent 子代理（reviewer / validate-fix
                                       # 等）独立 rollout 拷贝 + 蒸馏；spawn metadata
                                       # （role / nickname / prompt / call_id）写在
                                       # manifest.json::spawn 下。

Host 耦合说明:
本模块当前只支持 **Codex** rollout JSONL 作为 transcript 输入。下列函数
显式或隐式依赖 Codex schema（``event_msg.user_message`` / ``response_item.message``
等 record type）：

- ``_codex_user_message_text``（私有 helper）
- ``find_rvf_start_in_jsonl``（透过上述 helper 解析 user message）
- ``capture_run``（在 same-session 分支里写出 ``rollout.codex.jsonl``，
  通过 ``trajectory_distill.distill_codex_jsonl`` 蒸馏 Codex schema）

未来若要支持 Claude Code transcript（``~/.claude/projects/<proj>/<sid>.jsonl``，
schema 是 ``type: user|assistant|tool_use|tool_result`` 的不同 NDJSON），
应当并行实现 ``_claude_user_message_text`` + ``find_rvf_start_in_claude_jsonl``，
并在 ``capture_run`` 入口按 transcript 探测分派；不要原地扩展现有 Codex 解析器。
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
    distill_codex_jsonl,
    distill_reviewer_stream,
    write_jsonl,
)

SCHEMA_VERSION = 1
LARGE_FILE_BYTES = 200 * 1024 * 1024  # 200 MB

RVF_FORK_MARKER = "RVF_FORKED_REVIEW_VALIDATE_FIX"
KANBAN_FOLLOWUP_MARKER = "RVF_KANBAN_FOLLOWUP_TRIGGER"
RVF_PROMPT_MARKERS = (
    "RVF_FORK_EXPERIMENT",
    "RVF_HANDOFF_FILE",
)
DEFAULT_MARKERS = (RVF_FORK_MARKER, KANBAN_FOLLOWUP_MARKER, *RVF_PROMPT_MARKERS)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def find_rvf_start_in_jsonl(
    path: Path,
    *,
    markers: tuple[str, ...] = DEFAULT_MARKERS,
    since_timestamp: str | None = None,
) -> CutPoint | None:
    """扫 JSONL，找首个 user message 文本含 markers 中任意 marker 的位置。

    返回该 user message **行**的 cut point；pre = [0, byte_offset)，post = [byte_offset, end)。

    ``since_timestamp``（ISO8601 UTC 字符串，例如 ``"2026-05-04T04:18:29Z"``）：仅匹配
    ``record["timestamp"] >= since_timestamp`` 的 marker 行。用于同会话连续两次 RVF
    的场景——第二次 finalize 不应把第一次的 marker 当 cut。本仓库 timestamp
    统一带 ``Z`` 后缀的 UTC ISO8601，字典序即时间序，无需 datetime 解析。

    Host 耦合：此函数透过 ``_codex_user_message_text`` 解析 user message，
    只识别 Codex rollout schema。若未来要支持 Claude Code transcript，
    应当新增 ``find_rvf_start_in_claude_jsonl`` 平行实现而非在此扩展。
    """
    last_index = -1
    for line_index, byte_start, _byte_end, record in _iter_jsonl_with_offsets(path):
        last_index = line_index
        if record is None:
            continue
        text = _codex_user_message_text(record)
        if not text:
            continue
        matched = next((marker for marker in markers if marker in text), None)
        if matched is None:
            continue
        ts = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        if since_timestamp is not None:
            if ts is None or ts < since_timestamp:
                continue
        return CutPoint(
            line_index=line_index,
            byte_offset=byte_start,
            timestamp=ts,
            marker_matched=matched,
            line_count_total=last_index + 1,  # tentative; updated below if more lines follow
        )
    return None


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
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """同会话切片：写出 src 的 [0, cut.byte_offset) 字节到 dst_jsonl。"""
    dst_jsonl.parent.mkdir(parents=True, exist_ok=True)
    src_size = src.stat().st_size
    if src_size > LARGE_FILE_BYTES:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "too_large_pointer_only",
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
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dst_jsonl.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "source_unavailable",
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
) -> dict[str, Any]:
    dst_jsonl.parent.mkdir(parents=True, exist_ok=True)
    src_size = src.stat().st_size
    src_sha = _sha256_file(src)
    if src_size > LARGE_FILE_BYTES:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "too_large_pointer_only",
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

    pre_manifest: dict[str, Any]
    post_manifest: dict[str, Any]

    if forked and parent_transcript is not None:
        pre_kind = "forked-source-full"
        post_kind = "forked-target-full"
        pre_manifest = _write_full_copy(
            src=parent_transcript,
            dst_jsonl=pre_dir / "rollout.codex.jsonl",
            dst_manifest=pre_dir / "manifest.json",
            source_kind=pre_kind,
            source_session_id=parent_session_id,
            extra_meta={"event_session_id": event_session_id},
        )
        if current_transcript is not None:
            post_manifest = _write_full_copy(
                src=current_transcript,
                dst_jsonl=rvf_dir / "rollout.codex.jsonl",
                dst_manifest=rvf_dir / "rollout.codex.manifest.json",
                source_kind=post_kind,
                source_session_id=event_session_id,
            )
        else:
            post_manifest = {
                "schema_version": SCHEMA_VERSION,
                "status": "rollout_unavailable",
                "source_kind": post_kind,
                "source_session_id": event_session_id,
                "generated_at": _utc_now(),
            }
            (rvf_dir / "rollout.codex.manifest.json").write_text(
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
            cut = find_rvf_start_in_jsonl(
                current_transcript,
                since_timestamp=since_timestamp,
            )
            if cut is not None:
                pre_kind = "same-session-slice"
                post_kind = "same-session-slice"
                pre_manifest = _write_pre_slice(
                    src=current_transcript,
                    dst_jsonl=pre_dir / "rollout.codex.jsonl",
                    dst_manifest=pre_dir / "manifest.json",
                    cut=cut,
                    source_kind=pre_kind,
                    source_session_id=event_session_id,
                )
                post_manifest = _write_post_slice(
                    src=current_transcript,
                    dst_jsonl=rvf_dir / "rollout.codex.jsonl",
                    dst_manifest=rvf_dir / "rollout.codex.manifest.json",
                    cut=cut,
                    source_session_id=event_session_id,
                )
            else:
                # 找不到 marker → 全部归 post，pre 为空 manifest
                pre_kind = "none"
                pre_manifest = {
                    "schema_version": SCHEMA_VERSION,
                    "status": "no_marker_found",
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
                    dst_jsonl=rvf_dir / "rollout.codex.jsonl",
                    dst_manifest=rvf_dir / "rollout.codex.manifest.json",
                    source_kind=post_kind,
                    source_session_id=event_session_id,
                )
        else:
            pre_manifest = {
                "schema_version": SCHEMA_VERSION,
                "status": "rollout_unavailable",
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
                "source_kind": "none",
                "generated_at": _utc_now(),
            }
            (rvf_dir / "rollout.codex.manifest.json").write_text(
                json.dumps(post_manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    # 蒸馏 post-rvf
    distill_index: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "rollout_file": "rollout.codex.jsonl",
        "record_count": 0,
        "kind_counts": {},
    }
    rvf_rollout = rvf_dir / "rollout.codex.jsonl"
    if rvf_rollout.exists():
        distilled, distill_index = distill_codex_jsonl(
            rollout_path=rvf_rollout,
            rollout_filename="rollout.codex.jsonl",
            repo=repo,
        )
        write_jsonl(distilled, rvf_dir / "trajectory.jsonl")
        (rvf_dir / "trajectory.index.json").write_text(
            json.dumps(distill_index, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    reviewer_summaries = _capture_reviewers(artifacts_dir=artifacts_dir, out_dir=rvf_dir)

    # 抓取 spawn_agent 子代理 rollouts。reviewer / validate-fix 子代理走 Codex
    # 内置 spawn_agent，独立 session_id；它们的 apply_patch 不会出现在主 rollout，
    # 必须从 ~/.codex/sessions/ 拉回来才能让 causality.json 看到真正的 fix patch。
    subagent_manifests: list[dict[str, Any]] = []
    if rvf_rollout.exists():
        subagent_manifests = capture_all_subagents(
            main_rollout_path=rvf_rollout,
            dst_root=rvf_dir / "subagents",
            repo=repo,
        )

    summary = {
        "schema_version": SCHEMA_VERSION,
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
