#!/usr/bin/env python3
"""把子代理的独立 transcript 拉回 RVF run 目录（host 中性 facade）。

背景
====

RVF 被审会话可能派生子代理（reviewer / validate-fix / 开发者用 Task 派的探查
子代理等），每个子代理拥有独立 transcript，其内部 ``apply_patch`` / ``Edit`` 等
write-op 不出现在主轨迹——只看主轨迹时 ``causality.json::patches[]`` 与
``subagent_patch_event_count`` 会漏算真正的修复 patch。本模块负责发现并把这些
子代理 transcript 拷回 ``<run_dir>/artifacts/trajectory/rvf/subagents/<agent_id>/``，
蒸馏成统一 ``trajectory.jsonl`` + ``trajectory.index.json`` + ``manifest.json``。

Host 归一（S2 / handoff A2）
============================

「如何发现 spawn、如何定位子代理 transcript」是 host 耦合的，分派到
``adapters/<host>/subagent.py``：

- **Codex**：主 rollout 里的 ``collab_agent_spawn_end`` + Codex sessions 目录下
  ``rollout-*-<agent_id>.jsonl`` glob（``adapters/codex/subagent.py``）。
- **Claude Code**：父会话同名目录 ``<uuid>/subagents/agent-*.jsonl``
  （``adapters/claude_code/subagent.py``）。

本 facade 只保留 host 中性的 copy / distill / manifest 骨架，按 host_kind 选取
discovery、distill 与 originator 提取函数。``SpawnRecord`` /
``discover_spawned_agents`` / ``find_subagent_rollout`` / ``codex_sessions_root``
从 adapters re-export，保持既有 import 点与测试兼容。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _rvf_pyroot  # noqa: E402,F401  — 把 pyroot 加入 sys.path，供 core.* / adapters.* import

from trajectory_distill import (  # noqa: E402
    HOST_CLAUDE,
    HOST_CODEX,
    distill_claude_jsonl,
    distill_codex_jsonl,
    read_codex_originator,
    write_jsonl,
)
from core.subagents.models import SpawnRecord  # noqa: E402,F401  — re-export
from adapters.codex.subagent import (  # noqa: E402
    codex_sessions_root,  # noqa: F401  — re-export
    discover_spawned_agents,  # noqa: F401  — re-export
    find_subagent_rollout,  # noqa: F401  — re-export
    resolve_subagents as _codex_resolve_subagents,
)
from adapters.claude_code.subagent import (  # noqa: E402
    read_claude_originator,
    resolve_subagents as _claude_resolve_subagents,
)

SCHEMA_VERSION = 1
LARGE_FILE_BYTES = 200 * 1024 * 1024  # 200 MB; pointer-only 上界，与 trajectory_capture 同
SUBAGENTS_DIR_NAME = "subagents"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _host_distill(host_kind: str):
    """按 host 选 distiller（公共边界仍返回 ``(list[dict], index)``）。"""
    if host_kind == HOST_CLAUDE:
        return distill_claude_jsonl
    return distill_codex_jsonl


def _host_originator(host_kind: str, src: Path) -> str | None:
    if host_kind == HOST_CLAUDE:
        return read_claude_originator(src)
    return read_codex_originator(src)


def _resolve_subagents(
    host_kind: str,
    *,
    main_rollout_path: Path,
    original_transcript: Path | None,
    sessions_root: Path | None,
) -> list[tuple[SpawnRecord, Path | None]]:
    if host_kind == HOST_CLAUDE:
        return _claude_resolve_subagents(
            main_rollout_path=main_rollout_path,
            original_transcript=original_transcript,
            sessions_root=sessions_root,
        )
    return _codex_resolve_subagents(
        main_rollout_path=main_rollout_path,
        original_transcript=original_transcript,
        sessions_root=sessions_root,
    )


def _spawn_meta(spawn: SpawnRecord) -> dict[str, Any]:
    return {
        "agent_id": spawn.agent_id,
        "spawn_call_id": spawn.call_id,
        "role": spawn.role,
        "nickname": spawn.nickname,
        "spawned_at": spawn.ts,
        "main_rollout_line_index": spawn.line_index,
        "prompt": spawn.prompt,
    }


def _write_manifest(
    *,
    dst_manifest: Path,
    spawn: SpawnRecord,
    status: str,
    extra: dict[str, Any],
    host: str = HOST_CODEX,
    host_originator: str | None = None,
) -> dict[str, Any]:
    """Subagent manifest 的统一 writer。``host`` 由调用方按 host_kind 传入；
    ``host_originator`` 来自子代理 transcript（Codex = session_meta.originator，
    Claude 无此概念为 None），缺失时为 None。"""
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "host": host,
        "host_originator": host_originator,
        "spawn": _spawn_meta(spawn),
        "generated_at": _utc_now(),
    }
    manifest.update(extra)
    dst_manifest.parent.mkdir(parents=True, exist_ok=True)
    dst_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def capture_subagent(
    spawn: SpawnRecord,
    *,
    dst_dir: Path,
    repo: Path | None = None,
    sessions_root: Path | None = None,
    host_kind: str = HOST_CODEX,
    src: Path | None = None,
) -> dict[str, Any]:
    """把单个 subagent 的 transcript 拷贝 + 蒸馏到 ``dst_dir``。

    ``src`` 为已定位的源 transcript；未提供且为 Codex 时按 agent_id glob 回退
    （兼容旧直调）。缺失 / 过大时只写 manifest 指针，不抛异常。
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_rollout = dst_dir / "rollout.jsonl"
    dst_manifest = dst_dir / "manifest.json"

    if src is None and host_kind == HOST_CODEX:
        src = find_subagent_rollout(spawn.agent_id, sessions_root=sessions_root)
    if src is None:
        return _write_manifest(
            dst_manifest=dst_manifest,
            spawn=spawn,
            status="rollout_unavailable",
            extra={"source_path": None},
            host=host_kind,
            host_originator=None,
        )

    src_size = src.stat().st_size
    src_sha = _sha256_file(src)
    src_originator = _host_originator(host_kind, src)

    if src_size > LARGE_FILE_BYTES:
        return _write_manifest(
            dst_manifest=dst_manifest,
            spawn=spawn,
            status="too_large_pointer_only",
            extra={
                "source_path": str(src),
                "source_size_bytes": src_size,
                "source_sha256": src_sha,
            },
            host=host_kind,
            host_originator=src_originator,
        )

    shutil.copyfile(src, dst_rollout)

    distill_index: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "rollout_file": "rollout.jsonl",
        "record_count": 0,
        "kind_counts": {},
    }
    try:
        distilled, distill_index = _host_distill(host_kind)(
            rollout_path=dst_rollout,
            rollout_filename="rollout.jsonl",
            repo=repo,
        )
        write_jsonl(distilled, dst_dir / "trajectory.jsonl")
        (dst_dir / "trajectory.index.json").write_text(
            json.dumps(distill_index, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        distill_status = "ok"
        distill_error: str | None = None
    except Exception as exc:  # noqa: BLE001 — capture must never raise
        distill_status = "distill_failed"
        distill_error = f"{type(exc).__name__}: {exc}"

    return _write_manifest(
        dst_manifest=dst_manifest,
        spawn=spawn,
        status="ok",
        extra={
            "source_path": str(src),
            "source_size_bytes": src_size,
            "source_sha256": src_sha,
            "captured_size_bytes": dst_rollout.stat().st_size,
            "captured_sha256": _sha256_file(dst_rollout),
            "distill_status": distill_status,
            "distill_error": distill_error,
            "distill_index": distill_index,
        },
        host=host_kind,
        host_originator=src_originator,
    )


def capture_all_subagents(
    *,
    main_rollout_path: Path,
    dst_root: Path,
    repo: Path | None = None,
    sessions_root: Path | None = None,
    host_kind: str = HOST_CODEX,
    original_transcript: Path | None = None,
) -> list[dict[str, Any]]:
    """发现并逐一捕获子代理。按 ``host_kind`` 分派发现逻辑。

    ``dst_root`` 是 ``<rvf_dir>/subagents``；每个 agent 获得自己的子目录。
    ``original_transcript`` 供 Claude 路径定位 ``<uuid>/subagents``（Codex 忽略）。
    返回 manifests 列表（与发现顺序一致）；无子代理时返回 ``[]`` 且不建目录。
    """
    resolved = _resolve_subagents(
        host_kind,
        main_rollout_path=main_rollout_path,
        original_transcript=original_transcript,
        sessions_root=sessions_root,
    )
    if not resolved:
        return []
    dst_root.mkdir(parents=True, exist_ok=True)
    manifests: list[dict[str, Any]] = []
    for spawn, src in resolved:
        sub_dir = dst_root / spawn.agent_id
        manifest = capture_subagent(
            spawn,
            dst_dir=sub_dir,
            repo=repo,
            sessions_root=sessions_root,
            host_kind=host_kind,
            src=src,
        )
        manifests.append(manifest)
    return manifests


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover subagents (Codex spawn_agent / Claude Task) and capture their transcripts."
    )
    parser.add_argument(
        "--main-rollout", required=True, help="Path to the main RVF rollout JSONL."
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        help="Destination root (will write subagents/<agent_id>/ underneath).",
    )
    parser.add_argument("--repo", help="Optional repo root to normalize patch paths.")
    parser.add_argument(
        "--host",
        choices=[HOST_CODEX, HOST_CLAUDE],
        default=HOST_CODEX,
        help="Host kind for discovery dispatch (default: codex).",
    )
    parser.add_argument(
        "--original-transcript",
        help="Original parent transcript path (Claude: locates <uuid>/subagents/).",
    )
    parser.add_argument(
        "--sessions-root",
        help="Override Codex sessions dir (default resolved by adapters/codex/subagent.py).",
    )
    args = parser.parse_args()

    main_rollout = Path(args.main_rollout).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    repo = Path(args.repo).expanduser().resolve() if args.repo else None
    original_transcript = (
        Path(args.original_transcript).expanduser().resolve()
        if args.original_transcript
        else None
    )
    sessions_root = (
        Path(args.sessions_root).expanduser().resolve() if args.sessions_root else None
    )
    manifests = capture_all_subagents(
        main_rollout_path=main_rollout,
        dst_root=out_dir,
        repo=repo,
        sessions_root=sessions_root,
        host_kind=args.host,
        original_transcript=original_transcript,
    )
    print(json.dumps(manifests, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
