#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent


WRITE_TOOL_NAMES = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CODEX_TOOL_NAMES = {"apply_patch", "exec_command"}


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def maybe_read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        return read_json(path)
    except Exception:
        return None


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_info(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
    }
    if not path.is_file():
        return info
    stat = path.stat()
    info.update(
        {
            "sha256": sha256_file(path),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }
    )
    return info


def source_features(path: Path) -> dict[str, Any]:
    info = file_info(path)
    if not path.is_file():
        return info
    text = path.read_text(encoding="utf-8", errors="replace")
    info["features"] = {
        "manifest_tracker_field": '"tracker": tracker_payload' in text,
        "manifest_tracker_enabled_arg": "tracker_enabled" in text,
        "stop_gate_tracker_refresh": "refresh_global_diff_tracker" in text,
        "stop_gate_allocator": "allocate_auto_review_scope" in text,
        "codex_payload_parser": 'record.get("payload")' in text and "payload_tool_name" in text,
        "claude_message_tool_use_parser": 'record.get("message")' in text and "tool_use" in text,
        "claude_write_tool_parser": any(name in text for name in WRITE_TOOL_NAMES),
    }
    return info


def run_git(repo: Path, args: list[str], *, text: bool = True) -> str | bytes | None:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=text,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout


def git_root(repo: Path | None) -> Path | None:
    if repo is None:
        return None
    raw = run_git(repo, ["rev-parse", "--show-toplevel"])
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw.strip()).resolve()


def parse_status_z(data: bytes) -> set[str]:
    parts = [part for part in data.split(b"\0") if part]
    paths: set[str] = set()
    index = 0
    while index < len(parts):
        record = parts[index].decode("utf-8", "surrogateescape")
        if len(record) >= 4:
            xy = record[:2]
            path = record[3:]
            paths.add(path)
            if "R" in xy or "C" in xy:
                index += 1
                if index < len(parts):
                    paths.add(parts[index].decode("utf-8", "surrogateescape"))
        index += 1
    return paths


def dirty_paths(repo: Path | None) -> list[str]:
    if repo is None:
        return []
    raw = run_git(repo, ["status", "--porcelain=v1", "-z", "-uall"], text=False)
    if not isinstance(raw, bytes):
        return []
    return sorted(parse_status_z(raw))


def normalize_repo_path(repo: Path | None, value: str | None) -> str | None:
    if repo is None or not isinstance(value, str) or not value.strip():
        return None
    root = repo.resolve()
    candidate = Path(value.strip())
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def extract_event_paths(stop_event: dict[str, Any] | None) -> tuple[Path | None, Path | None]:
    if not isinstance(stop_event, dict):
        return None, None
    cwd = stop_event.get("cwd")
    repo = Path(cwd).expanduser().resolve() if isinstance(cwd, str) and cwd else None
    transcript = stop_event.get("transcript_path")
    if not isinstance(transcript, str):
        transcript = stop_event.get("transcript")
    transcript_path = Path(transcript).expanduser().resolve() if isinstance(transcript, str) and transcript else None
    return repo, transcript_path


def line_timestamp(record: dict[str, Any]) -> str | None:
    value = record.get("timestamp")
    return value if isinstance(value, str) else None


def before_or_at(timestamp: str | None, stop_timestamp: str | None) -> bool:
    if not timestamp or not stop_timestamp:
        return True
    return timestamp <= stop_timestamp


