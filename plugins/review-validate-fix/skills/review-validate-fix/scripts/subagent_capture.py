#!/usr/bin/env python3
"""把 Codex ``spawn_agent`` 子代理的独立 rollout 拉回 RVF run 目录。

背景
====

Codex 主会话通过内置工具 ``spawn_agent`` 派生子代理（reviewer / validate-fix
等），每个子代理拥有独立的 session_id 和 ``~/.codex/sessions/.../rollout-*-<id>.jsonl``。
主会话的 rollout 不包含子代理内部的 ``apply_patch`` 等细节——只看主 rollout 时
``causality.json::patches[]`` 永远缺"真正修复 patch"。本模块负责：

1. 在主 rollout 中找出 ``event_msg.collab_agent_spawn_end`` records，列出本次 RVF
   spawn 过的子代理 (call_id / agent_id / role / nickname / prompt / ts)。
2. 对每个 agent_id，在 Codex 会话目录里 glob ``rollout-*-<agent_id>.jsonl``。
3. 拷贝命中的 rollout 到 ``<run_dir>/artifacts/trajectory/rvf/subagents/<agent_id>/``，
   并复用 ``trajectory_distill.distill_codex_jsonl`` 生成
   ``trajectory.jsonl`` + ``trajectory.index.json``，以及 ``manifest.json``
   （含 spawn metadata 与 sha256）。

Host 耦合
=========

只识别 Codex spawn_agent / collab_agent_spawn_end / Codex rollout 路径布局。
未来其他 host 不存在等价 spawn primitive，应单独实现并按 host 分派。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trajectory_distill import (  # noqa: E402
    HOST_KIND,
    distill_codex_jsonl,
    read_codex_originator,
    write_jsonl,
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


def codex_sessions_root() -> Path:
    """Codex rollout 的根目录。

    优先级：``$CODEX_HOME/sessions`` > ``~/.codex/sessions``。这是 Codex CLI
    自身查 ``~/.codex/`` 时使用的同一约定。
    """
    home = os.environ.get("CODEX_HOME")
    if home:
        candidate = Path(home).expanduser()
    else:
        candidate = Path.home() / ".codex"
    return candidate / "sessions"


@dataclasses.dataclass(frozen=True)
class SpawnRecord:
    """主 rollout 中一次 ``spawn_agent`` 的描述，足以定位独立 rollout。"""

    call_id: str | None
    agent_id: str
    role: str | None
    nickname: str | None
    prompt: str | None
    ts: str | None
    line_index: int


def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_index, raw in enumerate(handle):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                yield line_index, obj


def discover_spawned_agents(rollout_path: Path) -> list[SpawnRecord]:
    """扫主 rollout，挑出所有 ``collab_agent_spawn_end`` events。

    按出现顺序返回；不去重（同 agent_id 理论上只 spawn 一次，但保留多份以防
    schema 变化）。
    """
    out: list[SpawnRecord] = []
    for line_index, record in _iter_jsonl(rollout_path):
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "collab_agent_spawn_end":
            continue
        agent_id = payload.get("new_thread_id")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        ts = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        call_id = payload.get("call_id") if isinstance(payload.get("call_id"), str) else None
        role = payload.get("new_agent_role") if isinstance(payload.get("new_agent_role"), str) else None
        nickname = (
            payload.get("new_agent_nickname")
            if isinstance(payload.get("new_agent_nickname"), str)
            else None
        )
        prompt = payload.get("prompt") if isinstance(payload.get("prompt"), str) else None
        out.append(
            SpawnRecord(
                call_id=call_id,
                agent_id=agent_id,
                role=role,
                nickname=nickname,
                prompt=prompt,
                ts=ts,
                line_index=line_index,
            )
        )
    return out


def find_subagent_rollout(
    agent_id: str,
    *,
    sessions_root: Path | None = None,
) -> Path | None:
    """在 Codex sessions 目录里 glob ``rollout-*-<agent_id>.jsonl``。

    匹配多个时返回 mtime 最新的一份（理论上只有一份，多份说明用户 / Codex
    出过 anomaly，新的更可信）。找不到返回 None。
    """
    root = sessions_root if sessions_root is not None else codex_sessions_root()
    if not root.is_dir():
        return None
    matches = list(root.glob(f"**/rollout-*-{agent_id}.jsonl"))
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


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
    host_originator: str | None = None,
) -> dict[str, Any]:
    """Subagent manifest 的统一 writer。``host`` 始终为 ``HOST_KIND="codex"``
    （Codex spawn_agent 派生的子代理也是 Codex rollout schema）；
    ``host_originator`` 来自子代理 rollout 的 session_meta，缺失时为 None。"""
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "host": HOST_KIND,
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
) -> dict[str, Any]:
    """把单个 subagent 的 rollout 拷贝 + 蒸馏到 ``dst_dir``。

    ``dst_dir`` 通常是 ``<run_dir>/artifacts/trajectory/rvf/subagents/<agent_id>/``。
    缺失 / 过大时只写 manifest 指针，不抛异常。
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_rollout = dst_dir / "rollout.jsonl"
    dst_manifest = dst_dir / "manifest.json"

    src = find_subagent_rollout(spawn.agent_id, sessions_root=sessions_root)
    if src is None:
        return _write_manifest(
            dst_manifest=dst_manifest,
            spawn=spawn,
            status="rollout_unavailable",
            extra={"source_path": None},
            host_originator=None,
        )

    src_size = src.stat().st_size
    src_sha = _sha256_file(src)
    src_originator = read_codex_originator(src)

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
        distilled, distill_index = distill_codex_jsonl(
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
        host_originator=src_originator,
    )


def capture_all_subagents(
    *,
    main_rollout_path: Path,
    dst_root: Path,
    repo: Path | None = None,
    sessions_root: Path | None = None,
) -> list[dict[str, Any]]:
    """对 ``main_rollout_path`` 中所有 spawned 子代理逐一 ``capture_subagent``。

    ``dst_root`` 是 ``<rvf_dir>/subagents``；每个 agent 获得自己的子目录。
    返回 manifests 列表（与 spawn 顺序一致）。
    """
    if not main_rollout_path.exists():
        return []
    spawns = discover_spawned_agents(main_rollout_path)
    if not spawns:
        return []
    dst_root.mkdir(parents=True, exist_ok=True)
    manifests: list[dict[str, Any]] = []
    for spawn in spawns:
        sub_dir = dst_root / spawn.agent_id
        manifest = capture_subagent(
            spawn,
            dst_dir=sub_dir,
            repo=repo,
            sessions_root=sessions_root,
        )
        manifests.append(manifest)
    return manifests


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover Codex spawn_agent subagents and capture their rollouts."
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
        "--sessions-root",
        help="Override Codex sessions dir (default: $CODEX_HOME/sessions or ~/.codex/sessions).",
    )
    args = parser.parse_args()

    main_rollout = Path(args.main_rollout).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    repo = Path(args.repo).expanduser().resolve() if args.repo else None
    sessions_root = (
        Path(args.sessions_root).expanduser().resolve() if args.sessions_root else None
    )
    manifests = capture_all_subagents(
        main_rollout_path=main_rollout,
        dst_root=out_dir,
        repo=repo,
        sessions_root=sessions_root,
    )
    print(json.dumps(manifests, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