def parse_transcript_writes(
    transcript: Path | None,
    *,
    repo: Path | None,
    stop_timestamp: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(transcript) if transcript is not None else None,
        "exists": bool(transcript and transcript.is_file()),
        "record_count": 0,
        "parse_error_count": 0,
        "claude_tool_counts": {},
        "codex_tool_counts": {},
        "claude_write_paths": [],
        "claude_write_path_counts": {},
        "claude_write_events_before_stop": 0,
        "codex_owned_signal_events_before_stop": 0,
    }
    if transcript is None or not transcript.is_file():
        return result

    claude_counts: Counter[str] = Counter()
    codex_counts: Counter[str] = Counter()
    write_paths: Counter[str] = Counter()
    write_events = 0
    codex_owned_events = 0

    with transcript.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            result["record_count"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                result["parse_error_count"] += 1
                continue
            if not isinstance(record, dict) or not before_or_at(line_timestamp(record), stop_timestamp):
                continue

            payload = record.get("payload")
            if isinstance(payload, dict):
                name = payload.get("name")
                if isinstance(name, str):
                    codex_counts[name] += 1
                    if name in CODEX_TOOL_NAMES:
                        codex_owned_events += 1

            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                name = item.get("name")
                if isinstance(name, str):
                    claude_counts[name] += 1
                if name not in WRITE_TOOL_NAMES:
                    continue
                tool_input = item.get("input")
                if not isinstance(tool_input, dict):
                    continue
                rel = normalize_repo_path(repo, tool_input.get("file_path"))
                if rel is not None:
                    write_paths[rel] += 1
                write_events += 1

    result["claude_tool_counts"] = dict(claude_counts.most_common())
    result["codex_tool_counts"] = dict(codex_counts.most_common())
    result["claude_write_paths"] = sorted(write_paths)
    result["claude_write_path_counts"] = dict(write_paths.most_common())
    result["claude_write_events_before_stop"] = write_events
    result["codex_owned_signal_events_before_stop"] = codex_owned_events
    return result


def default_runtime_script_dirs() -> list[Path]:
    home = Path.home()
    candidates: list[Path] = []
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        candidates.append(Path(plugin_root).expanduser() / "skills" / "review-validate-fix" / "scripts")
    candidates.extend(
        [
            home
            / ".claude"
            / "local-marketplaces"
            / "review-validate-fix"
            / "plugins"
            / "review-validate-fix"
            / "skills"
            / "review-validate-fix"
            / "scripts",
            home
            / "plugins"
            / "review-validate-fix"
            / "skills"
            / "review-validate-fix"
            / "scripts",
            home
            / ".codex"
            / "plugins"
            / "cache"
            / "local-codex-plugins"
            / "rvf"
            / "0.1.0"
            / "skills"
            / "review-validate-fix"
            / "scripts",
            SCRIPT_DIR,
        ]
    )
    seen: set[Path] = set()
    result: list[Path] = []
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def hook_wrapper_info() -> dict[str, Any]:
    hook_path = (
        Path.home()
        / ".claude"
        / "local-marketplaces"
        / "review-validate-fix"
        / "plugins"
        / "review-validate-fix"
        / "hooks"
        / "stop.py"
    )
    info = file_info(hook_path)
    if not hook_path.is_file():
        return info
    text = hook_path.read_text(encoding="utf-8", errors="replace")
    info["sets_dev_sync_zero"] = 'CODEX_RVF_DEV_SYNC", "0"' in text or "CODEX_RVF_DEV_SYNC=0" in text
    info["sets_claude_log_root"] = ".claude" in text and "rvf" in text and "CODEX_RVF_LOG_ROOT" in text
    info["rvf_core_expression"] = "CLAUDE_PLUGIN_ROOT" if "CLAUDE_PLUGIN_ROOT" in text else None
    return info


def runtime_versions(script_dirs: list[Path], reference_scripts_dir: Path) -> list[dict[str, Any]]:
    reference_manifest = reference_scripts_dir / "session_manifest.py"
    reference_stop = reference_scripts_dir / "codex_stop_review_validate_fix.py"
    reference_manifest_sha = sha256_file(reference_manifest)
    reference_stop_sha = sha256_file(reference_stop)

    versions: list[dict[str, Any]] = []
    for script_dir in script_dirs:
        manifest = script_dir / "session_manifest.py"
        stop = script_dir / "codex_stop_review_validate_fix.py"
        manifest_info = source_features(manifest)
        stop_info = source_features(stop)
        versions.append(
            {
                "scripts_dir": str(script_dir),
                "session_manifest": manifest_info,
                "stop_core": stop_info,
                "matches_reference": {
                    "session_manifest": bool(reference_manifest_sha and manifest_info.get("sha256") == reference_manifest_sha),
                    "stop_core": bool(reference_stop_sha and stop_info.get("sha256") == reference_stop_sha),
                },
            }
        )
    return versions


def load_run_artifacts(summary_path: Path | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    summary = maybe_read_json(summary_path)
    if not isinstance(summary, dict):
        return None, None, None
    run_dir_value = summary.get("run_dir")
    run_dir = Path(run_dir_value).expanduser().resolve() if isinstance(run_dir_value, str) else summary_path.parent
    artifacts = run_dir / "artifacts"
    manifest = maybe_read_json(artifacts / "session-manifest.json")
    stop_event = maybe_read_json(artifacts / "stop-event.json")
    return summary, manifest, stop_event


def run_reference_manifest(repo: Path | None, transcript: Path | None, reference_scripts_dir: Path) -> dict[str, Any] | None:
    if repo is None or transcript is None or not transcript.is_file():
        return None
    script = reference_scripts_dir / "session_manifest.py"
    if not script.is_file():
        return None
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--repo",
            str(repo),
            "--transcript",
            str(transcript),
            "--no-tracker",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return {
            "status": "failed",
            "returncode": completed.returncode,
            "stderr": completed.stderr.strip(),
            "stdout": completed.stdout.strip()[:1000],
        }
    try:
        manifest = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"status": "invalid_json", "stdout": completed.stdout[:1000]}
    return {
        "status": "ok",
        "owned_dirty_paths": manifest.get("owned_dirty_paths"),
        "owned_paths": manifest.get("owned_paths"),
        "unattributed_dirty_paths": manifest.get("unattributed_dirty_paths"),
        "confidence": manifest.get("confidence"),
        "has_tracker_field": isinstance(manifest, dict) and "tracker" in manifest,
    }


def build_diagnoses(payload: dict[str, Any]) -> list[dict[str, str]]:
    diagnoses: list[dict[str, str]] = []
    run_manifest = payload.get("run_manifest") if isinstance(payload.get("run_manifest"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    transcript = payload.get("transcript_probe") if isinstance(payload.get("transcript_probe"), dict) else {}
    runtime = payload.get("runtime_versions") if isinstance(payload.get("runtime_versions"), list) else []

    if summary.get("reason_code") == "no_session_owned_dirty" and not run_manifest.get("owned_dirty_paths"):
        diagnoses.append(
            {
                "code": "stop_hook_skipped_no_session_owned_dirty",
                "message": "Stop hook skipped because the generated manifest had no owned dirty paths.",
            }
        )

    if run_manifest and "tracker" not in run_manifest:
        diagnoses.append(
            {
                "code": "run_manifest_missing_tracker_field",
                "message": "The run artifact manifest has no tracker field, which indicates a legacy manifest path or a tracker-disabled path.",
            }
        )

    write_paths = set(transcript.get("claude_dirty_write_paths") or transcript.get("claude_write_paths") or [])
    owned_dirty = set(run_manifest.get("owned_dirty_paths") or [])
    if write_paths and not owned_dirty:
        diagnoses.append(
            {
                "code": "claude_writes_not_attributed",
                "message": "Claude transcript contains write-tool file paths before the Stop event, but the run manifest attributed none of them.",
            }
        )

    for item in runtime:
        if not isinstance(item, dict):
            continue
        matches = item.get("matches_reference") if isinstance(item.get("matches_reference"), dict) else {}
        manifest = item.get("session_manifest") if isinstance(item.get("session_manifest"), dict) else {}
        features = manifest.get("features") if isinstance(manifest.get("features"), dict) else {}
        if manifest.get("exists") and matches.get("session_manifest") is False:
            diagnoses.append(
                {
                    "code": "runtime_session_manifest_differs_from_reference",
                    "message": f"Runtime session_manifest.py differs from the reference scripts dir: {item.get('scripts_dir')}",
                }
            )
        if manifest.get("exists") and not features.get("manifest_tracker_field"):
            diagnoses.append(
                {
                    "code": "runtime_session_manifest_lacks_tracker_field",
                    "message": f"Runtime session_manifest.py lacks the tracker manifest field: {item.get('scripts_dir')}",
                }
            )
        if manifest.get("exists") and not features.get("claude_write_tool_parser"):
            diagnoses.append(
                {
                    "code": "runtime_session_manifest_lacks_claude_write_parser",
                    "message": f"Runtime session_manifest.py does not appear to parse Claude write tools: {item.get('scripts_dir')}",
                }
            )

    hook = payload.get("hook_wrapper") if isinstance(payload.get("hook_wrapper"), dict) else {}
    if hook.get("sets_dev_sync_zero"):
        diagnoses.append(
            {
                "code": "hook_wrapper_disables_dev_sync",
                "message": "Claude hook wrapper sets CODEX_RVF_DEV_SYNC=0, so it will not self-update from the development repo.",
            }
        )

    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for item in diagnoses:
        code = item["code"]
        if code in seen:
            continue
        seen.add(code)
        unique.append(item)
    return unique


def diagnose(args: argparse.Namespace) -> dict[str, Any]:
    summary_path = Path(args.summary).expanduser().resolve() if args.summary else None
    summary, run_manifest, stop_event = load_run_artifacts(summary_path)
    event_repo, event_transcript = extract_event_paths(stop_event)

    repo_arg = Path(args.repo).expanduser().resolve() if args.repo else None
    transcript_arg = Path(args.transcript).expanduser().resolve() if args.transcript else None
    repo = git_root(repo_arg or event_repo)
    transcript = transcript_arg or event_transcript

    runtime_dirs = (
        [Path(value).expanduser().resolve() for value in args.runtime_scripts_dir]
        if args.runtime_scripts_dir
        else default_runtime_script_dirs()
    )
    reference_scripts_dir = (
        Path(args.reference_scripts_dir).expanduser().resolve()
        if args.reference_scripts_dir
        else SCRIPT_DIR
    )

    payload: dict[str, Any] = {
        "schema": "rvf.stop-hook-scope-diagnosis.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary_path": str(summary_path) if summary_path is not None else None,
        "summary": summary,
        "repo": str(repo) if repo is not None else None,
        "transcript": str(transcript) if transcript is not None else None,
        "dirty_paths": dirty_paths(repo),
        "run_manifest": run_manifest,
        "stop_event": stop_event,
        "hook_wrapper": hook_wrapper_info(),
        "runtime_versions": runtime_versions(runtime_dirs, reference_scripts_dir),
        "transcript_probe": parse_transcript_writes(transcript, repo=repo, stop_timestamp=summary.get("timestamp") if summary else None),
    }
    if isinstance(payload.get("transcript_probe"), dict):
        probe = payload["transcript_probe"]
        probe["claude_dirty_write_paths"] = sorted(
            set(probe.get("claude_write_paths") or []) & set(payload.get("dirty_paths") or [])
        )
    if args.run_reference_manifest:
        payload["reference_manifest_probe"] = run_reference_manifest(repo, transcript, reference_scripts_dir)
    payload["diagnoses"] = build_diagnoses(payload)
    return payload


def render_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    transcript = payload.get("transcript_probe") if isinstance(payload.get("transcript_probe"), dict) else {}
    lines.append("RVF Stop hook scope diagnosis")
    lines.append(f"- summary: {payload.get('summary_path')}")
    lines.append(f"- repo: {payload.get('repo')}")
    lines.append(f"- reason: {summary.get('reason_code')}")
    lines.append(f"- dirty paths: {len(payload.get('dirty_paths') or [])}")
    dirty_write_paths = transcript.get("claude_dirty_write_paths") or []
    lines.append(f"- Claude write paths before stop: {len(transcript.get('claude_write_paths') or [])}")
    lines.append(f"- Claude write paths still dirty: {len(dirty_write_paths)}")
    if dirty_write_paths:
        for path in dirty_write_paths:
            lines.append(f"  - {path}")
    lines.append("")
    lines.append("Diagnoses:")
    diagnoses = payload.get("diagnoses") if isinstance(payload.get("diagnoses"), list) else []
    if not diagnoses:
        lines.append("- none")
    else:
        for item in diagnoses:
            if isinstance(item, dict):
                lines.append(f"- {item.get('code')}: {item.get('message')}")
    lines.append("")
    lines.append("Runtime script dirs:")
    for item in payload.get("runtime_versions") or []:
        if not isinstance(item, dict):
            continue
        matches = item.get("matches_reference") if isinstance(item.get("matches_reference"), dict) else {}
        manifest = item.get("session_manifest") if isinstance(item.get("session_manifest"), dict) else {}
        lines.append(
            f"- {item.get('scripts_dir')} "
            f"manifest_exists={manifest.get('exists')} "
            f"manifest_matches_reference={matches.get('session_manifest')}"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose why an RVF Stop hook run did or did not register session-owned dirty scope.",
    )
    parser.add_argument("--summary", help="Path to an RVF run summary.json.")
    parser.add_argument("--repo", help="Override target repo path.")
    parser.add_argument("--transcript", help="Override transcript JSONL path.")
    parser.add_argument(
        "--runtime-scripts-dir",
        action="append",
        default=[],
        help="Scripts directory to compare against the reference. Can be repeated.",
    )
    parser.add_argument(
        "--reference-scripts-dir",
        help="Reference scripts directory. Defaults to this script's directory.",
    )
    parser.add_argument(
        "--run-reference-manifest",
        action="store_true",
        help="Run the reference session_manifest.py with --no-tracker as an extra probe.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()

    payload = diagnose(args)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
